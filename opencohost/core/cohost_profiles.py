"""Persistent, guarded profiles for Kira Co-host Agenda Mode."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any

from opencohost.config.settings import COHOST_PROFILES_FILE
from opencohost.config.storage import atomic_write_text
from opencohost.config.logger import get_logger


logger = get_logger()


DEFAULT_COHOST_PROFILES: dict[str, dict[str, Any]] = {
    "Natural": {
        "style": "Soná como co-host natural de stream: cercana, con humor seco. Acompañá sin robar protagonismo. Nunca te quejes del chat ni digas que está aburrido. Si no hay nada que decir, hacé una pausa natural.",
        "default_priority": "normal",
        "default_response_length": "normal",
    },
    "Picante con matiz": {
        "style": "Podés tomar postura y ser picante, pero siempre con matiz y sin desprecio. No conviertas una opinión polémica en verdad absoluta. NUNCA insultes al chat ni digas que es básico o que deberían leer un manual.",
        "default_priority": "normal",
        "default_response_length": "expandida",
    },
    "Docente entretenida": {
        "style": "Explicá con claridad y ejemplos cotidianos, sin sonar académica ni leer definiciones. NUNCA menosprecies a quien pregunta algo básico. Si no sabés algo, decilo con honestidad.",
        "default_priority": "normal",
        "default_response_length": "normal",
    },
}


def load_cohost_profiles() -> dict[str, dict[str, Any]]:
    if os.path.exists(COHOST_PROFILES_FILE):
        try:
            with open(COHOST_PROFILES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError:
            # Corrupt file: quarantine instead of silently returning defaults,
            # so a crash-truncated file is preserved and not overwritten blind.
            corrupt = str(COHOST_PROFILES_FILE) + ".corrupt"
            try:
                os.replace(COHOST_PROFILES_FILE, corrupt)
            except OSError:
                pass
            logger.warning("cohost profiles corrupt — quarantined to %s", corrupt)
            return deepcopy(DEFAULT_COHOST_PROFILES)
        except Exception as e:
            logger.error(f"Error cargando perfiles Co-host: {e}")
            return deepcopy(DEFAULT_COHOST_PROFILES)
        if isinstance(data, dict) and data:
            return {name: normalize_cohost_profile(profile) for name, profile in data.items()}
    return deepcopy(DEFAULT_COHOST_PROFILES)


def save_cohost_profiles(profiles: dict[str, dict[str, Any]]) -> bool:
    """Persist co-host profiles atomically. Never raises (fail-open + log).

    Returns True when the write landed, False when it failed — the API
    handler checks the bool and surfaces a 503 instead of a false 200.
    """
    safe = {sanitize_profile_name(name): normalize_cohost_profile(profile) for name, profile in profiles.items() if sanitize_profile_name(name)}
    try:
        atomic_write_text(
            Path(COHOST_PROFILES_FILE),
            json.dumps(safe or deepcopy(DEFAULT_COHOST_PROFILES), indent=4, ensure_ascii=False),
        )
        return True
    except Exception as e:
        logger.error(f"Error guardando perfiles Co-host: {type(e).__name__}")
        return False


def sanitize_profile_name(name: str) -> str:
    clean = " ".join((name or "").split())
    if len(clean) > 40:
        clean = clean[:40].strip()
    return clean


def normalize_cohost_profile(profile: dict[str, Any]) -> dict[str, Any]:
    style = " ".join(str((profile or {}).get("style", "")).replace("\n", " ").split())
    if len(style) > 600:
        style = style[:600].strip()
    return {
        "style": style or DEFAULT_COHOST_PROFILES["Natural"]["style"],
        "default_priority": str((profile or {}).get("default_priority", "normal")).lower(),
        "default_response_length": str((profile or {}).get("default_response_length", "normal")).lower(),
    }
