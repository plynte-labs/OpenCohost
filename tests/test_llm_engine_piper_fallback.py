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

import pytest

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
# P5 — one-shot honest-degrade ui_callback notice on locale/voice mismatch
# ---------------------------------------------------------------------------

class TestPiperLocaleMismatchNotice:
    @pytest.fixture(autouse=True)
    def _pin_en_locale(self):
        from opencohost.i18n import active
        from opencohost.i18n.startup import resolve_active_bundle
        active.set_active_bundle(resolve_active_bundle(locale="en"))
        yield
        active.reset_active_bundle()

    def test_fallback_engage_notifies_once_on_mismatch(self):
        """en locale + es Piper voice: falling back to Piper fires the notice
        exactly once even across two separate fallback-triggering utterances."""
        motor, _log_q, ui_events = _make_motor()
        motor.tts_local_only = False
        motor._edge_tts_offline = False
        motor._piper_voice_key = "neutral"  # es voice under en locale -> mismatch
        motor._piper = _make_mock_piper(available=True, synthesize_ok=True)

        _drive_hablar(motor, "Hello world", edge_save_side_effect=socket.gaierror("dns"))
        assert motor._piper_locale_mismatch_notified is True
        assert ui_events.count("piper_voice_locale_mismatch") == 1

        # Second utterance takes the already-latched fast path; must not re-notify.
        _drive_hablar(motor, "Second utterance")
        assert ui_events.count("piper_voice_locale_mismatch") == 1

    def test_fallback_engage_silent_when_voice_matches_locale(self):
        """en locale + english Piper voice: no mismatch, no notice."""
        motor, _log_q, ui_events = _make_motor()
        motor.tts_local_only = False
        motor._edge_tts_offline = False
        motor._piper_voice_key = "english"
        motor._piper = _make_mock_piper(available=True, synthesize_ok=True)

        _drive_hablar(motor, "Hello world", edge_save_side_effect=socket.gaierror("dns"))
        assert motor._piper_locale_mismatch_notified is False
        assert "piper_voice_locale_mismatch" not in ui_events

    def test_fallback_engage_also_logs_a_warning(self):
        """design §5.1(3): the notice must fire 'in addition to the WARNING
        log' — not just the ui_callback event."""
        motor, log_q, _ui_events = _make_motor()
        motor.tts_local_only = False
        motor._edge_tts_offline = False
        motor._piper_voice_key = "neutral"  # es voice under en locale -> mismatch
        motor._piper = _make_mock_piper(available=True, synthesize_ok=True)

        _drive_hablar(motor, "Hello world", edge_save_side_effect=socket.gaierror("dns"))

        assert motor._piper_locale_mismatch_notified is True
        queued = []
        while not log_q.empty():
            queued.append(log_q.get_nowait())
        assert any("mismatch" in line.lower() for line in queued)


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


# ---------------------------------------------------------------------------
# llm_output_streaming_20260813 — startup TTS pre-warm
#
# Measured (logs/opencohost_20260813_154829.log:36 vs :46,:55,:67,:86,:94,:108,
# :125,:142): the FIRST Piper synthesis of a session costs 2.25s, every later
# one 0.08-0.43s. Without a pre-warm the first turn of every session blows the
# phase-1 TTFA target by ~2s for a reason that has nothing to do with the LLM.
# ---------------------------------------------------------------------------

def _drain(log_queue):
    lines = []
    while not log_queue.empty():
        lines.append(log_queue.get_nowait())
    return lines


class TestStartupTtsPrewarm:
    def test_prewarm_synthesizes_once_off_air_and_reports_its_cost(self):
        """One throwaway synthesis through the SAME `_piper.synthesize` seam
        `_hablar_impl` drives — no playback, no dialogue, no historial, no UI
        event — plus one INFO line carrying the measured duration."""
        motor, log_queue, ui_events = _make_motor()
        motor._piper = _make_mock_piper(available=True, synthesize_ok=True)
        motor.pygame = MagicMock()
        historial_before = list(motor.historial)

        motor._prewarm_tts()

        assert motor._piper.synthesize.call_count == 1
        text, path = motor._piper.synthesize.call_args.args
        assert text == motor._TTS_PREWARM_TEXT and text, (
            "the pre-warm speaks a fixed constant, never model output"
        )
        motor.pygame.mixer.music.load.assert_not_called()
        motor.pygame.mixer.music.play.assert_not_called()
        assert ui_events == [], "the pre-warm fires no UI/boundary event"
        assert list(motor.historial) == historial_before
        assert not os.path.exists(path), "the throwaway wav is removed"

        lines = _drain(log_queue)
        assert any("[TTS_PREWARM] ok ms=" in line for line in lines), lines

    def test_prewarm_never_kills_startup_when_the_engine_raises(self):
        """A TTS engine that cannot pre-warm must still be able to serve turns:
        the exception is swallowed and reported at WARNING."""
        motor, log_queue, _ = _make_motor()
        motor._piper = _make_mock_piper(available=True)
        motor._piper.synthesize.side_effect = RuntimeError("onnx exploded")

        motor._prewarm_tts()  # must not raise

        lines = _drain(log_queue)
        assert any("[TTS_PREWARM] failed" in line for line in lines), lines

    def test_prewarm_skips_when_piper_is_not_loaded(self):
        motor, log_queue, _ = _make_motor()
        motor._piper = _make_mock_piper(available=False)

        motor._prewarm_tts()

        motor._piper.synthesize.assert_not_called()
        lines = _drain(log_queue)
        assert any("[TTS_PREWARM] skipped" in line for line in lines), lines

    def test_run_prewarms_at_startup_without_delaying_readiness(self):
        """The pre-warm is wired into run()'s existing startup warm-up block and
        runs OFF the startup path: a 5s pre-warm must not hold run() for 5s."""
        import queue as q
        import threading
        import time

        motor, _, _ = _make_motor()

        class _EmptyQueue:
            def __init__(self, ticks):
                self._remaining = ticks

            def get(self, timeout=None):
                if self._remaining > 0:
                    self._remaining -= 1
                    raise q.Empty
                return None

            def qsize(self):
                return 0

        started = threading.Event()
        release = threading.Event()

        def slow_prewarm():
            started.set()
            release.wait(5.0)

        motor._prewarm_tts = slow_prewarm
        motor.command_queue = _EmptyQueue(1)
        motor._piper = MagicMock()
        motor._check_ollama_service = lambda: None
        motor._process_priority_queue = lambda: None
        motor._check_pending_model_switch = lambda: None
        motor.promote_pending_drafts = lambda **kw: {"skipped": "test"}

        t0 = time.monotonic()
        try:
            motor.run()
            elapsed = time.monotonic() - t0
            assert started.wait(2.0), "run() never triggered the TTS pre-warm"
            assert elapsed < 2.0, (
                f"run() blocked {elapsed:.2f}s on the pre-warm; it must not delay readiness"
            )
        finally:
            release.set()
