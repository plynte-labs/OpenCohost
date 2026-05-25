from core.llm_tiers import LLMTierConfig, LLMTierState
from config import settings


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
