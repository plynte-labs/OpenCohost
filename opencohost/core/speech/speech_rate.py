"""
opencohost/core/speech/speech_rate.py

TTS speech rate conversion and calculation utilities.
Extracted from MotorVocalIA.
"""

from __future__ import annotations

import asyncio
import random
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

try:
    import aiohttp
except ImportError:
    aiohttp = None  # type: ignore


def edge_rate_for_length_scale(scale: float) -> str:
    """Convert a Piper length_scale into the equivalent Edge-TTS rate string.

    length_scale multiplies phoneme durations (higher = SLOWER), so the
    effective speed multiplier is 1/scale. Edge-TTS's `rate` kwarg wants a
    signed percentage ("-23%"), so both engines end up moving together
    instead of in opposite directions (1.30 must slow Edge-TTS down too, not
    speed it up 30%). Guard scale <= 0 (never divide by zero / invert
    direction) by returning "+0%".
    """
    if scale <= 0:
        return "+0%"
    pct = round((1.0 / scale - 1.0) * 100)
    return f"{pct:+d}%"


@dataclass(frozen=True)
class EdgeFailure:
    """Classification of an Edge-TTS synth failure. Carries no free text."""

    kind: str  # "offline" | "http" | "timeout" | "other"
    status: Optional[int] = None
    permanent: bool = False

    @property
    def code(self) -> str:
        """Fixed, log/UI-safe code: offline, http_<status>, http, timeout, error."""
        if self.kind == "offline":
            return "offline"
        if self.kind == "http":
            return f"http_{self.status}" if self.status else "http"
        if self.kind == "timeout":
            return "timeout"
        return "error"


def classify_edge_failure(exc: BaseException) -> EdgeFailure:
    """Walk the cause chain and classify an Edge-TTS failure.

    offline (gaierror, SSLError, ClientConnectorError) is session-permanent;
    http (aiohttp.ClientResponseError, e.g. a 403 handshake), timeout and
    anything else are transient.
    """
    chain = []
    seen: set = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        chain.append(e)
        e = e.__cause__ or e.__context__  # type: ignore[assignment]
    for e in chain:
        if isinstance(e, (socket.gaierror, ssl.SSLError)):
            return EdgeFailure("offline", None, True)
        if aiohttp is not None and isinstance(e, aiohttp.ClientConnectorError):
            return EdgeFailure("offline", None, True)
    for e in chain:
        if aiohttp is not None and isinstance(e, aiohttp.ClientResponseError):
            status = getattr(e, "status", None)
            return EdgeFailure("http", status if isinstance(status, int) else None, False)
    for e in chain:
        if isinstance(e, (asyncio.TimeoutError, TimeoutError)):
            return EdgeFailure("timeout", None, False)
    return EdgeFailure("other", None, False)


def _is_connection_error(exc: BaseException) -> bool:
    """True only for network-offline errors (thin wrapper over the classifier)."""
    return classify_edge_failure(exc).kind == "offline"


class EdgeBreaker:
    """Circuit breaker for transient Edge-TTS failures (thread-safe).

    N consecutive failures open it; while open, allow() is False until the
    cooldown elapses, then exactly one probe is allowed. Probe success closes
    it; probe failure re-opens with a doubled cooldown (capped).
    """

    def __init__(self, threshold: int = 3, cooldown_s: float = 120,
                 max_cooldown_s: float = 900, clock: Callable[[], float] = time.monotonic):
        self._threshold = max(1, int(threshold))
        self._base_cooldown = cooldown_s
        self._max_cooldown = max_cooldown_s
        self._clock = clock
        self._lock = threading.Lock()
        self._consecutive = 0
        self._state = "closed"  # closed | open | half_open
        self._until = 0.0
        self.cooldown_s = cooldown_s
        self.last_error: Optional[str] = None

    @property
    def consecutive(self) -> int:
        return self._consecutive

    @property
    def degraded(self) -> bool:
        return self._state != "closed"

    def try_acquire(self) -> "tuple[bool, Optional[str]]":
        """Like allow(), also reporting the open -> half_open transition ("half_open")."""
        with self._lock:
            if self._state == "closed":
                return True, None
            if self._state == "open" and self._clock() >= self._until:
                self._state = "half_open"
                return True, "half_open"
            return False, None

    def allow(self) -> bool:
        return self.try_acquire()[0]

    def record_failure(self, code: str) -> Optional[str]:
        """Record a transient failure; returns "open" when the breaker (re)opens."""
        with self._lock:
            self.last_error = code
            self._consecutive += 1
            if self._state == "half_open":
                self.cooldown_s = min(self.cooldown_s * 2, self._max_cooldown)
            elif self._state == "closed" and self._consecutive >= self._threshold:
                self.cooldown_s = self._base_cooldown
            else:
                return None
            self._state = "open"
            self._until = self._clock() + self.cooldown_s
            return "open"

    def record_success(self) -> Optional[str]:
        with self._lock:
            was_open = self._state != "closed"
            self._consecutive = 0
            self._state = "closed"
            self.cooldown_s = self._base_cooldown
            self.last_error = None
            return "closed" if was_open else None


def calculate_speech_pause(text: str) -> float:
    """Calculate natural, biologically plausible pause duration based on punctuation hierarchy.

    Hierarchical pause model (Prosodic Boundary Strengths):
      - Ellipsis / suspense (`...`, `…`): 0.45s - 0.65s (deep breath / trailing thought)
      - Sentence-final terminator (`.`, `!`, `?`):
          * Long sentence (>18 words): 0.45s - 0.60s (recovery breath)
          * Standard sentence: 0.35s - 0.50s (respiratory reset)
      - Intra-sentence clause break (`,`, `;`, `:`): 0.14s - 0.24s (cadence without stall)
      - Default / unpunctuated chunk: 0.25s - 0.38s
    """
    stripped = (text or "").strip()
    if not stripped:
        return random.uniform(0.20, 0.30)

    # 1. Ellipsis / suspense
    if stripped.endswith("...") or stripped.endswith("…"):
        return random.uniform(0.45, 0.65)

    # 2. Major sentence boundary (. ! ?)
    if stripped.endswith((".", "!", "?")):
        word_count = len(stripped.split())
        if word_count > 18:
            return random.uniform(0.45, 0.60)
        return random.uniform(0.35, 0.50)

    # 3. Minor clause boundary (, ; :)
    if stripped.endswith((",", ";", ":")):
        return random.uniform(0.14, 0.24)

    # 4. Default fallback
    return random.uniform(0.25, 0.38)


