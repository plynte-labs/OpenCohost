"""i18n-core — persisted locale state (next-boot model).

The CLI writes the desired locale; it takes effect on the NEXT startup. The
running process keeps the locale it loaded at startup — there is no runtime
switch (owner decision). Persistence IS the next-boot mechanism: the live
process never re-reads this file, so a write only affects the next start.

Reads are tolerant (corrupt/missing → default); the explicit write is loud
(raises on failure). Mirrors the atomic tmp + os.replace pattern of
settings.save_last_model.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

DEFAULT_LOCALE = "es"


def _default_locale_file() -> Path:
    from opencohost.config.storage import USER_DATA_DIR

    return Path(USER_DATA_DIR) / "config" / "locale.json"


def get_locale(locale_file: Path | str | None = None) -> str:
    """Return the persisted locale, or :data:`DEFAULT_LOCALE` if unset/corrupt."""
    path = Path(locale_file) if locale_file else _default_locale_file()
    try:
        if path.exists():
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            code = str(data.get("locale", "")).strip()
            if code:
                return code
    except Exception:
        pass
    return DEFAULT_LOCALE


def set_locale(code: str, locale_file: Path | str | None = None) -> None:
    """Persist the desired locale (takes effect on next start). Atomic write.

    Raises on I/O failure — an explicit set should fail loudly, unlike the
    best-effort runtime persistence elsewhere.
    """
    path = Path(locale_file) if locale_file else _default_locale_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"locale": code, "set_at": datetime.now().isoformat()}, f)
    os.replace(tmp, path)
