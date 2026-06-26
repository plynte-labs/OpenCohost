"""Phase 0 (ram_llm_hardening_20260626): Ollama memory-config defaults.

Covers A1 (finite keep_alive) at the settings level. The per-call-site behavioral
assertions live in test_llm_engine_timeouts.py (warm-up),
test_context_overflow_guardrail.py (chat), and test_smart_aggregator_ui.py (Vibe).
"""

from opencohost.config import settings


def test_keep_alive_default_is_finite():
    """LLM_KEEP_ALIVE must be a finite keep_alive, never the -1 'pin forever'
    value that keeps the model weights resident in RAM for the whole session."""
    assert settings.LLM_KEEP_ALIVE not in (-1, "-1", 0, "0")
    assert settings.LLM_KEEP_ALIVE == "7m"
