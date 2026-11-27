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
import os
from typing import Optional

from opencohost.config.settings import LLM_PROVIDER_CONFIG_FILE
from opencohost.config.storage import atomic_write_text

DEFAULT_FALLBACK_MODE = "auto"

_ENV_TRUE = frozenset({"1", "true", "yes", "on"})
_ENV_FALSE = frozenset({"0", "false", "no", "off"})


def _coerce_bool(val, *, default: bool = False) -> bool:
    """Strictly coerce JSON/config booleans without letting 'false' or '0' turn True."""
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        norm = val.strip().lower()
        if norm in _ENV_TRUE:
            return True
        if norm in _ENV_FALSE or norm == "":
            return False
    return default


def resolve_cloud_promotion_env_override() -> Optional[bool]:
    """Return explicit boolean env override for ``cloud_promotion_enabled``, or None."""
    raw = os.environ.get("OPENCOHOST_CLOUD_PROMOTION_ENABLED", "").strip().lower()
    if raw in _ENV_TRUE:
        return True
    if raw in _ENV_FALSE:
        return False
    return None


def is_cloud_promotion_enabled(cfg: Optional[dict] = None) -> bool:
    """Effective runtime opt-in state (env override beats persisted config)."""
    env_override = resolve_cloud_promotion_env_override()
    if env_override is not None:
        return env_override
    if not isinstance(cfg, dict):
        return False
    return _coerce_bool(cfg.get("cloud_promotion_enabled", False))


def _default_config() -> dict:
    return {
        "active_provider": "local",
        "fallback_mode": DEFAULT_FALLBACK_MODE,
        "pregen_enabled": False,
        "cloud_promotion_enabled": False,
        # T4 (live_session_fixes_20260925): "discard stale speech when a
        # regenerated reply arrives" -- default OFF, today's behavior
        # byte-identical. See MotorVocalIA._maybe_discard_stale_fallback_notice.
        "discard_stale_speech_on_regenerated_reply": False,
        "profiles": {},
    }


def load_provider_config(config_file: Optional[str] = None) -> dict:
    """Load the persisted provider config, defaulting to local-only.

    Absent, unreadable, or corrupt file all resolve to a fresh default dict
    (never raises) — a missing/broken config is byte-identical to the
    pre-track local-only behavior. Environment overrides remain runtime-only
    (via :func:`is_cloud_promotion_enabled`) so PUT /api/llm/provider never
    leaks an env override into persisted disk state.
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
    cfg["pregen_enabled"] = _coerce_bool(data.get("pregen_enabled", False))
    cfg["cloud_promotion_enabled"] = _coerce_bool(data.get("cloud_promotion_enabled", False))
    cfg["discard_stale_speech_on_regenerated_reply"] = _coerce_bool(
        data.get("discard_stale_speech_on_regenerated_reply", False)
    )
    profiles = data.get("profiles")
    if isinstance(profiles, dict):
        # Drop non-dict entries so a hand-corrupted file degrades instead of 500ing.
        cfg["profiles"] = {k: v for k, v in profiles.items() if isinstance(v, dict)}
    return cfg


def save_provider_config(cfg: dict, config_file: Optional[str] = None) -> None:
    """Persist *cfg* atomically (temp file + os.replace). Non-secret fields only."""
    path = config_file if config_file is not None else LLM_PROVIDER_CONFIG_FILE
    atomic_write_text(path, json.dumps(cfg, indent=2))
