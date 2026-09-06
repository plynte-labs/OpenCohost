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


# ===========================================================================
# Researcher Gate Characterization Tests (ADR-056 Verification)
# ===========================================================================

def test_gemma_native_128k_no_snapshot_does_not_imply_128k_allocation():
    """[BLOCKER] Gemma native=131072 + no /api/ps snapshot MUST NOT imply num_ctx=131072."""
    from opencohost.core.engine.llm_engine_models import ModelManagementMixin

    class DummyEngine(ModelManagementMixin):
        def __init__(self):
            self.health_monitor = None

    engine = DummyEngine()
    resolved = engine._resolve_effective_ctx_limit("gemma4:e4b", native_ctx=131072)
    assert resolved == 4096
    assert resolved != 131072


def test_telemetry_calibration_lifecycle_gates_budget_adaptation():
    """ADR-056 P1: Telemetry state gates adaptive budgeting:
    - COLD (sample_count=1): MUST NOT alter generation budget (tps_ewma=0.0)
    - WARMING (sample_count=9): MUST NOT alter generation budget (tps_ewma=0.0)
    - CALIBRATED (sample_count=10): MAY alter generation budget (tps_ewma=prof.ewma_tps)
    """
    from opencohost.core.engine.inference_telemetry import InferenceTelemetryTracker, CalibrationState

    tracker = InferenceTelemetryTracker()
    assembler = PromptContextAssembler()

    # 1 sample -> COLD
    tracker.record_turn(
        provider="local",
        model_id="llama3",
        model_digest="sha256:d",
        allocated_context=4096,
        eval_count=100,
        eval_duration_ns=2_000_000_000,  # 50 TPS
        size_bytes=1000,
        size_vram_bytes=1000,
    )
    prof1 = tracker.get_profile("local", "llama3", allocated_context=4096)
    assert prof1.sample_count == 1
    assert prof1.state == CalibrationState.COLD

    def _assemble_test(tracker):
        return assembler.assemble(
            "hola",
            "direct",
            system_prompt="Eres Kira.",
            use_system_role=True,
            is_local=True,
            provider_cfg={},
            request_model="llama3",
            history_snapshot=[],
            telemetry_tracker=tracker,
            effective_ctx_resolver=lambda m, n: 4096,
        )

    setup1 = _assemble_test(tracker)
    # Budget must NOT be altered by the 50 TPS EWMA; uses default fallback (768)
    assert setup1.opciones_llm["num_predict"] == 768
    assert setup1.budget_resolution.calibrated_tps == 0.0

    # Record 8 more samples -> 9 total (WARMING)
    for _ in range(8):
        tracker.record_turn(
            provider="local",
            model_id="llama3",
            model_digest="sha256:d",
            allocated_context=4096,
            eval_count=100,
            eval_duration_ns=2_000_000_000,
            size_bytes=1000,
            size_vram_bytes=1000,
        )
    prof9 = tracker.get_profile("local", "llama3", allocated_context=4096)
    assert prof9.sample_count == 9
    assert prof9.state == CalibrationState.WARMING

    setup9 = _assemble_test(tracker)
    assert setup9.opciones_llm["num_predict"] == 768
    assert setup9.budget_resolution.calibrated_tps == 0.0

    # Record 10th sample -> 10 total (CALIBRATED)
    tracker.record_turn(
        provider="local",
        model_id="llama3",
        model_digest="sha256:d",
        allocated_context=4096,
        eval_count=100,
        eval_duration_ns=2_000_000_000,
        size_bytes=1000,
        size_vram_bytes=1000,
    )
    prof10 = tracker.get_profile("local", "llama3", allocated_context=4096)
    assert prof10.sample_count == 10
    assert prof10.state == CalibrationState.CALIBRATED

    setup10 = _assemble_test(tracker)
    # CALIBRATED: adaptive budget active! 50 TPS * 4.0s = 200 tokens
    assert setup10.budget_resolution.calibrated_tps == pytest.approx(50.0)
    assert setup10.opciones_llm["num_predict"] == 200


def test_chat_at_50_tps_presets_order_strictly():
    """[P1] CHAT @ 50 TPS: fast < balanced < quality."""
    fast = resolve_generation_budget(
        intent=InferenceIntent.CHAT,
        allocated_context=8192,
        tps_ewma=50.0,
        preset="fast",
    )
    balanced = resolve_generation_budget(
        intent=InferenceIntent.CHAT,
        allocated_context=8192,
        tps_ewma=50.0,
        preset="balanced",
    )
    quality = resolve_generation_budget(
        intent=InferenceIntent.CHAT,
        allocated_context=8192,
        tps_ewma=50.0,
        preset="quality",
    )
    assert fast.effective_budget < balanced.effective_budget < quality.effective_budget
    assert fast.effective_budget == 100
    assert balanced.effective_budget == 200
    assert quality.effective_budget == 400


