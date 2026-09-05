"""Model parameters configuration and persistence (ADR-056 WU4).

Schema v2 separating reasoning, generation, and context policies
with automatic migration from Schema v1 and strict semantic validation.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Optional

from opencohost.config.storage import USER_DATA_DIR, atomic_write_text

logger = logging.getLogger("OpenCohost")

MODEL_PARAMETERS_CONFIG_FILE = os.path.join(
    str(USER_DATA_DIR), "config", "model_parameters.json"
)

SCHEMA_VERSION: int = 2
MIN_BUDGET_TOKENS: int = 32
MAX_BUDGET_TOKENS: int = 8192
DEFAULT_BUDGET_TOKENS: int = 512
VALID_PRESETS = frozenset({"fast", "balanced", "quality", "custom"})

DEFAULT_MODEL_PARAMETERS: dict[str, Any] = {
    "version": SCHEMA_VERSION,
    "reasoning_enabled": False,  # Backward compatibility with v1
    "reasoning_budget_tokens": DEFAULT_BUDGET_TOKENS,  # Backward compatibility with v1
    "reasoning": {
        "enabled": False,
        "budget_tokens": DEFAULT_BUDGET_TOKENS,
        "preset": "balanced",
    },
    "generation": {
        "default_intent": "chat",
        "max_tokens": DEFAULT_BUDGET_TOKENS,
    },
    "context": {
        "policy": "runtime_truth",
    },
    "models": {},
}

_config_lock = threading.Lock()


def _sanitize_budget(val: Any) -> int:
    """Sanitize and clamp budget tokens against negative numbers or string numbers."""
    if val is None:
        return DEFAULT_BUDGET_TOKENS
    try:
        num = int(float(val))
        return max(MIN_BUDGET_TOKENS, min(MAX_BUDGET_TOKENS, num))
    except (TypeError, ValueError):
        return DEFAULT_BUDGET_TOKENS


def _sanitize_enabled(val: Any) -> bool:
    """Sanitize boolean enabled values."""
    if val is None:
        return False
    if isinstance(val, str):
        return val.strip().lower() in ("true", "1", "yes", "on")
    return bool(val)


def _sanitize_preset(val: Any) -> str:
    """Sanitize preset string."""
    clean = str(val or "balanced").strip().lower()
    return clean if clean in VALID_PRESETS else "balanced"


def _migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """Migrate legacy Schema v1 or incomplete config to Schema v2."""
    cfg = copy.deepcopy(DEFAULT_MODEL_PARAMETERS)

    # 1. Resolve root reasoning
    if "reasoning" in data and isinstance(data["reasoning"], dict):
        r_block = data["reasoning"]
        r_enabled = _sanitize_enabled(r_block.get("enabled", False))
        r_budget = _sanitize_budget(r_block.get("budget_tokens", DEFAULT_BUDGET_TOKENS))
        r_preset = _sanitize_preset(r_block.get("preset", "balanced"))
    else:
        r_enabled = _sanitize_enabled(data.get("reasoning_enabled", False))
        r_budget = _sanitize_budget(data.get("reasoning_budget_tokens", DEFAULT_BUDGET_TOKENS))
        r_preset = "balanced"

    cfg["reasoning_enabled"] = r_enabled
    cfg["reasoning_budget_tokens"] = r_budget
    cfg["reasoning"] = {
        "enabled": r_enabled,
        "budget_tokens": r_budget,
        "preset": r_preset,
    }

    # 2. Resolve generation & context
    if "generation" in data and isinstance(data["generation"], dict):
        cfg["generation"].update(data["generation"])
    if "context" in data and isinstance(data["context"], dict):
        cfg["context"].update(data["context"])

    # 3. Resolve models overrides
    if "models" in data and isinstance(data["models"], dict):
        migrated_models = {}
        for m_id, m_cfg in data["models"].items():
            if not isinstance(m_cfg, dict):
                continue
            entry = {
                "enabled": _sanitize_enabled(m_cfg.get("enabled", r_enabled)),
                "budget_tokens": _sanitize_budget(m_cfg.get("budget_tokens", r_budget)),
            }
            if "preset" in m_cfg:
                entry["preset"] = _sanitize_preset(m_cfg["preset"])
            migrated_models[m_id] = entry
        cfg["models"] = migrated_models

    return cfg


def _get_config_path(config_file: Optional[str] = None) -> Path:
    return Path(config_file or MODEL_PARAMETERS_CONFIG_FILE)


def load_model_parameters(config_file: Optional[str] = None) -> dict[str, Any]:
    """Load the model parameters configuration with automatic migration to Schema v2.

    Fails open to default parameters on missing file, read errors, or corrupt JSON.
    """
    path = _get_config_path(config_file)
    if not path.is_file():
        return copy.deepcopy(DEFAULT_MODEL_PARAMETERS)

    try:
        raw_text = path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
        if not isinstance(data, dict):
            logger.warning(
                "Model parameters file %s did not contain a JSON object; using defaults.",
                path,
            )
            return copy.deepcopy(DEFAULT_MODEL_PARAMETERS)

        return _migrate_v1_to_v2(data)
    except Exception as exc:
        logger.warning(
            "Failed to load model parameters from %s (%s); using defaults.",
            path,
            exc,
        )
        return copy.deepcopy(DEFAULT_MODEL_PARAMETERS)


def save_model_parameters(cfg: dict[str, Any], config_file: Optional[str] = None) -> None:
    """Persist the model parameters atomically."""
    path = _get_config_path(config_file)
    # Ensure synchronization between v1 root fields and v2 reasoning dict
    r_block = cfg.get("reasoning", {})
    if isinstance(r_block, dict):
        cfg["reasoning_enabled"] = r_block.get("enabled", cfg.get("reasoning_enabled", False))
        cfg["reasoning_budget_tokens"] = r_block.get("budget_tokens", cfg.get("reasoning_budget_tokens", DEFAULT_BUDGET_TOKENS))
    elif "reasoning_enabled" in cfg:
        cfg["reasoning"] = {
            "enabled": bool(cfg.get("reasoning_enabled", False)),
            "budget_tokens": int(cfg.get("reasoning_budget_tokens", DEFAULT_BUDGET_TOKENS)),
            "preset": "balanced",
        }
    cfg["version"] = SCHEMA_VERSION
    serialized = json.dumps(cfg, indent=2, ensure_ascii=False)
    atomic_write_text(path, serialized)


def get_model_reasoning_settings(
    model: Optional[str] = None,
    config_file: Optional[str] = None,
    config_dict: Optional[dict[str, Any]] = None,
    include_preset: bool = False,
) -> dict[str, Any]:
    """Get the effective reasoning settings for a model or globally.

    Returns:
        dict with keys: "enabled" (bool), "budget_tokens" (int)
        and optionally "preset" (str) if include_preset=True or preset exists.
    """
    cfg = config_dict if config_dict is not None else load_model_parameters(config_file)
    r_block = cfg.get("reasoning", {})
    default_enabled = bool(r_block.get("enabled", cfg.get("reasoning_enabled", False)))
    default_budget = int(r_block.get("budget_tokens", cfg.get("reasoning_budget_tokens", DEFAULT_BUDGET_TOKENS)))
    default_preset = str(r_block.get("preset", "balanced"))

    if model:
        models = cfg.get("models")
        if isinstance(models, dict) and model in models and isinstance(models[model], dict):
            model_entry = models[model]
            res = {
                "enabled": bool(model_entry.get("enabled", default_enabled)),
                "budget_tokens": int(model_entry.get("budget_tokens", default_budget)),
            }
            if include_preset or "preset" in model_entry:
                res["preset"] = str(model_entry.get("preset", default_preset))
            return res

    res = {
        "enabled": default_enabled,
        "budget_tokens": default_budget,
    }
    if include_preset:
        res["preset"] = default_preset
    return res


def update_model_reasoning_settings(
    enabled: Optional[bool] = None,
    budget_tokens: Optional[int] = None,
    preset: Optional[str] = None,
    model: Optional[str] = None,
    config_file: Optional[str] = None,
) -> dict[str, Any]:
    """Update reasoning settings (globally or for a specific model) and persist."""
    with _config_lock:
        cfg = load_model_parameters(config_file)
        if model:
            if "models" not in cfg or not isinstance(cfg["models"], dict):
                cfg["models"] = {}
            current = get_model_reasoning_settings(model, config_file, config_dict=cfg)
            override = dict(cfg["models"].get(model, current))
            if enabled is not None:
                override["enabled"] = _sanitize_enabled(enabled)
            if budget_tokens is not None:
                override["budget_tokens"] = _sanitize_budget(budget_tokens)
            if preset is not None:
                override["preset"] = _sanitize_preset(preset)
            cfg["models"][model] = override
            save_model_parameters(cfg, config_file)
            ret = {
                "enabled": bool(override["enabled"]),
                "budget_tokens": int(override["budget_tokens"]),
            }
            if preset is not None or "preset" in override:
                if preset is not None:
                    ret["preset"] = override["preset"]
            return ret
        else:
            r_block = cfg.setdefault("reasoning", {})
            if enabled is not None:
                clean_en = _sanitize_enabled(enabled)
                cfg["reasoning_enabled"] = clean_en
                r_block["enabled"] = clean_en
            if budget_tokens is not None:
                clean_bd = _sanitize_budget(budget_tokens)
                cfg["reasoning_budget_tokens"] = clean_bd
                r_block["budget_tokens"] = clean_bd
            if preset is not None:
                r_block["preset"] = _sanitize_preset(preset)
            save_model_parameters(cfg, config_file)
            ret = {
                "enabled": bool(r_block.get("enabled", cfg.get("reasoning_enabled", False))),
                "budget_tokens": int(r_block.get("budget_tokens", cfg.get("reasoning_budget_tokens", DEFAULT_BUDGET_TOKENS))),
            }
            if preset is not None:
                ret["preset"] = r_block["preset"]
            return ret
