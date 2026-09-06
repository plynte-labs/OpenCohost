"""Tests for opencohost.config.model_parameters."""

import json
from pathlib import Path

from opencohost.config.model_parameters import (
    DEFAULT_MODEL_PARAMETERS,
    get_model_reasoning_settings,
    load_model_parameters,
    save_model_parameters,
    update_model_reasoning_settings,
)


def test_load_defaults_when_file_missing(tmp_path: Path):
    cfg_file = str(tmp_path / "non_existent.json")
    cfg = load_model_parameters(cfg_file)
    assert cfg["reasoning_enabled"] is False
    assert cfg["reasoning_budget_tokens"] == 512
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

