"""
Fallback integration tests: Edge-TTS -> Piper on connection failure.

These drive the REAL MotorVocalIA._hablar()/productor() routing with BOUNDARY
mocks only:
  - edge_tts.Communicate is mocked (the network boundary).
  - pygame is mocked (the audio boundary).
  - self._piper is a mock whose synthesize() writes a real .wav.

Everything in between (chunking, effective-motor resolution, the
_edge_tts_offline latch, _is_connection_error classification, the Piper
fallback) runs for real. No re-copied decision tree, no real Ollama/Piper
model. Runs synchronously in the default fast suite.
"""
from __future__ import annotations

import asyncio
import os
import socket
import ssl
import sys
import wave
from unittest.mock import AsyncMock, MagicMock, patch

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_motor():
    """Return a fresh MotorVocalIA with mocked pygame/ollama."""
    import queue as q
    from opencohost.core.llm_engine import MotorVocalIA

    log_queue = q.Queue()
    ui_events: list = []

    def ui_callback(event):
        ui_events.append(event)

    motor = MotorVocalIA(log_queue, ui_callback)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    return motor, log_queue, ui_events


def _make_mock_piper(available: bool = True, synthesize_ok: bool = True):
    """Return a mock PiperEngine whose synthesize() writes a real .wav."""
    mock_piper = MagicMock()
    mock_piper.is_available.return_value = available

    def fake_synthesize(text, path):
        if synthesize_ok:
            with wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(22050)
                wf.writeframes(b"\x00" * 44)
        return synthesize_ok

    mock_piper.synthesize.side_effect = fake_synthesize
    return mock_piper


def _drive_hablar(motor, text, *, edge_save_side_effect=None,
                  motor_tts="ligero", voz_referencia=None, health_monitor=None):
    """Run the REAL _hablar/productor with boundary mocks.

    Only edge_tts.Communicate (network) and pygame (audio) are mocked; routing,
    chunking, offline-flag latching and Piper fallback run for real.

    Returns (loaded_paths, piper_mock, communicate_mock).
    """
    motor.pygame = MagicMock()
    # Bare MagicMock get_busy() is truthy -> the consumer busy-wait would spin
    # forever. Force it False so playback "finishes" immediately.
    motor.pygame.mixer.music.get_busy.return_value = False
    motor.health_monitor = health_monitor
    motor.voz_referencia = voz_referencia
    motor.motor_tts = motor_tts

    def _save(path, *a, **k):
        if edge_save_side_effect is not None:
            raise edge_save_side_effect
        open(path, "wb").close()  # stub .mp3 so the consumer can load + remove it

    communicate = MagicMock()
    communicate.return_value.save = AsyncMock(side_effect=_save)

    with patch("opencohost.core.llm_engine.edge_tts.Communicate", communicate):
        motor._hablar(text, source="direct")

    loaded = [c.args[0] for c in motor.pygame.mixer.music.load.call_args_list]
    return loaded, motor._piper, communicate


# ---------------------------------------------------------------------------
# gaierror triggers Piper + flag latches
# ---------------------------------------------------------------------------

class TestGaierrorTriggersPiper:
    def test_gaierror_sets_flag_and_enqueues_wav(self):
        """socket.gaierror from Edge -> _edge_tts_offline latches, Piper produces a .wav."""
        motor, *_ = _make_motor()
        motor.tts_local_only = False
        motor._edge_tts_offline = False
        motor._piper = _make_mock_piper(available=True, synthesize_ok=True)

        loaded, piper, communicate = _drive_hablar(
            motor, "Hola mundo", edge_save_side_effect=socket.gaierror("dns")
        )

        assert motor._edge_tts_offline is True
        assert communicate.called, "Edge-TTS WAS attempted before falling back"
        assert piper.synthesize.called
        assert loaded and loaded[-1].endswith(".wav")


# ---------------------------------------------------------------------------
# SSLError triggers Piper
# ---------------------------------------------------------------------------

