"""i18n-core — persisted locale state (next-boot model).

The CLI writes the desired locale; it takes effect on the NEXT startup. The
running process keeps the locale it loaded at startup — there is no runtime
switch (owner decision). Persistence IS the next-boot mechanism: the live
process never re-reads this file, so a write only affects the next start.

Cross-process contract with LiveAudio (``liveaudio-cli serve``):
LiveAudio reads Kira's persisted locale file at startup to align speech
recognition language with OpenCohost:
- Path: ``<OPENCOHOST_DATA_ROOT>/config/locale.json`` (when
  ``OPENCOHOST_DATA_ROOT`` is unset, LiveAudio falls back to
  ``%LOCALAPPDATA%\\OpenCohostData\\config\\locale.json``).
- Format: JSON object ``{"locale": "<BCP-47 tag>", "set_at": "<ISO timestamp>"}``.
  LiveAudio inspects only the primary subtag and accepts ``"es"`` or ``"en"``.
Do not rename this file, move it outside ``<OPENCOHOST_DATA_ROOT>``, or change
the top-level ``"locale"`` string key without coordinating with LiveAudio.

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
    """Return ``<USER_DATA_DIR>/config/locale.json`` (external contract with LiveAudio; ``USER_DATA_DIR`` follows ``OPENCOHOST_DATA_ROOT``)."""
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

    Preserves the cross-process LiveAudio contract in
    ``<OPENCOHOST_DATA_ROOT>/config/locale.json`` with a top-level ``"locale"``
    string key (``"es"``/``"en"`` BCP-47 tag) and ``"set_at"`` ISO timestamp.

    Raises on I/O failure — an explicit set should fail loudly, unlike the
    best-effort runtime persistence elsewhere.
    """
    path = Path(locale_file) if locale_file else _default_locale_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = str(path) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"locale": code, "set_at": datetime.now().isoformat()}, f)
    os.replace(tmp, path)

    # Reachability fix (kira_english_default_locale gap, judge-confirmed
    # 2026-08-14): profile first-run seeding (profiles.cargar_perfiles) fires
    # only ONCE, for whatever locale was active at that boot. A machine that
    # boots es and only later switches to en would otherwise never get the
    # English personas written to disk. Seed additively the moment a locale
    # actually lands here. Lazy import: profiles.py imports this module at
    # top level (i18n_state.get_locale), so a top-level import here would be
    # circular.
    try:
        from opencohost.core.profiles import profiles as _profiles
        from opencohost.i18n.tags import primary as _primary

        target_locale = _primary(code) or DEFAULT_LOCALE
        _profiles.seed_locale_profiles(target_locale)
        _profiles.align_last_profile(target_locale)
    except Exception:
        # Convenience, not a gate: the locale write above already succeeded
        # and must never be undone by a seeding failure.
        pass

    try:
        from opencohost.config.settings import fill_piper_voice_for_locale

        # Only a missing slot is filled: the user's saved voice for this locale is never replaced.
        fill_piper_voice_for_locale(code)
    except Exception:
        pass
