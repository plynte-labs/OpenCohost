"""Focused tests for LLM/TTS timeout coordination."""

import queue
import threading
from unittest.mock import MagicMock

from core import llm_engine
from config.settings import TTS_HEAVY_TIMEOUT, TTS_LIGHT_TIMEOUT


def test_audio_queue_timeout_waits_for_heavy_tts_plus_margin():
    """The consumer must not time out before a heavy TTS request can finish."""
    assert llm_engine.TTS_AUDIO_QUEUE_TIMEOUT > TTS_HEAVY_TIMEOUT
    assert llm_engine.TTS_AUDIO_QUEUE_TIMEOUT == max(TTS_HEAVY_TIMEOUT, TTS_LIGHT_TIMEOUT) + 15


def test_prepare_model_warms_selected_ollama_model():
    events = []
    motor = llm_engine.MotorVocalIA(queue.Queue(), events.append)
    motor.is_ready = True
    motor.ollama = MagicMock()

    assert motor._prepare_model("llama3") is True

    motor.ollama.generate.assert_called_once()
    call = motor.ollama.generate.call_args.kwargs
    assert call["model"] == "llama3"
    assert call["keep_alive"] == -1
    assert call["options"]["num_predict"] == 1
    assert motor._warmed_model == "llama3"
    assert "model_warming" in events
    assert events[-1] == "ready"


def test_check_ollama_service_warms_current_model_after_service_ready():
    events = []
    motor = llm_engine.MotorVocalIA(queue.Queue(), events.append)
    motor.current_model = "llama3"
    motor.ollama = MagicMock()

    assert motor._check_ollama_service() is True

    motor.ollama.list.assert_called_once()
    motor.ollama.generate.assert_called_once()
    assert motor._warmed_model == "llama3"


def test_replace_pending_keeps_latest_item_for_same_source():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)

    motor.enqueue("old agenda", priority=2, source="kira-agenda")
    motor.enqueue("ptt input", priority=0, source="ptt")
    motor.replace_pending("new agenda", priority=2, source="kira-agenda")

    queued = [(item[2], item[3]) for item in motor._priority_queue]
    assert ("old agenda", "kira-agenda") not in queued
    assert ("new agenda", "kira-agenda") in queued
    assert ("ptt input", "ptt") in queued


def test_drop_pending_sources_removes_agenda_without_dropping_ptt_or_chat():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.enqueue("agenda", priority=2, source="kira-agenda")
    motor.enqueue("closing", priority=2, source="kira-agenda-stop")
    motor.enqueue("chat", priority=1, source="chat")
    motor.enqueue("ptt", priority=0, source="ptt")
    motor.enqueue_accumulation("old agenda overflow", source="kira-agenda")
    motor.enqueue_accumulation("old chat overflow", source="chat")

    removed = motor.drop_pending_sources(("kira-agenda",))

    assert removed == 3
    assert all(not item[3].startswith("kira-agenda") for item in motor._priority_queue)
    assert {item[3] for item in motor._priority_queue} == {"ptt", "chat"}
    assert [item[1] for item in motor._accumulation_buffer] == ["old chat overflow"]


def test_pending_ptt_or_chat_can_pause_cached_agenda_prefetch():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.enqueue("agenda", priority=2, source="kira-agenda")

    assert motor.has_pending_priority_before(2) is False

    motor.enqueue("chat pulse", priority=1, source="chat")
    assert motor.has_pending_priority_before(2) is True

    motor.enqueue("ptt input", priority=0, source="ptt")
    assert motor.has_pending_priority_before(1) is True


def test_agenda_output_sanitizer_replaces_artificial_closings():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)

    sanitized = motor._sanitize_agenda_output("Y eso es todo. ¡Hasta luego, próximo episodio!")

    assert "Hasta luego" not in sanitized
    assert "próximo episodio" not in sanitized
    assert "matices" in sanitized


def test_agenda_prefetch_generates_text_without_speaking_until_consumed():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    spoken = []
    spoke = threading.Event()
    motor._generar_dialogo = MagicMock(return_value="Texto cacheado")

    def fake_hablar(text, source="direct"):
        spoken.append(text)
        spoke.set()

    motor._hablar = fake_hablar

    assert motor.prefetch_agenda("prompt agenda", source="kira-agenda") is True
    assert motor.wait_prefetched_agenda(timeout=1.0) is True
    assert spoken == []

    assert motor.play_prefetched_agenda() is True
    assert spoke.wait(1.0) is True
    assert spoken == ["Texto cacheado"]
    assert list(motor.historial)[-2:] == [
        {"role": "user", "content": "[agenda segura: prompt interno omitido]"},
        {"role": "assistant", "content": "Texto cacheado"},
    ]


def test_agenda_prefetch_is_cleared_when_agenda_pending_is_replaced():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._prefetched_agenda = {"payload": "old", "dialogo": "viejo", "source": "kira-agenda", "priority": 2}

    motor.replace_pending("nuevo", priority=2, source="kira-agenda")

    assert motor._prefetched_agenda is None


def test_agenda_generation_uses_controller_guardrail_before_history_or_speech():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor.use_system_role = True
    motor.ollama = MagicMock()
    motor.ollama.chat.return_value = {"message": {"content": "Según el resumen, el chat dice que..."}}
    motor.agenda_output_validator = MagicMock(return_value=False)

    dialogo = motor._generar_dialogo("CHAT COMPACTO FILTRADO: secreto", source="kira-agenda", commit_history=True)

    assert dialogo == ""
    motor.agenda_output_validator.assert_called_once()
    assert list(motor.historial) == []


def test_agenda_output_transformer_caps_before_history_commit():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor.use_system_role = True
    motor.ollama = MagicMock()
    motor.ollama.chat.return_value = {"message": {"content": "uno dos tres cuatro"}}
    motor.agenda_output_transformer = MagicMock(return_value="uno dos")

    dialogo = motor._generar_dialogo("prompt interno", source="kira-agenda", commit_history=True)

    assert dialogo == "uno dos"
    motor.agenda_output_transformer.assert_called_once_with("uno dos tres cuatro")
    assert list(motor.historial)[-1] == {"role": "assistant", "content": "uno dos"}


def test_agenda_history_redacts_raw_compact_prompt_when_committed():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)

    motor._commit_history("CHAT COMPACTO FILTRADO: usuario dice algo", "Salida segura", source="kira-agenda")

    history_text = "\n".join(item["content"] for item in motor.historial)
    assert "CHAT COMPACTO FILTRADO" not in history_text
    assert "usuario dice algo" not in history_text
    assert "Salida segura" in history_text
