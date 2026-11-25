"""Read-only local VRM assets and the latest actual playback chunk."""
from pathlib import Path
import json
import logging
import struct
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

_GLB_MAGIC = 0x46546C67
_CHUNK_JSON = 0x4E4F534A
_MAX_JSON_BYTES = 32 * 1024 * 1024
_MAX_THUMB_BYTES = 8 * 1024 * 1024
_THUMB_MIMES = {"image/png", "image/jpeg", "image/webp"}


def _is_index(value, size: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < size


def _thumbnail_image_index(gltf: dict) -> int | None:
    """Image index of the embedded VRM 1.0 / 0.x thumbnail, or None."""
    ext = gltf.get("extensions")
    if not isinstance(ext, dict):
        return None
    v1 = ext.get("VRMC_vrm")
    meta = v1.get("meta") if isinstance(v1, dict) else None
    if isinstance(meta, dict) and meta.get("thumbnailImage") is not None:
        return meta["thumbnailImage"]
    v0 = ext.get("VRM")
    meta = v0.get("meta") if isinstance(v0, dict) else None
    tex = meta.get("texture") if isinstance(meta, dict) else None
    textures = gltf.get("textures")
    if isinstance(textures, list) and _is_index(tex, len(textures)) and isinstance(textures[tex], dict):
        return textures[tex].get("source")
    return None


def _read_vrm_thumbnail(path: Path) -> tuple[bytes, str] | None:
    """Extract the embedded thumbnail from a GLB, reading only the needed ranges."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(20)
            if len(head) < 20:
                return None
            magic, version, _total, json_len, json_type = struct.unpack("<IIIII", head)
            if magic != _GLB_MAGIC or version != 2 or json_type != _CHUNK_JSON:
                return None
            if json_len <= 0 or json_len > _MAX_JSON_BYTES:
                return None
            raw = fh.read(json_len)
            if len(raw) != json_len:
                return None
            gltf = json.loads(raw)
            if not isinstance(gltf, dict):
                return None
            image_index = _thumbnail_image_index(gltf)
            images = gltf.get("images")
            views = gltf.get("bufferViews")
            if not isinstance(images, list) or not isinstance(views, list):
                return None
            if not _is_index(image_index, len(images)) or not isinstance(images[image_index], dict):
                return None
            image = images[image_index]
            mime = image.get("mimeType")
            if mime not in _THUMB_MIMES or not _is_index(image.get("bufferView"), len(views)):
                return None
            view = views[image["bufferView"]]
            if not isinstance(view, dict) or view.get("buffer") != 0:
                return None
            offset = view.get("byteOffset", 0)
            length = view.get("byteLength")
            if not (isinstance(offset, int) and isinstance(length, int)) or isinstance(offset, bool) or isinstance(length, bool):
                return None
            if offset < 0 or length <= 0 or length > _MAX_THUMB_BYTES:
                return None
            fh.seek(20 + json_len)  # BIN chunk header follows the JSON chunk
            bin_head = fh.read(8)
            if len(bin_head) < 8:
                return None
            bin_len, bin_type = struct.unpack("<II", bin_head)
            if bin_type != 0x004E4942 or offset + length > bin_len:
                return None
            fh.seek(offset, 1)
            data = fh.read(length)
            return (data, mime) if len(data) == length else None
    except (OSError, ValueError, struct.error, RecursionError):
        return None


@router.get("/thumbnail")
def get_thumbnail(name: str):
    # Same exposure as /model: plain GETs carry no router-level auth, and the
    # name is confined to the model folder by _model_path.
    found = _read_vrm_thumbnail(_model_path(name))
    if found is None:
        raise HTTPException(404, "No thumbnail")
    data, mime = found
    return Response(content=data, media_type=mime, headers={"Cache-Control": "private, max-age=3600"})


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
