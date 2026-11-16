"""Tests for headless audio mode when pygame.mixer cannot be initialized."""
import queue
import threading
from unittest.mock import MagicMock, patch

from opencohost.core.llm_engine import MotorVocalIA
from tests.test_tts_ptt_voice_death import _wire_fake_piper


def test_mixer_init_failure_enables_audio_disabled_and_continues(tmp_path):
    """When pygame.mixer.init() raises, log a warning and continue in headless mode."""
    log_q = queue.Queue()
    ui_events = []
    motor = MotorVocalIA(log_q, lambda ev: ui_events.append(ev))

    mock_pygame = MagicMock()
    mock_pygame.mixer.init.side_effect = RuntimeError("No available audio device")
    mock_ollama = MagicMock()

    # Pre-queue a stop sentinel so the run() loop terminates immediately after startup
    motor.command_queue.put(None)

    with patch.dict("sys.modules", {"pygame": mock_pygame, "ollama": mock_ollama}):
        with patch.object(motor, "_check_ollama_service"):
            with patch.object(motor, "_prewarm_tts"):
                motor.run()

    assert motor._audio_disabled is True
    assert motor.startup_check_done is True

    # Check warning log was emitted
    logs = []
    while not log_q.empty():
        logs.append(log_q.get_nowait())
    warning_logs = [
        msg for msg in logs
        if "ADVERTENCIA: No se pudo inicializar pygame.mixer" in str(msg)
    ]
    assert len(warning_logs) == 1
    assert "Continuando en modo headless/audio deshabilitado." in warning_logs[0]


def test_audio_disabled_skips_playback_gracefully(tmp_path):
    """When _audio_disabled is True, _hablar skips mixer calls and completes."""
    log_q = queue.Queue()
    motor = MotorVocalIA(log_q, lambda ev: None)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor._audio_disabled = True
    _wire_fake_piper(motor, tmp_path)

    outcome = motor._hablar("Hola mundo de prueba.", source="direct")

    motor.pygame.mixer.music.load.assert_not_called()
    motor.pygame.mixer.music.play.assert_not_called()
    assert outcome.interrupted is False
    assert outcome.error is None
    assert len(outcome.skipped) == 0
    assert len(outcome.spoken) > 0


def test_mixer_get_init_none_skips_playback_gracefully(tmp_path):
    """When pygame.mixer.get_init() is None, playback is gracefully skipped."""
    log_q = queue.Queue()
    motor = MotorVocalIA(log_q, lambda ev: None)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.pygame.mixer.get_init.return_value = None
    motor.is_ready = True
    motor._audio_disabled = False
    _wire_fake_piper(motor, tmp_path)

    outcome = motor._hablar("Probando mixer no inicializado.", source="direct")

    motor.pygame.mixer.music.load.assert_not_called()
    motor.pygame.mixer.music.play.assert_not_called()
    assert outcome.interrupted is False
    assert outcome.error is None
    assert len(outcome.skipped) == 0


def test_mark_audio_suspect_noops_when_audio_disabled():
    """mark_audio_suspect should not flag reinit when audio is disabled."""
    log_q = queue.Queue()
    motor = MotorVocalIA(log_q, lambda ev: None)
    motor._audio_disabled = True
    assert motor._audio_reinit_needed is False

    motor.mark_audio_suspect()
    assert motor._audio_reinit_needed is False
