import json

from opencohost.config import settings
from opencohost.core.providers.llm_tiers import LLMTierConfig, LLMTierState


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


def test_resolve_startup_model_uses_installed_when_default_missing(
    tmp_path, monkeypatch
):
    """No saved model + DEFAULT_MODEL not installed → pick a real installed model
    instead of a model that would make Kira reach 'ready' but never answer."""
    monkeypatch.setattr(settings, "LAST_MODEL_FILE", str(tmp_path / "nope.json"))

    assert settings.resolve_startup_model(installed_model_tags={"qwen3:4b"}) == (
        "qwen3:4b",
        "installed_fallback",
    )


def test_resolve_startup_model_picks_smallest_installed_catalog_model(
    tmp_path, monkeypatch
):
    """Among several installed catalog models, the smallest (fast, low VRAM) wins."""
    monkeypatch.setattr(settings, "LAST_MODEL_FILE", str(tmp_path / "nope.json"))
    installed = {"qwen3:4b", "qwen3:1.7b", "llama3.2:3b"}  # 2.6 / 1.1 / 2.0 GB

    assert settings.resolve_startup_model(installed_model_tags=installed) == (
        "qwen3:1.7b",
        "installed_fallback",
    )


def test_resolve_startup_model_keeps_default_when_installed(tmp_path, monkeypatch):
    """If DEFAULT_MODEL is actually installed, keep it — no surprise switch."""
    monkeypatch.setattr(settings, "LAST_MODEL_FILE", str(tmp_path / "nope.json"))
    installed = {settings.DEFAULT_MODEL, "qwen3:1.7b"}

    assert settings.resolve_startup_model(installed_model_tags=installed) == (
        settings.DEFAULT_MODEL,
        "default",
    )


def test_resolve_startup_model_keeps_default_without_discovery(tmp_path, monkeypatch):
    """Empty installed set (Ollama down / no info) → keep DEFAULT_MODEL; do not
    invent a fallback we cannot verify."""
    monkeypatch.setattr(settings, "LAST_MODEL_FILE", str(tmp_path / "nope.json"))

    assert settings.resolve_startup_model(installed_model_tags=set()) == (
        settings.DEFAULT_MODEL,
        "default",
    )


def test_resolve_startup_model_installed_fallback_on_invalid_saved(
    tmp_path, monkeypatch
):
    """Saved model gone + DEFAULT not installed → use an installed model, not the
    broken default."""
    last_model_file = tmp_path / "last_model.json"
    last_model_file.write_text(
        json.dumps({"model": "ghost:1b", "source": "test"}), encoding="utf-8"
    )
    monkeypatch.setattr(settings, "LAST_MODEL_FILE", str(last_model_file))

    assert settings.resolve_startup_model(installed_model_tags={"qwen3:1.7b"}) == (
        "qwen3:1.7b",
        "installed_fallback",
    )


def test_resolve_startup_model_falls_back_for_uninstalled_saved_catalog_model(
    tmp_path, monkeypatch
):
    """A saved CATALOG model that was later uninstalled (e.g. `ollama rm`) must
    NOT silent-ready: catalog membership alone is not proof of installation."""
    last_model_file = tmp_path / "last_model.json"
    last_model_file.write_text(
        json.dumps({"model": "qwen3:4b", "source": "test"}), encoding="utf-8"
    )
    monkeypatch.setattr(settings, "LAST_MODEL_FILE", str(last_model_file))

    assert settings.resolve_startup_model(installed_model_tags={"qwen3:1.7b"}) == (
        "qwen3:1.7b",
        "installed_fallback",
    )


def test_resolve_startup_model_ignores_zero_size_sentinel(tmp_path, monkeypatch):
    """The 0.0 size_gb 'unknown size' placeholder (gemma4-uncensored) must never
    win the 'smallest installed' selection over a real model."""
    monkeypatch.setattr(settings, "LAST_MODEL_FILE", str(tmp_path / "nope.json"))
    installed = {"gemma4-uncensored", "qwen3:1.7b"}  # 0.0 sentinel vs 1.1 GB

    assert settings.resolve_startup_model(installed_model_tags=installed) == (
        "qwen3:1.7b",
        "installed_fallback",
    )


def test_resolve_startup_model_breaks_size_ties_deterministically(
    tmp_path, monkeypatch
):
    """Two installed catalog models of equal size resolve to the alphabetically
    first tag, stably across repeated calls (no hash-order nondeterminism)."""
    monkeypatch.setattr(settings, "LAST_MODEL_FILE", str(tmp_path / "nope.json"))
    installed = {"phi4-mini", "gemma4:e4b"}  # both 2.5 GB; nothing smaller installed

    first = settings.resolve_startup_model(installed_model_tags=installed)
    second = settings.resolve_startup_model(installed_model_tags=installed)

    assert first == ("gemma4:e4b", "installed_fallback")
    assert first == second


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
