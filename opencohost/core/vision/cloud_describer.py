"""Cloud vision describer (cloud-providers-vision-20261111 P3).

``CloudVisionDescriber`` is the second ``VisionDescriber`` adapter: it sends the
frames to a cloud vision model through the OpenAI-compatible client (the same
one chat uses, with the provider preset's headers, reasoning style and dropped
params) and returns a short text description, so a user with a local or
non-vision chat model and no GPU still gets vision.

Also holds the describer factory (by ``vision_source``) and the key lookup.

PRIVACY: the image, the question, the description and the key are never
logged. Logs carry the ``[VISION]`` tag, a reason code and latency only.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Mapping, Optional

import requests

from opencohost.config.settings import LLM_PROVIDER_PRESETS
from opencohost.core.providers.cloud import cloud_llm_client as _client
from opencohost.core.vision import direct_cloud
from opencohost.core.vision.describer import (
    DEFAULT_NUM_PREDICT,
    DESCRIBER_LANGS,
    MOSAIC_HINT,
    OllamaVisionDescriber,
    VISION_KEEP_ALIVE,
    VisionDescribeError,
    Frames,
    _as_frames,
    _prompts,
    resolve_describer_lang,
)

logger = logging.getLogger(__name__)

SOURCE_LOCAL = "local"
SOURCE_CHAT_MODEL = "chat_model"
SOURCE_CLOUD_MODEL = "cloud_model"
VISION_SOURCES = (SOURCE_LOCAL, SOURCE_CHAT_MODEL, SOURCE_CLOUD_MODEL)

# Key-store entry id of a preset's own vision key (matches the profile id charset).
VISION_KEY_PREFIX = "vision_"

# Cloud models may think before answering; keep room but stay small.
CLOUD_NUM_PREDICT = DEFAULT_NUM_PREDICT


def vision_key_id(preset_id: str) -> str:
    return f"{VISION_KEY_PREFIX}{preset_id}"


def _sniff_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


class CloudVisionDescriber:
    """``VisionDescriber`` backed by a cloud OpenAI-compatible vision model."""

    def __init__(
        self,
        preset_id: str,
        model: str,
        *,
        key_provider: Callable[[], Optional[str]],
        base_url: str | None = None,
        lang: str | None = None,
    ) -> None:
        preset = LLM_PROVIDER_PRESETS.get(preset_id) or {}
        self.preset_id = preset_id
        self.model = model
        self.base_url = base_url or preset.get("base_url", "")
        self._extra_headers = preset.get("extra_headers") or {}
        self._reasoning_style = preset.get("reasoning_style")
        self._drop_params = preset.get("drop_params") or []
        self._key_provider = key_provider
        self.lang = lang if lang in DESCRIBER_LANGS else (
            resolve_describer_lang() if lang is None else "en"
        )

    def _messages(self, frames: list[bytes], question: str, hint: str | None) -> list:
        system_prompt, template, multi_hint, mosaic_hint = _prompts(self.lang)
        content = template.format(question=question)
        if hint:
            content = f"{content}\n{mosaic_hint if hint == MOSAIC_HINT else hint}"
        elif len(frames) > 1:
            content = f"{content}\n{multi_hint}"
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ]
        images = [(frame, _sniff_mime(frame)) for frame in frames]
        return direct_cloud.attach_images(messages, images)

    def describe(
        self,
        jpeg: Frames,
        question: str,
        *,
        timeout_s: float,
        cancel: threading.Event | None = None,
        num_ctx: int | None = None,
        hint: str | None = None,
        num_predict: int | None = None,
    ) -> str:
        started = time.monotonic()
        try:
            text = self._describe(
                jpeg, question, timeout_s=timeout_s, cancel=cancel,
                hint=hint, num_predict=num_predict, started=started,
            )
        except VisionDescribeError as exc:
            logger.info(
                "[VISION] describe_failed source=cloud reason=%s latency_ms=%d",
                exc.reason, int((time.monotonic() - started) * 1000),
            )
            raise
        logger.info(
            "[VISION] describe_ok source=cloud latency_ms=%d", int((time.monotonic() - started) * 1000)
        )
        return text

    def _describe(
        self,
        jpeg: Frames,
        question: str,
        *,
        timeout_s: float,
        cancel: threading.Event | None,
        hint: str | None,
        num_predict: int | None,
        started: float,
    ) -> str:
        if cancel is not None and cancel.is_set():
            raise VisionDescribeError("cancelled")
        frames = _as_frames(jpeg)
        if not frames:
            raise VisionDescribeError("empty", "no_frames")
        try:
            key = self._key_provider()
        except Exception:
            key = None
        if not key:
            raise VisionDescribeError("unavailable", "no_key")

        messages = self._messages(frames, question, hint)
        options = {"temperature": 0, "num_predict": num_predict or CLOUD_NUM_PREDICT}
        outcome: dict[str, Any] = {}
        done = threading.Event()

        def _call() -> None:
            try:
                outcome["response"] = _client.send_chat_completion(
                    self.base_url, key, self.model, messages, options, timeout_s,
                    disable_reasoning=True,
                    reasoning_style=self._reasoning_style,
                    extra_headers=self._extra_headers,
                    drop_params=self._drop_params,
                )
            except BaseException as exc:  # handed to the caller thread
                outcome["error"] = exc
            finally:
                done.set()

        threading.Thread(target=_call, name="vision-cloud-describe", daemon=True).start()
        deadline = started + timeout_s
        while not done.wait(0.02):
            if cancel is not None and cancel.is_set():
                raise VisionDescribeError("cancelled")
            if time.monotonic() >= deadline:
                raise VisionDescribeError("timeout")
        if "error" in outcome:
            raise self._map_error(outcome["error"]) from None
        response = outcome["response"]
        message = response.get("message") if isinstance(response, dict) else None
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, str) or not content.strip():
            raise VisionDescribeError("empty")
        self._record_usage(response, started)
        return content.strip()

    @staticmethod
    def _map_error(exc: BaseException) -> VisionDescribeError:
        """A stable code only: never the response body, headers or key."""
        if isinstance(exc, requests.exceptions.Timeout):
            return VisionDescribeError("timeout")
        status = getattr(exc, "status_code", None)
        if status is not None:
            kind = _client.classify_cloud_error(exc)
            if kind == _client.CLOUD_ERROR_BAD_KEY:
                return VisionDescribeError("http", "bad_key")
            if status == 429:
                return VisionDescribeError("http", "rate_limited")
            return VisionDescribeError("http", f"status={int(status)}")
        if isinstance(exc, requests.exceptions.ConnectionError):
            return VisionDescribeError("unavailable")
        return VisionDescribeError("http", type(exc).__name__)

    def _record_usage(self, response: Mapping, started: float) -> None:
        try:
            from opencohost.core.usage import ledger

            ledger.record_response(
                response, model=self.model, is_local=False, kind="vision",
                wall_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception:
            logger.debug("usage ledger vision hook failed", exc_info=True)


class DirectOnlyDescriber:
    """Placeholder describer for ``chat_model``: images go inside the chat call,
    so no describe call exists. Reached only when the direct route is not
    eligible (for example the chat fell back to the local model); it fails
    through the normal vision failure path instead of starting a local daemon."""

    def describe(self, jpeg: Frames, question: str, **kwargs: Any) -> str:
        raise VisionDescribeError("unavailable", "direct_only")


def resolve_cloud_key(values: Any, provider_cfg: Mapping, key_store: Any) -> Optional[str]:
    """The API key the cloud describer uses: the stored chat key of a profile
    with the same preset when ``vision_cloud_use_chat_key``, else (or when no
    such key exists) the preset's own ``vision_<preset>`` key."""
    preset = getattr(values, "vision_cloud_preset", "")
    if not preset:
        return None
    if getattr(values, "vision_cloud_use_chat_key", False):
        for profile_id, profile in ((provider_cfg or {}).get("profiles") or {}).items():
            if isinstance(profile, dict) and profile.get("preset") == preset:
                stored = key_store.load(profile_id) or {}
                if stored.get("api_key"):
                    return stored["api_key"]
    stored = key_store.load(vision_key_id(preset)) or {}
    return stored.get("api_key") or None


def make_describer(values: Any, base_url: Optional[str], key_provider: Callable[[], Optional[str]]):
    """The describer for ``values.vision_source`` (local Ollama by default)."""
    source = getattr(values, "vision_source", SOURCE_LOCAL)
    if source == SOURCE_CLOUD_MODEL:
        return CloudVisionDescriber(
            values.vision_cloud_preset, values.vision_cloud_model, key_provider=key_provider
        )
    if source == SOURCE_CHAT_MODEL:
        return DirectOnlyDescriber()
    return OllamaVisionDescriber(base_url=base_url, model=values.model, keep_alive=VISION_KEEP_ALIVE)
