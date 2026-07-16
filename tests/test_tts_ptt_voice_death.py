"""Repro test for the silent-TTS-after-PTT bug (runtime session 2026-07-15).

Confirmed mechanism (see engram / AGENT_HANDOFF for the full report): the
chunk-playback consumer loop inside ``MotorVocalIA._hablar`` (llm_engine.py,
around line 2945) catches ANY exception raised by pygame during
``mixer.music.play()`` / the ``get_busy()`` busy-wait with a bare
``logger.warning`` and never increments ``error_count``. Because the
end-of-utterance "N fragmento(s) fallaron" warning (around line 2990) is
gated purely on ``error_count``, a chunk that raised during playback is
NEVER counted as a failure -- the pipeline logs "completado" and fires
``speaking_end`` unconditionally, exactly as if the turn had played back
normally, even though zero audio reached the speakers.

This is the structural reason the 2026-07-15 session went audible-then-
permanently-silent mid-PTT-turn without a single error anywhere in the
logs: once pygame's mixer handle degrades, every subsequent turn (PTT or
typed, Piper or Edge-TTS) reports full success while producing no sound.

Repro: force ``pygame.mixer.music.play()`` to raise for a PTT turn's only
chunk and assert the engine surfaces that as a fragment failure instead of
silently reporting a clean completion.
"""
from __future__ import annotations

import queue
from unittest.mock import MagicMock


def _drain_logs(log_q: queue.Queue) -> list[str]:
    logs = []
    while not log_q.empty():
        logs.append(log_q.get_nowait())
    return logs


def _wire_fake_piper(motor, tmp_path):
    """Mirrors the Piper fast-path setup shared by every test below."""
    motor.voz_referencia = None
    motor.motor_tts = "ligero"
    motor.tts_local_only = False
    motor._edge_tts_offline = True  # fast-path straight to Piper, no network

    fake_wav = tmp_path / "chunk.wav"
    fake_wav.write_bytes(b"\x00" * 64)

    def fake_synthesize(text, out_path):
        import shutil

        shutil.copy(str(fake_wav), out_path)
        return True

    motor._piper = MagicMock()
    motor._piper.is_available.return_value = True
    motor._piper.synthesize.side_effect = fake_synthesize


def _make_motor():
    """Return a MotorVocalIA with pygame/ollama mocked (mirrors
    tests/test_audio_teardown.py::_make_motor)."""
    log_q: queue.Queue = queue.Queue()
    ui_events: list[str] = []

    def ui_callback(event):
        ui_events.append(event)

    from opencohost.core.llm_engine import MotorVocalIA

    motor = MotorVocalIA(log_q, ui_callback)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.pygame.mixer.music.get_busy.return_value = False
    motor.is_ready = True
    return motor, log_q, ui_events


