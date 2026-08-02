"""GET/PUT/DELETE /api/personalization
(moved verbatim from main.py, refactor_core_api_20260802 B5).

`_personalization_response` was a closure nested inside `create_app()` in
main.py (its only three callers -- get/update/the module itself -- all move
here) -- it touches no create_app-local state, only its `data` argument, so
it is relocated here as a plain private helper (same precedent as
`routers/i18n_tts.py`'s `_i18n_state_response`). `_personalization_lock`
lives in `opencohost.api.shared` (never monkeypatched). `load_personalization`
and the `PERSONALIZATION_*` length caps are never monkeypatched either, so
they are imported directly from their home modules. `save_personalization`
and `clear_personalization` ARE monkeypatched directly on
`opencohost.api.main` (test_api_personalization.py, to exercise the 503
write-failure paths), so they go through `deps`.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from opencohost.api import deps
from opencohost.api.models import PersonalizationResponse, PersonalizationUpdateRequest
from opencohost.api.shared import _personalization_lock
from opencohost.config.settings import (
    PERSONALIZATION_INSTRUCTIONS_MAX,
    PERSONALIZATION_INTERESTS_MAX,
    PERSONALIZATION_NICKNAME_MAX,
    PERSONALIZATION_OCCUPATION_MAX,
)
from opencohost.core.personalization import load_personalization

router = APIRouter()


def _personalization_response(data: dict) -> PersonalizationResponse:
    # Explicit field picks only — never `**data` — so the internal
    # `version` key (or any future field) can never leak through.
    return PersonalizationResponse(
        enabled=bool(data.get("enabled", True)),
        nickname=str(data.get("nickname", "")),
        occupation=str(data.get("occupation", "")),
        interests=str(data.get("interests", "")),
        custom_instructions=str(data.get("custom_instructions", "")),
        updated_at=data.get("updated_at"),
    )


@router.get("/api/personalization", response_model=PersonalizationResponse)
def get_personalization() -> PersonalizationResponse:
    # Lock-free like list_perfiles/get_perfil — GET never mutates and
    # load_personalization() already fails open to defaults.
    return _personalization_response(load_personalization())


@router.put("/api/personalization", response_model=PersonalizationResponse)
def update_personalization(body: PersonalizationUpdateRequest):
    if body.nickname is not None and len(body.nickname) > PERSONALIZATION_NICKNAME_MAX:
        return JSONResponse(status_code=422, content={"detail": "nickname exceeds max length"})
    if body.occupation is not None and len(body.occupation) > PERSONALIZATION_OCCUPATION_MAX:
        return JSONResponse(
            status_code=422, content={"detail": "occupation exceeds max length"}
        )
    if body.interests is not None and len(body.interests) > PERSONALIZATION_INTERESTS_MAX:
        return JSONResponse(status_code=422, content={"detail": "interests exceeds max length"})
    if (
        body.custom_instructions is not None
        and len(body.custom_instructions) > PERSONALIZATION_INSTRUCTIONS_MAX
    ):
        return JSONResponse(
            status_code=422, content={"detail": "custom_instructions exceeds max length"}
        )
    with _personalization_lock:
        data = load_personalization()
        if body.enabled is not None:
            data["enabled"] = body.enabled
        if body.nickname is not None:
            data["nickname"] = body.nickname
        if body.occupation is not None:
            data["occupation"] = body.occupation
        if body.interests is not None:
            data["interests"] = body.interests
        if body.custom_instructions is not None:
            data["custom_instructions"] = body.custom_instructions
        if not deps.save_personalization(data):
            return JSONResponse(
                status_code=503, content={"detail": "personalization_write_failed"}
            )
        # Re-read: save_personalization stamps its own `updated_at` and
        # re-clamps, so the response must reflect the persisted values,
        # not the pre-save `data` dict.
        data = load_personalization()
    return _personalization_response(data)


@router.delete("/api/personalization")
def delete_personalization():
    with _personalization_lock:
        if not deps.clear_personalization():
            return JSONResponse(
                status_code=503, content={"detail": "personalization_write_failed"}
            )
    return {"ok": True}
