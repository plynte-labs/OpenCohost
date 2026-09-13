"""Pure LLM inference service extracted from ``MotorVocalIA``.

This service encapsulates model dispatching to local Ollama runners and cloud
providers, client lifecycle timeouts, and streaming token generation without
coupling to audio queues or UI loops.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import re
import threading
import time
from typing import Any, Callable, Iterator, Optional
import uuid

import httpx

try:
    import ollama
except ImportError:
    ollama = None

from opencohost.config.settings import (
    LLM_KEYS_FILE,
    OLLAMA_CHAT_TIMEOUT,
    OLLAMA_REQUEST_TIMEOUT,
    STREAM_IDLE_PROBE_SECONDS,
    STREAM_IDLE_TIMEOUT_SECONDS,
)
from opencohost.core.providers.cloud import cloud_llm_client
from opencohost.core.speech.sentence_splitter import SentenceSplitter
from opencohost.stream_admin.oauth_store import OAuthStore

logger = logging.getLogger("OpenCohost")


# ── Immutable cloud sampling params (evidence 2026-09-09, NVIDIA NIM) ──────
#
# Some OpenAI-compatible providers pin sampling knobs per model and answer a
# fast HTTP 400 naming the required value instead of coercing, e.g. NIM:
#   "Validation: `top_p` is immutable for this model and must be 0.95, got 0.9"
# Our engine always sends its own `LLM_TOP_P` (0.9), so EVERY turn to such a
# model burns instantly with `cloud_llm_error` — no timeout, no retry, just a
# dead provider that looks configured. `readiness` cannot catch it: the
# `GET /models` probe never validates sampling params.
#
# `cloud_chat` below therefore retries a 400 that names an immutable param,
# bounded (1 original + up to 3 single-param corrections) and cached per
# (base_url, model) so later turns skip the 400 round-trip. Anything without a
# parseable `must be <number>` requirement re-raises untouched.

_IMMUTABLE_PARAM_RE = re.compile(r"`([A-Za-z_]\w*)`[^`]{0,200}?must be (\d+(?:\.\d+)?)")

# OpenAI body name -> Ollama-shaped options key used by our callers.
_IMMUTABLE_PARAM_MAP = {
    "temperature": "temperature",
    "top_p": "top_p",
    "presence_penalty": "presence_penalty",
    "frequency_penalty": "frequency_penalty",
    "max_tokens": "num_predict",
}

_MAX_IMMUTABLE_FIXES = 3


@dataclass(frozen=True)
class InferenceRequest:
    """Normalized inference request across local and cloud providers."""

    messages: list[dict[str, Any]]
    model: str
    options: dict[str, Any] = field(default_factory=dict)
    is_local: bool = True
    provider_config: dict[str, Any] = field(default_factory=dict)
    watchdog_timeout: Optional[float] = None
    stream: bool = False


@dataclass
class InferenceResponse:
    """Normalized inference result."""

    text: str
    gen_ms: int = -1
    provider: str = "local"
    is_local: bool = True
    raw: Any = None
    error: Optional[str] = None


def get_inference_service(engine: Any) -> LLMInferenceService:
    """Retrieve or dynamically bind an LLMInferenceService for an engine instance."""
    service = getattr(engine, "_inference_service", None)
    if service is not None and isinstance(service, LLMInferenceService):
        return service
    service = LLMInferenceService(
        ollama_resolver=lambda: getattr(
            engine, "ollama", getattr(engine, "_ollama_chat_client", None)
        ),
        api_key_resolver=lambda pid: (
            getattr(engine, "_cloud_api_key")(pid)
            if hasattr(engine, "_cloud_api_key")
            else ""
        ),
        clients_resolver=lambda: getattr(engine, "_ollama_chat_clients", None),
    )
    if hasattr(engine, "_ollama_chat_clients") and isinstance(
        engine._ollama_chat_clients, dict
    ):
        service._ollama_chat_clients = engine._ollama_chat_clients
    if hasattr(engine, "_ollama_chat_client"):
        service.set_default_ollama_client(engine._ollama_chat_client)
    try:
        object.__setattr__(engine, "_inference_service", service)
    except Exception:
        try:
            engine._inference_service = service
        except Exception:
            pass
    return service


class LLMInferenceService:
    """Pure LLM inference runner supporting local Ollama and Cloud providers."""

    def __init__(
        self,
        *,
        ollama_client: Any = None,
        cloud_client_module: Any = None,
        api_key_resolver: Optional[Callable[[str], str]] = None,
        ollama_resolver: Optional[Callable[[], Any]] = None,
        clients_resolver: Optional[Callable[[], Optional[dict[float, Any]]]] = None,
    ) -> None:
        self._ollama = ollama if ollama_client is None else ollama_client
        self._ollama_resolver = ollama_resolver
        self._clients_resolver = clients_resolver
        self._cloud_client = (
            cloud_llm_client if cloud_client_module is None else cloud_client_module
        )
        self._api_key_resolver = api_key_resolver or self._default_cloud_api_key
        self._ollama_chat_clients: dict[float, Any] = {}
        self._default_ollama_client: Any = None
        # (base_url, model) -> {ollama-option: required value} learned from
        # 400 immutable-param rejections (see module note above).
        self._immutable_param_cache: dict[tuple[str, str], dict[str, Any]] = {}

    def _get_ollama(self) -> Any:
        if self._ollama_resolver is not None:
            try:
                resolved = self._ollama_resolver()
                if resolved is not None:
                    return resolved
            except Exception:
                pass
        return self._ollama

    def _default_cloud_api_key(self, profile_id: str) -> str:
        token = OAuthStore(LLM_KEYS_FILE).load(profile_id)
        if isinstance(token, dict):
            return str(token.get("api_key") or "")
        return ""

    def create_chat_client(self, client_factory: Any, *, timeout: float) -> Optional[Any]:
        """Pre-warm a memoized client scoped to a specific timeout budget."""
        if not hasattr(client_factory, "Client"):
            return None
        try:
            client = client_factory.Client(timeout=timeout)
            self._ollama_chat_clients[timeout] = client
            return client
        except TypeError as exc:
            logger.warning("Ollama Client does not support timeout: %s", exc)
            return None

    def select_chat_client(self, chat_timeout: Optional[float]) -> Any:
        """Select the memoized client for chat_timeout, or fallback to default."""
        if self._clients_resolver is not None:
            try:
                cache = self._clients_resolver()
                if cache and chat_timeout is not None and chat_timeout in cache:
                    return cache[chat_timeout]
            except Exception:
                pass
        if chat_timeout is not None and chat_timeout in self._ollama_chat_clients:
            return self._ollama_chat_clients[chat_timeout]
        return self._default_ollama_client or self._get_ollama()

    def set_default_ollama_client(self, client: Any) -> None:
        self._default_ollama_client = client

    def call_with_watchdog(
        self,
        call: Callable[..., Any],
        *,
        timeout: float,
        label: str = "OllamaChatWatchdog",
        **kwargs: Any,
    ) -> Any:
        """Run call on a daemon thread and enforce timeout budget."""
        if kwargs.get("stream"):
            raise ValueError(
                "_call_with_watchdog cannot supervise stream=True: a generator-returning "
                "call returns before its body runs, so the watchdog would always succeed. "
                "Use the dedicated streaming seam, which iterates on the calling thread."
            )

        result: dict[str, Any] = {}
        done = threading.Event()

        def worker() -> None:
            try:
                result["response"] = call(**kwargs)
            except httpx.TimeoutException:
                result["error"] = TimeoutError(f"watchdog_timeout:{timeout:.2f}s")
            except Exception as exc:
                result["error"] = exc
            finally:
                done.set()

        thread = threading.Thread(
            target=worker,
            name=f"{label}-{uuid.uuid4().hex[:8]}",
            daemon=True,
        )
        thread.start()
        if not done.wait(timeout=max(0.1, float(timeout))):
            raise TimeoutError(f"watchdog_timeout:{timeout:.2f}s")
        if "error" in result:
            raise result["error"]
        return result.get("response")

    def chat_with_watchdog(
        self,
        *,
        timeout: float,
        chat_callable: Optional[Callable[..., Any]] = None,
        **kwargs: Any,
    ) -> Any:
        """Execute chat under the watchdog budget."""
        call = chat_callable or (lambda **kw: self.chat(chat_timeout=timeout, **kw))
        return self.call_with_watchdog(call, timeout=timeout, **kwargs)

    def chat(
        self,
        *,
        provider_cfg: Optional[dict[str, Any]] = None,
        is_local: bool = True,
        chat_timeout: Optional[float] = None,
        **kwargs: Any,
    ) -> Any:
        """Execute chat dispatching to Cloud or Local Ollama."""
        if not is_local:
            return self.cloud_chat(
                provider_cfg=provider_cfg,
                timeout=chat_timeout or OLLAMA_REQUEST_TIMEOUT,
                **kwargs,
            )
        client = self.select_chat_client(chat_timeout)
        if client is None:
            raise RuntimeError("No Ollama client available for local chat")
        return client.chat(**kwargs)

    def _immutable_fix(
        self,
        cache_key: tuple[str, str],
        options: dict[str, Any],
        exc: Exception,
    ) -> Optional[dict[str, Any]]:
        """Corrected Ollama-shaped options for a 400 immutable-param rejection.

        Returns None when the error is not a parseable `must be <number>`
        requirement (caller re-raises the original), or when the requirement
        already holds (nothing to correct — retrying would loop).
        """
        if getattr(exc, "status_code", None) != 400:
            return None
        excerpt = str(getattr(exc, "body_excerpt", "") or "")
        match = _IMMUTABLE_PARAM_RE.search(excerpt)
        if match is None:
            return None
        openai_name, raw_value = match.group(1), match.group(2)
        ollama_name = _IMMUTABLE_PARAM_MAP.get(openai_name)
        if ollama_name is None:
            return None
        value: Any = float(raw_value) if "." in raw_value else int(raw_value)
        if options.get(ollama_name) == value:
            return None
        fixed = dict(options)
        fixed[ollama_name] = value
        learned = dict(self._immutable_param_cache.get(cache_key, {}))
        learned[ollama_name] = value
        self._immutable_param_cache[cache_key] = learned
        logger.warning(
            "cloud immutable param: %s must be %r for model %s; retrying with corrected options",
            ollama_name,
            value,
            cache_key[1],
        )
        return fixed

    def cloud_chat(
        self,
        *,
        provider_cfg: Optional[dict[str, Any]] = None,
        messages: list[dict[str, Any]],
        options: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
        **_ignored: Any,
    ) -> Any:
        """Dispatch chat to cloud provider."""
        cfg = provider_cfg or {}
        active_id = cfg.get("active_provider")
        profiles = cfg.get("profiles") or {}
        profile = profiles.get(active_id) if active_id else None
        if profile is None:
            raise self._cloud_client.CloudLLMResponseError(
                "cloud provider active but no profile configured"
            )
        api_key = self._api_key_resolver(active_id) if active_id else ""
        base_url = str(profile.get("base_url") or "")
        model = str(profile.get("model") or "")
        cache_key = (base_url, model)
        opts = dict(options or {})
        for cached_name, cached_value in self._immutable_param_cache.get(cache_key, {}).items():
            opts[cached_name] = cached_value
        err_cls = getattr(self._cloud_client, "CloudLLMResponseError", None)
        if not (isinstance(err_cls, type) and issubclass(err_cls, Exception)):
            # Test doubles without a real exception type: single direct call,
            # byte-identical to the pre-fix behavior.
            return self._cloud_client.send_chat_completion(
                base_url=base_url,
                api_key=api_key,
                model=model,
                messages=messages,
                options=opts,
                timeout=timeout or OLLAMA_REQUEST_TIMEOUT,
            )
        attempts = 1 + _MAX_IMMUTABLE_FIXES
        for _ in range(attempts):
            try:
                return self._cloud_client.send_chat_completion(
                    base_url=base_url,
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    options=opts,
                    timeout=timeout or OLLAMA_REQUEST_TIMEOUT,
                )
            except err_cls as exc:
                fixed = self._immutable_fix(cache_key, opts, exc)
                if fixed is None:
                    raise
                opts = fixed
        # Unreachable: the loop above either returns or raises (a no-op fix
        # also raises). Kept as a defensive tail instead of trusting that.
        raise self._cloud_client.CloudLLMResponseError(
            "cloud immutable-param retry budget exhausted"
        )

    def chat_streaming(
        self,
        *,
        timeout: float,
        chat_client: Any = None,
        time_fn: Optional[Callable[[], float]] = None,
        **kwargs: Any,
    ) -> Iterator[Any]:
        """Yield raw chunks from local ollama.chat(stream=True) bounded by watchdog."""
        get_time = time_fn or time.monotonic
        idle_budget = float(STREAM_IDLE_TIMEOUT_SECONDS)
        client = chat_client or self.select_chat_client(idle_budget)
        if client is None:
            raise RuntimeError("No Ollama client available for streaming")

        started = get_time()
        first_chunk_pending = True
        stream = None
        try:
            stream = client.chat(stream=True, **kwargs)
            for chunk in stream:
                if first_chunk_pending:
                    first_chunk_pending = False
                    waited = get_time() - started
                    if waited > STREAM_IDLE_PROBE_SECONDS:
                        logger.warning(
                            "[STREAM_IDLE_PROBE] first_chunk_wait_s=%.2f threshold_s=%.2f "
                            "idle_budget_s=%.2f total_budget_s=%.2f",
                            waited,
                            float(STREAM_IDLE_PROBE_SECONDS),
                            idle_budget,
                            float(timeout),
                        )
                if (get_time() - started) > timeout:
                    raise TimeoutError(f"watchdog_timeout:{timeout:.2f}s")
                yield chunk
        except httpx.TimeoutException:
            raise TimeoutError(f"watchdog_timeout:{idle_budget:.2f}s")
        finally:
            if stream is not None and hasattr(stream, "close"):
                try:
                    stream.close()
                except Exception:
                    logger.debug("Failed closing response stream", exc_info=True)

    def execute_chat(
        self,
        request: InferenceRequest,
        *,
        on_raw_chunk: Optional[Callable[[Any], None]] = None,
    ) -> InferenceResponse:
        """Execute a synchronous chat completion."""
        start_time = time.monotonic()
        try:
            timeout = request.watchdog_timeout or (
                OLLAMA_CHAT_TIMEOUT if request.is_local else OLLAMA_REQUEST_TIMEOUT
            )
            raw = self.chat_with_watchdog(
                timeout=timeout,
                model=request.model,
                messages=request.messages,
                options=request.options,
                provider_cfg=request.provider_config,
                is_local=request.is_local,
            )
            gen_ms = max(0, int((time.monotonic() - start_time) * 1000))
            content = ""
            if isinstance(raw, dict):
                content = raw.get("message", {}).get("content", "") or raw.get("text", "")
            elif hasattr(raw, "message") and hasattr(raw.message, "content"):
                content = raw.message.content
            return InferenceResponse(
                text=content,
                gen_ms=gen_ms,
                provider=request.provider_config.get("active_provider")
                or ("local" if request.is_local else "cloud"),
                is_local=request.is_local,
                raw=raw,
            )
        except Exception as exc:
            gen_ms = max(0, int((time.monotonic() - start_time) * 1000))
            return InferenceResponse(
                text="",
                gen_ms=gen_ms,
                provider="local"
                if request.is_local
                else (request.provider_config.get("active_provider") or "cloud"),
                is_local=request.is_local,
                error=str(exc),
            )

    def stream_tokens(
        self,
        request: InferenceRequest,
    ) -> Iterator[str]:
        """Stream raw tokens from local or cloud providers."""
        if request.is_local:
            timeout = request.watchdog_timeout or OLLAMA_CHAT_TIMEOUT
            stream = self.chat_streaming(
                timeout=timeout,
                model=request.model,
                messages=request.messages,
                options=request.options,
            )
            for chunk in stream:
                token = ""
                if isinstance(chunk, dict):
                    token = chunk.get("message", {}).get("content", "")
                elif hasattr(chunk, "message") and hasattr(chunk.message, "content"):
                    token = chunk.message.content
                if token:
                    yield token
        else:
            timeout = request.watchdog_timeout or OLLAMA_REQUEST_TIMEOUT
            response = self.cloud_chat(
                provider_cfg=request.provider_config,
                messages=request.messages,
                options=request.options,
                timeout=timeout,
            )
            if isinstance(response, dict):
                yield response.get("text", "")

    def stream_sentences(
        self,
        request: InferenceRequest,
        splitter: Optional[SentenceSplitter] = None,
    ) -> Iterator[str]:
        """Stream complete sentence clauses using SentenceSplitter."""
        sent_splitter = splitter or SentenceSplitter()
        for token in self.stream_tokens(request):
            for sentence in sent_splitter.feed(token):
                yield sentence
        final_sentence = sent_splitter.flush()
        if final_sentence:
            yield final_sentence
