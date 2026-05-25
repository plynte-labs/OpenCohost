"""Focused tests for LLM/TTS timeout coordination."""

import logging
import queue
import threading
import time
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


def test_ollama_chat_client_uses_configured_timeout():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    created = {}

    class FakeOllamaModule:
        @staticmethod
        def Client(**kwargs):
            created.update(kwargs)
            return MagicMock()

    client = motor._create_ollama_chat_client(FakeOllamaModule)

    assert client is not None
    assert created == {"timeout": llm_engine.OLLAMA_CHAT_TIMEOUT}


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
    motor.agenda_output_preview_validator = MagicMock(return_value=True)
    motor.agenda_output_recorder = MagicMock()

    def fake_hablar(text, source="direct"):
        spoken.append(text)
        spoke.set()

    motor._hablar = fake_hablar

    assert motor.prefetch_agenda("prompt agenda", source="kira-agenda") is True
    assert motor.wait_prefetched_agenda(timeout=1.0) is True
    motor.agenda_output_preview_validator.assert_called_once_with("Texto cacheado")
    assert spoken == []

    assert motor.play_prefetched_agenda() is True
    assert spoke.wait(1.0) is True
    assert spoken == ["Texto cacheado"]
    motor.agenda_output_recorder.assert_called_once_with("Texto cacheado")
    assert list(motor.historial)[-2:] == [
        {"role": "user", "content": "[agenda segura: prompt interno omitido]"},
        {"role": "assistant", "content": "Texto cacheado"},
    ]


def test_agenda_prefetch_rejects_repeated_text_before_caching():
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._generar_dialogo = MagicMock(return_value="Texto repetido")
    motor.agenda_output_preview_validator = MagicMock(return_value=False)

    assert motor.prefetch_agenda("prompt agenda", source="kira-agenda") is True
    assert motor.wait_prefetched_agenda(timeout=1.0) is False
    assert motor.play_prefetched_agenda() is False


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


def test_ollama_chat_timeout_is_logged_and_returns_empty(caplog):
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor.use_system_role = True
    motor.ollama = MagicMock()
    motor.ollama.chat.side_effect = TimeoutError("chat stalled")

    with caplog.at_level(logging.WARNING, logger="VoiceAI"):
        dialogo = motor._generar_dialogo("hola", source="direct", commit_history=True)

    assert dialogo == ""
    assert motor.ollama.chat.call_count == 1
    assert motor._last_llm_failure == {
        "model": "llama3",
        "source": "direct",
        "attempt": 1,
        "reason": "TimeoutError",
        "message": "chat stalled",
    }
    assert any("ERROR Ollama chat (TimeoutError)" in item for item in list(motor.log_queue.queue))
    assert any("Ollama chat transport failure" in record.message for record in caplog.records)
    assert list(motor.historial) == []


def test_ollama_chat_connection_error_is_logged_and_returns_empty(caplog):
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor.use_system_role = True
    motor.ollama = MagicMock()
    motor.ollama.chat.side_effect = ConnectionError("ollama refused")

    with caplog.at_level(logging.WARNING, logger="VoiceAI"):
        dialogo = motor._generar_dialogo("hola", source="chat", commit_history=True)

    assert dialogo == ""
    assert motor.ollama.chat.call_count == 1
    assert motor._last_llm_failure["reason"] == "ConnectionError"
    assert motor._last_llm_failure["message"] == "ollama refused"
    assert any("ERROR Ollama chat (ConnectionError)" in item for item in list(motor.log_queue.queue))
    assert any("Ollama chat transport failure" in record.message for record in caplog.records)
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


def test_ptt_priority_wins_over_chat_and_agenda():
    """PTT (priority 0) must always be processed before chat (1) and agenda (2)."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)

    motor.enqueue("agenda topic", priority=2, source="kira-agenda")
    motor.enqueue("chat comment", priority=1, source="chat")
    motor.enqueue("ptt input", priority=0, source="ptt")

    # Queue is sorted by priority ascending: PTT first
    items = motor._priority_queue
    assert items[0][3] == "ptt"
    assert items[0][0] == 0
    assert items[1][3] == "chat"
    assert items[1][0] == 1
    assert items[2][3] == "kira-agenda"
    assert items[2][0] == 2


def test_overflow_drops_lowest_priority_preserving_ptt():
    """When queue exceeds max, lowest priority items are dropped first."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._pq_max_items = 3

    motor.enqueue("agenda 1", priority=2, source="kira-agenda")
    motor.enqueue("agenda 2", priority=2, source="kira-agenda")
    motor.enqueue("chat 1", priority=1, source="chat")
    motor.enqueue("ptt 1", priority=0, source="ptt")

    # Should have 3 items: PTT, chat, and one agenda (the newest agenda)
    sources = [item[3] for item in motor._priority_queue]
    assert "ptt" in sources
    assert "chat" in sources
    # One agenda should remain (the one that wasn't dropped)
    assert sources.count("kira-agenda") == 1
    assert len(motor._priority_queue) == 3


