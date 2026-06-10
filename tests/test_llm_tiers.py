import json

from config import settings
from core.llm_tiers import LLMTierConfig, LLMTierState


def test_tier_config_resolves_configured_models():
    config = LLMTierConfig(quality="llama3", balanced="qwen3:4b", fast="qwen3:1.7b")

    assert config.model_for("quality") == "llama3"
    assert config.model_for("balanced") == "qwen3:4b"
    assert config.model_for("fast") == "qwen3:1.7b"


def test_missing_tier_is_unavailable_without_crashing():
    config = LLMTierConfig(quality="llama3", balanced="", fast=None)

    assert config.model_for("fast") is None
    assert config.is_available("fast") is False


def test_active_tier_defaults_to_first_available_slot():
    state = LLMTierState(
        config=LLMTierConfig(quality="", balanced="qwen3:4b", fast="qwen3:1.7b"),
        active_tier="quality",
    )

    assert state.active_tier == "balanced"
    assert state.active_model == "qwen3:4b"


def test_invalid_tier_names_are_unavailable():
    config = LLMTierConfig(quality="llama3", balanced="qwen3:4b", fast="qwen3:1.7b")

    assert config.model_for("turbo") is None
    assert config.is_available("turbo") is False


def test_resolve_llm_tiers_uses_operator_config_file(tmp_path, monkeypatch):
    tiers_file = tmp_path / "llm_tiers.json"
    tiers_file.write_text(
        '{"quality": "gemma4:e4b", "balanced": "llama3", "fast": "qwen3:1.7b"}',
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "LLM_TIERS_FILE", str(tiers_file))

    assert settings.resolve_llm_tiers() == {
        "quality": "gemma4:e4b",
        "balanced": "llama3",
        "fast": "qwen3:1.7b",
    }


def test_models_catalog_includes_gemma4_12b():
    model = settings.MODELS_CATALOG["gemma4:12b"]

    assert model["display"].startswith("Gemma 4 (12B)")
    assert model["family"] == "gemma"
    assert model["size_gb"] > 0


def test_resolve_startup_model_restores_installed_non_curated_model(
    tmp_path, monkeypatch
):
    last_model_file = tmp_path / "last_model.json"
    last_model_file.write_text(
        json.dumps({"model": "bespoke:9b", "source": "test"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "LAST_MODEL_FILE", str(last_model_file))

    assert settings.resolve_startup_model(installed_model_tags={"bespoke:9b"}) == (
        "bespoke:9b",
        "saved_runtime",
    )


def test_resolve_startup_model_falls_back_for_missing_non_curated_model(
    tmp_path, monkeypatch
):
    last_model_file = tmp_path / "last_model.json"
    last_model_file.write_text(
        json.dumps({"model": "bespoke:9b", "source": "test"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "LAST_MODEL_FILE", str(last_model_file))

    assert settings.resolve_startup_model(installed_model_tags=set()) == (
        settings.DEFAULT_MODEL,
        "invalid_saved_fallback",
    )


def test_resolve_llm_tiers_accepts_installed_non_curated_override(
    tmp_path, monkeypatch
):
    tiers_file = tmp_path / "llm_tiers.json"
    tiers_file.write_text(
        json.dumps(
            {
                "quality": "bespoke:9b",
                "balanced": "llama3",
                "fast": "qwen3:1.7b",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "LLM_TIERS_FILE", str(tiers_file))

    assert settings.resolve_llm_tiers(installed_model_tags={"bespoke:9b"}) == {
        "quality": "bespoke:9b",
        "balanced": "llama3",
        "fast": "qwen3:1.7b",
    }


def test_resolve_llm_tiers_keeps_safe_default_for_missing_override(
    tmp_path, monkeypatch
):
    tiers_file = tmp_path / "llm_tiers.json"
    tiers_file.write_text(
        json.dumps(
            {
                "quality": "missing:9b",
                "balanced": "llama3",
                "fast": "qwen3:1.7b",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "LLM_TIERS_FILE", str(tiers_file))

    assert settings.resolve_llm_tiers(installed_model_tags=set()) == {
        "quality": settings.DEFAULT_LLM_TIERS["quality"],
        "balanced": "llama3",
        "fast": "qwen3:1.7b",
    }
