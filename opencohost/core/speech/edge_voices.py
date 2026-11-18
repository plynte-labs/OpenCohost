"""Edge-TTS voice catalog with an in-process cache and an offline fallback.

``edge_tts.list_voices()`` needs the network. The fetch is injectable so tests never touch it.
Results are cached for ``ttl_s`` (default 24h); after a failure the previous cache is served
stale, and with no cache the locale bundles' own default voices are returned as ``fallback``.
A failed fetch is not retried for ``retry_after_s`` so an offline machine does not stall every
request on a network timeout.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from typing import Callable

logger = logging.getLogger("opencohost.edge_voices")

TTL_S = 24 * 3600
RETRY_AFTER_S = 60
FETCH_TIMEOUT_S = 8.0


def _live_fetch() -> list[dict]:
    import edge_tts

    async def _go():
        return await asyncio.wait_for(edge_tts.list_voices(), timeout=FETCH_TIMEOUT_S)

    return asyncio.run(_go())


def _norm(tag: str) -> str:
    return (tag or "").replace("_", "-").strip().lower()


def _voice(raw: dict) -> dict | None:
    short = str(raw.get("ShortName") or "").strip()
    if not short:
        return None
    locale = str(raw.get("Locale") or "").strip() or "-".join(short.split("-")[:2])
    label = str(raw.get("FriendlyName") or "").strip() or short
    return {"id": short, "label": label, "locale": locale, "gender": str(raw.get("Gender") or "")}


def bundle_fallback_voices(lang: str | None) -> list[str]:
    """Edge voices declared by the locale bundles (``tts.edge_voice``) for ``lang`` (all when None)."""
    wanted = _norm(lang).split("-")[0] if lang else None
    found: list[str] = []
    try:
        from opencohost.i18n.startup import load_registry

        for code, bundle in sorted(load_registry().items()):
            voice = str(((bundle.data or {}).get("tts") or {}).get("edge_voice") or "").strip()
            if voice and (wanted is None or _norm(code).split("-")[0] == wanted) and voice not in found:
                found.append(voice)
    except Exception:
        logger.warning("[EDGE_VOICES] bundle fallback unavailable")
    if not found:
        try:
            from opencohost.i18n import active

            voice = active.edge_voice()
            if wanted is None or _norm(voice).split("-")[0] == wanted:
                found.append(voice)
        except Exception:
            pass
    return found


class EdgeVoiceCatalog:
    def __init__(
        self,
        fetch: Callable[[], list[dict]] = _live_fetch,
        ttl_s: float = TTL_S,
        retry_after_s: float = RETRY_AFTER_S,
        clock: Callable[[], float] = time.monotonic,
        fallback: Callable[[str | None], list[str]] = bundle_fallback_voices,
    ):
        self._fetch = fetch
        self._ttl_s = ttl_s
        self._retry_after_s = retry_after_s
        self._clock = clock
        self._fallback = fallback
        self._lock = threading.Lock()
        self._voices: list[dict] | None = None
        self._fetched_at = 0.0
        self._failed_at: float | None = None

    def _refresh_if_needed(self) -> str:
        """Return the source of the data now held: live | cache | none."""
        now = self._clock()
        if self._voices is not None and now - self._fetched_at < self._ttl_s:
            return "cache"
        if self._failed_at is not None and now - self._failed_at < self._retry_after_s:
            return "cache" if self._voices is not None else "none"
        try:
            voices = [v for v in (_voice(r) for r in self._fetch() or []) if v]
            if not voices:
                raise ValueError("empty voice list")
        except Exception as exc:
            self._failed_at = now
            logger.warning("[EDGE_VOICES] fetch failed: %s", type(exc).__name__)
            return "cache" if self._voices is not None else "none"
        self._voices, self._fetched_at, self._failed_at = voices, now, None
        return "live"

    def peek(self) -> set[str] | None:
        """ShortNames of the cached live catalog (even if stale), or None. Never touches the network."""
        # Lock-free on purpose: ``get`` holds the lock during a network fetch, and the engine
        # command thread must never wait on it. ``_voices`` is only ever replaced whole, so one
        # reference read is a consistent snapshot.
        voices = self._voices
        return {v["id"] for v in voices} if voices is not None else None

    def get(self, lang: str | None = None) -> tuple[list[dict], str]:
        """``(voices, source)`` with source ``live`` | ``cache`` | ``fallback``."""
        with self._lock:
            source = self._refresh_if_needed()
            voices = list(self._voices) if self._voices is not None else None
        if voices is None:
            voices = [
                {"id": s, "label": s, "locale": "-".join(s.split("-")[:2]), "gender": ""}
                for s in self._fallback(lang)
            ]
            return voices, "fallback"
        wanted = _norm(lang).split("-")[0] if lang else None
        if wanted:
            voices = [v for v in voices if _norm(v["locale"]).split("-")[0] == wanted]
        return sorted(voices, key=lambda v: v["id"]), source


DEFAULT_CATALOG = EdgeVoiceCatalog()
