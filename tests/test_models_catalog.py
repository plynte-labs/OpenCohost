"""Curated MODELS_CATALOG: only owner-tested models, real sizes, min_vram_gb."""

from __future__ import annotations

import pytest

from opencohost.config import settings
from opencohost.core.engine import llm_readiness

REMOVED = [
    "phi4-mini",
    "llama3.2:3b",
    "llama3.2:1b",
    "hf.co/carsenk/llama3.2_1b_2025_uncensored_v2",
    "qwen2.5:3b",
    "smollm2:1.7b",
    "gemma4-uncensored",
    "gemma4:12b",
]

EXPECTED = {
    "qwen3:1.7b": (1.4, 0),
    "hf.co/unsloth/Qwen3.5-4B-GGUF:Q4_K_M": (3.4, 5),
    "qwen3:4b": (2.5, 4),
    "llama3": (4.7, 6),
    "hf.co/unsloth/gemma-4-E4B-it-GGUF:IQ4_NL": (5.8, 7.5),
    "hf.co/empero-ai/Qwen3.8-9B-Distill-GGUF:Q4_K_M": (5.8, 7.5),
    "gemma4:e2b": (7.2, 8),
    "gemma4:e4b": (9.6, 10),
}


def test_catalog_has_exactly_the_curated_models():
    assert set(settings.MODELS_CATALOG) == set(EXPECTED)


@pytest.mark.parametrize("tag", REMOVED)
def test_removed_tags_absent(tag):
    assert tag not in settings.MODELS_CATALOG


@pytest.mark.parametrize("tag,expected", EXPECTED.items())
def test_real_sizes_and_min_vram(tag, expected):
    size, vram = expected
    entry = settings.MODELS_CATALOG[tag]
    assert entry["size_gb"] == size
    assert entry["min_vram_gb"] == vram


def test_every_entry_has_full_schema():
    for tag, entry in settings.MODELS_CATALOG.items():
        for key in ("display", "desc", "size_gb", "family", "min_vram_gb"):
            assert key in entry, (tag, key)
        assert isinstance(entry["min_vram_gb"], (int, float))
        assert entry["size_gb"] > 0


def test_tier_critical_models_and_defaults_preserved():
    for tag in ("llama3", "gemma4:e4b", "qwen3:1.7b"):
        assert tag in settings.MODELS_CATALOG
    assert settings.DEFAULT_MODEL == "gemma4:e4b"
    assert settings.DEFAULT_LLM_TIERS == {
        "quality": "gemma4:e4b",
        "balanced": "llama3",
        "fast": "qwen3:1.7b",
    }


def test_api_model_exposes_min_vram_gb():
    from opencohost.api.models import ModelCatalogEntry

    entry = ModelCatalogEntry(**settings.MODELS_CATALOG["llama3"])
    assert entry.min_vram_gb == 6
    legacy = ModelCatalogEntry(display="x", desc="y", size_gb=1.0, family="f")
    assert legacy.min_vram_gb == 0


@pytest.mark.parametrize(
    "gpu,vram,tag,cpu_only",
    [
        (None, 0, "qwen3:1.7b", True),
        ("GPU", 0, "qwen3:1.7b", True),
        ("GPU", 2048, "qwen3:1.7b", False),
        ("GPU", 4999, "qwen3:1.7b", False),
        ("GPU", 5000, "hf.co/unsloth/Qwen3.5-4B-GGUF:Q4_K_M", False),
        ("GPU", 6144, "hf.co/unsloth/Qwen3.5-4B-GGUF:Q4_K_M", False),
        ("GPU", 7499, "hf.co/unsloth/Qwen3.5-4B-GGUF:Q4_K_M", False),
        ("GPU", 7500, "hf.co/unsloth/gemma-4-E4B-it-GGUF:IQ4_NL", False),
        ("GPU", 12288, "hf.co/unsloth/gemma-4-E4B-it-GGUF:IQ4_NL", False),
    ],
)
def test_hardware_guideline_thresholds(monkeypatch, gpu, vram, tag, cpu_only):
    monkeypatch.setattr(llm_readiness, "_detect_gpu_hardware", lambda: (gpu, vram))
    hw = llm_readiness.get_hardware_guideline()
    assert hw["recommended_tag"] == tag
    assert hw["recommended_command"] == f"ollama run {tag}"
    assert hw["cpu_only"] is cpu_only