def test_playback_failure_during_ptt_turn_is_reported_not_swallowed(tmp_path):
    """A pygame playback exception must be counted as a failed fragment.

    Current (buggy) behaviour: the consumer loop's except block (llm_engine.py
    ~2945-2946) swallows the pygame exception as a bare ``logger.warning`` and
    never touches ``error_count``, so the "N fragmento(s) fallaron" warning
    can never fire from a playback failure -- the turn is reported as fully
    completed (``speaking_end``) while silent.
    """
    motor, log_q, ui_events = _make_motor()
    motor.voz_referencia = None
    motor.motor_tts = "ligero"
    motor.tts_local_only = False
    motor._edge_tts_offline = True  # fast-path straight to Piper, no network

    fake_wav = tmp_path / "chunk.wav"
    fake_wav.write_bytes(b"\x00" * 64)

    def fake_synthesize(text, out_path):
        import shutil

        shutil.copy(str(fake_wav), out_path)
        return True

    motor._piper = MagicMock()
    motor._piper.is_available.return_value = True
    motor._piper.synthesize.side_effect = fake_synthesize

    # Simulate a disrupted audio endpoint: playback raises for the chunk,
    # exactly the branch the bug report cites as "swallowed as a warning".
    motor.pygame.mixer.music.play.side_effect = RuntimeError(
        "audio endpoint disrupted"
    )

    motor._hablar("Segundo turno de PTT que deberia sonar.", source="ptt")

    logs = []
    while not log_q.empty():
        logs.append(log_q.get_nowait())

    # The engine still reports a clean lifecycle -- this is the "silent
    # success" symptom from the bug report: no crash, no visible error.
    assert ui_events == ["speaking_start", "speaking_end"], (
        f"engine must still report the turn lifecycle even when playback "
        f"fails; got {ui_events}"
    )

    # Expected-correct behaviour: a chunk whose playback raised must be
    # counted as a failed fragment so the existing failure-warning fires.
    assert any("fallaron" in line for line in logs), (
        "a chunk whose pygame playback raised must increment error_count so "
        "the 'N fragmento(s) fallaron' warning fires -- instead the "
        "exception is silently swallowed and the turn is logged as fully "
        f"completed while silent (mechanism from the 2026-07-15 PTT "
        f"voice-death session). log_queue contents: {logs}"
    )


def test_playback_exception_flags_reinit_for_next_pipeline_run(tmp_path):
    """A playback exception must not just log a warning -- it must set the
    recovery flag so the mixer is quit+re-inited before the NEXT turn's
    first chunk, and that flag must be cleared once consumed."""
    motor, log_q, ui_events = _make_motor()
    _wire_fake_piper(motor, tmp_path)

    # Turn 1: playback raises -> marks the mixer suspect.
    motor.pygame.mixer.music.play.side_effect = RuntimeError("audio endpoint disrupted")
    motor._hablar("Primer turno que falla en reproduccion.", source="ptt")

    assert motor._audio_reinit_needed is True
    motor.pygame.mixer.quit.assert_not_called()  # never mid-turn, only next turn's start

    # Turn 2: playback works again, but the pipeline must re-init the mixer
    # BEFORE this turn's first chunk, then clear the flag.
    motor.pygame.mixer.music.play.side_effect = None
    motor._hablar("Segundo turno que deberia reinicializar el mixer.", source="ptt")

    motor.pygame.mixer.quit.assert_called_once()
    motor.pygame.mixer.init.assert_called_once()
    assert motor._audio_reinit_needed is False
    assert any("re-inicializado" in line for line in _drain_logs(log_q))


def test_mark_audio_suspect_reinits_mixer_before_first_chunk(tmp_path):
    """mark_audio_suspect() set externally (the PTT session-close hook) must
    make the NEXT _hablar() pipeline re-init the mixer before its first
    chunk plays, even with no playback exception at all."""
    motor, log_q, ui_events = _make_motor()
    _wire_fake_piper(motor, tmp_path)

    motor.mark_audio_suspect()  # e.g. PttController._on_session_closed
    motor._hablar("Turno normal tras cierre de sesion PTT.", source="ptt")

    motor.pygame.mixer.quit.assert_called_once()
    motor.pygame.mixer.init.assert_called_once()
    assert motor._audio_reinit_needed is False


def test_normal_pipeline_never_touches_mixer_quit_without_flag(tmp_path):
    """CTK safety invariant: with the suspect flag never set (no PTT close,
    no playback exception), the pipeline must NEVER call mixer.quit()/init().
    CTK's AudioBedEngine (opencohost/core/audio_bed.py) shares this same
    mixer for background music; an unconditional re-init would glitch or
    kill it, so the recovery path must stay flag-gated."""
    motor, log_q, ui_events = _make_motor()
    _wire_fake_piper(motor, tmp_path)

    assert motor._audio_reinit_needed is False
    motor._hablar("Turno normal sin incidentes.", source="direct")

    motor.pygame.mixer.quit.assert_not_called()
    motor.pygame.mixer.init.assert_not_called()
