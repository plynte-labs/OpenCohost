"""GET/PUT /api/decisions, POST /api/decisions/worker/restart
(laya-decision-layer-20261009 T4).

Owner settings for the decision layer: per decision (``vision``, ``memory``,
``reply_length``, ``respond``, ``web``, ``clock``) a mode ``off | shadow | on`` and the confidence
threshold Laya must clear to act in ``on``, plus the Laya worker status and
the per-turn budget. ``respond`` is shadow-only: ``on`` is rejected. Operator
tier on PUT/POST is enforced by the generic mutating-route middleware.

Errors keep a stable code instead of FastAPI's validation list: 422
``{detail: "invalid_decision_setting"}``, 503
``{detail: "decision_settings_write_failed"}``. The typed request model stays
in the OpenAPI schema so the UI's generated types carry the mode Literal.
"""

import threading

from fastapi import APIRouter
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from opencohost.api.models import DecisionsRequest, DecisionsResponse, DecisionWorkerStatus
from opencohost.config import decisions as decision_settings
from opencohost.config import settings
from opencohost.core.decision import runtime as decision_runtime

_INVALID = {"detail": "invalid_decision_setting"}


class _DecisionsRoute(APIRoute):
    """Maps request validation failures to the stable 422 code."""

    def get_route_handler(self):
        handler = super().get_route_handler()

        async def _handler(request):
            try:
                return await handler(request)
            except RequestValidationError:
                return JSONResponse(status_code=422, content=_INVALID)

        return _handler


router = APIRouter(route_class=_DecisionsRoute)

_decisions_lock = threading.Lock()


def _snapshot(state=None) -> dict:
    return {
        "decisions": state if state is not None else decision_settings.get_decision_settings(),
        "worker": decision_runtime.worker_status(),
        "budget_ms": int(settings.DECISION_BUDGET_MS),
    }


@router.get("/api/decisions", response_model=DecisionsResponse)
def get_decisions() -> dict:
    """``{decisions: {id: {mode, threshold}}, worker, budget_ms}``."""
    return _snapshot()


@router.put("/api/decisions", response_model=DecisionsResponse)
def put_decisions(body: DecisionsRequest):
    """Persist and apply a partial update; effective from the next turn."""
    update = body.decisions.model_dump(exclude_none=True)
    with _decisions_lock:
        try:
            saved = decision_settings.save_decisions(update)
        except ValueError:
            return JSONResponse(status_code=422, content=_INVALID)
    if saved is None:
        return JSONResponse(status_code=503, content={"detail": "decision_settings_write_failed"})
    return _snapshot(saved)


@router.post("/api/decisions/worker/restart", response_model=DecisionWorkerStatus)
def restart_decision_worker() -> dict:
    """Restart the Laya worker (resets its crash budget). Returns its status;
    an unconfigured worker stays ``unavailable``."""
    return decision_runtime.restart_worker()
