"""Tests for ADR-056 WU3: Budget Engine & Output Policy.

Covers:
1. Pure budget engine governance (resolve_generation_budget):
   - BUDGET_INFEASIBLE when remaining_context <= 0 (never fabricates capacity).
   - Latency target scaling via empirical EWMA TPS.
   - Context pressure clamping when budget exceeds headroom.
   - VRAM residency degradation scaling when layers spill to host RAM.
2. Intent-driven policy:
   - DRAFTING drops "2-4 oraciones" restriction from prompt and disables TTS.
   - CHAT preserves "2-4 oraciones" and keeps TTS enabled.
   - REASONING enables thinking.
3. MotorVocalIA TTS bypass when intent == "drafting".
"""

import queue
from unittest.mock import MagicMock
import pytest

from opencohost.core.engine.llm_budget_engine import (
    InferenceIntent,
    BudgetVerdict,
    BudgetResolution,
    resolve_generation_budget,
    SAFETY_RESERVE_TOKENS,
)
from opencohost.core.context.prompt_assembler import PromptContextAssembler


def test_budget_infeasible_when_remaining_context_exhausted():
    """ADR-056 Block 1: Never fabricate capacity. Return BUDGET_INFEASIBLE with effective_budget=0."""
    # Context 4096, prompt 4096 -> remaining is negative
    res = resolve_generation_budget(
        intent=InferenceIntent.CHAT,
        allocated_context=4096,
        prompt_tokens=4096,
        safety_reserve=128,
    )
    assert res.verdict == BudgetVerdict.BUDGET_INFEASIBLE
    assert res.effective_budget == 0
    assert res.clamp_reason == "CONTEXT_EXHAUSTED"

    # Exactly at boundary: prompt + safety == allocated
    res2 = resolve_generation_budget(
        intent=InferenceIntent.CHAT,
        allocated_context=4096,
        prompt_tokens=4096 - SAFETY_RESERVE_TOKENS,
        safety_reserve=SAFETY_RESERVE_TOKENS,
    )
    assert res2.verdict == BudgetVerdict.BUDGET_INFEASIBLE
    assert res2.effective_budget == 0


def test_latency_target_scaling_with_calibrated_tps():
    """Calibrated EWMA TPS scales budget according to target latency."""
    # 50 tok/s with CHAT intent (target 4.0s) -> 200 tokens
    res = resolve_generation_budget(
        intent=InferenceIntent.CHAT,
        allocated_context=8192,
        prompt_tokens=1000,
        tps_ewma=50.0,
    )
    assert res.verdict == BudgetVerdict.ALLOWED
    assert res.effective_budget == 200
    assert res.tts_eligible is True


def test_context_pressure_clamping():
    """When latency target or requested budget exceeds remaining context, clamp to remaining."""
    # 50 tok/s with 30s target -> 1500 tokens, but remaining context is only 300
    res = resolve_generation_budget(
        intent=InferenceIntent.DRAFTING,
        allocated_context=1500,
        prompt_tokens=1072,  # 1500 - 1072 - 128 = 300 remaining
        tps_ewma=50.0,
    )
    assert res.verdict == BudgetVerdict.CLAMPED_CONTEXT
    assert res.effective_budget == 300
    assert res.clamp_reason == "CONTEXT_PRESSURE_CLAMP"


def test_residency_spill_scaling():
    """When VRAM residency ratio < 1.0 (CPU offloading), scale back budget to avoid freezing."""
    # 50 tok/s * 4s = 200, with 50% residency -> 200 * 0.5 = 100
    res = resolve_generation_budget(
        intent=InferenceIntent.CHAT,
        allocated_context=4096,
        prompt_tokens=500,
        tps_ewma=50.0,
        residency_ratio=0.5,
    )
    assert res.effective_budget == 100
    assert res.clamp_reason == "RESIDENCY_SPILL_CLAMP"


def test_drafting_intent_disables_tts_and_relaxes_prompt():
    """DRAFTING intent disables TTS eligibility and strips 2-4 sentence rule."""
    res = resolve_generation_budget(
        intent=InferenceIntent.DRAFTING,
        allocated_context=8192,
        prompt_tokens=500,
    )
    assert res.tts_eligible is False

    base_system = (
        "Eres Kira.\n"
        "- Respondes en 2-4 oraciones. Nunca monólogos, pero tampoco monosílabos\n"
        "- Sé sarcástica."
    )
    assembler = PromptContextAssembler()
    setup = assembler.assemble(
        "redacta una agenda",
        source="direct",
        system_prompt=base_system,
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
        intent="drafting",
    )

    sys_msg = setup.messages[0]["content"]
    assert "2-4 oraciones" not in sys_msg
    assert "MODO REDACCIÓN" in sys_msg
    assert setup.tts_eligible is False


def test_chat_intent_preserves_conversational_rule_and_tts():
    """CHAT intent preserves 2-4 sentence rule and enables TTS."""
    base_system = (
        "Eres Kira.\n"
        "- Respondes en 2-4 oraciones. Nunca monólogos, pero tampoco monosílabos\n"
        "- Sé sarcástica."
    )
    assembler = PromptContextAssembler()
    setup = assembler.assemble(
        "hola qué tal",
        source="direct",
        system_prompt=base_system,
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
        intent="chat",
    )

    sys_msg = setup.messages[0]["content"]
    assert "2-4 oraciones" in sys_msg
    assert setup.tts_eligible is True


def test_motor_vocal_ia_bypasses_tts_on_drafting():
    """When MotorVocalIA runs with intent='drafting', _speak_or_submit is bypassed."""
    from opencohost.core import llm_engine

    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor.use_system_role = True
    motor.ollama = MagicMock()
    motor.ollama.chat.return_value = {
        "message": {"content": "Este es un borrador largo de múltiples párrafos."},
        "eval_count": 150,
        "eval_duration": 3_000_000_000,
        "prompt_eval_count": 50,
    }
    motor._speak_or_submit = MagicMock()

    reply = motor._generar_dialogo(
        "redacta un resumen largo",
        source="direct",
        intent="drafting",
        commit_history=False,
    )

    assert "Este es un borrador largo" in reply
    # Verifies TTS was never called!
    assert motor._speak_or_submit.call_count == 0
