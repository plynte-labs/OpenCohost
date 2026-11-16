"""GET/PUT /api/self-knowledge (self-knowledge K3).

Owner toggles (``enabled`` master, ``viewers``) and the editable identity card
(``creator``, ``repository``, ``description``). PUT is a partial update: only
the keys sent change. Operator tier on PUT is enforced by the generic
mutating-route middleware (same as PUT /api/guardrails).
"""

import threading

from fastapi import APIRouter, Body
from fastapi.responses import JSONResponse

from opencohost.config import self_knowledge_settings as sks
from opencohost.core.self_knowledge.texts import DEFAULT_CREATOR, DEFAULT_REPOSITORY

router = APIRouter()

_lock = threading.Lock()


def _payload() -> dict:
    s = sks.load_settings()
    return {
        "enabled": s.enabled,
        "viewers": s.viewers,
        "creator": s.creator,
        "repository": s.repository,
        "description": s.description,
        # What Kira says while a field is empty, so a UI can show it as a hint.
        "defaults": {"creator": DEFAULT_CREATOR, "repository": DEFAULT_REPOSITORY},
    }


@router.get("/api/self-knowledge")
def get_self_knowledge() -> dict:
    """Current toggles, identity fields and the built-in identity defaults."""
    return _payload()


@router.put("/api/self-knowledge")
def put_self_knowledge(body: dict = Body(...)):
    """Partial update. 422 ``{detail: "invalid_self_knowledge", errors:
    [{field, code: "unknown" | "invalid" | "too_long"}]}``; 503 when the file
    cannot be written."""
    with _lock:
        result = sks.save_settings(body)
    if result is None:
        return JSONResponse(status_code=503, content={"detail": "self_knowledge_write_failed"})
    if result:
        return JSONResponse(
            status_code=422, content={"detail": "invalid_self_knowledge", "errors": result}
        )
    return _payload()