def test_budget_infeasible_must_not_issue_ollama_request_with_num_predict_zero():
    """[P1] BUDGET_INFEASIBLE MUST NOT issue Ollama request with num_predict=0."""
    import threading
    from opencohost.core.engine.generation_orchestrator import GenerationOrchestrator
    from opencohost.core.context.prompt_assembler import GenerationSetup

    fake_host = MagicMock()
    fake_host._lock = threading.Lock()
    fake_host._ollama_chat_with_watchdog = MagicMock()

    orchestrator = GenerationOrchestrator(fake_host)

    setup = GenerationSetup(
        messages=[{"role": "user", "content": "hi"}],
        opciones_llm={"num_predict": 0, "num_ctx": 4096},
        chat_timeout=30.0,
        max_intentos=2,
        start_llm=0.0,
        native_ctx=4096,
        effective_ctx=4096,
        ctx_evicted=0,
        editorial_block="",
        history_snapshot=[],
        budget_resolution=BudgetResolution(
            intent=InferenceIntent.CHAT,
            verdict=BudgetVerdict.BUDGET_INFEASIBLE,
            effective_budget=0,
            think=False,
            allocated_context=4096,
            remaining_context=0,
            clamp_reason="CONTEXT_EXHAUSTED",
            calibrated_tps=0.0,
            tts_eligible=False,
        ),
    )

    outcome = orchestrator.execute_generation_attempt(
        setup,
        source="direct",
        commit_history=False,
        is_local=True,
        provider_cfg={},
        request_model="gemma4:e4b",
        watchdog_timeout=None,
    )

    # Must cleanly abort without issuing chat call to Ollama
    assert outcome.early_return == ""
    assert fake_host._ollama_chat_with_watchdog.call_count == 0


def test_residency_ratio_routing_and_staleness_guard():
    """[ADR completeness]
    - Current matching residency_ratio reaches budget resolver
    - Stale/different-model residency never reaches it (None)
    """
    import time
    from opencohost.core.engine.llm_engine_models import ModelManagementMixin
    from opencohost.core.observability.health_monitor import OllamaResidencySnapshot

    class MockEngine(ModelManagementMixin):
        def __init__(self):
            self.health_monitor = MagicMock()

    engine = MockEngine()

    # 1. Matching & fresh (within 60s) -> ratio reaches resolver
    engine.health_monitor.residency_snapshot = OllamaResidencySnapshot(
        model="gemma4:e4b",
        digest="sha256:xyz",
        size_bytes=10_000_000_000,
        size_vram_bytes=6_000_000_000,
        context_length=4096,
        observed_at=time.time(),
    )
    ratio = engine._resolve_model_residency_ratio("gemma4:e4b")
    assert ratio == pytest.approx(0.6)

    # Assembler receives and passes it to budget resolver
    assembler = PromptContextAssembler()
    setup = assembler.assemble(
        "hola",
        "direct",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="gemma4:e4b",
        history_snapshot=[],
        residency_ratio_resolver=engine._resolve_model_residency_ratio,
        effective_ctx_resolver=lambda m, n: 4096,
    )
    assert setup.budget_resolution is not None
    # Spill was detected (0.6 < 1.0) -> scaled and clamp_reason recorded
    assert setup.budget_resolution.clamp_reason == "RESIDENCY_SPILL_CLAMP"

    # 2. Different model -> returns None, never reaches resolver
    diff_model_ratio = engine._resolve_model_residency_ratio("llama3")
    assert diff_model_ratio is None

    setup_diff = assembler.assemble(
        "hola",
        "direct",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        history_snapshot=[],
        residency_ratio_resolver=engine._resolve_model_residency_ratio,
        effective_ctx_resolver=lambda m, n: 4096,
    )
    assert setup_diff.budget_resolution.clamp_reason != "RESIDENCY_SPILL_CLAMP"

    # 3. Stale snapshot (> 60s old) -> returns None
    engine.health_monitor.residency_snapshot = OllamaResidencySnapshot(
        model="gemma4:e4b",
        digest="sha256:xyz",
        size_bytes=10_000_000_000,
        size_vram_bytes=6_000_000_000,
        context_length=4096,
        observed_at=time.time() - 120.0,  # 2 minutes old
    )
    stale_ratio = engine._resolve_model_residency_ratio("gemma4:e4b")
    assert stale_ratio is None

    setup_stale = assembler.assemble(
        "hola",
        "direct",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="gemma4:e4b",
        history_snapshot=[],
        residency_ratio_resolver=engine._resolve_model_residency_ratio,
        effective_ctx_resolver=lambda m, n: 4096,
    )
    assert setup_stale.budget_resolution.clamp_reason != "RESIDENCY_SPILL_CLAMP"


