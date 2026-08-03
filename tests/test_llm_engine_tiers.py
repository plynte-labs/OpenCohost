import queue
from unittest.mock import MagicMock
from unittest.mock import patch

from opencohost.config.settings import LLM_TIER_EFFECTIVE_CTX_CAPS
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.core.providers.llm_tiers import LLMTierConfig
from opencohost.i18n import active as i18n_active


def _ready_motor():
    events = []
    motor = MotorVocalIA(queue.Queue(), events.append)
    motor.is_ready = True
    motor.ollama = MagicMock()
    motor._loaded_model = motor.current_model
    return motor, events


def test_manual_tier_switch_routes_next_request_to_selected_model():
    motor, events = _ready_motor()
    motor.configure_llm_tiers(
        LLMTierConfig(quality="llama3", balanced="qwen3:4b", fast="qwen3:1.7b")
    )
    motor.current_model = "llama3"
    motor.ollama.chat.return_value = {"message": {"content": "respuesta rapida"}}

    assert motor.switch_llm_tier("fast") is True
    dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == "respuesta rapida"
    assert motor.current_model == "qwen3:1.7b"
    assert motor.active_llm_tier == "fast"
    assert motor.ollama.chat.call_args.kwargs["model"] == "qwen3:1.7b"
    assert "llm_tier_switch_applied" in events


def test_failed_tier_switch_keeps_previous_active_model_and_tier():
    motor, events = _ready_motor()
    motor.configure_llm_tiers(
        LLMTierConfig(quality="llama3", balanced="qwen3:4b", fast="missing-model")
    )
    motor.current_model = "qwen3:4b"
    motor._desired_model = "qwen3:4b"
    motor._loaded_model = "qwen3:4b"
    motor._warmed_model = "qwen3:4b"
    motor._owns_ollama_model = True
    motor.active_llm_tier = "balanced"
    motor.ollama.generate.side_effect = RuntimeError("not found")

    assert motor.switch_llm_tier("fast") is False

    assert motor.current_model == "qwen3:4b"
    assert motor.active_llm_tier == "balanced"
    assert motor._desired_model == "qwen3:4b"
    assert motor._loaded_model == "qwen3:4b"
    assert motor._warmed_model == "qwen3:4b"
    assert motor._owns_ollama_model is True
    assert motor._last_switch_failure["requested_tier"] == "fast"
    assert motor._last_switch_failure["current"] == "qwen3:4b"
    assert "llm_tier_switch_failed" in events


def test_same_tier_switch_is_noop_without_prepare_or_callback():
    motor, events = _ready_motor()
    motor.configure_llm_tiers(
        LLMTierConfig(quality="gemma4:e4b", balanced="llama3", fast="qwen3:1.7b"),
        active_tier="quality",
    )
    motor.current_model = "gemma4:e4b"
    motor._desired_model = "gemma4:e4b"
    motor._loaded_model = "gemma4:e4b"
    motor._warmed_model = "gemma4:e4b"

    with patch.object(motor, "_prepare_model") as prepare_model, \
        patch("opencohost.core.llm_engine.save_last_model") as save_last_model, \
        patch.object(motor, "_log") as log:
        assert motor.switch_llm_tier("quality") is True

    prepare_model.assert_not_called()
    save_last_model.assert_not_called()
    log.assert_not_called()
    assert events == []


def test_model_switch_to_active_model_is_noop_without_warm_or_callback():
    motor, events = _ready_motor()
    motor.current_model = "gemma4:e4b"
    motor._desired_model = "gemma4:e4b"
    motor._loaded_model = "gemma4:e4b"
    motor._warmed_model = "gemma4:e4b"

    with patch.object(motor, "_switch_and_prepare_model") as switch_and_prepare, \
        patch("opencohost.core.llm_engine.save_last_model") as save_last_model:
        assert motor._apply_model_switch("gemma4:e4b") is True

    switch_and_prepare.assert_not_called()
    save_last_model.assert_not_called()
    assert "model_switch_applied" not in events


