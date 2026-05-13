"""Focused tests for LLM/TTS timeout coordination."""

import queue
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
