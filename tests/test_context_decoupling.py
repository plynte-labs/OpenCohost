"""Tests for ADR-056 WU2: Context Decoupling.

Proves that:
1. Switching LLM tier presets (quality, balanced, fast) does NOT clamp or distort
   the allocated context for a given model.
2. Runtime truth from OllamaResidencyProbe (/api/ps context_length) takes precedence
   when the model is resident.
3. Native context from /api/show is respected directly.
4. Degrades cleanly to default on invalid or missing context without crashing.
"""

from unittest.mock import MagicMock
import pytest

import opencohost.core.llm_engine
from opencohost.core.observability.health_monitor import OllamaResidencySnapshot
from opencohost.core.engine.llm_engine_models import ModelManagementMixin


class MockEngine(ModelManagementMixin):
    def __init__(self):
        self.health_monitor = None
        self.llm_tiers = None


def test_tier_switching_does_not_change_allocated_context():
    """ADR-056 WU2: Switching between Quality/Balanced/Fast tier must NOT change allocated_context."""
    engine = MockEngine()

    # Mock tier manager
    mock_tiers = MagicMock()
    mock_tiers.active_model = "gemma4:e4b"
    mock_tiers.config.as_dict.return_value = {
        "quality": "gemma4:e4b",
        "balanced": "gemma4:e4b",
        "fast": "gemma4:e4b",
    }
    engine.llm_tiers = mock_tiers

    native_ctx = 8192

    # Under legacy behavior, "quality" clamped to 4096 and "fast" clamped to 6144.
    # Under ADR-056 decoupled behavior, native_ctx 8192 is preserved across ALL tiers.
    mock_tiers.active_tier = "quality"
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx) == 8192

    mock_tiers.active_tier = "balanced"
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx) == 8192

    mock_tiers.active_tier = "fast"
    assert engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx) == 8192


def test_runtime_residency_probe_takes_precedence_when_resident():
    """ADR-056 WU2: Runtime /api/ps context_length from probe takes priority over native."""
    engine = MockEngine()

    # Active resident model has 16384 allocated in Ollama VRAM
    snap = OllamaResidencySnapshot(
        model="qwen3:1.7b",
        digest="sha256:qwen3_digest",
        size_bytes=4000_000_000,
        size_vram_bytes=4000_000_000,
        context_length=16384,
        observed_at=1000.0,
    )
    mock_monitor = MagicMock()
    mock_monitor.residency_snapshot = snap
    engine.health_monitor = mock_monitor

    # Even if native was reported as 4096 or 8192, live residency truth (16384) wins
    resolved = engine._resolve_effective_ctx_limit("qwen3:1.7b", native_ctx=4096)
    assert resolved == 16384


def test_native_ctx_used_when_model_not_resident():
    """When model is not currently resident in probe, native_ctx is used without tier clamping."""
    engine = MockEngine()

    snap = OllamaResidencySnapshot(
        model="other_model",
        digest="sha256:other",
        size_bytes=1000,
        size_vram_bytes=1000,
        context_length=2048,
        observed_at=1000.0,
    )
    mock_monitor = MagicMock()
    mock_monitor.residency_snapshot = snap
    engine.health_monitor = mock_monitor

    resolved = engine._resolve_effective_ctx_limit("target_model", native_ctx=12288)
    assert resolved == 12288


def test_invalid_native_ctx_falls_back_cleanly():
    """Invalid or negative native_ctx safely falls back to CTX_FALLBACK_DEFAULT (4096)."""
    engine = MockEngine()

    assert engine._resolve_effective_ctx_limit("model", None) == 4096
    assert engine._resolve_effective_ctx_limit("model", "invalid") == 4096
    assert engine._resolve_effective_ctx_limit("model", -100) == 4096
    assert engine._resolve_effective_ctx_limit("model", 0) == 4096
