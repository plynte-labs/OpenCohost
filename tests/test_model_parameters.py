"""Tests for opencohost.config.model_parameters."""

import json
from pathlib import Path

from opencohost.config.model_parameters import (
    AUTO_CONTEXT_TOKENS,
    DEFAULT_MODEL_PARAMETERS,
    get_model_context_settings,
    get_model_reasoning_settings,
    load_model_parameters,
    resolve_auto_context_tokens,
    save_model_parameters,
    update_model_context_settings,
    update_model_reasoning_settings,
)


def test_load_defaults_when_file_missing(tmp_path: Path):
    cfg_file = str(tmp_path / "non_existent.json")
    cfg = load_model_parameters(cfg_file)
    assert cfg["reasoning_enabled"] is False
    # T2 (controls-cleanup-adaptive-reasoning-20260928): a fresh/unconfigured
    # install is Auto by default (None), not the historical literal 512 --
    # the adaptive budget engine decides at generation time. An EXISTING
    # config file with an explicit numeric value still preserves it (see
    # test_get_reasoning_settings_root_and_override below), now interpreted
    # as a manual cap rather than "the" budget.
    assert cfg["reasoning_budget_tokens"] is None
    assert cfg["reasoning"]["budget_tokens"] is None
    assert cfg["models"] == {}


def test_load_defaults_when_file_corrupt(tmp_path: Path):
    cfg_file = tmp_path / "corrupt.json"
    cfg_file.write_text("NOT VALID JSON! {", encoding="utf-8")
    cfg = load_model_parameters(str(cfg_file))
    assert cfg == DEFAULT_MODEL_PARAMETERS


def test_load_defaults_when_not_dict(tmp_path: Path):
    cfg_file = tmp_path / "array.json"
    cfg_file.write_text("[1, 2, 3]", encoding="utf-8")
    cfg = load_model_parameters(str(cfg_file))
    assert cfg == DEFAULT_MODEL_PARAMETERS


def test_save_and_load_roundtrip(tmp_path: Path):
    cfg_file = str(tmp_path / "model_params.json")
    payload = {
        "reasoning_enabled": True,
        "reasoning_budget_tokens": 1024,
        "models": {
            "gemma4:e4b": {"enabled": False, "budget_tokens": 256},
        },
    }
    save_model_parameters(payload, config_file=cfg_file)
    loaded = load_model_parameters(config_file=cfg_file)
    assert loaded["reasoning_enabled"] is True
    assert loaded["reasoning_budget_tokens"] == 1024
    assert loaded["models"]["gemma4:e4b"] == {"enabled": False, "budget_tokens": 256}


def test_get_reasoning_settings_root_and_override(tmp_path: Path):
    cfg_file = str(tmp_path / "model_params.json")
    save_model_parameters(
        {
            "reasoning_enabled": False,
            "reasoning_budget_tokens": 512,
            "models": {
                "qwen3:1.7b": {"enabled": True, "budget_tokens": 1024},
            },
        },
        config_file=cfg_file,
    )

    # Root / unconfigured model returns root defaults
    settings_root = get_model_reasoning_settings(None, config_file=cfg_file)
    assert settings_root == {"enabled": False, "budget_tokens": 512}

    settings_llama = get_model_reasoning_settings("llama3", config_file=cfg_file)
    assert settings_llama == {"enabled": False, "budget_tokens": 512}

    # Model with override returns its own settings
    settings_qwen = get_model_reasoning_settings("qwen3:1.7b", config_file=cfg_file)
    assert settings_qwen == {"enabled": True, "budget_tokens": 1024}


def test_update_reasoning_settings_root(tmp_path: Path):
    cfg_file = str(tmp_path / "model_params.json")

    res = update_model_reasoning_settings(
        enabled=True, budget_tokens=2048, config_file=cfg_file
    )
    assert res == {"enabled": True, "budget_tokens": 2048}

    loaded = load_model_parameters(config_file=cfg_file)
    assert loaded["reasoning_enabled"] is True
    assert loaded["reasoning_budget_tokens"] == 2048


