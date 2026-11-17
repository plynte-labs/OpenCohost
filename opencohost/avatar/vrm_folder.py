"""Owner-changeable folder the VRM models are read from.

The choice lives in ``avatar.yaml`` (``avatar.vrm_model_dir``), next to the other
avatar settings. This module never creates, writes into or deletes anything in the
chosen folder, and never logs its path (it can contain the Windows user name).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from opencohost.avatar.avatar_config import load_avatar_config
from opencohost.config import settings

SAVED = "saved"
ENV = "env"
DEFAULT = "default"

ENV_VAR = "OPENCOHOST_VRM_MODEL_DIR"


class VrmFolderError(ValueError):
    """A folder choice was refused; ``code`` is a stable enum for the front end."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _is_remote_drive(path: Path) -> bool:
    """True for a mapped network drive (Windows only; best effort, never raises)."""
    if sys.platform != "win32" or not path.drive:
        return False
    try:
        import ctypes

        return ctypes.windll.kernel32.GetDriveTypeW(path.drive + "\\") == 4  # DRIVE_REMOTE
    except Exception:
        return False


def validate_folder(raw: str) -> Path:
    """Return the resolved folder or raise :class:`VrmFolderError` with a stable code."""
    text = (raw or "").strip()
    if not text:
        raise VrmFolderError("path_empty")
    if "\x00" in text or len(text) > 1024:
        raise VrmFolderError("path_invalid")
    # UNC, extended-length and device namespaces (\\server\x, //server/x, \\?\C:\, \\.\PhysicalDrive0).
    if text.startswith(("\\\\", "//")):
        raise VrmFolderError("path_network_or_device")
    path = Path(text)
    if not path.is_absolute():
        raise VrmFolderError("path_not_absolute")
    if _is_remote_drive(path):
        raise VrmFolderError("path_network_or_device")
    try:
        if not path.exists():
            raise VrmFolderError("path_not_found")
        if not path.is_dir():
            raise VrmFolderError("path_not_directory")
        with os.scandir(path):
            pass
        return path.resolve()
    except VrmFolderError:
        raise
    except (OSError, ValueError):
        raise VrmFolderError("path_unreadable") from None


def resolve_vrm_model_dir(fallback: Path, config=None) -> tuple[Path, str]:
    """The single place that decides the folder, as ``(path, source)``.

    1. the folder saved by the owner, if it still exists and is a directory;
    2. env ``OPENCOHOST_VRM_MODEL_DIR`` (``fallback`` already holds it);
    3. the default (``fallback`` too, when the env var is not set).
    """
    try:
        cfg = config if config is not None else load_avatar_config()
        saved = (getattr(cfg, "vrm_model_dir", "") or "").strip()
        if saved:
            candidate = Path(saved)
            if candidate.is_absolute() and candidate.is_dir():
                return candidate, SAVED
    except OSError:
        pass
    return Path(fallback), ENV if os.environ.get(ENV_VAR, "").strip() else DEFAULT


def bundled_model() -> Path | None:
    """The bundled Kira v3 (``settings.PHONE_VRM_PATH``), or None when it is not on disk."""
    try:
        path = Path(settings.PHONE_VRM_PATH)
        if path.suffix.lower() == ".vrm" and path.is_file():
            return path
    except (OSError, ValueError):
        pass
    return None


def count_models(folder: Path) -> int:
    """Number of ``.vrm`` files directly inside ``folder`` (0 when unreadable)."""
    try:
        return sum(1 for p in folder.iterdir() if p.suffix.lower() == ".vrm")
    except OSError:
        return 0
