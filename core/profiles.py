import json
import os
from pathlib import Path

from config.settings import PROFILES_FILE, SYSTEM_PROMPT
from config.storage import BASE_DIR
from config.logger import get_logger

logger = get_logger()

# Shipped defaults — tracked in config/, never modified at runtime.
DEFAULT_PROFILES_FILE = str(BASE_DIR / "config" / "default_profiles.json")


def _load_default_profiles() -> dict | None:
    """Load curated defaults from config/default_profiles.json.

    Returns the parsed dict on success, or None if the file is absent,
    unreadable, invalid JSON, or empty — never raises.
    """
    try:
        if not os.path.exists(DEFAULT_PROFILES_FILE):
            return None
        with open(DEFAULT_PROFILES_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or not data:
            return None
        return data
    except Exception as e:
        logger.warning(f"Could not load default profiles: {e}")
        return None


def cargar_perfiles() -> dict:
    """Load profiles from PROFILES_FILE.

    On first run (file absent), seeds from config/default_profiles.json and
    writes the result to PROFILES_FILE so subsequent calls load from disk.
    Falls back to a bare SYSTEM_PROMPT profile when defaults are missing or
    corrupt — never raises.
    """
    if os.path.exists(PROFILES_FILE):
        try:
            with open(PROFILES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error cargando perfiles: {e}")

    # PROFILES_FILE does not exist — attempt to seed from shipped defaults.
    defaults = _load_default_profiles()
    if defaults:
        try:
            os.makedirs(os.path.dirname(PROFILES_FILE), exist_ok=True)
            with open(PROFILES_FILE, "w", encoding="utf-8") as f:
                json.dump(defaults, f, indent=4, ensure_ascii=False)
            logger.info(f"Seeded {len(defaults)} profiles from default_profiles.json")
        except Exception as e:
            logger.warning(f"Could not write seeded profiles to disk: {e}")
        return defaults

    # Bare fallback — no defaults available.
    return {
        "Kira (Default)": {
            "prompt": SYSTEM_PROMPT,
            "use_system": False,
        }
    }


def guardar_perfiles(perfiles: dict) -> None:
    try:
        with open(PROFILES_FILE, "w", encoding="utf-8") as f:
            json.dump(perfiles, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error guardando perfiles: {e}")
