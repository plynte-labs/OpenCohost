"""Focused tests for LLM/TTS timeout coordination."""

from core import llm_engine
from config.settings import TTS_HEAVY_TIMEOUT, TTS_LIGHT_TIMEOUT


def test_audio_queue_timeout_waits_for_heavy_tts_plus_margin():
    """The consumer must not time out before a heavy TTS request can finish."""
    assert llm_engine.TTS_AUDIO_QUEUE_TIMEOUT > TTS_HEAVY_TIMEOUT
    assert llm_engine.TTS_AUDIO_QUEUE_TIMEOUT == max(TTS_HEAVY_TIMEOUT, TTS_LIGHT_TIMEOUT) + 15
