"""Owner master switch for web lookups (web-fetch-laya-20261009).

``{"enabled": bool}``, default off. Same shape as ``reply_length``:
  * The switch lives in memory; ``is_enabled()`` never touches disk, so the
    turn path pays nothing.
  * Nothing reads disk until ``load_web_fetch()`` (API startup), which keeps
    unrelated tests hermetic and leaves the legacy CTk surface off.
  * ``OPENCOHOST_WEB_FETCH`` (``settings.WEB_FETCH_DEFAULT``) gives the
    default; the persisted file wins. A missing, corrupt or invalid file
    means the default.
  * Writes are atomic (temp file + ``os.replace``); the in-memory value only
    changes after a successful write.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Optional

from opencohost.config import settings
from opencohost.config.logger import get_logger
from opencohost.config.settings import WEB_FETCH_FILE

logger = get_logger()

_lock = threading.Lock()
_enabled: bool = False


def _install(enabled: bool) -> None:
    global _enabled
    with _lock:
        _enabled = bool(enabled)


def is_enabled() -> bool:
    """The switch, in memory only."""
    return _enabled


def set_enabled(enabled: bool) -> None:
    """Install in memory only (tests, callers that persist elsewhere)."""
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a bool")
    _install(enabled)


def reset_web_fetch() -> None:
    """Back to the env default. Does not touch disk."""
    _install(bool(settings.WEB_FETCH_DEFAULT))


def load_web_fetch() -> bool:
    """Load the switch from disk into memory. Missing, corrupt or invalid -> default."""
    path = WEB_FETCH_FILE
    enabled = bool(settings.WEB_FETCH_DEFAULT)
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            candidate = data.get("enabled") if isinstance(data, dict) else None
            if isinstance(candidate, bool):
                enabled = candidate
            else:
                logger.warning("[WEB] invalid settings file, using the default")
        except Exception as e:
            logger.warning("[WEB] unreadable settings file (%s), using the default", type(e).__name__)
    _install(enabled)
    logger.info("[WEB] setting enabled=%s (loaded)", enabled)
    return enabled


def save_web_fetch(enabled) -> Optional[bool]:
    """Validate, write atomically and install.

    Returns the saved value, or ``None`` when the write failed (callers map
    that to 503). Raises ``ValueError`` for a non-bool (callers map that to
    422). The in-memory value only changes on success.
    """
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a bool")
    path = WEB_FETCH_FILE
    target_dir = os.path.dirname(path) or "."
    tmp_path = None
    try:
        os.makedirs(target_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".web_fetch_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"enabled": enabled}, f, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        logger.warning("Failed to save web lookup setting: %s", type(e).__name__)
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return None
    _install(enabled)
    logger.info("[WEB] setting enabled=%s (saved)", enabled)
    return enabled


reset_web_fetch()
