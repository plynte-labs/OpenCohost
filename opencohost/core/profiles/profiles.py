import json
import os
import tempfile
import uuid
from pathlib import Path

from opencohost.config.settings import PROFILES_FILE, SYSTEM_PROMPT, PACKAGE_CONFIG_DIR
from opencohost.config.logger import get_logger

logger = get_logger()

# Shipped defaults — tracked in opencohost/config/, never modified at runtime.
DEFAULT_PROFILES_FILE = str(Path(PACKAGE_CONFIG_DIR) / "default_profiles.json")


def _ensure_stable_ids(perfiles: dict) -> bool:
    """Seed a stable uuid4 ``id`` into any profile missing one, in place.

    Also resolves id collisions deterministically: if two profiles share the
    same id (e.g. a bad hand-edit or import), the first occurrence (by dict
    order) keeps it and every later occurrence is re-seeded — ids must be
    unique per profile, never shared, never silently reused (R12/RC-9).

    Returns True if any profile was modified (caller should persist).
    """
    seen_ids: set[str] = set()
    modified = False
    for datos in perfiles.values():
        if not isinstance(datos, dict):
            continue
        pid = datos.get("id")
        if not isinstance(pid, str) or not pid or pid in seen_ids:
            datos["id"] = str(uuid.uuid4())
            modified = True
        seen_ids.add(datos["id"])
    return modified


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
                perfiles = json.load(f)
            if not isinstance(perfiles, dict):
                return perfiles
            if _ensure_stable_ids(perfiles):
                guardar_perfiles(perfiles)
            return perfiles
        except Exception as e:
            logger.error(f"Error cargando perfiles: {e}")

    # PROFILES_FILE does not exist — attempt to seed from shipped defaults.
    defaults = _load_default_profiles()
    if defaults:
        _ensure_stable_ids(defaults)
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
            "id": str(uuid.uuid4()),
            "prompt": SYSTEM_PROMPT,
            "use_system": False,
        }
    }


def guardar_perfiles(perfiles: dict) -> bool:
    """Write profiles to PROFILES_FILE atomically.

    Writes to a temp file in the same directory, then ``os.replace()``s it
    into place — a crash or failure mid-write can never leave PROFILES_FILE
    empty or partially written (R12/MF-1). Fail-open: never raises. On
    failure, logs one warning with the path + exception type only — never
    profile content (CLAUDE.md: never expose raw chat/profile content in
    logs) — and leaves the pre-existing file on disk untouched.

    Returns True when the write landed, False when it failed (the never-raise
    contract is preserved; callers that must know — the API handlers — check
    the bool and surface a 503 instead of a false 200).
    """
    target_dir = os.path.dirname(PROFILES_FILE) or "."
    tmp_path = None
    try:
        os.makedirs(target_dir, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=target_dir, prefix=".perfiles_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(perfiles, f, indent=4, ensure_ascii=False)
        os.replace(tmp_path, PROFILES_FILE)
        return True
    except Exception as e:
        logger.warning(f"Failed to save profiles to {PROFILES_FILE}: {type(e).__name__}")
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False
