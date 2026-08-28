"""
opencohost/core/speech/speech_rate.py

TTS speech rate conversion and calculation utilities.
Extracted from MotorVocalIA.
"""

from __future__ import annotations

import socket
import ssl

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


def _is_connection_error(exc: BaseException) -> bool:
    """Walk the exception cause chain; return True only for network-offline errors.

    Classified as connection errors: socket.gaierror, ssl.SSLError,
    aiohttp.ClientConnectorError.  asyncio.TimeoutError and all other
    exceptions return False.
    """
    seen: set = set()
    e: BaseException | None = exc
    while e is not None and id(e) not in seen:
        seen.add(id(e))
        if isinstance(e, (socket.gaierror, ssl.SSLError)):
            return True
        if aiohttp is not None and isinstance(e, aiohttp.ClientConnectorError):
            return True
        e = e.__cause__ or e.__context__  # type: ignore[assignment]
    return False