def test_update_reasoning_settings_model_override(tmp_path: Path):
    cfg_file = str(tmp_path / "model_params.json")

    # Update for specific model
    res = update_model_reasoning_settings(
        enabled=True, budget_tokens=1024, model="gemma4:e4b", config_file=cfg_file
    )
    assert res == {"enabled": True, "budget_tokens": 1024}

    # Verify model override persisted without altering root defaults
    loaded = load_model_parameters(config_file=cfg_file)
    assert loaded["reasoning_enabled"] is False  # root unchanged
    assert loaded["models"]["gemma4:e4b"] == {"enabled": True, "budget_tokens": 1024}

    # Update partial (only budget_tokens)
    res2 = update_model_reasoning_settings(
        budget_tokens=256, model="gemma4:e4b", config_file=cfg_file
    )
    assert res2 == {"enabled": True, "budget_tokens": 256}

    # Verify getter reflects updated override
    q = get_model_reasoning_settings("gemma4:e4b", config_file=cfg_file)
    assert q == {"enabled": True, "budget_tokens": 256}


def test_get_model_reasoning_settings_defaults_to_auto_when_unconfigured(tmp_path: Path):
    """T2 (controls-cleanup-adaptive-reasoning-20260928): a truly fresh config
    (no file on disk) reports budget_tokens=None (Auto) both globally and for
    any model with no per-model override -- the adaptive engine decides at
    generation time; there is no more hardcoded 512 default."""
    cfg_file = str(tmp_path / "non_existent.json")
    assert get_model_reasoning_settings(None, config_file=cfg_file) == {
        "enabled": False,
        "budget_tokens": None,
    }
    assert get_model_reasoning_settings("gemma4:e4b", config_file=cfg_file) == {
        "enabled": False,
        "budget_tokens": None,
    }


def test_update_reasoning_settings_root_clear_budget_returns_to_auto(tmp_path: Path):
    cfg_file = str(tmp_path / "model_params.json")

    update_model_reasoning_settings(enabled=True, budget_tokens=2048, config_file=cfg_file)
    res = update_model_reasoning_settings(clear_budget=True, config_file=cfg_file)
    assert res == {"enabled": True, "budget_tokens": None}

    loaded = load_model_parameters(config_file=cfg_file)
    assert loaded["reasoning_budget_tokens"] is None
    assert loaded["reasoning"]["budget_tokens"] is None
    # enabled, set separately, survives the budget clear untouched
    assert loaded["reasoning_enabled"] is True


def test_update_reasoning_settings_model_clear_budget_returns_to_auto(tmp_path: Path):
    cfg_file = str(tmp_path / "model_params.json")

    update_model_reasoning_settings(budget_tokens=1024, model="gemma4:e4b", config_file=cfg_file)
    res = update_model_reasoning_settings(clear_budget=True, model="gemma4:e4b", config_file=cfg_file)
    assert res == {"enabled": False, "budget_tokens": None}

    q = get_model_reasoning_settings("gemma4:e4b", config_file=cfg_file)
    assert q == {"enabled": False, "budget_tokens": None}


def test_schema_v1_to_v2_migration(tmp_path: Path):
    """ADR-056 WU4: Legacy v1 files automatically migrate to Schema v2 on load."""
    cfg_file = tmp_path / "legacy_v1.json"
    # Legacy v1 format without version and without nested reasoning/generation/context
    legacy_json = {
        "reasoning_enabled": True,
        "reasoning_budget_tokens": 1024,
        "models": {
            "qwen3:1.7b": {"enabled": False, "budget_tokens": 256}
        }
    }
    cfg_file.write_text(json.dumps(legacy_json), encoding="utf-8")

    loaded = load_model_parameters(str(cfg_file))
    assert loaded["version"] == 2
    assert loaded["reasoning"]["enabled"] is True
    assert loaded["reasoning"]["budget_tokens"] == 1024
    assert loaded["reasoning"]["preset"] == "balanced"
    assert loaded["generation"]["default_intent"] == "chat"
    assert loaded["context"]["policy"] == "runtime_truth"
    assert loaded["models"]["qwen3:1.7b"]["enabled"] is False
    assert loaded["models"]["qwen3:1.7b"]["budget_tokens"] == 256


