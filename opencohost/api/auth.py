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
   invalid token; 503 if the token file is unusable.
2. Other ``/api/*`` POST/PUT/DELETE requests require the operator token, but
   only when ``settings.API_AUTH_ENFORCED`` is true (env OPENCOHOST_API_AUTH,
   default OFF). Owner decision D2: while the flag is off, a missing/invalid
   token logs one warning per call and the request succeeds unchanged, so the
   shipped Tauri app (which sends no token today) keeps working.
3. GET (and every non-mutating method) stays open in v1 — the read surface
   already keeps raw chat off HTTP entirely (R8).
"""

from __future__ import annotations

import hmac
import json
import logging
import secrets
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
