"""Provider config persistence for the multi-provider LLM engine.

Design: multi_provider_llm_20260723 Phase 1 ('Provider Config Surface').
Persists one JSON file (``config/llm_provider.json``) holding operator-
posture GLOBAL fields (``active_provider``, ``fallback_mode``,
``pregen_enabled``) plus a per-provider ``profiles`` map (``base_url``,
``model``, ``preset`` — never a key). API keys live in a SEPARATE store
(``config/llm_keys.json`` via ``opencohost.stream_admin.oauth_store.OAuthStore``,
one key per profile id) so this file stays safe to log/inspect.

Absent, unreadable, or corrupt file all resolve to the local-only default —
the byte-identical local-path guarantee (spec B) never depends on this file
existing.
"""

import json
from typing import Optional

from opencohost.config.settings import LLM_PROVIDER_CONFIG_FILE
from opencohost.config.storage import atomic_write_text

DEFAULT_FALLBACK_MODE = "auto"


def _default_config() -> dict:
    return {
        "active_provider": "local",
        "fallback_mode": DEFAULT_FALLBACK_MODE,
        "pregen_enabled": False,
        "profiles": {},
    }


def load_provider_config(config_file: Optional[str] = None) -> dict:
    """Load the persisted provider config, defaulting to local-only.

    Absent, unreadable, or corrupt file all resolve to a fresh default dict
    (never raises) — a missing/broken config is byte-identical to the
    pre-track local-only behavior.
    """
    path = config_file if config_file is not None else LLM_PROVIDER_CONFIG_FILE
    cfg = _default_config()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return cfg
    if not isinstance(data, dict):
        return cfg
    cfg["active_provider"] = str(data.get("active_provider") or "local")
    cfg["fallback_mode"] = data.get("fallback_mode") or DEFAULT_FALLBACK_MODE
    cfg["pregen_enabled"] = bool(data.get("pregen_enabled", False))
    profiles = data.get("profiles")
    if isinstance(profiles, dict):
        # Drop non-dict entries so a hand-corrupted file degrades instead of 500ing.
        cfg["profiles"] = {k: v for k, v in profiles.items() if isinstance(v, dict)}
    return cfg


def save_provider_config(cfg: dict, config_file: Optional[str] = None) -> None:
    """Persist *cfg* atomically (temp file + os.replace). Non-secret fields only."""
    path = config_file if config_file is not None else LLM_PROVIDER_CONFIG_FILE
    atomic_write_text(path, json.dumps(cfg, indent=2))
