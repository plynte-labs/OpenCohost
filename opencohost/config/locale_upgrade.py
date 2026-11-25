"""Upgrade-time locale preservation, Piper-voice inference, and *_en.json subdivision.

Runs once during API startup (before ``load_pronunciation()`` and ``EngineHost``
creation) so users upgrading from 0.3.0 / 0.5.0-alpha keep their persisted
locale or infer ``en`` when they switched to the English Piper voice without
calling ``/api/i18n``. When the resolved primary locale is ``en``, performs a
one-time non-destructive copy of any existing unsuffixed stores to their
``*_en.json`` counterparts without overwriting existing ``*_en.json`` files.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from opencohost.config import settings
from opencohost.config.logger import get_logger
from opencohost.config.settings import CONFIG_DIR
from opencohost.config.storage import atomic_write_text
from opencohost.core.profiles import cohost_profiles as cohost_profiles_mod
from opencohost.core.profiles import personalization as personalization_mod
from opencohost.core.profiles import profiles as profiles_mod
from opencohost.core.speech import pronunciation as pronunciation_mod
from opencohost.i18n import active as i18n_active
from opencohost.i18n import state as i18n_state
from opencohost.i18n.tags import primary as i18n_primary

logger = get_logger()

EN_LOCALE_MIGRATED_FILE: str = os.path.join(CONFIG_DIR, ".en_locale_migrated.json")


def _read_persisted_locale(locale_path: Path) -> str | None:
    """Return the persisted locale code if present and non-empty, else None."""
    if not locale_path.exists():
        return None
    try:
        with open(locale_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("locale")
    code = raw.strip() if isinstance(raw, str) else ""
    return code or None


def _infer_locale_from_piper_voice(piper_voice_path: Path) -> str | None:
    """Infer primary locale code from ``piper_voice.json`` when valid."""
    if not piper_voice_path.exists():
        return None
    try:
        with open(piper_voice_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    key = data.get("piper_voice")
    voice_key = key.strip() if isinstance(key, str) else ""
    voice_meta = settings.PIPER_VOICES.get(voice_key)
    if not isinstance(voice_meta, dict):
        return None
    lang = i18n_primary(str(voice_meta.get("lang") or ""))
    return lang or None


def _persist_inferred_locale(locale_path: Path, locale_code: str) -> bool:
    """Atomically write ``locale.json`` using the ``opencohost.i18n.state`` schema
    (``{"locale": ..., "set_at": ...}`` consumed cross-process by LiveAudio),
    without touching ``piper_voice.json``.
    """
    payload = {"locale": locale_code, "set_at": datetime.now().isoformat()}
    try:
        atomic_write_text(locale_path, json.dumps(payload))
        return True
    except Exception as exc:
        logger.warning("locale_upgrade: failed to persist locale (%s)", type(exc).__name__)
        return False


def _en_counterpart(base_path: Path) -> Path:
    return base_path.with_name(f"{base_path.stem}_en{base_path.suffix}")


def _copy_store_to_en_if_missing(src: Path) -> bool:
    """Non-destructively copy ``src`` to ``*_en.json`` if missing; never overwrite."""
    dst = _en_counterpart(src)
    if dst.exists():
        return True
    if not src.exists() or not src.is_file():
        return True
    try:
        content = src.read_text(encoding="utf-8")
        atomic_write_text(dst, content)
        return True
    except Exception as exc:
        logger.warning("locale_upgrade: failed migrating %s (%s)", dst.name, type(exc).__name__)
        return False


def _migrate_en_stores_once() -> None:
    """Run one-time copy of unsuffixed stores to ``*_en.json`` when marker is absent."""
    marker_path = Path(EN_LOCALE_MIGRATED_FILE)
    if marker_path.exists():
        return

    base_paths = (
        Path(personalization_mod.PERSONALIZATION_FILE),
        Path(pronunciation_mod.PRONUNCIATION_FILE),
        Path(cohost_profiles_mod.COHOST_PROFILES_FILE),
    )
    results = [_copy_store_to_en_if_missing(src) for src in base_paths]
    if all(results):
        marker_payload = {
            "version": 1,
            "migrated_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            atomic_write_text(marker_path, json.dumps(marker_payload, indent=2))
        except Exception as exc:
            logger.warning("locale_upgrade: failed writing marker (%s)", type(exc).__name__)


def ensure_upgrade_locale_and_en_files() -> str:
    """Preserve or infer locale at startup and subdivide ``*_en.json`` once for ``en``."""
    locale_path = i18n_state._default_locale_file()
    existing = _read_persisted_locale(locale_path)
    if existing is not None:
        resolved_primary = i18n_primary(existing) or i18n_state.DEFAULT_LOCALE
    else:
        inferred = _infer_locale_from_piper_voice(Path(settings.PIPER_VOICE_FILE))
        if inferred is not None:
            _persist_inferred_locale(locale_path, inferred)
            if inferred == "en":
                try:
                    profiles_mod.seed_locale_profiles("en")
                except Exception as exc:
                    logger.warning(
                        "locale_upgrade: failed seeding en profiles (%s)",
                        type(exc).__name__,
                    )
            i18n_active.reset_active_bundle()
            resolved_primary = inferred
        else:
            resolved_primary = i18n_state.DEFAULT_LOCALE

    if resolved_primary == "en":
        _migrate_en_stores_once()

    return resolved_primary
