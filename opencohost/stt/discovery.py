"""Discovery + ensure orchestration for the local LiveAudio backend.

- :func:`is_loopback_uri`: auto-spawn gate — only loopback/unset/default-local
  URIs may trigger a local spawn. Anything else (second capture PC) is used
  verbatim and NEVER spawns.
- :func:`find_liveaudio_port`: bounded ``base..base+9`` scan with a real
  hello handshake (``app == 'liveaudio'``, ``proto == 1``, ``port`` echoes
  the candidate). Never accepts an arbitrary WS in range.
- :func:`ensure_local_service`: probe → spawn/wait → scan-fallback → probe →
  runtime repoint (NO persistence) → readiness. Single-flight per supervisor.

No transcript/audio/secrets are logged here — only URIs (config, not speech)
at debug level and ports/codes at info level.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

logger = logging.getLogger("opencohost.stt.discovery")

HELLO_APP = "liveaudio"
HELLO_PROTO = 1
FALLBACK_RANGE = 10
DEFAULT_BASE_PORT = 8765
MAX_PORT = 65535
LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})

HANDSHAKE_TIMEOUT = 0.7

# Persistent single-flight fallback: used when a supervisor double carries no
# own ``ensure_lock`` (tests/legacy). Module-level on purpose — a per-call
# lock would serialize nothing and two concurrent ensures could double-spawn.
_ENSURE_LOCK = threading.Lock()


def is_loopback_host(uri) -> bool:
    """True when *uri* addresses a loopback host, any WS scheme.

    Display/routing predicate only — it says where the URI points, not what
    OpenCohost may do about it. ``wss://`` loopback included.
    """
    if not isinstance(uri, str):
        return False
    if not (uri.startswith("ws://") or uri.startswith("wss://")):
        return False
    try:
        host = (urlparse(uri).hostname or "").lower()
    except Exception:
        return False
    return host in LOOPBACK_HOSTS


def is_loopback_uri(uri) -> bool:
    """True only for plain-``ws://`` loopback URIs eligible for auto-spawn.

    ``wss://`` loopback is deliberately NOT spawn-eligible: a TLS endpoint is
    a configured/manual connection (a reverse proxy or a hardened local
    setup), treated exactly like a remote URI — used as-is, never spawned,
    never scanned, never repointed. Non-strings, invalid schemes, and any
    non-loopback host return False as well.
    """
    if not isinstance(uri, str) or not uri.startswith("ws://"):
        return False
    return is_loopback_host(uri)


def base_port_of(uri, default: int = DEFAULT_BASE_PORT) -> int:
    """Port configured in *uri*, or *default* when absent/invalid."""
    try:
        port = urlparse(uri).port if isinstance(uri, str) else None
    except Exception:
        port = None
    return port if isinstance(port, int) and 1 <= port <= 65535 else default


def with_port(uri: str, port: int) -> str:
    """Return *uri* with its port replaced (scheme/host/path preserved)."""
    parts = urlparse(uri)
    host = parts.hostname or "127.0.0.1"
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]:{port}"
    else:
        netloc = f"{host}:{port}"
    if parts.username:
        auth = parts.username + (f":{parts.password}" if parts.password else "") + "@"
        netloc = auth + netloc
    return parts._replace(netloc=netloc).geturl()


def validate_hello(payload, candidate: int) -> bool:
    """True when *payload* is a genuine LiveAudio hello for *candidate*."""
    if not isinstance(payload, dict):
        return False
    if payload.get("type") != "hello":
        return False
    if payload.get("app") != HELLO_APP:
        return False
    if payload.get("proto") != HELLO_PROTO:
        return False
    try:
        return int(payload.get("port")) == int(candidate)
    except (TypeError, ValueError):
        return False


def _default_handshake(uri: str, timeout: float = HANDSHAKE_TIMEOUT):
    """Connect, read the first frame, close. Returns the parsed dict or None.

    A successful connect counts as the first WS client, which is exactly what
    triggers the service lazy audio/ASR load — acceptable by contract.
    """

    async def _recv_first():
        import websockets

        async with websockets.connect(uri, open_timeout=timeout, close_timeout=2) as ws:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            return json.loads(raw) if isinstance(raw, str) else None

    try:
        return asyncio.run(_recv_first())
    except Exception:
        return None


def find_liveaudio_port(
    base: int,
    *,
    host: str = "127.0.0.1",
    scheme: str = "ws",
    timeout: float = HANDSHAKE_TIMEOUT,
    handshake: Callable = None,
    ports: int = FALLBACK_RANGE,
) -> Optional[int]:
    """Scan ``base..base+ports-1`` clamped to 65535; return the first port
    whose hello validates.

    ``handshake(uri, timeout)`` is injectable for deterministic tests. Never
    raises — an unreachable candidate simply yields None for that port.
    Out-of-range or non-integer bases scan nothing and return None.
    """
    try:
        base = int(base)
    except (TypeError, ValueError):
        return None
    if not 1 <= base <= MAX_PORT:
        return None
    greet = handshake or _default_handshake
    for port in range(base, min(base + ports, MAX_PORT + 1)):
        uri = f"{scheme}://{host}:{port}"
        try:
            payload = greet(uri, timeout)
        except Exception:
            continue
        if validate_hello(payload, port):
            return port
    return None


@dataclass
class EnsureOutcome:
    """Result of :func:`ensure_local_service` (loopback URIs only)."""

    uri: str
    spawned: bool = False
    available: bool = False
    loading: bool = False
    detail: str = "stt_unreachable"


def ensure_local_service(
    *,
    configured_uri: str,
    controller,
    supervisor,
    probe,
    scan=None,
    port_timeout: Optional[float] = None,
) -> EnsureOutcome:
    """Make a loopback STT URI usable, spawning LiveAudio at most once.

    - Remote (non-loopback) URIs are rejected here (defense in depth — the
      router checks first, this never spawns for them either).
    - Probe the configured URI first (manual LiveAudio already up → no spawn).
    - Else spawn via the supervisor (single-flight on ``ensure_lock``),
      wait for the stdout ``ws_port`` authority, else fall back to the
      hello-handshake scan (manual LiveAudio on a drifted port).
    - On success, repoint the controller RUNTIME ONLY via ``set_ws_uri``
      (the ephemeral fallback port is deliberately NOT persisted).
    - ``loading`` is True when the service is alive but ASR is still
      ``starting``/``loading`` — the caller waits bounded, then answers a
      retryable ``stt_loading`` instead of blocking forever.

    ``probe(uri)`` returns ``(ok, detail)`` (the bounded PTT probe).
    ``controller`` needs ``set_ws_uri``; ``scan`` defaults to
    :func:`find_liveaudio_port`. Never raises — failures ride the outcome.
    """
    outcome = EnsureOutcome(uri=configured_uri)
    if not is_loopback_uri(configured_uri):
        return outcome
    lock = getattr(supervisor, "ensure_lock", None) or _ENSURE_LOCK
    try:
        with lock:
            try:
                ok, _ = probe(configured_uri)
            except Exception:
                ok = False
            if ok:
                outcome.available = True
                outcome.detail = "connected"
                outcome.loading = bool(getattr(supervisor, "is_loading", lambda: False)())
                return outcome

            spawned = False
            try:
                spawned = bool(supervisor.ensure_started())
            except Exception:
                spawned = False
            outcome.spawned = spawned

            port: Optional[int] = None
            if spawned:
                try:
                    if port_timeout is None:
                        port = supervisor.wait_for_port()
                    else:
                        port = supervisor.wait_for_port(port_timeout)
                except Exception:
                    port = None
            if port is None:
                # Manual-LiveAudio compat: drifted port, no stdout authority.
                base = base_port_of(configured_uri)
                scan_fn = scan or find_liveaudio_port
                try:
                    port = scan_fn(base)
                except Exception:
                    port = None
            if port is None:
                outcome.loading = bool(getattr(supervisor, "is_loading", lambda: False)())
                outcome.detail = "stt_loading" if outcome.loading else "stt_unreachable"
                return outcome

            candidate = with_port(configured_uri, port)
            try:
                ok, _ = probe(candidate)
            except Exception:
                ok = False
            if not ok:
                outcome.loading = bool(getattr(supervisor, "is_loading", lambda: False)())
                outcome.detail = "stt_loading" if outcome.loading else "stt_unreachable"
                return outcome
            try:
                controller.set_ws_uri(candidate)
            except Exception:
                pass
            outcome.uri = candidate
            outcome.available = True
            outcome.detail = "connected"
            outcome.loading = bool(getattr(supervisor, "is_loading", lambda: False)())
            logger.info("liveaudio ensured uri port=%d spawned=%s", port, spawned)
            return outcome
    except Exception:
        return outcome
    return outcome
