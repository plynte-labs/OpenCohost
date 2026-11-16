"""GET/PUT /api/reply-length (reply-length-selector-20261009 T1).

Owner setting for how long Kira's owner-turn replies are: ``auto`` (per-turn
heuristic) or a fixed ``short | medium | long | very_long``. Operator tier on
PUT is enforced by the generic mutating-route middleware.

Errors keep a stable code instead of FastAPI's validation list: 422
``{detail: "invalid_reply_length"}``, 503 ``{detail: "reply_length_write_failed"}``.
The typed request model stays in the OpenAPI schema so the UI's generated
types carry the mode Literal.
"""

import threading

from fastapi import APIRouter
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from opencohost.api.models import ReplyLengthRequest, ReplyLengthResponse
from opencohost.config import reply_length


class _ReplyLengthRoute(APIRoute):
    """Maps request validation failures to the stable 422 code."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def _handler(request):
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse(status_code=422, content={"detail": "invalid_reply_length"})

        return _handler


router = APIRouter(route_class=_ReplyLengthRoute)

_reply_length_lock = threading.Lock()


@router.get("/api/reply-length", response_model=ReplyLengthResponse)
def get_reply_length() -> dict:
    """``{mode}``."""
    return {"mode": reply_length.get_reply_length_mode()}


@router.put("/api/reply-length", response_model=ReplyLengthResponse)
def put_reply_length(body: ReplyLengthRequest):
    """Persist and apply the mode; effective from the next owner turn."""
    with _reply_length_lock:
        try:
            saved = reply_length.save_reply_length(body.mode)
        except ValueError:
            return JSONResponse(status_code=422, content={"detail": "invalid_reply_length"})
    if saved is None:
        return JSONResponse(status_code=503, content={"detail": "reply_length_write_failed"})
    return {"mode": saved}
