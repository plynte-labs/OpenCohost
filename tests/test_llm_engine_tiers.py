import queue
from unittest.mock import MagicMock

from core.llm_engine import MotorVocalIA
from core.llm_tiers import LLMTierConfig


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
    assert messages[:3] == [
        {"role": "system", "content": "Prompt de Kira"},
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
