"""Installed models and their vision capability (vision-on-demand V1.17).

Backs ``GET /api/vision/models``. Lists the tags Ollama has installed through
``/api/tags`` on the first daemon that answers (the vision daemon when it runs,
then the main one; both share the model store) and asks ``/api/show`` whether
each one is vision-capable.

Rules:
- Every request is bounded (``timeout_s``) and the ``/api/show`` probes share a
  global budget (``probe_budget_s``), so one call never pins an API thread.
- Fail open: no daemon answering -> ``reachable=False`` and no models.
- Only a definitive ``/api/show`` answer is cached, keyed by tag and digest, so
  a re-pulled tag is asked again and the next call finishes what a spent
  budget left unprobed.
- Without a definitive answer the tag falls back to a name heuristic: True for
  known vision families, otherwise None (unknown). Gemma is deliberately not in
  the heuristic: gemma4 vision on Ollama 0.30.11 was broken in the V0 evidence.
  When ``/api/show`` reports vision for it, the reported value is kept.
- Logs carry a reason code only.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional, Sequence

from opencohost.core.providers.local.vision_daemon import HttpTransport, UrllibTransport

_logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 2.5
DEFAULT_PROBE_BUDGET_S = 4.0
DEFAULT_MAIN_OLLAMA_URL = "http://127.0.0.1:11434"

_VISION_NAME_MARKERS = (
    "-vl",
    "llava",  # also covers bakllava
    "minicpm-v",
    "llama3.2-vision",
    "moondream",
    "bakllava",
    "qwen2.5vl",
    "granite3.2-vision",
)


@dataclass(frozen=True)
class InventorySnapshot:
    reachable: bool
    installed: list = field(default_factory=list)


def canonical_tag(tag: object) -> str:
    """Same rule as ``config.settings._canonical_model_tag`` (core stays config-free)."""
    normalized = str(tag or "").strip()
    return normalized[:-7] if normalized.endswith(":latest") else normalized


def vision_by_name(tag: str) -> Optional[bool]:
    """True for known vision model families, None when the name says nothing."""
    name = (tag or "").strip().lower()
    if name and any(marker in name for marker in _VISION_NAME_MARKERS):
        return True
    return None


def main_ollama_base_url(env: Optional[Mapping[str, str]] = None) -> str:
    """Base URL of the main daemon, resolved from ``OLLAMA_HOST`` like the ollama client."""
    env = os.environ if env is None else env
    raw = (env.get("OLLAMA_HOST") or "").strip().rstrip("/")
    if not raw:
        return DEFAULT_MAIN_OLLAMA_URL
    scheme, sep, rest = raw.partition("://")
    if not sep:
        scheme, rest = "http", raw
    host, _, port = rest.partition(":")
    if host in ("", "0.0.0.0"):
        host = "127.0.0.1"
    return f"{scheme}://{host}:{port or '11434'}"


class VisionModelInventory:
    """Lists installed Ollama models and probes their vision capability."""

    def __init__(
        self,
        *,
        http: Optional[HttpTransport] = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        probe_budget_s: float = DEFAULT_PROBE_BUDGET_S,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._http = http if http is not None else UrllibTransport()
        self._timeout_s = timeout_s
        self._probe_budget_s = probe_budget_s
        self._monotonic = monotonic
        self._lock = threading.Lock()
        self._capabilities: dict[tuple[str, str], bool] = {}

    def snapshot(self, base_urls: Sequence[str]) -> InventorySnapshot:
        for base_url in base_urls:
            models = self._list(base_url)
            if models is not None:
                return InventorySnapshot(reachable=True, installed=self._describe(base_url, models))
        return InventorySnapshot(reachable=False, installed=[])

    # -- internals ----------------------------------------------------------------

    def _list(self, base_url: str) -> Optional[dict]:
        """``{canonical tag: {digest, size_bytes}}``, or None when unusable."""
        try:
            status, body = self._http.get(f"{base_url}/api/tags", self._timeout_s)
            if status != 200:
                return None
            models = json.loads(body).get("models")
        except Exception:
            return None
        if not isinstance(models, list):
            return None
        found: dict = {}
        for entry in models:
            if not isinstance(entry, dict):
                continue
            tag = canonical_tag(entry.get("name") or entry.get("model"))
            if not tag or tag in found:
                continue
            size = entry.get("size")
            found[tag] = {
                "digest": str(entry.get("digest") or ""),
                "size_bytes": size if isinstance(size, int) and not isinstance(size, bool) else None,
            }
        return found

    def _describe(self, base_url: str, models: dict) -> list:
        tags = sorted(models)
        with self._lock:
            known = {t: self._capabilities.get((t, models[t]["digest"])) for t in tags}
        deadline = self._monotonic() + self._probe_budget_s
        for tag in tags:
            if known[tag] is not None:
                continue
            remaining = deadline - self._monotonic()
            if remaining <= 0:
                _logger.info("[VISION_MODELS] reason=probe_budget_spent")
                break
            try:
                verdict = self._show(base_url, tag, min(self._timeout_s, remaining))
            except Exception:
                _logger.info("[VISION_MODELS] reason=show_unreachable")
                break
            if verdict is not None:
                known[tag] = verdict
                with self._lock:
                    self._capabilities[(tag, models[tag]["digest"])] = verdict
        return [
            {
                "tag": tag,
                "vision": known[tag] if known[tag] is not None else vision_by_name(tag),
                "size_bytes": models[tag]["size_bytes"],
            }
            for tag in tags
        ]

    def _show(self, base_url: str, tag: str, timeout: float) -> Optional[bool]:
        """True/False from ``capabilities``; None when the answer is not definitive.

        Raises on a transport failure so the caller stops probing a dead daemon.
        """
        status, body = self._http.post_json(f"{base_url}/api/show", {"model": tag}, timeout)
        if status != 200:
            return None
        try:
            capabilities = json.loads(body).get("capabilities")
        except (TypeError, ValueError, AttributeError):
            return None
        if not isinstance(capabilities, list):
            return None
        return "vision" in capabilities
