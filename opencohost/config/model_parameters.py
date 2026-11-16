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
MIN_CONTEXT_TOKENS: int = 2048
MAX_CONTEXT_TOKENS: int = 131072
# "runtime_truth": no EXPLICIT per-model override is honored (legacy
#   behavior for that half of the policy). "requested": an explicit
#   per-model `context_tokens` IS honored on every load.
# T1 (model_context_auto_config_20260928, owner decision 2026-09-28): Auto
# (see AUTO_CONTEXT_TOKENS below) is NOT gated by this policy -- it applies
# regardless of "runtime_truth" vs "requested" whenever a model has no
# explicit override. `policy` therefore now only decides whether an
# EXPLICIT override is honored; Auto is the orthogonal default that fills
# the gap when there isn't one. See `_resolve_requested_ctx_plan` in
# `opencohost/core/engine/llm_engine_models.py` for where this plays out.
VALID_CONTEXT_POLICIES = frozenset({"runtime_truth", "requested"})

# T1 owner decision (2026-09-28):
# Auto mode cap for a model with no explicit `context_tokens` override.
# Auto = min(native_context, AUTO_CONTEXT_TOKENS), further clamped by the
# SAME VRAM machinery (ceiling + spill ratchet + NVML advisory step-down)
# that protects an explicit override -- never a parallel mechanism.
AUTO_CONTEXT_TOKENS: int = 16384


def resolve_auto_context_tokens(native_context: Optional[int]) -> int:
    """Return the Auto target for a model with no explicit override.

    ``min(native_context, AUTO_CONTEXT_TOKENS)``. An unknown/invalid
    ``native_context`` (None, non-positive, non-numeric) yields
    ``AUTO_CONTEXT_TOKENS`` unclamped -- the caller (the VRAM-clamp plan)
    still bounds the result by whatever native ceiling it separately knows.
    """
    try:
        native = int(native_context)
    except (TypeError, ValueError):
        return AUTO_CONTEXT_TOKENS
    if native <= 0:
        return AUTO_CONTEXT_TOKENS
    return min(native, AUTO_CONTEXT_TOKENS)

