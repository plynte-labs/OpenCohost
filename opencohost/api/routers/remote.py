"""Operator side of remote phone pairing (remote-voice-gateway-20261020 T2).

- ``POST /api/remote/pair-code``: mint the one live pairing code.
- ``GET /api/remote/devices``: paired devices, never their token hashes.
- ``DELETE /api/remote/devices/{device_id}``: revoke one device.
- ``GET /api/remote/messages?after=<seq>``: accepted phone messages (in-memory
  feed, text included) for the operator's own conversation view. Never served by
  the gateway app.

Every path under ``/api/remote/`` needs the OPERATOR token for every method,
even while ``API_AUTH_ENFORCED`` is off: ``auth_middleware`` gates the prefix
before any handler here runs (a pairing code is the key to the house). With the
gateway flag off, or no registry on ``app.state``, each route answers 409
``{error: "gateway_disabled"}``. The registry is the one the lifespan created
and also injected into the gateway app.
"""

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from opencohost.api.models import (
    RemoteDevicesResponse,
    RemoteMessagesResponse,
    RemotePairCodeResponse,
    RemoteRevokeResponse,
)
from opencohost.config import settings

router = APIRouter()


def _registry(request: Request):
    if not settings.REMOTE_GATEWAY_ENABLED:
        return None
    return getattr(request.app.state, "remote_devices", None)


def _disabled() -> JSONResponse:
    return JSONResponse(status_code=409, content={"error": "gateway_disabled"})


@router.post(
    "/api/remote/pair-code",
    response_model=RemotePairCodeResponse,
    responses={409: {"description": "gateway_disabled"}},
)
def post_remote_pair_code(request: Request, response: Response):
    registry = _registry(request)
    if registry is None:
        return _disabled()
    code, expires_at = registry.mint_code()
    response.headers["Cache-Control"] = "no-store"
    return {"code": code, "expires_at": expires_at, "gateway_port": settings.REMOTE_GATEWAY_PORT}


@router.get(
    "/api/remote/devices",
    response_model=RemoteDevicesResponse,
    responses={409: {"description": "gateway_disabled"}},
)
def get_remote_devices(request: Request):
    registry = _registry(request)
    if registry is None:
        return _disabled()
    return {"devices": registry.list_devices()}


@router.delete(
    "/api/remote/devices/{device_id}",
    response_model=RemoteRevokeResponse,
    responses={404: {"description": "unknown_device"}, 409: {"description": "gateway_disabled"}},
)
def delete_remote_device(device_id: str, request: Request):
    registry = _registry(request)
    if registry is None:
        return _disabled()
    if not registry.revoke(device_id):
        return JSONResponse(status_code=404, content={"error": "unknown_device"})
    return {"device_id": device_id, "revoked": True}


@router.get(
    "/api/remote/messages",
    response_model=RemoteMessagesResponse,
    responses={409: {"description": "gateway_disabled"}},
)
def get_remote_messages(request: Request, response: Response, after: int = 0):
    feed = getattr(request.app.state, "remote_feed", None)
    if not settings.REMOTE_GATEWAY_ENABLED or feed is None:
        return _disabled()
    response.headers["Cache-Control"] = "no-store"
    return feed.since(max(after, 0))
