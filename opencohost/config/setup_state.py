"""Persisted setup-assistant status: ``{"status": "pending"|"skipped"|"completed"}``.

One tiny JSON file (``settings.SETUP_STATE_FILE``), written atomically. A missing,
corrupt or wrongly typed file means ``pending``. The path is read from
``settings`` at call time so tests can redirect it.
"""
from __future__ import annotations

import json
import sys

from opencohost.config import settings
from opencohost.config.storage import atomic_write_text

STATUSES = ("pending", "skipped", "completed")
DEFAULT_STATUS = "pending"


def load_status() -> str:
    try:
        with open(settings.SETUP_STATE_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return DEFAULT_STATUS
    status = raw.get("status") if isinstance(raw, dict) else None
    return status if status in STATUSES else DEFAULT_STATUS


def save_status(status: str) -> None:
    """Persist *status* atomically. Raises ``ValueError`` for an unknown status and
    ``OSError`` when the write fails (callers map that to 503)."""
    if status not in STATUSES:
        raise ValueError(f"invalid setup status: {status!r}")
    atomic_write_text(settings.SETUP_STATE_FILE, json.dumps({"status": status}, indent=2))


_LEGACY_INSTALL_KEY = r"Software\opencohost\OpenCohost"


def detect_legacy_install() -> bool:
    r"""True when an earlier OpenCohost install left its per-user registry entry.

    Read-only: opens the registry key HKCU\Software\opencohost\OpenCohost and checks that the
    default value is non-empty. The value (the old install path) is never returned,
    logged or followed. Non-Windows or any error means ``False``.
    """
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _LEGACY_INSTALL_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, "")
    except Exception:
        return False
    return isinstance(value, str) and bool(value.strip())
