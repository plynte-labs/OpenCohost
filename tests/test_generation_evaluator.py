"""
tests/test_generation_evaluator.py

Unit tests for GenerationEvaluator and pure output inspection helpers.
"""

from unittest.mock import MagicMock

import pytest

from opencohost.core.context.generation_evaluator import (
    GenerationEvaluator,
    guard_rule_id,
    output_guard_with_tts_check,
)
from opencohost.core.context.prompt_assembler import GenerationSetup


def _make_setup(**kwargs) -> GenerationSetup:
    defaults = dict(
        messages=[{"role": "user", "content": "hola"}],
        opciones_llm={"temperature": 0.7},
        chat_timeout=30.0,
        max_intentos=2,
        start_llm=100.0,
        native_ctx=4096,
        effective_ctx=4096,
        ctx_evicted=0,
        editorial_block="",
        history_snapshot=[],
    )
    defaults.update(kwargs)
    return GenerationSetup(**defaults)


def test_guard_rule_id_extraction():
    assert guard_rule_id("Non-negotiable violation [no_ai_self_identification]: matched") == "no_ai_self_identification"
    assert guard_rule_id("[tts-sanitized] violation [bad_word]: matched") == "bad_word"
    assert guard_rule_id("plain reason without brackets") == "unknown_rule"


def test_output_guard_with_tts_check():
    mock_guard = MagicMock(return_value=(True, ""))
    allowed, reason = output_guard_with_tts_check("Hola mundo", source="direct", output_guard_fn=mock_guard)
    assert allowed is True
    assert reason == ""
    assert mock_guard.call_count == 2

    # If raw check fails, second check is skipped
    mock_guard_fail = MagicMock(return_value=(False, "[rule_x] blocked"))
    allowed, reason = output_guard_with_tts_check("Texto malo", source="direct", output_guard_fn=mock_guard_fail)
    assert allowed is False
    assert reason == "[rule_x] blocked"
    assert mock_guard_fail.call_count == 1


def test_evaluator_normalizes_text_and_computes_telemetry():
    evaluator = GenerationEvaluator()
    setup = _make_setup(start_llm=0.0)

    class MockResp:
        prompt_eval_count = 1000
        prompt_eval_duration = 50_000_000   # 50ms
        eval_duration = 100_000_000         # 100ms
        load_duration = 20_000_000          # 20ms
        eval_count = 50

    res = evaluator.evaluate(
        "\x00  \ufeffHola mundo!   ",
        MockResp(),
        setup,
        source="direct",
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        desired_model="llama3",
        current_model="llama3",
        loaded_model="llama3",
        current_profile_name="default",
    )

    assert res.dialogo == "Hola mundo!"
    assert res.telemetry is not None
    assert res.telemetry.pec == 1000
    assert res.telemetry.prefill_ms == 50.0
    assert res.telemetry.decode_ms == 100.0
    assert res.telemetry.load_ms == 20.0
    assert res.telemetry.eval_count == 50
    assert res.telemetry.pressure_high is False
    assert res.telemetry.snapshot["model"] == "llama3"


def test_evaluator_detects_model_mismatch():
    evaluator = GenerationEvaluator()
    setup = _make_setup()

    res = evaluator.evaluate(
        "Respuesta",
        None,
        setup,
        source="direct",
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        desired_model="llama3",
        current_model="mistral",  # mismatch!
        loaded_model="mistral",
        current_profile_name="default",
    )

    assert res.model_trace.is_mismatch is True
    assert "desired=llama3" in res.model_trace.trace_msg
    assert "active=mistral" in res.model_trace.trace_msg


def test_evaluator_runs_agenda_sanitizer_and_transformer():
    evaluator = GenerationEvaluator()
    setup = _make_setup()

    sanitizer = lambda text: text.replace("AGENDA_RAW:", "").strip()
    transformer = lambda text: f"TRANSFORMED: {text}"

    res = evaluator.evaluate(
        "AGENDA_RAW: Mi tema del dia",
        None,
        setup,
        source="kira-agenda",
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        desired_model="llama3",
        current_model="llama3",
        loaded_model="llama3",
        current_profile_name="default",
        agenda_output_sanitizer=sanitizer,
        agenda_output_transformer=transformer,
    )

    assert res.dialogo == "TRANSFORMED: Mi tema del dia"


def test_evaluator_detects_chat_repetition():
    evaluator = GenerationEvaluator(
        guardrail_fallback_fn=lambda src, reason: "Fallback neutral line."
    )
    recent_history = [
        {"role": "user", "content": "hola"},
        {"role": "assistant", "content": "¡Hola streamer! ¿Cómo estás hoy?"},
    ]
    setup = _make_setup(history_snapshot=recent_history)

    # Identical repeated response in chat
    res = evaluator.evaluate(
        "¡Hola streamer! ¿Cómo estás hoy?",
        None,
        setup,
        source="chat",
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        desired_model="llama3",
        current_model="llama3",
        loaded_model="llama3",
        current_profile_name="default",
    )

    assert res.repetition_verdict is not None
    assert res.repetition_verdict.is_repetitive is True
    assert evaluator.resolve_fallback_line("chat") == "Fallback neutral line."


def test_evaluator_repairs_clause_repetition_before_guardrails():
    evaluator = GenerationEvaluator()
    setup = _make_setup()

    # Repetitive clause text
    raw_text = "No había roadmap, no había monetización, no había roadmap, no había roadmap."
    res = evaluator.evaluate(
        raw_text,
        None,
        setup,
        source="chat",
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        desired_model="llama3",
        current_model="llama3",
        loaded_model="llama3",
        current_profile_name="default",
        is_streamed=False,
    )

    assert res.clause_verdict is not None
    assert res.clause_verdict.verdict in ("repaired", "rejected")
    assert res.dialogo != raw_text


def test_evaluator_skips_static_guard_when_streamed():
    mock_guard = MagicMock(return_value=(False, "[blocked] bad word"))
    evaluator = GenerationEvaluator(output_guard_fn=mock_guard)
    setup = _make_setup()

    res = evaluator.evaluate(
        "Texto de prueba",
        None,
        setup,
        source="direct",
        is_local=True,
        provider_cfg={},
        request_model="llama3",
        desired_model="llama3",
        current_model="llama3",
        loaded_model="llama3",
        current_profile_name="default",
        is_streamed=True,
    )

    assert res.guard_allowed is True
    assert res.guard_reason == ""
    mock_guard.assert_not_called()
