"""Owner switch for the once-per-launch update check (update-notification-20261111).

``{"enabled": bool}``, default ON. Same shape as ``web_fetch``: the value lives
in memory, nothing reads disk until ``load_update_check()`` (API startup), a
missing/corrupt/invalid file means the default, and writes are atomic with the
in-memory value changing only after a successful write.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Optional

from opencohost.config.logger import get_logger
from opencohost.config.settings import UPDATE_CHECK_FILE

logger = get_logger()

_lock = threading.Lock()
_enabled: bool = True


def _install(enabled: bool) -> None:
    global _enabled
    with _lock:
        _enabled = bool(enabled)


def is_enabled() -> bool:
    """The switch, in memory only."""
    return _enabled


def set_enabled(enabled: bool) -> None:
    """Install in memory only (tests)."""
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a bool")
    _install(enabled)


def reset_update_check() -> None:
    """Back to the default (on). Does not touch disk."""
    _install(True)


def load_update_check() -> bool:
    """Load the switch from disk into memory. Missing, corrupt or invalid -> on."""
    enabled = True
    if os.path.exists(UPDATE_CHECK_FILE):
        try:
            with open(UPDATE_CHECK_FILE, encoding="utf-8") as f:
                data = json.load(f)
            candidate = data.get("enabled") if isinstance(data, dict) else None
            if isinstance(candidate, bool):
                enabled = candidate
            else:
                logger.warning("[UPDATE_CHECK] invalid settings file, using the default")
        except Exception as e:
            logger.warning("[UPDATE_CHECK] unreadable settings file (%s), using the default", type(e).__name__)
    _install(enabled)
    logger.info("[UPDATE_CHECK] setting enabled=%s (loaded)", enabled)
    return enabled


def save_update_check(enabled) -> Optional[bool]:
    """Validate, write atomically and install.

    Returns the saved value, or ``None`` when the write failed (callers map that
    to 503). Raises ``ValueError`` for a non-bool (callers map that to 422).
    """
    if not isinstance(enabled, bool):
        raise ValueError("enabled must be a bool")
    path = UPDATE_CHECK_FILE
    target_dir = os.path.dirname(path) or "."
    tmp_path = None
    try:
        os.makedirs(target_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".update_check_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"enabled": enabled}, f, indent=2)
        os.replace(tmp_path, path)
    except Exception as e:
        logger.warning("Failed to save update check setting: %s", type(e).__name__)
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return None
    _install(enabled)
    logger.info("[UPDATE_CHECK] setting enabled=%s (saved)", enabled)
    return enabled
