"""R2 — REAL-env reasoning-model budget premises (ADR-014).

Opt-in via ``OPENCOHOST_REALENV_TESTS=1``; auto-skips without Ollama / qwen3:1.7b.
Bounded: a metadata RPC plus ONE tiny capped generation, each under a hard
per-test timeout (no pytest-timeout in flux_env, so run_bounded does thread+join).

Validates empirically the two premises the mocked unit suite
(``tests/test_reasoning_token_budget.py``) hand-builds against fakes:

  1. qwen3 really advertises a 'thinking' capability, AND the production probe
     ``MotorVocalIA._check_capabilities_reasoning`` reads it from a REAL
     ``ollama.show`` (not a monkeypatched stub).
  2. A tiny ``num_predict`` cap on a reasoning model really spends the visible
     budget on internal thinking — empty ``content`` + non-empty ``thinking`` —
     the exact response shape ADR-014's runtime self-heal keys off.

These are real-env PROBES (not routing decisions), so a real generation is the
correct tool here; a mock would only re-assert the hand-built shape.
"""
from __future__ import annotations

import pytest

from tests.realenv._helpers import require_model, run_bounded

pytestmark = pytest.mark.realenv

MODEL = "qwen3:1.7b"


def _new_motor():
    """MotorVocalIA with ``__init__`` bypassed — only the attrs the capability
    probe touches, same pattern as test_reasoning_token_budget.py:23 but wired
    to the REAL ``ollama.show``."""
    from opencohost.core.llm_engine import MotorVocalIA

    m = MotorVocalIA.__new__(MotorVocalIA)
    # _fetch_show gates on the _is_local PROPERTY, which reads both of these.
    # Without them the gate raises AttributeError, and _check_capabilities_reasoning
    # swallows it as False — a red test that reads exactly like a real negative.
    m._provider_config = {"active_provider": "local", "profiles": {}}
    m._cloud_fallback_active = False
    m._model_ctx_limit = {}
    m._ctx_show_cache = {}
    m._reasoning_model_cache = {}
    m._log = lambda *a, **k: None
    return m


def test_capability_probe_detects_thinking_real():
    """Real ollama.show -> _check_capabilities_reasoning == True, caps carry thinking."""
    require_model(MODEL)
    m = _new_motor()

    # _check_capabilities_reasoning does the real (metadata-only) ollama.show.
    detected = run_bounded(lambda: m._check_capabilities_reasoning(MODEL), seconds=20)
    assert detected is True

    import ollama

    resp = run_bounded(lambda: ollama.show(MODEL), seconds=20)
    caps = resp.get("capabilities") if isinstance(resp, dict) else getattr(resp, "capabilities", None)
    assert caps is not None, "real ollama.show exposed no capabilities"
    assert "thinking" in caps and "completion" in caps


def test_capped_generation_spends_budget_on_thinking_real():
    """A tiny num_predict cap yields empty visible content + non-empty thinking."""
    require_model(MODEL)
    import ollama

    resp = run_bounded(
        lambda: ollama.chat(
            model=MODEL,
            messages=[{"role": "user", "content": "Hola"}],
            options={"num_predict": 8, "temperature": 0},
        ),
        seconds=60,
    )
    msg = resp["message"] if isinstance(resp, dict) else resp.message
    content = (msg.get("content") if isinstance(msg, dict) else getattr(msg, "content", "")) or ""
    thinking = (msg.get("thinking") if isinstance(msg, dict) else getattr(msg, "thinking", "")) or ""

    # ponytail: num_predict=8 verified empty-content + thinking_len 21 in flux_env;
    # drop the cap to 4 if a future qwen3 build makes 8 emit visible content.
    assert not content.strip(), f"expected empty visible content, got {content!r}"
    assert thinking.strip(), "expected non-empty internal thinking at num_predict=8"
