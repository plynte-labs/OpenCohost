"""Read-only local VRM assets and the latest actual playback chunk."""
from pathlib import Path
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

from opencohost.config.settings import VRM_MODEL_DIR
from opencohost.api.auth import TokenFileError, _bearer_token, _resolve_role

router = APIRouter(prefix="/api/avatar/vrm")


def _model_path(name: str) -> Path:
    # Check both platform separators and Windows drive/ADS syntax everywhere.
    if not name or any(c in name for c in "/\\:\x00") or Path(name).suffix.lower() != ".vrm":
        raise HTTPException(400, "Invalid model name")
    try:
        root = Path(VRM_MODEL_DIR).resolve()
        path = (root / name).resolve(strict=True)
        if path.parent != root or not path.is_file():
            raise OSError
        return path
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(404, "Model unavailable") from None


@router.get("/list")
def list_models():
    result = []
    try:
        names = sorted(p.name for p in Path(VRM_MODEL_DIR).iterdir())
    except OSError:
        names = []
    for name in names:
        if Path(name).suffix.lower() != ".vrm":
            continue
        try:
            stat = _model_path(name).stat()
            result.append({"filename": name, "size_mb": round(stat.st_size / 1048576, 3), "modified_at": stat.st_mtime})
        except (HTTPException, OSError):
            continue
    return result


@router.get("/model")
def get_model(request: Request, name: str):
    path = _model_path(name)
    try:
        stat = path.stat()
    except OSError:
        raise HTTPException(404, "Model unavailable") from None
    etag = f'"{stat.st_mtime_ns:x}-{stat.st_size:x}"'
    headers = {"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return FileResponse(path, media_type="model/gltf-binary", headers=headers)


def _audio(request: Request):
    host = getattr(request.app.state, "host", None)
    motor = getattr(host, "motor", None)
    snapshot = getattr(motor, "_vrm_audio_snapshot", None)
    active = bool(snapshot is not None and snapshot.active and getattr(motor, "_speaking", False))
    return snapshot, active


@router.get("/audio/state")
def audio_state(request: Request):
    snapshot, active = _audio(request)
    return JSONResponse({
        "sequence": snapshot.sequence if snapshot else 0,
        "active": active,
        "elapsed_ms": max(0, (time.monotonic() - snapshot.started_at) * 1000) if active else 0,
    }, headers={"Cache-Control": "no-store"})


@router.get("/audio/last")
def last_audio(request: Request, sequence: int | None = None):
    headers = {"Cache-Control": "no-store"}
    # Plain GETs bypass auth middleware. Speech bytes are operator-only.
    try:
        token = _bearer_token(request)
        role = _resolve_role(token) if token else None
    except TokenFileError:
        return JSONResponse({"detail": "auth_unavailable"}, status_code=503, headers=headers)
    if role != "operator":
        return JSONResponse({"detail": "unauthorized"}, status_code=401, headers=headers)
    snapshot, active = _audio(request)
    if not active:
        return Response(status_code=404, headers=headers)
    if sequence is not None and sequence != snapshot.sequence:
        return Response(status_code=409, headers=headers)
    return Response(snapshot.data, media_type=snapshot.content_type, headers=headers)
