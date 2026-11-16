"""Who am I? build_version + install_id for ``GET /api/health``.

The desktop supervisor compares these with its own install's runtime manifest so
it never reuses a backend that belongs to another install or an older build.

``install_id`` source:
  1. ``<data root>/state/runtime-manifest.json`` -> ``install_id`` (written by the
     first-run code next to the registry ``InstallId``), so both sides agree;
  2. None when the manifest is missing/corrupt/blank (dev runs). No id is ever
     generated, so the supervisor never sees a mismatch it cannot explain.
``build_version`` is the package version (the supervisor normalizes spellings).
Never raises; never logs the id.
"""

from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from typing import Optional

_lock = threading.Lock()
_cached: Optional[str] = None
_resolved = False


def build_version() -> str:
    import opencohost

    return str(opencohost.__version__)


def _data_root() -> Optional[Path]:
    explicit = os.environ.get("OPENCOHOST_DATA_ROOT", "").strip()
    if explicit:
        return Path(os.path.expandvars(os.path.expanduser(explicit)))
    if getattr(sys, "frozen", False):
        from opencohost.config.storage import get_user_data_dir

        return get_user_data_dir()
    return None


def _non_blank(value: object) -> Optional[str]:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _resolve() -> Optional[str]:
    """The install_id from ``<data root>/state/runtime-manifest.json`` only.

    Missing or corrupt manifest -> None (the supervisor then falls back to legacy
    reuse instead of flagging a mismatch). Nothing is ever generated or written.
    """
    root = _data_root()
    if root is None:
        return None
    try:
        manifest = json.loads((root / "state" / "runtime-manifest.json").read_text(encoding="utf-8"))
        return _non_blank(manifest.get("install_id")) if isinstance(manifest, dict) else None
    except Exception:
        return None


def install_id() -> Optional[str]:
    """Stable per data root; cached for the life of the process."""
    global _cached, _resolved
    with _lock:
        if not _resolved:
            _cached = _resolve()
            _resolved = _cached is not None  # retry later if it could not be resolved
        return _cached


def reset_cache() -> None:
    """Test hook: forget the cached id."""
    global _cached, _resolved
    with _lock:
        _cached, _resolved = None, False