def test_tier_switch_preserves_profile_prompt_and_conversation_memory():
    motor, _ = _ready_motor()
    motor.configure_llm_tiers(
        LLMTierConfig(quality="llama3", balanced="qwen3:4b", fast="qwen3:1.7b")
    )
    motor.current_model = "llama3"
    motor.use_system_role = True
    motor.system_prompt = "Prompt de Kira"
    motor.historial.append({"role": "user", "content": "recuerdo previo"})
    motor.historial.append({"role": "assistant", "content": "respuesta previa"})
    motor.ollama.chat.return_value = {"message": {"content": "nueva respuesta"}}

    assert motor.switch_llm_tier("fast") is True
    dialogo = motor._generar_dialogo("nuevo mensaje", source="direct", commit_history=True)

    messages = motor.ollama.chat.call_args.kwargs["messages"]
    assert dialogo == "nueva respuesta"
    # The profile prompt survives the tier switch verbatim and stays FIRST in the
    # system message. grounding_authority_temporal_humility appends the engine's
    # always-present grounding rules after it (llm_engine._generar_dialogo), so
    # the expected system content is composed rather than a bare literal — still
    # an exact equality, no substring softening.
    expected_system = f"Prompt de Kira\n\n{i18n_active.grounding_rules()}"
    assert messages[:3] == [
        {"role": "system", "content": expected_system},
        {"role": "user", "content": "recuerdo previo"},
        {"role": "assistant", "content": "respuesta previa"},
    ]


def test_generation_captures_model_at_request_start():
    motor, _ = _ready_motor()
    motor.configure_llm_tiers(
        LLMTierConfig(quality="llama3", balanced="qwen3:4b", fast="qwen3:1.7b")
    )
    motor.current_model = "llama3"
    captured_models = []

    def fake_chat(**kwargs):
        captured_models.append(kwargs["model"])
        motor.current_model = "qwen3:1.7b"
        motor.active_llm_tier = "fast"
        return {"message": {"content": "respuesta"}}

    motor.ollama.chat.side_effect = fake_chat

    assert (
        motor._generar_dialogo("hola", source="direct", commit_history=False)
        == "respuesta"
    )

    assert captured_models == ["llama3"]


def test_qwen3_and_gemma_e_models_do_not_use_fixed_token_budget():
    assert MotorVocalIA._uses_reasoning_token_budget("qwen3:1.7b") is True
    assert MotorVocalIA._uses_reasoning_token_budget("qwen3:4b") is True
    assert MotorVocalIA._uses_reasoning_token_budget("gemma4:e4b") is True
    assert MotorVocalIA._uses_reasoning_token_budget("llama3") is False


def test_fast_qwen_native_ctx_is_clamped_to_effective_cap_for_options_and_budget():
    motor, _ = _ready_motor()
    motor.configure_llm_tiers(
        LLMTierConfig(quality="gemma4:e4b", balanced="llama3", fast="qwen3:1.7b"),
        active_tier="fast",
    )
    motor.current_model = "qwen3:1.7b"
    motor._model_ctx_limit["qwen3:1.7b"] = 40960
    motor.ollama.chat.return_value = {"message": {"content": "respuesta rapida"}}

    captured_budget = {}
    from opencohost.core.context import context_budget
    real_apply = context_budget.apply_char_budget

    def spy_apply(messages, *, ctx_limit, max_output_tokens, safety_factor):
        captured_budget["ctx_limit"] = ctx_limit
        return real_apply(
            messages,
            ctx_limit=ctx_limit,
            max_output_tokens=max_output_tokens,
            safety_factor=safety_factor,
        )

    with patch("opencohost.core.llm_engine.context_budget.apply_char_budget", side_effect=spy_apply):
        assert motor._generar_dialogo("hola", source="direct", commit_history=False) == "respuesta rapida"

    assert motor._model_ctx_limit["qwen3:1.7b"] == 40960
    assert captured_budget["ctx_limit"] == LLM_TIER_EFFECTIVE_CTX_CAPS["fast"]
    assert motor.ollama.chat.call_args.kwargs["options"]["num_ctx"] == LLM_TIER_EFFECTIVE_CTX_CAPS["fast"]


def test_gemma_quality_still_omits_num_ctx_when_effective_cap_exists():
    motor, _ = _ready_motor()
    motor.configure_llm_tiers(
        LLMTierConfig(quality="gemma4:e4b", balanced="llama3", fast="qwen3:1.7b"),
        active_tier="quality",
    )
    motor.current_model = "gemma4:e4b"
    motor._model_ctx_limit["gemma4:e4b"] = 131072
    motor.ollama.chat.return_value = {"message": {"content": "respuesta quality"}}

    assert motor._generar_dialogo("hola", source="direct", commit_history=False) == "respuesta quality"

    assert "num_ctx" not in motor.ollama.chat.call_args.kwargs["options"]
