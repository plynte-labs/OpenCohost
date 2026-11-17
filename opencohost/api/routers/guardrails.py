"""GET/PUT /api/guardrails (guardrail-toggles T2).

Owner policy for the toggleable output guardrails (R9/R10/R11); locked safety
rules are listed but can never be disabled. Operator tier on PUT is enforced by
the generic mutating-route middleware (same as PUT /api/personalization).
"""

import threading

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from opencohost.config import guardrail_policy

router = APIRouter()

_guardrails_lock = threading.Lock()


def _payload() -> dict:
    return {"rules": guardrail_policy.list_rules()}


@router.get("/api/guardrails")
def get_guardrails() -> dict:
    """``{rules: [{id, tier, category, enabled}]}``."""
    return _payload()


@router.put("/api/guardrails")
def put_guardrails(body: dict = Body(...)):
    """Full replace of the disabled set. 422 ``{detail: "invalid_guardrails",
    errors: [{id, code: "unknown" | "locked" | "invalid"}]}``; 503 when the
    file cannot be written."""
    with _guardrails_lock:
        result = guardrail_policy.save_guardrails(body.get("disabled"))
    if result is None:
        return JSONResponse(status_code=503, content={"detail": "guardrails_write_failed"})
    if result:
        return JSONResponse(
            status_code=422, content={"detail": "invalid_guardrails", "errors": result}
        )
    return _payload()
