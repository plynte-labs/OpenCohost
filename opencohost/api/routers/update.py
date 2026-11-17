"""GET /api/update and PUT /api/update/config (update-notification-20261111 N1).

``GET`` returns the once-per-launch cached check plus the owner switch. ``PUT``
flips the switch. Operator tier on PUT is enforced by the generic
mutating-route middleware.

Errors keep a stable code: 422 ``{detail: "invalid_update_setting"}``, 503
``{detail: "update_setting_write_failed"}``.
"""

import threading

from fastapi import APIRouter
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from opencohost.api.models import UpdateConfigRequest, UpdateConfigResponse, UpdateStatusResponse
from opencohost.config import update_setting
from opencohost.core import update_check

_INVALID = {"detail": "invalid_update_setting"}


class _UpdateRoute(APIRoute):
    """Maps request validation failures to the stable 422 code."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def _handler(request):
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse(status_code=422, content=_INVALID)

        return _handler


router = APIRouter(route_class=_UpdateRoute)

_write_lock = threading.Lock()


@router.get("/api/update", response_model=UpdateStatusResponse)
def get_update() -> dict:
    """Sync handler: runs in the threadpool, so the bounded (5 s) first request never blocks the loop."""
    result = update_check.check()
    return {"enabled": update_setting.is_enabled(), **result}


@router.put("/api/update/config", response_model=UpdateConfigResponse)
def put_update_config(body: UpdateConfigRequest):
    with _write_lock:
        try:
            saved = update_setting.save_update_check(body.enabled)
        except ValueError:
            return JSONResponse(status_code=422, content=_INVALID)
    if saved is None:
        return JSONResponse(status_code=503, content={"detail": "update_setting_write_failed"})
    return {"enabled": saved}