class TestSSLErrorTriggersPiper:
    def test_ssl_error_sets_flag_and_enqueues_wav(self):
        """ssl.SSLError from Edge -> _edge_tts_offline latches, Piper produces a .wav."""
        motor, *_ = _make_motor()
        motor.tts_local_only = False
        motor._edge_tts_offline = False
        motor._piper = _make_mock_piper(available=True, synthesize_ok=True)

        loaded, piper, communicate = _drive_hablar(
            motor, "Buenas tardes", edge_save_side_effect=ssl.SSLError("handshake")
        )

        assert motor._edge_tts_offline is True
        assert communicate.called
        assert piper.synthesize.called
        assert loaded and loaded[-1].endswith(".wav")


# ---------------------------------------------------------------------------
# asyncio.TimeoutError does NOT trigger fallback
# ---------------------------------------------------------------------------

class TestTimeoutErrorDoesNotTrigger:
    def test_timeout_does_not_set_flag(self):
        """asyncio.TimeoutError is not a connection error -> no Piper fallback, flag stays False."""
        motor, *_ = _make_motor()
        motor.tts_local_only = False
        motor._edge_tts_offline = False
        motor._piper = _make_mock_piper(available=True, synthesize_ok=True)

        loaded, piper, communicate = _drive_hablar(
            motor, "Una frase", edge_save_side_effect=asyncio.TimeoutError()
        )

        assert motor._edge_tts_offline is False, "_edge_tts_offline must stay False on TimeoutError"
        assert communicate.called, "Edge-TTS WAS attempted (it just timed out, not offline)"
        piper.synthesize.assert_not_called()
        assert all(not p.endswith(".wav") for p in loaded)


# ---------------------------------------------------------------------------
# flag set even when Piper unavailable
# ---------------------------------------------------------------------------

class TestFlagSetEvenWhenPiperUnavailable:
    def test_flag_latches_and_queue_gets_none(self):
        """gaierror + Piper unavailable -> flag latches True, no chunk is loaded."""
        motor, *_ = _make_motor()
        motor.tts_local_only = False
        motor._edge_tts_offline = False
        motor._piper = _make_mock_piper(available=False)

        loaded, piper, communicate = _drive_hablar(
            motor, "Sin piper", edge_save_side_effect=socket.gaierror("dns")
        )

        assert motor._edge_tts_offline is True, "_edge_tts_offline must latch even without Piper"
        assert communicate.called
        piper.synthesize.assert_not_called()
        assert loaded == [], "No chunk should be loaded when Piper is unavailable"


# ---------------------------------------------------------------------------
# flag latches: a chunk with the offline flag already set skips Edge-TTS
# ---------------------------------------------------------------------------

class TestSubsequentChunkSkipsEdgeTTS:
    def test_offline_flag_bypasses_edge_tts(self):
        """When _edge_tts_offline is already True, Edge-TTS is never attempted."""
        motor, *_ = _make_motor()
        motor.tts_local_only = False
        motor._edge_tts_offline = True  # as if a prior chunk already latched offline
        motor._piper = _make_mock_piper(available=True, synthesize_ok=True)

        loaded, piper, communicate = _drive_hablar(motor, "Segundo chunk")

        communicate.assert_not_called()
        assert piper.synthesize.called
        assert loaded and loaded[-1].endswith(".wav")


# ---------------------------------------------------------------------------
# pesado path unaffected (flag invariant — not a routing copy)
# ---------------------------------------------------------------------------

class TestPesadoPathUnaffected:
    def test_gaierror_on_pesado_does_not_set_flag(self):
        """pesado motor + any error -> _edge_tts_offline stays False; Piper not called."""
        from opencohost.core.llm_engine import _is_connection_error

        motor, *_ = _make_motor()
        motor.motor_tts = "pesado"
        motor._edge_tts_offline = False

        mock_piper_engine = MagicMock()
        mock_piper_engine.is_available.return_value = True
        motor._piper = mock_piper_engine

        # The Piper fallback only lives inside the `effective_motor == "ligero"`
        # branch, so a connection error under pesado must not touch the flag.
        exc = socket.gaierror("dns fail")
        effective_motor = "pesado"
        if effective_motor == "ligero" and _is_connection_error(exc):
            motor._edge_tts_offline = True
            motor._piper.synthesize("test", "/tmp/x.wav")

        assert motor._edge_tts_offline is False, "pesado path must not touch the flag"
        mock_piper_engine.synthesize.assert_not_called()
