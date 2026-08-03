"""/api/perfiles/* -- GET/POST /api/perfiles, GET/PUT/DELETE
/api/perfiles/{name}, POST /api/perfiles/switch
(moved verbatim from main.py, refactor_core_api_20260802 B5).

`cargar_perfiles` IS monkeypatched directly on `opencohost.api.main` by
tests/test_api_phase1.py (every perfiles CRUD test and every
POST /api/perfiles/switch test), so it goes through `deps.cargar_perfiles()`
-- a plain top-level import would bind the pre-patch function object.
`guardar_perfiles` and `save_last_profile` are never monkeypatched (confirmed
by grep), so they import directly from their home modules. `_profiles_lock`,
`_PROFILE_NAME_MAX_LENGTH`, `_PROFILE_PROMPT_MAX_LENGTH` are used ONLY by
this family and are never monkeypatched, so `_profiles_lock` comes from
`opencohost.api.shared` (shared identity with nothing else needing it
outside this file) and the two length caps relocate here as this router's
own module constants.

CAUTION (registration order / path ambiguity, verified during the move):
this file registers 3 literal paths (`GET/POST /api/perfiles`,
`POST /api/perfiles/switch`) and 1 template (`GET/PUT/DELETE
/api/perfiles/{name}`). `/api/perfiles` (2 segments after `/api/`) and
`/api/perfiles/{name}` / `/api/perfiles/switch` (3 segments) never collide on
segment count. `/api/perfiles/{name}` and `/api/perfiles/switch` DO share a
segment count, but never a method (GET/PUT/DELETE vs POST-only) -- and
Starlette's router does not stop at a path-only ("partial") match on method
mismatch, it keeps scanning for a full path+method match before ever
returning 405, so relative registration order between the template and
`switch` is cosmetic, not load-bearing (same guarantee obs.py/B4 documented
for its own non-overlapping paths).
"""

import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from opencohost.api import deps
from opencohost.api.models import (
    ProfileCreateRequest,
    ProfileDetailResponse,
    ProfileUpdateRequest,
    ProfilesListResponse,
    SwitchProfileRequest,
)
from opencohost.api.shared import _profiles_lock
from opencohost.config.settings import save_last_profile
from opencohost.core.profiles.profiles import guardar_perfiles

router = APIRouter()

# POST/PUT /api/perfiles bounds — profiles.json is loaded whole per request,
# so cap the write to keep a single profile from ballooning the file.
_PROFILE_NAME_MAX_LENGTH = 100
_PROFILE_PROMPT_MAX_LENGTH = 20000


@router.get("/api/perfiles", response_model=ProfilesListResponse)
def list_perfiles() -> ProfilesListResponse:
    perfiles = deps.cargar_perfiles()
    if not isinstance(perfiles, dict):
        return ProfilesListResponse(profiles=[])
    return ProfilesListResponse(profiles=list(perfiles.keys()))


@router.get("/api/perfiles/{name}", response_model=ProfileDetailResponse)
def get_perfil(name: str):
    # R8/D5: explicit field picks — never `**data` — so only name/id/
    # prompt/use_system are ever returned; any future persisted field
    # (or chat content) can never leak through. `id` IS returned so the
    # FE can target this profile's memoria rows (stored keyed by uuid).
    # GET is lock-free like list.
    perfiles = deps.cargar_perfiles()
    if not isinstance(perfiles, dict) or name not in perfiles or not isinstance(
        perfiles[name], dict
    ):
        return JSONResponse(status_code=404, content={"detail": "profile not found"})
    data = perfiles[name]
    return ProfileDetailResponse(
        name=name,
        id=str(data.get("id", "")),
        prompt=str(data.get("prompt", "")),
        use_system=bool(data.get("use_system", False)),
        locale=data.get("locale"),
    )