def test_models_match_residency_delimiter_safety():
    """Verify delimited prefix matching prevents false matches between different model sizes."""
    from opencohost.core.engine.llm_engine_models import _models_match_residency

    # True matches: exact, case variations, tag variants, :latest
    assert _models_match_residency("gemma4:e4b", "gemma4:e4b") is True
    assert _models_match_residency("Gemma4:e4b", "gemma4:E4B") is True
    assert _models_match_residency("gemma4:e4b", "gemma4:e4b-instruct") is True
    assert _models_match_residency("gemma4:e4b", "gemma4:e4b:latest") is True
    assert _models_match_residency("gemma4:e4b", "gemma4:e4b:q4_k_m") is True
    assert _models_match_residency("llama3", "llama3:8b") is True

    # False matches: different model scales or versions MUST NOT match
    assert _models_match_residency("qwen2.5:7b", "qwen2.5:72b") is False
    assert _models_match_residency("qwen3:1.7b", "qwen3:14b") is False
    assert _models_match_residency("llama3", "llama3.1:8b") is False
    assert _models_match_residency("gemma4:e4b", "gemma4:e2b") is False
    assert _models_match_residency("", "gemma4:e4b") is False
    assert _models_match_residency("gemma4:e4b", None) is False


def test_resolve_generation_budget_handles_none_tps_and_string_intent():
    """Defensive handling: tps_ewma=None does not raise TypeError, string intent normalized."""
    from opencohost.core.engine.llm_budget_engine import DEFAULT_FALLBACK_BUDGET

    res = resolve_generation_budget(
        intent="CHAT",
        allocated_context=4096,
        prompt_tokens=500,
        tps_ewma=None,
    )
    assert res.verdict == BudgetVerdict.ALLOWED
    assert res.effective_budget == DEFAULT_FALLBACK_BUDGET
    assert res.calibrated_tps == 0.0

    res_unknown = resolve_generation_budget(
        intent="unknown_intent_string",
        allocated_context=4096,
        prompt_tokens=500,
        tps_ewma=None,
    )
    assert res_unknown.intent == InferenceIntent.CHAT


def test_streaming_attempt_aborts_on_budget_infeasible_without_calling_ollama():
    """run_streaming_attempt must abort cleanly on BUDGET_INFEASIBLE without calling _ollama_chat_streaming."""
    from opencohost.core.engine.generation_orchestrator import GenerationOrchestrator
    from opencohost.core.context.prompt_assembler import GenerationSetup

    fake_host = MagicMock()
    fake_host._ollama_chat_streaming = MagicMock()
    fake_host._log = MagicMock()

    orchestrator = GenerationOrchestrator(fake_host)
    setup = GenerationSetup(
        messages=[{"role": "user", "content": "hi"}],
        opciones_llm={"num_predict": 0, "num_ctx": 4096},
        chat_timeout=30.0,
        max_intentos=2,
        start_llm=0.0,
        native_ctx=4096,
        effective_ctx=4096,
        ctx_evicted=0,
        editorial_block="",
        history_snapshot=[],
        budget_resolution=BudgetResolution(
            intent=InferenceIntent.CHAT,
            verdict=BudgetVerdict.BUDGET_INFEASIBLE,
            effective_budget=0,
            think=False,
            allocated_context=4096,
            remaining_context=0,
            clamp_reason="CONTEXT_EXHAUSTED",
            calibrated_tps=0.0,
            tts_eligible=False,
        ),
    )

    respuesta, stream_state = orchestrator.run_streaming_attempt(
        setup,
        source="direct",
        request_model="gemma4:e4b",
        contexto=None,
        history_text=None,
    )
    assert respuesta is None
    assert stream_state.abort_reason == "budget_infeasible"
    assert fake_host._ollama_chat_streaming.call_count == 0


def test_reactive_trimming_loop_rescues_budget_infeasible():
    """When budget is initially infeasible, reactive trimming drops multiple pairs until feasible."""
    assembler = PromptContextAssembler()
    # Create history with multiple turn pairs that push prompt over 4096
    # 4096 chars ~ 1170 tokens. Context is 1200.
    history = [
        {"role": "user", "content": "x" * 600},
        {"role": "assistant", "content": "y" * 600},
        {"role": "user", "content": "x" * 600},
        {"role": "assistant", "content": "y" * 600},
        {"role": "user", "content": "x" * 600},
        {"role": "assistant", "content": "y" * 600},
        {"role": "user", "content": "x" * 600},
        {"role": "assistant", "content": "y" * 600},
    ]
    setup = assembler.assemble(
        "pregunta actual",
        source="direct",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="gemma4:e4b",
        history_snapshot=history,
        effective_ctx_resolver=lambda m, n: 1200,
    )
    # The reactive trimming loop drops pairs until it fits or is infeasible
    assert setup.ctx_evicted > 0
