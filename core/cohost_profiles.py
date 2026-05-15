"""Persistent, guarded profiles for Kira Co-host Agenda Mode."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from typing import Any

from config.settings import BASE_DIR
from config.logger import get_logger


logger = get_logger()
COHOST_PROFILES_FILE = os.path.join(BASE_DIR, "cohost_profiles.json")


DEFAULT_COHOST_PROFILES: dict[str, dict[str, Any]] = {
    "Natural": {
        "style": "Soná como co-host natural de stream: cercana, con humor seco, sin anunciar estructura ni despedirte entre ideas.",
        "default_priority": "normal",
        "default_response_length": "normal",
    },
    "Picante con matiz": {
        "style": "Podés tomar postura y ser picante, pero siempre con matiz. No conviertas una opinión polémica en verdad absoluta.",
        "default_priority": "normal",
        "default_response_length": "expandida",
    },
    "Docente entretenida": {
        "style": "Explicá con claridad y ejemplos cotidianos, sin sonar académica ni leer definiciones.",
        "default_priority": "normal",
        "default_response_length": "normal",
    },
}


def load_cohost_profiles() -> dict[str, dict[str, Any]]:
    if os.path.exists(COHOST_PROFILES_FILE):
        try:
            with open(COHOST_PROFILES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data:
                return {name: normalize_cohost_profile(profile) for name, profile in data.items()}
        except Exception as e:
            logger.error(f"Error cargando perfiles Co-host: {e}")
    return deepcopy(DEFAULT_COHOST_PROFILES)


def save_cohost_profiles(profiles: dict[str, dict[str, Any]]) -> None:
    safe = {sanitize_profile_name(name): normalize_cohost_profile(profile) for name, profile in profiles.items() if sanitize_profile_name(name)}
    try:
        with open(COHOST_PROFILES_FILE, "w", encoding="utf-8") as f:
            json.dump(safe or deepcopy(DEFAULT_COHOST_PROFILES), f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error guardando perfiles Co-host: {e}")


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
