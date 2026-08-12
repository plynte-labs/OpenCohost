"""Bearer-token auth for the OpenCohost HTTP API.

Track agent_context_gateway_20260705, Phase 1 (design.md 'Auth design',
ADR-3/ADR-4). Two static tokens — ``operator`` and ``agent`` — are minted once
at API startup into ``USER_DATA_DIR/config/api_tokens.json`` and never
regenerated (delete the file to rotate). Verification uses
``hmac.compare_digest``; the operator token is a strict superset (accepted
everywhere the agent token is, never the reverse — ADR-3).

One ASGI-level middleware (``auth_middleware``, registered in
``create_app()``) applies three rules (ADR-4):

1. Paths under ``/api/agent/`` require the agent OR operator token — ALWAYS
   enforced (new surface, nothing shipped calls it yet). 401 on missing or
   invalid token; 503 if the token file is unusable; mutating calls are
   additionally rate limited per token (``RateLimiter``, 429 on breach).
2. Other ``/api/*`` POST/PUT/DELETE requests require the operator token, but
   only when ``settings.API_AUTH_ENFORCED`` is true (env OPENCOHOST_API_AUTH,
   default OFF). Owner decision D2: while the flag is off, a missing/invalid
   token logs one warning per call and the request succeeds unchanged, so the
   shipped Tauri app (which sends no token today) keeps working.
3. GET (and every non-mutating method) stays open in v1, because the read
   surface carries state and metadata rather than viewer content.
   ONE EXCEPTION, and it must stay the only one: ``GET
   /api/stream/chat-live/messages`` serves raw viewer chat text and usernames,
   so it does not rely on this rule — it gates itself on the operator token
   unconditionally (see ``routers/stream.py``). Any future GET that would carry
   viewer content must do the same; do not read this rule as permission.
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
import threading
import time
from pathlib import Path

from starlette.responses import JSONResponse

from opencohost.config import settings
from opencohost.config.storage import atomic_write_text

logger = logging.getLogger("opencohost.api.auth")

_AGENT_PREFIX = "/api/agent/"
_MUTATING_METHODS = frozenset({"POST", "PUT", "DELETE"})
# Order matters for _resolve_role: operator first so logs/semantics prefer
# the higher-trust label when (never expected) both tokens match.
_TOKEN_ROLES = ("operator", "agent")


class TokenFileError(Exception):
    """The token file is missing, unreadable, or malformed."""


def _tokens_path() -> Path:
    # Resolved lazily so tests can monkeypatch settings.API_TOKENS_FILE.
    return Path(settings.API_TOKENS_FILE)


def ensure_tokens() -> None:
    """Mint the token file if absent; never regenerate an existing one.

    Rotation = delete the file and restart the backend (documented in
    AGENT_GATEWAY.md, Phase 5). Best-effort by design: an OSError during the
    mint is logged, not raised, so a failed write degrades to 503s on the
    protected surfaces instead of blocking app startup.
    """
    path = _tokens_path()
    if path.exists():
        return
    payload = {
        "version": 1,
        "operator": secrets.token_urlsafe(32),
        "agent": secrets.token_urlsafe(32),
    }
    try:
        atomic_write_text(path, json.dumps(payload, indent=2))
    except OSError:
        logger.warning("Could not mint API token file at %s", path, exc_info=True)


def load_tokens() -> dict:
    """Return ``{"operator": str, "agent": str}`` from the token file.

    Raises TokenFileError when the file is missing, unreadable, or malformed —
    the middleware maps that to a 503 on protected surfaces (never a crash).
    """
    path = _tokens_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TokenFileError(f"cannot read token file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise TokenFileError("token file is not a JSON object")
    tokens = {}
    for role in _TOKEN_ROLES:
        value = data.get(role)
        if not isinstance(value, str) or not value:
            raise TokenFileError(f"token file missing {role!r} token")
        tokens[role] = value
    return tokens


def verify_token(candidate: str, expected: str) -> bool:
    """Constant-time comparison of a presented token against an expected one."""
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


class RateLimiter:
    """Fixed-window limiter for mutating agent-surface requests, per token.

    Design 'Rate limit' (agent_context_gateway Phase 2): 60 mutating
    requests per 60-second window per token, ``threading.Lock`` + dict —
    the same stdlib-only pattern as ``Dispatcher`` (dispatch.py). Ceiling
    justification: every agent store is already bounded (inbox 30 pending,
    notices 20, cards are slug-upserts), so this only bounds SQLite write
    churn and log spam; 60/min lets an agent sync its entire allowable
    state in one window while making tight-loop floods harmless.

    Only VALID tokens are counted (the middleware resolves the role first),
    so the dict can never grow past one entry per real token.

    # ponytail: fixed window, upgrade to sliding only if a real agent hits the edge
    """

    def __init__(
        self,
        limit: int = 60,
        window_seconds: float = 60.0,
        time_fn=time.time,
    ) -> None:
        self._limit = limit
        self._window_seconds = window_seconds
        self._now = time_fn
        self._lock = threading.Lock()
        # token -> (window_start, count)
        self._windows: dict[str, tuple[float, int]] = {}

    def allow(self, token: str) -> bool:
        """Consume one slot for ``token``; False when the window is exhausted."""
        with self._lock:
            now = self._now()
            window_start, count = self._windows.get(token, (now, 0))
            if now - window_start >= self._window_seconds:
                window_start, count = now, 0
            if count >= self._limit:
                self._windows[token] = (window_start, count)
                return False
            self._windows[token] = (window_start, count + 1)
            return True


_rate_limiter = RateLimiter()


def _bearer_token(request) -> str | None:
    value = request.headers.get("authorization") or ""
    scheme, _, token = value.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _resolve_role(token: str) -> str | None:
    """Return "operator"/"agent" for a valid token, None for an invalid one.

    Propagates TokenFileError so callers decide between 503 (enforced
    surfaces) and warn-and-pass (D2 warn-only mode).
    """
    # ponytail: per-request file read — the file is tiny and only mutating
    # requests reach here; cache per-app if it ever shows up in profiles.
    tokens = load_tokens()
    for role in _TOKEN_ROLES:
        if verify_token(token, tokens[role]):
            return role
    return None


async def auth_middleware(request, call_next):
    """Single auth gate for the whole API (ADR-4). See module docstring."""
    path = request.url.path

    # Rule 1: agent surface — always enforced, any method.
    if path.startswith(_AGENT_PREFIX):
        token = _bearer_token(request)
        if token is None:
            return JSONResponse(status_code=401, content={"detail": "missing bearer token"})
        try:
            role = _resolve_role(token)
        except TokenFileError:
            return JSONResponse(status_code=503, content={"detail": "auth_unavailable"})
        if role is None:
            return JSONResponse(status_code=401, content={"detail": "invalid token"})
        # Mutating agent-surface calls are rate limited per token (design
        # 'Rate limit'); reads (GET /api/agent/status) are never counted.
        # Operator routes (rule 2) stay unlimited — the Tauri app is the
        # operator's own hands, not an unattended loop.
        if request.method in _MUTATING_METHODS and not _rate_limiter.allow(token):
            return JSONResponse(status_code=429, content={"detail": "rate_limited"})
        return await call_next(request)

    # Rule 2: other mutating /api/* routes — operator token, warn-only until
    # API_AUTH_ENFORCED flips on (owner decision D2).
    if path.startswith("/api/") and request.method in _MUTATING_METHODS:
        token = _bearer_token(request)
        if not settings.API_AUTH_ENFORCED:
            role = None
            if token is not None:
                try:
                    role = _resolve_role(token)
                except TokenFileError:
                    role = None
            if role != "operator":
                logger.warning(
                    "Unauthenticated %s %s accepted (OPENCOHOST_API_AUTH not enforced; "
                    "this request will require the operator token once the flag flips on)",
                    request.method,
                    path,
                )
            return await call_next(request)
        if token is None:
            return JSONResponse(status_code=401, content={"detail": "missing bearer token"})
        try:
            role = _resolve_role(token)
        except TokenFileError:
            return JSONResponse(status_code=503, content={"detail": "auth_unavailable"})
        if role == "operator":
            return await call_next(request)
        if role == "agent":
            return JSONResponse(status_code=403, content={"detail": "operator token required"})
        return JSONResponse(status_code=401, content={"detail": "invalid token"})

    # Rule 3: GET/HEAD/OPTIONS and non-API paths stay open in v1.
    return await call_next(request)