# T2 owner decision (2026-09-28):
# `budget_tokens: None` means Auto -- the adaptive budget engine
# (`resolve_generation_budget`) decides the generation budget at request
# time from calibrated throughput / latency targets, exactly like a
# just-installed model with no reasoning config at all. A concrete int is a
# REAL cap on that adaptive estimate (min(), never a floor or override) --
# see `resolve_generation_budget`'s "MANUAL_CAP_CLAMP" branch. Backward
# compatibility: an EXISTING config file that already has a numeric
# `budget_tokens` (written before this change, when it was "the" budget
# whenever calibrated latency clamping didn't apply) is preserved as-is on
# load and now simply behaves as a manual cap instead of being silently
# ignored once TPS calibrated -- that silent-ignore was the reported bug.
DEFAULT_MODEL_PARAMETERS: dict[str, Any] = {
    "version": SCHEMA_VERSION,
    "reasoning_enabled": False,  # Backward compatibility with v1
    "reasoning_budget_tokens": None,  # None = Auto (T2); see comment above
    "reasoning": {
        "enabled": False,
        "budget_tokens": None,  # None = Auto (T2); see comment above
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


def _sanitize_budget_or_auto(val: Any) -> Optional[int]:
    """Sanitize a manual budget cap while treating an explicit ``None`` as Auto.

    T2 (controls-cleanup-adaptive-reasoning-20260928): unlike `_sanitize_budget`
    (which always returns a concrete int, defaulting to DEFAULT_BUDGET_TOKENS),
    this is used everywhere `budget_tokens` now carries Auto-vs-manual-cap
    semantics: `None` passes through unchanged (Auto), a valid number is
    clamped to [MIN_BUDGET_TOKENS, MAX_BUDGET_TOKENS] (the manual cap), and a
    non-numeric/corrupt value (never an explicit `None`) still recovers to
    DEFAULT_BUDGET_TOKENS rather than silently becoming Auto -- corrupt data
    is not the same thing as an owner's explicit "no cap" choice.
    """
    if val is None:
        return None
    try:
        num = int(float(val))
    except (TypeError, ValueError):
        return DEFAULT_BUDGET_TOKENS
    return max(MIN_BUDGET_TOKENS, min(MAX_BUDGET_TOKENS, num))


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


def _sanitize_context_tokens(val: Any) -> Optional[int]:
    """Clamp a per-model context request; None when absent or not a number."""
    if val is None or isinstance(val, bool):
        return None
    try:
        num = int(float(val))
    except (TypeError, ValueError):
        return None
    return max(MIN_CONTEXT_TOKENS, min(MAX_CONTEXT_TOKENS, num))


def _sanitize_context_policy(val: Any) -> str:
    clean = str(val or "runtime_truth").strip().lower()
    return clean if clean in VALID_CONTEXT_POLICIES else "runtime_truth"


def _migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """Migrate legacy Schema v1 or incomplete config to Schema v2."""
    cfg = copy.deepcopy(DEFAULT_MODEL_PARAMETERS)

    # 1. Resolve root reasoning
    # T2: the fallback for an ABSENT budget_tokens key is now None (Auto),
    # not DEFAULT_BUDGET_TOKENS -- a key that IS present (even explicitly
    # null) always wins via `_sanitize_budget_or_auto` regardless of this
    # fallback; only a truly missing key reaches it.
    if "reasoning" in data and isinstance(data["reasoning"], dict):
        r_block = data["reasoning"]
        r_enabled = _sanitize_enabled(r_block.get("enabled", False))
        r_budget = _sanitize_budget_or_auto(r_block.get("budget_tokens", None))
        r_preset = _sanitize_preset(r_block.get("preset", "balanced"))
    else:
        r_enabled = _sanitize_enabled(data.get("reasoning_enabled", False))
        r_budget = _sanitize_budget_or_auto(data.get("reasoning_budget_tokens", None))
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
    cfg["context"]["policy"] = _sanitize_context_policy(cfg["context"].get("policy"))

    # 3. Resolve models overrides
    if "models" in data and isinstance(data["models"], dict):
        migrated_models = {}
        for m_id, m_cfg in data["models"].items():
            if not isinstance(m_cfg, dict):
                continue
            entry = {
                "enabled": _sanitize_enabled(m_cfg.get("enabled", r_enabled)),
                "budget_tokens": _sanitize_budget_or_auto(m_cfg.get("budget_tokens", r_budget)),
            }
            if "preset" in m_cfg:
                entry["preset"] = _sanitize_preset(m_cfg["preset"])
            ctx_tokens = _sanitize_context_tokens(m_cfg.get("context_tokens"))
            if ctx_tokens is not None:
                entry["context_tokens"] = ctx_tokens
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
        cfg["reasoning_budget_tokens"] = r_block.get("budget_tokens", cfg.get("reasoning_budget_tokens"))
    elif "reasoning_enabled" in cfg:
        cfg["reasoning"] = {
            "enabled": bool(cfg.get("reasoning_enabled", False)),
            "budget_tokens": _sanitize_budget_or_auto(cfg.get("reasoning_budget_tokens")),
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
        dict with keys: "enabled" (bool), "budget_tokens" (int or None -- None
        means Auto, T2 controls-cleanup-adaptive-reasoning-20260928, the
        adaptive budget engine decides at generation time; a number is a
        REAL manual cap on that adaptive estimate)
        and optionally "preset" (str) if include_preset=True or preset exists.
    """
    cfg = config_dict if config_dict is not None else load_model_parameters(config_file)
    r_block = cfg.get("reasoning", {})
    default_enabled = bool(r_block.get("enabled", cfg.get("reasoning_enabled", False)))
    default_budget = _sanitize_budget_or_auto(r_block.get("budget_tokens", cfg.get("reasoning_budget_tokens")))
    default_preset = str(r_block.get("preset", "balanced"))

    if model:
        models = cfg.get("models")
        if isinstance(models, dict) and model in models and isinstance(models[model], dict):
            model_entry = models[model]
            res = {
                "enabled": bool(model_entry.get("enabled", default_enabled)),
                "budget_tokens": _sanitize_budget_or_auto(model_entry.get("budget_tokens", default_budget)),
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


def get_model_context_settings(
    model: Optional[str] = None,
    config_file: Optional[str] = None,
    config_dict: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Get the context policy and the context size requested for a model.

    Returns:
        dict with keys: "policy" (str) and "requested_tokens" (int or None).
        A per-model `context_tokens` only counts under policy "requested";
        None means the caller keeps its own default (runtime truth / fallback).
    """
    cfg = config_dict if config_dict is not None else load_model_parameters(config_file)
    c_block = cfg.get("context")
    policy = _sanitize_context_policy(
        c_block.get("policy") if isinstance(c_block, dict) else None
    )
    requested: Optional[int] = None
    if policy == "requested" and model:
        models = cfg.get("models")
        entry = models.get(model) if isinstance(models, dict) else None
        if isinstance(entry, dict):
            requested = _sanitize_context_tokens(entry.get("context_tokens"))
    return {"policy": policy, "requested_tokens": requested}


def update_model_reasoning_settings(
    enabled: Optional[bool] = None,
    budget_tokens: Optional[int] = None,
    preset: Optional[str] = None,
    model: Optional[str] = None,
    config_file: Optional[str] = None,
    clear_budget: bool = False,
) -> dict[str, Any]:
    """Update reasoning settings (globally or for a specific model) and persist.

    T2 (controls-cleanup-adaptive-reasoning-20260928): ``budget_tokens=None``
    (the default) means "leave the current value untouched" -- the
    pre-existing partial-update convention shared with ``enabled``/``preset``.
    ``clear_budget=True`` is a SEPARATE, explicit signal that resets
    budget_tokens to ``None`` (Auto), independent of the ``budget_tokens``
    argument. This distinguishes an HTTP PUT body that OMITS
    ``budget_tokens`` entirely (leave unchanged) from one that explicitly
    sends ``budget_tokens: null`` (clear to Auto) -- the router resolves
    which one applies from ``UpdateModelReasoningRequest``'s
    ``model_fields_set`` before calling in here (see
    ``opencohost/api/routers/status.py::update_model_reasoning``).
    """
    with _config_lock:
        cfg = load_model_parameters(config_file)
        if model:
            if "models" not in cfg or not isinstance(cfg["models"], dict):
                cfg["models"] = {}
            current = get_model_reasoning_settings(model, config_file, config_dict=cfg)
            override = dict(cfg["models"].get(model, current))
            if enabled is not None:
                override["enabled"] = _sanitize_enabled(enabled)
            if clear_budget:
                override["budget_tokens"] = None
            elif budget_tokens is not None:
                override["budget_tokens"] = _sanitize_budget_or_auto(budget_tokens)
            if preset is not None:
                override["preset"] = _sanitize_preset(preset)
            cfg["models"][model] = override
            save_model_parameters(cfg, config_file)
            ret = {
                "enabled": bool(override["enabled"]),
                "budget_tokens": override.get("budget_tokens"),
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
            if clear_budget:
                cfg["reasoning_budget_tokens"] = None
                r_block["budget_tokens"] = None
            elif budget_tokens is not None:
                clean_bd = _sanitize_budget_or_auto(budget_tokens)
                cfg["reasoning_budget_tokens"] = clean_bd
                r_block["budget_tokens"] = clean_bd
            if preset is not None:
                r_block["preset"] = _sanitize_preset(preset)
            save_model_parameters(cfg, config_file)
            ret = {
                "enabled": bool(r_block.get("enabled", cfg.get("reasoning_enabled", False))),
                "budget_tokens": r_block.get("budget_tokens", cfg.get("reasoning_budget_tokens")),
            }
            if preset is not None:
                ret["preset"] = r_block["preset"]
            return ret


def update_model_context_settings(
    model: str,
    context_tokens: Optional[int],
    config_file: Optional[str] = None,
) -> dict[str, Any]:
    """Persist a per-model context override, or clear it back to Auto.

    T2 (model_context_auto_config_20260928): mirrors
    ``update_model_reasoning_settings``'s writer pattern. ``context_tokens=None``
    removes the model's override (it falls back to Auto). A provided value is
    sanitized/clamped via ``_sanitize_context_tokens`` (silent clamp, same
    convention as the reasoning writer's ``_sanitize_budget`` -- never a hard
    validation error at this layer). Setting an explicit override also flips
    the global ``context.policy`` to "requested" when it is not already, so
    the override is honored immediately regardless of a stale hand-edited
    "runtime_truth" policy (Auto itself never needed this, see
    ``resolve_auto_context_tokens`` / ``VALID_CONTEXT_POLICIES`` docstring).
    Clearing to Auto (``context_tokens=None``) leaves ``policy`` untouched --
    Auto applies regardless of policy, so there is nothing to flip.

    Atomic save via ``save_model_parameters`` (``atomic_write_text``), same
    fail-safe persistence as the reasoning writer.

    Returns ``{"model": model, "context_tokens": int | None, "policy": str}``.
    """
    with _config_lock:
        cfg = load_model_parameters(config_file)
        if "models" not in cfg or not isinstance(cfg["models"], dict):
            cfg["models"] = {}
        entry = dict(cfg["models"].get(model, {}))
        clean = _sanitize_context_tokens(context_tokens)
        if clean is None:
            entry.pop("context_tokens", None)
        else:
            entry["context_tokens"] = clean
        cfg["models"][model] = entry

        c_block = cfg.setdefault("context", {})
        if clean is not None:
            c_block["policy"] = "requested"
        else:
            c_block["policy"] = _sanitize_context_policy(c_block.get("policy"))

        save_model_parameters(cfg, config_file)
        return {
            "model": model,
            "context_tokens": entry.get("context_tokens"),
            "policy": c_block["policy"],
        }
