"""GET/PUT /api/avatar/config (moved verbatim from main.py, refactor_core_api_20260802 B4).

`_avatar_config_response`, `_apply_avatar_runtime`, and `_config_lock` live
in `opencohost.api.shared` (refactor_core_api_20260802 B5 Part B -- moved
out of main.py to break the routers<->main module-level import cycle; never
monkeypatched by any test, confirmed by grep, so a plain import is safe).
`_config_lock` is the SAME lock instance `obs.py` guards `avatar.yaml`
writes with (one file, one lock, D4) -- importing the shared module-level
singleton preserves that shared identity.
"""

import base64
import binascii
from pathlib import Path

from fastapi import APIRouter, Request

from opencohost.api.shared import _apply_avatar_runtime, _avatar_config_response, _config_lock
from opencohost.api.models import AvatarConfigRequest, AvatarConfigResponse, AvatarUploadRequest
from opencohost.avatar import avatar_config as avatar_config_mod
from opencohost.avatar.avatar_config import (
    VALID_STATES,
    AvatarConfigUnreadableError,
    commit_upload_tmp,
    load_avatar_config,
    prepare_upload_tmp,
    resolve_state_image,
    save_avatar_config,
)
from fastapi.responses import FileResponse, JSONResponse, Response

router = APIRouter()


@router.get("/api/avatar/config", response_model=AvatarConfigResponse)
def get_avatar_config() -> AvatarConfigResponse:
    return _avatar_config_response(load_avatar_config())


_MEDIA_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
}


@router.get("/api/avatar/image")
def get_avatar_image(request: Request, state: str):
    """Serve the resolved image bytes for an avatar *state* (Tauri KiraCover).

    Resolution: configured map → user assets_folder → bundled defaults → idle
    fallbacks (`resolve_state_image`). Unknown state -> 422, nothing found ->
    404 (the client falls back to its static art). Conditional GET via ETag
    (`st_mtime_ns`-`st_size`) so the 700ms speaking flap revalidates cheaply.
    """
    if state not in VALID_STATES:
        return JSONResponse(status_code=422, content={"detail": f"unknown avatar state: {state}"})
    path = resolve_state_image(load_avatar_config(), state)
    if path is None:
        return JSONResponse(status_code=404, content={"detail": f"no image for state '{state}'"})
    try:
        stat = path.stat()
    except OSError:
        return JSONResponse(status_code=404, content={"detail": f"no image for state '{state}'"})
    etag = f'"{stat.st_mtime_ns}-{stat.st_size}"'
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    return FileResponse(
        path=path,
        media_type=_MEDIA_BY_SUFFIX.get(path.suffix.lower(), "application/octet-stream"),
        headers={"ETag": etag, "Cache-Control": "public, max-age=0, must-revalidate"},
    )


@router.post("/api/avatar/upload", response_model=AvatarConfigResponse)
def post_avatar_upload(request: Request, body: AvatarUploadRequest):
    """Upload avatar image bytes for one state (Tauri `AvatarCard`).

    JSON+base64 on purpose: no `python-multipart` in the dependency set and no
    other multipart endpoint exists — one image (~1-2MB) fits JSON fine. The
    bytes are validated (state/extension/10MB cap/magic) and copied into the
    user avatar dir, so the source file can be moved or deleted afterwards.
    """
    if body.state not in VALID_STATES:
        return JSONResponse(status_code=422, content={"detail": f"unknown avatar state: {body.state}"})
    # Pre-decode guard (judge finding): the 10MB cap must bite BEFORE the full
    # base64 body is inflated in memory. 4/3 inflation + slack.
    if len(body.content_b64) > avatar_config_mod.MAX_B64_CHARS:
        return JSONResponse(status_code=413, content={"detail": "image too large"})
    try:
        raw = base64.b64decode(body.content_b64, validate=True)
    except (binascii.Error, ValueError):
        return JSONResponse(status_code=422, content={"detail": "invalid base64 content"})
    if len(raw) > avatar_config_mod.MAX_UPLOAD_BYTES:
        return JSONResponse(status_code=413, content={"detail": "image too large"})
    # Slow disk write happens BEFORE the global config lock; the lock then
    # only covers load → publish → save (judge finding).
    try:
        staged, suffix = prepare_upload_tmp(body.state, body.filename, raw)
    except ValueError as e:
        return JSONResponse(status_code=422, content={"detail": str(e)})
    with _config_lock:
        try:
            cfg = load_avatar_config(strict=True)
        except AvatarConfigUnreadableError:
            return JSONResponse(status_code=503, content={"detail": "config_unreadable"})
        cfg = commit_upload_tmp(cfg, body.state, staged, suffix)
        try:
            save_avatar_config(cfg)
        except (OSError, RuntimeError):
            return JSONResponse(status_code=503, content={"detail": "config_write_failed"})
        response = _avatar_config_response(cfg)
    # Same FIX-B as the PUT: the live OBSClient snapshots state_images.
    _apply_avatar_runtime(request)
    return response


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
