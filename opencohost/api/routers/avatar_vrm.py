"""Read-only local VRM assets and the latest actual playback chunk."""
from pathlib import Path
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel

from opencohost.config.settings import VRM_MODEL_DIR
from opencohost.api.auth import TokenFileError, _bearer_token, _resolve_role
from opencohost.api.shared import _config_lock
from opencohost.avatar import avatar_config as avatar_config_mod
from opencohost.avatar import vrm_folder

logger = logging.getLogger("opencohost.api.avatar_vrm")

router = APIRouter(prefix="/api/avatar/vrm")


def _current_dir() -> tuple[Path, str]:
    """Resolved per request, so a folder change applies with no restart."""
    return vrm_folder.resolve_vrm_model_dir(Path(VRM_MODEL_DIR))


def _valid_name(name: str) -> None:
    # Check both platform separators and Windows drive/ADS syntax everywhere.
    if not name or any(c in name for c in "/\\:\x00") or Path(name).suffix.lower() != ".vrm":
        raise HTTPException(400, "Invalid model name")


def _model_path(name: str) -> Path:
    _valid_name(name)
    # The bundled model wins by name and is served by its fixed known path only.
    bundled = vrm_folder.bundled_model()
    if bundled is not None and name == bundled.name:
        return bundled
    try:
        root = _current_dir()[0].resolve()
        path = (root / name).resolve(strict=True)
        if path.parent != root or not path.is_file():
            raise OSError
        return path
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(404, "Model unavailable") from None


def _item(name: str, stat, bundled: bool) -> dict:
    return {
        "filename": name,
        "size_mb": round(stat.st_size / 1048576, 3),
        "modified_at": stat.st_mtime,
        "bundled": bundled,
    }


@router.get("/list")
def list_models():
    result = []
    bundled = vrm_folder.bundled_model()
    if bundled is not None:
        try:
            result.append(_item(bundled.name, bundled.stat(), True))
        except OSError:
            bundled = None
    try:
        names = sorted(p.name for p in _current_dir()[0].iterdir())
    except OSError:
        names = []
    for name in names:
        if Path(name).suffix.lower() != ".vrm":
            continue
        if bundled is not None and name == bundled.name:
            continue  # de-duplicated by name, the bundled one wins
        try:
            result.append(_item(name, _folder_model(name).stat(), False))
        except (HTTPException, OSError):
            continue
    return result


def _folder_model(name: str) -> Path:
    """A model that lives in the chosen folder (never the bundled shortcut)."""
    _valid_name(name)
    try:
        root = _current_dir()[0].resolve()
        path = (root / name).resolve(strict=True)
        if path.parent != root or not path.is_file():
            raise OSError
        return path
    except (OSError, RuntimeError, ValueError):
        raise HTTPException(404, "Model unavailable") from None


@router.get("/model")
def get_model(request: Request, name: str):
    return model_response(request, _model_path(name))


class VrmFolderRequest(BaseModel):
    path: str


def _folder_response() -> dict:
    folder, source = _current_dir()
    return {
        "path": str(folder),
        "source": source,
        "exists": folder.is_dir(),
        "model_count": vrm_folder.count_models(folder),
    }


def _log(outcome: str, models: int = 0) -> None:
    # Enum outcome and a count only: the path can contain the Windows user name.
    logger.info("[VRM_FOLDER] outcome=%s models=%d", outcome, models)


@router.get("/folder")
def get_folder():
    return _folder_response()


@router.put("/folder")
def put_folder(body: VrmFolderRequest):
    try:
        chosen = vrm_folder.validate_folder(body.path)
    except vrm_folder.VrmFolderError as exc:
        _log(exc.code)
        return JSONResponse(status_code=422, content={"detail": exc.code})
    with _config_lock:
        try:
            cfg = avatar_config_mod.load_avatar_config(strict=True)
        except avatar_config_mod.AvatarConfigUnreadableError:
            _log("config_unreadable")
            return JSONResponse(status_code=503, content={"detail": "config_unreadable"})
        cfg.vrm_model_dir = str(chosen)
        try:
            avatar_config_mod.save_avatar_config(cfg)
        except (OSError, RuntimeError):
            _log("config_write_failed")
            return JSONResponse(status_code=503, content={"detail": "config_write_failed"})
    response = _folder_response()
    _log("set", response["model_count"])
    return response


@router.delete("/folder")
def reset_folder():
    with _config_lock:
        try:
            cfg = avatar_config_mod.load_avatar_config(strict=True)
        except avatar_config_mod.AvatarConfigUnreadableError:
            _log("config_unreadable")
            return JSONResponse(status_code=503, content={"detail": "config_unreadable"})
        cfg.vrm_model_dir = ""
        try:
            avatar_config_mod.save_avatar_config(cfg)
        except (OSError, RuntimeError):
            _log("config_write_failed")
            return JSONResponse(status_code=503, content={"detail": "config_write_failed"})
    response = _folder_response()
    _log("reset", response["model_count"])
    return response


def model_response(request: Request, path: Path):
    """Stream ``path`` read-only with an ETag, honouring If-None-Match."""
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
