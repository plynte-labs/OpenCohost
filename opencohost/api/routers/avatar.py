"""GET/PUT /api/avatar/config (moved verbatim from main.py, refactor_core_api_20260802 B4).

`_avatar_config_response`, `_apply_avatar_runtime`, and `_config_lock` stay
defined in main.py (never monkeypatched by any test -- confirmed by grep)
and are imported directly here. `_config_lock` is the SAME lock instance
`obs.py` guards `avatar.yaml` writes with (one file, one lock, D4) --
importing the module-level singleton preserves that shared identity.
"""

from pathlib import Path

from fastapi import APIRouter, Request

from opencohost.api.main import _apply_avatar_runtime, _avatar_config_response, _config_lock
from opencohost.api.models import AvatarConfigRequest, AvatarConfigResponse
from opencohost.avatar.avatar_config import (
    VALID_STATES,
    AvatarConfigUnreadableError,
    load_avatar_config,
    save_avatar_config,
)
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/api/avatar/config", response_model=AvatarConfigResponse)
def get_avatar_config() -> AvatarConfigResponse:
    return _avatar_config_response(load_avatar_config())


@router.put("/api/avatar/config", response_model=AvatarConfigResponse)
def put_avatar_config(request: Request, body: AvatarConfigRequest):
    if body.state_images is not None:
        unknown = sorted(set(body.state_images) - VALID_STATES)
        if unknown:
            return JSONResponse(
                status_code=422, content={"detail": f"unknown avatar state(s): {unknown}"}
            )
    with _config_lock:
        try:
            cfg = load_avatar_config(strict=True)
        except AvatarConfigUnreadableError:
            return JSONResponse(status_code=503, content={"detail": "config_unreadable"})
        if body.enabled is not None:
            cfg.enabled = body.enabled
        if body.mode is not None:
            cfg.mode = body.mode
        if body.state_images is not None:
            new_state_images = dict(cfg.state_images)
            for state, path in body.state_images.items():
                new_state_images[state] = Path(path)
            cfg.state_images = new_state_images
        try:
            save_avatar_config(cfg)
        except (OSError, RuntimeError):
            return JSONResponse(status_code=503, content={"detail": "config_write_failed"})
        response = _avatar_config_response(cfg)
    # FIX-B: a live OBSClient snapshots state_images at construction, so an
    # avatar-card change needs a full runtime rebuild, not just a reconnect.
    _apply_avatar_runtime(request)
    return response