def test_overflow_drops_agenda_before_chat():
    """Agenda (priority 2) must be dropped before chat (priority 1)."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._pq_max_items = 3

    motor.enqueue("agenda 1", priority=2, source="kira-agenda")
    motor.enqueue("chat 1", priority=1, source="chat")
    motor.enqueue("chat 2", priority=1, source="chat")
    motor.enqueue("ptt 1", priority=0, source="ptt")

    sources = [item[3] for item in motor._priority_queue]
    assert "ptt" in sources
    # Both chats should survive over agenda
    assert sources.count("chat") == 2
    assert sources.count("kira-agenda") <= 1


def test_stale_chat_expires_before_processing():
    """Expired chat should be discarded, not reacted to later as accumulation."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._pq_ttl_seconds = 0.01  # 10ms TTL for fast test
    motor._ejecutar_inferencia = MagicMock()

    motor.enqueue("old chat", priority=1, source="chat")
    time.sleep(0.02)  # Wait for TTL to expire

    motor._processing = False
    motor._speaking = False
    motor._process_priority_queue()

    assert motor._priority_queue == []
    assert motor._accumulation_buffer == []
    motor._ejecutar_inferencia.assert_not_called()


def test_accumulation_expiry_logs_count_without_raw_payload(caplog):
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._accum_ttl = 1.0
    raw_expired_payload = "RAW_EXPIRED_CHAT_SECRET"
    raw_fresh_payload = "RAW_FRESH_CHAT_SECRET"
    raw_source = "secret-source"

    now = time.time()
    motor._accumulation_buffer = [
        (now - 2.0, raw_expired_payload, raw_source),
        (now, raw_fresh_payload, raw_source),
    ]

    with caplog.at_level(logging.WARNING, logger="VoiceAI"):
        accumulated = motor._flush_accumulation()

    log_text = "\n".join(
        [record.message for record in caplog.records] + list(motor.log_queue.queue)
    )
    assert "descartados 1 mensajes expirados" in log_text
    assert raw_expired_payload not in log_text
    assert raw_fresh_payload not in log_text
    assert raw_source not in log_text
    assert raw_fresh_payload in accumulated
    assert motor._last_accumulation_flush_count == 1


def test_accumulation_item_overflow_logs_count_without_raw_payload(caplog):
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._accum_max_items = 1
    raw_first_payload = "RAW_ITEM_OVERFLOW_SECRET_1"
    raw_second_payload = "RAW_ITEM_OVERFLOW_SECRET_2"
    raw_source = "secret-source"

    with caplog.at_level(logging.WARNING, logger="VoiceAI"):
        motor.enqueue_accumulation(raw_first_payload, source=raw_source)
        motor.enqueue_accumulation(raw_second_payload, source=raw_source)

    log_text = "\n".join(
        [record.message for record in caplog.records] + list(motor.log_queue.queue)
    )
    assert "descartados 1 mensajes por límite de items" in log_text
    assert raw_first_payload not in log_text
    assert raw_second_payload not in log_text
    assert raw_source not in log_text


def test_accumulation_char_overflow_logs_count_without_raw_payload(caplog):
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._accum_max_chars = 5
    raw_payload = "RAW_CHAR_OVERFLOW_SECRET"
    raw_source = "secret-source"

    with caplog.at_level(logging.WARNING, logger="VoiceAI"):
        motor.enqueue_accumulation(raw_payload, source=raw_source)

    log_text = "\n".join(
        [record.message for record in caplog.records] + list(motor.log_queue.queue)
    )
    assert "descartados 1 mensajes por límite de caracteres" in log_text
    assert raw_payload not in log_text
    assert raw_source not in log_text


def test_ptt_items_never_expire_via_ttl():
    """PTT (priority 0) items must not be expired by TTL check."""
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor._pq_ttl_seconds = 0.01
    motor._ejecutar_inferencia = MagicMock()

    motor.enqueue("ptt important", priority=0, source="ptt")
    time.sleep(0.02)

    motor._processing = False
    motor._speaking = False
    motor._process_priority_queue()

    motor._ejecutar_inferencia.assert_called_once_with("ptt important", source="ptt")
