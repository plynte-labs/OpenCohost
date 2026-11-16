"""Gateway FastAPI app. Deliberately NOT the main API: no shared middleware, no CORS,
no docs. Host, dispatcher and the device registry live on THIS app's state, never
the main app's.

Route allowlist: ``GET /``, ``GET /phone.js`` and ``GET /avatar.js`` (static code),
``GET /animations/{clip}.vrma`` (fixed clip allowlist), ``GET /status`` and ``POST /pair``
are open;
every other route (``POST /turn``, ``GET /reply``, ``GET /whoami``, ``GET /avatar/model``) is registered on
the ``protected`` router, whose dependency ``require_device`` demands a paired device's bearer token. A route added anywhere
else is a security bug.
"""

import json
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from opencohost.api.routers import avatar_vrm
from opencohost.config import settings

_PAIR_BODY_MAX_BYTES = 1024
# Mirrors VRMA_PATHS in OpenCohost_UI/src/features/avatar-vrm/domain/vrmAnimator.ts.
_VRMA_CLIPS = frozenset({
    "VRMA_02", "idle", "VRMA_06", "005_smartphone", "004_hello_1",
    "006_drinkwater", "001_motion_pose", "VRMA_03", "VRMA_04",
})
_NO_STORE = {"Cache-Control": "no-store"}
_STATIC_DIR = Path(__file__).resolve().parent / "static"
# Inline styles are allowed; inline scripts are not (the JS is a separate file).
_PAGE_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
        "connect-src 'self' blob:; img-src 'self' data: blob:; base-uri 'none'; form-action 'none'; "
        "frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}


def _bearer_token(request: Request):
    scheme, _, token = (request.headers.get("authorization") or "").partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def require_device(request: Request) -> str:
    """Device bearer-token gate: the paired device id, or 401."""
    registry = getattr(request.app.state, "devices", None)
    token = _bearer_token(request)
    device_id = registry.verify(token) if registry is not None and token is not None else None
    if device_id is None:
        raise HTTPException(status_code=401, detail="unauthorized", headers={"WWW-Authenticate": "Bearer"})
    return device_id


async def _read_capped_body(request: Request, limit: int):
    """The raw body, or None when it exceeds ``limit`` (checked while streaming)."""
    declared = request.headers.get("content-length")
    if declared is not None and (not declared.isdigit() or int(declared) > limit):
        return None
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def create_gateway_app(host, dispatcher, registry=None, clock=None, feed=None) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, debug=False)
    app.state.host = host
    app.state.dispatcher = dispatcher
    # The main API lifespan injects its own registry: one per process, shared
    # with the operator endpoints. Without one, /pair and every protected route
    # fail closed with 401.
    app.state.devices = registry
    protected = APIRouter(dependencies=[Depends(require_device)])

    # Static phone page: read once at app creation, open (no auth), never embeds a token.
    page_html = (_STATIC_DIR / "phone.html").read_bytes()
    page_js = (_STATIC_DIR / "phone.js").read_bytes()
    avatar_file = _STATIC_DIR / "avatar.js"
    avatar_js = avatar_file.read_bytes() if avatar_file.is_file() else None

    @app.get("/")
    def phone_page() -> Response:
        return Response(page_html, media_type="text/html; charset=utf-8", headers=_PAGE_HEADERS)

    @app.get("/phone.js")
    def phone_script() -> Response:
        return Response(page_js, media_type="application/javascript", headers=_PAGE_HEADERS)

    @app.get("/avatar.js")
    def avatar_script() -> Response:
        if avatar_js is None:
            raise HTTPException(status_code=404)
        return Response(avatar_js, media_type="application/javascript", headers=_PAGE_HEADERS)

    @app.get("/animations/{clip}.vrma")
    def animation(clip: str) -> Response:
        # Fixed allowlist: the request never reaches the filesystem as a name.
        if clip not in _VRMA_CLIPS:
            raise HTTPException(status_code=404)
        try:
            data = (Path(settings.VRMA_DIR) / f"{clip}.vrma").read_bytes()
        except OSError:
            raise HTTPException(status_code=404) from None
        return Response(
            data,
            media_type="application/octet-stream",
            headers={**_PAGE_HEADERS, "Cache-Control": "private, max-age=86400"},
        )

    @app.get("/status")
    def status() -> dict:
        # Readiness only: never text, tokens or settings.
        return {
            "ok": True,
            "gateway": "v0",
            "engine_ready": getattr(app.state.host, "motor", None) is not None,
        }

    @app.post("/pair")
    async def pair(request: Request):
        # One generic 401 for wrong, expired, reused and burned codes alike.
        body = await _read_capped_body(request, _PAIR_BODY_MAX_BYTES)
        if body is None:
            return JSONResponse(status_code=413, content={"error": "too_large"})
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("code"), str):
            return JSONResponse(status_code=400, content={"error": "bad_request"})
        registry = app.state.devices
        result = registry.exchange(data["code"], data.get("name")) if registry is not None else None
        if result is None:
            return JSONResponse(status_code=401, content={"error": "pairing_failed"}, headers=_NO_STORE)
        device_id, token = result
        return JSONResponse(content={"device_id": device_id, "token": token}, headers=_NO_STORE)

    @protected.get("/avatar/model")
    def avatar_model(request: Request) -> Response:
        # No parameter selects the file: always settings.PHONE_VRM_PATH (Kira v3), read-only.
        path = Path(settings.PHONE_VRM_PATH)
        if path.suffix.lower() != ".vrm" or not path.is_file():
            raise HTTPException(404, "Model unavailable")
        return avatar_vrm.model_response(request, path)

    @protected.get("/whoami")
    def whoami(device_id: str = Depends(require_device)) -> dict:
        return {"device_id": device_id}

    from opencohost.gateway.turns import TurnRelay, register_turn_routes

    relay = TurnRelay(host, dispatcher, clock) if clock is not None else TurnRelay(host, dispatcher)
    register_turn_routes(protected, relay, _read_capped_body, feed, registry)

    app.include_router(protected)
    return app