@router.post("/api/perfiles", response_model=ProfileDetailResponse)
def create_perfil(body: ProfileCreateRequest):
    name = body.name.strip()
    if not name or len(name) > _PROFILE_NAME_MAX_LENGTH:
        return JSONResponse(status_code=422, content={"detail": "invalid profile name"})
    if len(body.prompt) > _PROFILE_PROMPT_MAX_LENGTH:
        return JSONResponse(status_code=422, content={"detail": "prompt exceeds max length"})
    with _profiles_lock:
        perfiles = deps.cargar_perfiles()
        if not isinstance(perfiles, dict):
            return JSONResponse(status_code=503, content={"detail": "profiles_unavailable"})
        if name in perfiles:
            return JSONResponse(status_code=409, content={"detail": "profile already exists"})
        # Stable id minted server-side (R12) — never accepted from the wire.
        new_id = str(uuid.uuid4())
        perfiles[name] = {
            "id": new_id,
            "prompt": body.prompt,
            "use_system": body.use_system,
            "locale": body.locale,
        }
        if not guardar_perfiles(perfiles):
            return JSONResponse(status_code=503, content={"detail": "profiles_write_failed"})
    return ProfileDetailResponse(
        name=name,
        id=new_id,
        prompt=body.prompt,
        use_system=body.use_system,
        locale=body.locale,
    )


@router.put("/api/perfiles/{name}", response_model=ProfileDetailResponse)
def update_perfil(name: str, body: ProfileUpdateRequest):
    new_name = body.new_name.strip() if body.new_name is not None else None
    if new_name is not None and (not new_name or len(new_name) > _PROFILE_NAME_MAX_LENGTH):
        return JSONResponse(status_code=422, content={"detail": "invalid profile name"})
    if body.prompt is not None and len(body.prompt) > _PROFILE_PROMPT_MAX_LENGTH:
        return JSONResponse(status_code=422, content={"detail": "prompt exceeds max length"})
    with _profiles_lock:
        perfiles = deps.cargar_perfiles()
        if not isinstance(perfiles, dict) or name not in perfiles:
            return JSONResponse(status_code=404, content={"detail": "profile not found"})
        # Copy preserves the stable id (R12) across both edit and rename.
        data = dict(perfiles[name])
        if body.prompt is not None:
            data["prompt"] = body.prompt
        if body.use_system is not None:
            data["use_system"] = body.use_system
        if body.locale is not None:
            data["locale"] = body.locale
        target = name
        if new_name and new_name != name:
            if new_name in perfiles:
                return JSONResponse(
                    status_code=409, content={"detail": "profile already exists"}
                )
            del perfiles[name]
            target = new_name
        perfiles[target] = data
        if not guardar_perfiles(perfiles):
            return JSONResponse(status_code=503, content={"detail": "profiles_write_failed"})
    return ProfileDetailResponse(
        name=target,
        id=str(data.get("id", "")),
        prompt=str(data.get("prompt", "")),
        use_system=bool(data.get("use_system", False)),
        locale=data.get("locale"),
    )


@router.delete("/api/perfiles/{name}")
def delete_perfil(name: str):
    with _profiles_lock:
        perfiles = deps.cargar_perfiles()
        if not isinstance(perfiles, dict) or name not in perfiles:
            return JSONResponse(status_code=404, content={"detail": "profile not found"})
        # Last-profile guard ONLY (resolution 2905): deleting the active
        # profile is allowed — the engine keeps its in-memory copy until
        # the next switch (CTK parity, not an improvement).
        if len(perfiles) <= 1:
            return JSONResponse(
                status_code=409, content={"detail": "cannot delete the last profile"}
            )
        del perfiles[name]
        if not guardar_perfiles(perfiles):
            return JSONResponse(status_code=503, content={"detail": "profiles_write_failed"})
    return {"ok": True}


@router.post("/api/perfiles/switch")
def switch_profile(request: Request, body: SwitchProfileRequest):
    key = request.headers.get("Idempotency-Key") or body.idempotency_key
    perfiles = deps.cargar_perfiles()
    if not isinstance(perfiles, dict) or body.name not in perfiles:
        return JSONResponse(status_code=404, content={"detail": "profile not found"})
    data = dict(perfiles[body.name])
    data["_profile_name"] = body.name
    result = request.app.state.dispatcher.dispatch("set_profile", data, key)
    if result.state in ("accepted", "replay"):
        # FIX-A: remember this as the last-used profile so the next engine
        # boot seeds it (best-effort — persistence must never fail the
        # switch; save_last_profile already swallows, the guard is defense
        # in depth).
        try:
            save_last_profile(body.name)
        except Exception:
            pass
        return {"accepted": True, "command_id": result.command_id, "status": "queued"}
    if result.state == "conflict":
        return JSONResponse(status_code=409, content={"accepted": False, "reason": "conflict"})
    return JSONResponse(status_code=429, content={"accepted": False, "reason": "queue_full"})