def test_semantic_validation_clamping(tmp_path: Path):
    """ADR-056 WU4: Semantic validation prevents strings/negatives/invalid presets from crashing."""
    cfg_file = tmp_path / "bad_types.json"
    bad_json = {
        "reasoning_enabled": "true",
        "reasoning_budget_tokens": "-500",  # negative string number
        "reasoning": {
            "enabled": "1",
            "budget_tokens": "999999",  # exceeds max
            "preset": "invalid_preset",
        },
        "models": {
            "m1": {"enabled": "yes", "budget_tokens": "not_a_number"}
        }
    }
    cfg_file.write_text(json.dumps(bad_json), encoding="utf-8")

    loaded = load_model_parameters(str(cfg_file))
    # Enabled parsed as boolean
    assert loaded["reasoning"]["enabled"] is True
    # Budget clamped to max 8192
    assert loaded["reasoning"]["budget_tokens"] == 8192
    # Preset sanitized to balanced
    assert loaded["reasoning"]["preset"] == "balanced"
    # Model with invalid budget falls back cleanly to 512, never crashes
    assert loaded["models"]["m1"]["budget_tokens"] == 512
    assert loaded["models"]["m1"]["enabled"] is True



def test_context_tokens_survive_load_and_clamp(tmp_path: Path):
    cfg_file = tmp_path / "model_params.json"
    cfg_file.write_text(json.dumps({
        "version": 2,
        "context": {"policy": "requested"},
        "models": {
            "gemma4:e4b": {"enabled": True, "budget_tokens": 2048, "context_tokens": 16384},
            "tiny": {"context_tokens": 100},
            "huge": {"context_tokens": 10_000_000},
            "junk": {"context_tokens": "abc"},
        },
    }), encoding="utf-8")
    cfg = load_model_parameters(str(cfg_file))
    assert cfg["context"]["policy"] == "requested"
    assert cfg["models"]["gemma4:e4b"]["context_tokens"] == 16384
    assert cfg["models"]["tiny"]["context_tokens"] == 2048
    assert cfg["models"]["huge"]["context_tokens"] == 131072
    assert "context_tokens" not in cfg["models"]["junk"]


def test_context_policy_invalid_falls_back_to_runtime_truth(tmp_path: Path):
    cfg_file = tmp_path / "model_params.json"
    cfg_file.write_text(json.dumps({"context": {"policy": "whatever"}}), encoding="utf-8")
    cfg = load_model_parameters(str(cfg_file))
    assert cfg["context"]["policy"] == "runtime_truth"


def test_context_tokens_roundtrip_through_save(tmp_path: Path):
    cfg_file = str(tmp_path / "model_params.json")
    cfg = load_model_parameters(cfg_file)
    cfg["context"]["policy"] = "requested"
    cfg["models"]["gemma4:e4b"] = {"enabled": False, "budget_tokens": 512, "context_tokens": 16384}
    save_model_parameters(cfg, cfg_file)
    again = load_model_parameters(cfg_file)
    assert again["models"]["gemma4:e4b"]["context_tokens"] == 16384


