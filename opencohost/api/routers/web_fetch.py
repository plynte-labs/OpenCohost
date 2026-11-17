"""GET/PUT /api/web-fetch (web-fetch-laya-20261009 T3).

Owner master switch for web lookups on owner turns: ``{enabled: bool}``,
default off. Operator tier on PUT is enforced by the generic mutating-route
middleware.

Errors keep a stable code instead of FastAPI's validation list: 422
``{detail: "invalid_web_fetch_setting"}``, 503 ``{detail: "web_fetch_write_failed"}``.
"""

import threading

from fastapi import APIRouter
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from opencohost.api.models import WebFetchRequest, WebFetchResponse, TavilyKeyRequest, TavilyKeyResponse
from opencohost.security import search_keys
from opencohost.security.keyring_store import KeyStoreError
from opencohost.config import web_fetch

_INVALID = {"detail": "invalid_web_fetch_setting"}


class _WebFetchRoute(APIRoute):
    """Maps request validation failures to the stable 422 code."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def _handler(request):
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse(status_code=422, content={"detail": "invalid_tavily_setting"}
                                    if request.url.path == "/api/web-fetch/tavily" else _INVALID)

        return _handler


router = APIRouter(route_class=_WebFetchRoute)

_web_fetch_lock = threading.Lock()


@router.get("/api/web-fetch", response_model=WebFetchResponse)
def get_web_fetch() -> dict:
    """``{enabled}``."""
    return {"enabled": web_fetch.is_enabled()}


@router.put("/api/web-fetch", response_model=WebFetchResponse)
def put_web_fetch(body: WebFetchRequest):
    """Persist and apply the switch; effective from the next owner turn."""
    with _web_fetch_lock:
        try:
            saved = web_fetch.save_web_fetch(body.enabled)
        except ValueError:
            return JSONResponse(status_code=422, content=_INVALID)
    if saved is None:
        return JSONResponse(status_code=503, content={"detail": "web_fetch_write_failed"})
    return {"enabled": saved}


@router.get("/api/web-fetch/tavily", response_model=TavilyKeyResponse)
def get_tavily_settings() -> dict:
    """Return presence/source only, without testing or returning the credential."""
    return search_keys.get_tavily_status()


@router.put("/api/web-fetch/tavily", response_model=TavilyKeyResponse)
def put_tavily_settings(body: TavilyKeyRequest):
    """Store/remove a credential locally. Saving is not provider validation."""
    try:
        return search_keys.save_tavily_key(body.api_key.get_secret_value())
    except KeyStoreError:
        return JSONResponse(status_code=503, content={"detail": "tavily_key_store_unavailable"})