def test_get_model_context_settings(tmp_path: Path):
    cfg_file = str(tmp_path / "model_params.json")
    # Unset config: runtime truth, nothing requested.
    assert get_model_context_settings("gemma4:e4b", cfg_file) == {
        "policy": "runtime_truth", "requested_tokens": None,
    }
    cfg = load_model_parameters(cfg_file)
    cfg["models"]["gemma4:e4b"] = {"enabled": False, "budget_tokens": 512, "context_tokens": 16384}
    save_model_parameters(cfg, cfg_file)
    # context_tokens is only honored under policy "requested".
    assert get_model_context_settings("gemma4:e4b", cfg_file)["requested_tokens"] is None
    cfg["context"]["policy"] = "requested"
    save_model_parameters(cfg, cfg_file)
    assert get_model_context_settings("gemma4:e4b", cfg_file) == {
        "policy": "requested", "requested_tokens": 16384,
    }
    assert get_model_context_settings("other", cfg_file)["requested_tokens"] is None
    assert get_model_context_settings(None, cfg_file)["requested_tokens"] is None


# ---------------------------------------------------------------------------
# T1/T2 (model_context_auto_config_20260928): Auto default + context writer.
# ---------------------------------------------------------------------------


def test_resolve_auto_context_tokens_caps_large_native():
    assert resolve_auto_context_tokens(262144) == AUTO_CONTEXT_TOKENS
    assert AUTO_CONTEXT_TOKENS == 16384


def test_resolve_auto_context_tokens_keeps_small_native():
    assert resolve_auto_context_tokens(8192) == 8192


def test_resolve_auto_context_tokens_unknown_native_falls_back_to_cap():
    assert resolve_auto_context_tokens(None) == AUTO_CONTEXT_TOKENS
    assert resolve_auto_context_tokens("garbage") == AUTO_CONTEXT_TOKENS
    assert resolve_auto_context_tokens(0) == AUTO_CONTEXT_TOKENS
    assert resolve_auto_context_tokens(-1) == AUTO_CONTEXT_TOKENS


def test_update_model_context_settings_roundtrip(tmp_path: Path):
    cfg_file = str(tmp_path / "model_params.json")

    res = update_model_context_settings("gemma4:e4b", 32768, config_file=cfg_file)
    assert res == {"model": "gemma4:e4b", "context_tokens": 32768, "policy": "requested"}

    loaded = load_model_parameters(cfg_file)
    assert loaded["models"]["gemma4:e4b"]["context_tokens"] == 32768
    assert loaded["context"]["policy"] == "requested"

    # get_model_context_settings reflects the write immediately.
    assert get_model_context_settings("gemma4:e4b", cfg_file) == {
        "policy": "requested", "requested_tokens": 32768,
    }


def test_update_model_context_settings_clamps_out_of_range(tmp_path: Path):
    cfg_file = str(tmp_path / "model_params.json")

    res = update_model_context_settings("tiny", 1, config_file=cfg_file)
    assert res["context_tokens"] == 2048  # MIN_CONTEXT_TOKENS

    res = update_model_context_settings("huge", 10_000_000, config_file=cfg_file)
    assert res["context_tokens"] == 131072  # MAX_CONTEXT_TOKENS


def test_update_model_context_settings_none_clears_back_to_auto(tmp_path: Path):
    cfg_file = str(tmp_path / "model_params.json")

    update_model_context_settings("gemma4:e4b", 32768, config_file=cfg_file)
    res = update_model_context_settings("gemma4:e4b", None, config_file=cfg_file)
    assert res == {"model": "gemma4:e4b", "context_tokens": None, "policy": "requested"}

    loaded = load_model_parameters(cfg_file)
    assert "context_tokens" not in loaded["models"]["gemma4:e4b"]

    # No explicit override left: get_model_context_settings reports nothing
    # requested even though policy stayed "requested" from the earlier set.
    assert get_model_context_settings("gemma4:e4b", cfg_file)["requested_tokens"] is None


def test_update_model_context_settings_does_not_disturb_other_models(tmp_path: Path):
    cfg_file = str(tmp_path / "model_params.json")

    update_model_context_settings("gemma4:e4b", 32768, config_file=cfg_file)
    update_model_context_settings("llama3", 8192, config_file=cfg_file)

    loaded = load_model_parameters(cfg_file)
    assert loaded["models"]["gemma4:e4b"]["context_tokens"] == 32768
    assert loaded["models"]["llama3"]["context_tokens"] == 8192
