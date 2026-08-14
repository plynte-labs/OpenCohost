"""Audio teardown regression tests — strict TDD for the 4 confirmed bugs.

Benchmark triage 2026-06-13 (engram #1891):
  Bug 1 [P1]: Agenda teardown never calls audio_bed.stop()
  Bug 2 [P1]: on_boundary() resets idle-drain timer, making idle-stop unreachable
  Bug 3 [P2]: _can_transition_now() returns True when current_track is None
  Bug 4 [P2]: TTS consumer loop never calls music.stop(); emergency_stop doesn't
              set _speaking=False so in-flight _hablar thread keeps draining
"""
from __future__ import annotations

import queue
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_library_with_track():
    """Return a MusicLibrary stub with one valid track."""
    from opencohost.core.music.music_library import MusicLibrary, MusicTrack

    lib = MagicMock(spec=MusicLibrary)
    track = MusicTrack(
        id="t1",
        original_name="loop.wav",
        path="/fake/loop.wav",
        mood="normal",
        label="normal",
        variant_index=0,
    )
    lib.valid_tracks.return_value = [track]
    lib.select_for_mood.return_value = track
    return lib, track


def _make_audio_bed():
    """Return an AudioBedEngine with pygame mocked out, current_track set."""
    from opencohost.core.music.audio_bed import AudioBedEngine, AudioBedPolicy

    lib, track = _make_library_with_track()
    policy = AudioBedPolicy(
        min_play_seconds=1.0,
        max_play_seconds=2.0,
        idle_loop_limit=2,
        idle_check_interval=0.1,
        fade_ms=0,
    )
    bed = AudioBedEngine(library=lib, policy=policy)
    mock_pygame = MagicMock()
    mock_channel = MagicMock()
    mock_pygame.mixer.find_channel.return_value = mock_channel
    mock_pygame.mixer.Sound.return_value = MagicMock()
    bed._pygame = mock_pygame
    bed._channel = mock_channel
    bed.current_track = track  # simulate already-playing state
    bed.started_at = time.time() - 5.0
    return bed, mock_channel, track


def _make_motor():
    """Return a MotorVocalIA with all external deps mocked."""
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
    return motor, ui_events


def _wait_until(pred, *, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return False


def _make_shell_stub(bed, motor=None):
    """Return a SimpleNamespace that looks enough like AppShell for teardown tests."""
    import types
    from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController
    from opencohost.ui import app_shell as _mod

    shell = types.SimpleNamespace()
    shell.audio_bed = bed
    if motor is not None:
        shell.motor_ia = motor
    else:
        # Provide a minimal motor stub so emergency_stop doesn't AttributeError
        stub_motor = MagicMock()
        stub_motor._lock = threading.Lock()
        stub_motor._speaking = False
        shell.motor_ia = stub_motor
    shell._kira_agenda_tick_id = None
    shell._kira_agenda_prefetched_action = None
    shell._kira_agenda_update_status = MagicMock()
    shell._on_stream_admin_log = MagicMock()
    shell._kira_agenda_restore_chat_filter = MagicMock()
    shell._clear_obs_joyita = MagicMock()
    shell._enqueue_kira_agenda_action = MagicMock()
    shell.after_cancel = MagicMock()
    shell.kira_agenda = KiraAgendaController()

    shell._kira_agenda_emergency_stop = (
        lambda: _mod.VocalAIApp._kira_agenda_emergency_stop(shell)
    )
    shell._kira_agenda_soft_stop = (
        lambda: _mod.VocalAIApp._kira_agenda_soft_stop(shell)
    )
    shell._check_pending_audio_bed_stop = (
        lambda: _mod.VocalAIApp._check_pending_audio_bed_stop(shell)
    )
    return shell


# ===========================================================================
# Bug 1 — Agenda teardown never calls audio_bed.stop()
# ===========================================================================


class TestBug1AgendaTeardownStopsAudioBed:
    """_kira_agenda_emergency_stop and _kira_agenda_soft_stop must stop the bed."""

    def test_emergency_stop_calls_audio_bed_stop_emergency(self):
        bed, ch, _track = _make_audio_bed()
        shell = _make_shell_stub(bed)
        assert bed.current_track is not None

        shell._kira_agenda_emergency_stop()

        # Hard stop: channel.stop() must have been called
        ch.stop.assert_called_once()
        # Teardown clears current_track
        assert bed.current_track is None

    def test_soft_stop_schedules_graceful_audio_bed_stop(self):
        """Soft-stop defers audio-bed stop while Kira is delivering closing speech.

        Fix 3 changed the original immediate-stop behaviour: the fade must not
        race Kira's closing speech.  The flag is honoured by _on_motor_speaking_end
        once the agenda is fully OFF (after the closing speech finishes).

        Round 2 note: when agenda is already OFF the stop fires synchronously.
        This test covers the deferred branch — agenda state is SPEAKING.
        """
        from opencohost.smart_aggregator.kira_agenda_controller import AgendaState

        bed, ch, _track = _make_audio_bed()
        shell = _make_shell_stub(bed)
        # Simulate agenda mid-closing-speech so the deferred branch is exercised
        shell.kira_agenda.enable()  # OFF → IDLE
        shell.kira_agenda.state = AgendaState.SPEAKING
        assert bed.current_track is not None

        shell._kira_agenda_soft_stop()

        # Must NOT stop immediately — deferred until speaking ends (state != OFF)
        ch.fadeout.assert_not_called()
        ch.stop.assert_not_called()
        # Pending flag must remain set so the deferred stop fires later
        assert getattr(shell, "_pending_audio_bed_stop", False) is True


# ===========================================================================
# Bug 2 — on_boundary() must NOT refresh _last_interaction
# ===========================================================================


class TestBug2OnBoundaryDoesNotRefreshIdleTimer:
    """on_boundary() must not call _mark_interaction() so idle drain can fire."""

    def test_on_boundary_does_not_update_last_interaction(self):
        bed, _ch, _track = _make_audio_bed()
        stale_ts = time.time() - 9999.0
        bed._last_interaction = stale_ts

        bed.on_boundary()

        assert bed._last_interaction == stale_ts, (
            "on_boundary() must NOT call _mark_interaction(); "
            "doing so perpetually resets the idle drain timer"
        )

    def test_on_boundary_does_not_start_idle_check_timer(self):
        bed, _ch, _track = _make_audio_bed()
        bed._cancel_idle_check()
        assert bed._idle_check_timer is None

        bed.on_boundary()

        assert bed._idle_check_timer is None, (
            "on_boundary() must not reschedule the idle-check timer"
        )

    def test_request_mood_still_updates_last_interaction(self):
        """Sanity: genuine human-initiated request_mood SHOULD call _mark_interaction."""
        bed, _ch, _track = _make_audio_bed()
        stale_ts = time.time() - 9999.0
        bed._last_interaction = stale_ts

        bed.request_mood("normal", force=True)

        assert bed._last_interaction > stale_ts, (
            "request_mood() must still update _last_interaction (human action)"
        )


# ===========================================================================
# Bug 3 — _can_transition_now() must return False when no track is playing
# ===========================================================================


class TestBug3CanTransitionNowNoneTrack:
    """_can_transition_now() on a None-track must return False."""

    def test_returns_false_when_current_track_is_none_and_no_pending(self):
        bed, _ch, _track = _make_audio_bed()
        bed.current_track = None
        bed.transition_pending = False

        result = bed._can_transition_now()

        assert result is False, (
            "_can_transition_now() must return False when current_track is None "
            "and transition_pending is False — prevents auto-restart after stop()"
        )

    def test_returns_false_when_idle_stopped(self):
        bed, _ch, _track = _make_audio_bed()
        bed._idle_stopped = True

        result = bed._can_transition_now()

        assert result is False, (
            "_can_transition_now() must honour _idle_stopped flag"
        )

    def test_on_boundary_does_not_auto_restart_after_stop(self):
        """Regression for #751: stop() + on_boundary() must not restart music."""
        bed, ch, _track = _make_audio_bed()
        bed.stop(emergency=True)
        assert bed.current_track is None
        assert bed.transition_pending is False

        played = bed.on_boundary()

        assert played is False, (
            "on_boundary() must not restart music after an explicit stop()"
        )
        ch.play.assert_not_called()

    def test_enabled_flag_honoured_in_on_boundary(self):
        """on_boundary must respect enabled=False (bug #751 second vector)."""
        bed, ch, _track = _make_audio_bed()
        bed.enabled = False
        bed.transition_pending = True  # pending before disabled

        played = bed.on_boundary()

        assert played is False
        ch.play.assert_not_called()


# ===========================================================================
# Bug 4 — TTS consumer loop: music.stop() called; emergency sets _speaking=False
# ===========================================================================


class TestBug4TtsConsumerInterrupt:
    """Consumer loop must honour _speaking=False; music.stop() must be called."""

    def test_consumer_exits_promptly_when_speaking_cleared_externally(self):
        """Setting motor._speaking=False externally terminates the consumer loop."""
        motor, _ui = _make_motor()
        motor.voz_referencia = None
        motor.motor_tts = "ligero"
        motor.tts_local_only = False
        motor._piper = MagicMock()
        motor._piper.is_available.return_value = False
        motor._edge_tts_offline = True

        call_count = {"n": 0}

        def get_busy_side_effect():
            call_count["n"] += 1
            if call_count["n"] >= 3:
                # External interrupt: clear _speaking
                with motor._lock:
                    motor._speaking = False
            return True  # claim "busy" so loop re-checks _speaking guard

        motor.pygame.mixer.music.get_busy.side_effect = get_busy_side_effect

        done = threading.Event()

        def run():
            motor._hablar("Consumer interrupt test sentence one two three.")
            done.set()

        with patch("os.path.exists", return_value=False):
            t = threading.Thread(target=run, daemon=True)
            t.start()
            assert done.wait(timeout=5.0), (
                "Consumer loop did not exit after external _speaking=False"
            )

    def test_music_stop_called_when_speaking_interrupted(self, tmp_path):
        """pygame.mixer.music.stop() must be called when _speaking goes False mid-chunk."""
        motor, _ui = _make_motor()
        motor.voz_referencia = None
        motor.motor_tts = "ligero"
        motor.tts_local_only = False
        motor._edge_tts_offline = True

        # Make Piper succeed and produce a real temp file so the consumer
        # actually reaches the load/play block
        fake_wav = tmp_path / "chunk.wav"
        fake_wav.write_bytes(b"\x00" * 64)

        def fake_synthesize(text, out_path):
            import shutil
            shutil.copy(str(fake_wav), out_path)
            return True

        motor._piper = MagicMock()
        motor._piper.is_available.return_value = True
        motor._piper.synthesize.side_effect = fake_synthesize

        # get_busy: clear _speaking on first call so interrupt fires during playback
        first_call = {"done": False}

        def get_busy_side_effect():
            if not first_call["done"]:
                first_call["done"] = True
                with motor._lock:
                    motor._speaking = False
            return False  # not "busy" after the interrupt

        motor.pygame.mixer.music.get_busy.side_effect = get_busy_side_effect

        done = threading.Event()

        def run():
            motor._hablar("Stop before unload interrupt sentence test here.")
            done.set()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        done.wait(timeout=5.0)

        # music.stop() must have been called during teardown
        assert motor.pygame.mixer.music.stop.called, (
            "pygame.mixer.music.stop() was never called; "
            "audio will keep playing after _speaking goes False"
        )

    def test_emergency_stop_sets_motor_speaking_false(self):
        """_kira_agenda_emergency_stop must set motor_ia._speaking=False."""
        motor, _ui = _make_motor()
        motor._speaking = True  # simulate in-flight _hablar

        bed, ch, _track = _make_audio_bed()
        shell = _make_shell_stub(bed, motor=motor)

        shell._kira_agenda_emergency_stop()

        assert motor._speaking is False, (
            "_kira_agenda_emergency_stop must set motor_ia._speaking=False "
            "so the in-flight _hablar thread terminates promptly"
        )


# ===========================================================================
# Fix 1 — temp-file leak: drain cola_audios on emergency interrupt
# ===========================================================================


class TestFix1TempFileDrainOnInterrupt:
    """After an emergency interrupt, pre-queued chunk temp files must be deleted."""

    def test_prequeued_temp_files_deleted_after_interrupt(self, tmp_path):
        """Files already in cola_audios when _speaking goes False must be cleaned up.

        The producer enqueues (out_path, idx, text) tuples where out_path is a
        temporary file created by synthesize().  We track what synthesize() writes
        and assert that all those paths are gone after the consumer exits via the
        drain loop.
        """
        import os

        motor, _ui = _make_motor()
        motor.voz_referencia = None
        motor.motor_tts = "ligero"
        motor.tts_local_only = False
        motor._edge_tts_offline = True

        # Track every out_path that synthesize writes so we can check deletion.
        written_paths: list[str] = []

        def fake_synthesize(text, out_path):
            # Write a minimal WAV header so the file exists on disk.
            with open(out_path, "wb") as f:
                f.write(b"\x00" * 64)
            written_paths.append(out_path)
            return True

        motor._piper = MagicMock()
        motor._piper.is_available.return_value = True
        motor._piper.synthesize.side_effect = fake_synthesize

        # Set _speaking=False via music.load so the interrupt fires as soon as
        # the consumer tries to load the FIRST chunk — this means chunks 2 and 3
        # (already enqueued by the producer) are left in cola_audios and must be
        # cleaned up by the drain loop.
        def load_and_interrupt(path):
            with motor._lock:
                motor._speaking = False

        motor.pygame.mixer.music.load.side_effect = load_and_interrupt
        motor.pygame.mixer.music.get_busy.return_value = False

        done = threading.Event()

        def run():
            motor._hablar(
                "Sentence one drain test. "
                "Sentence two drain test. "
                "Sentence three drain test."
            )
            done.set()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert done.wait(timeout=8.0), "Consumer did not exit within timeout"

        # Give any residual OS buffering a moment (shouldn't be needed but safe)
        assert len(written_paths) > 0, "synthesize() was never called — test setup broken"
        surviving = [p for p in written_paths if os.path.exists(p)]
        assert surviving == [], (
            f"Temp files leaked after interrupt: {surviving}\n"
            "drain loop after early break must delete all remaining queued chunks"
        )

    def test_no_leak_when_producer_lags_behind_drain(self, tmp_path):
        """Deterministic reproduction of the CI-only leak: a slow producer.

        When the producer is still synthesizing a chunk at the moment the
        consumer interrupts and drains, the drain finds the queue empty; the
        producer then writes+enqueues that chunk AFTER the drain.  If the
        producer is joined only AFTER the drain (the bug), that chunk's temp
        file leaks permanently.  Making synthesize() slow for chunks after the
        first forces this interleaving on any machine.
        """
        import os

        motor, _ui = _make_motor()
        motor.voz_referencia = None
        motor.motor_tts = "ligero"
        motor.tts_local_only = False
        motor._edge_tts_offline = True

        written_paths: list[str] = []
        call_index = {"value": 0}

        def fake_synthesize(text, out_path):
            idx = call_index["value"]
            call_index["value"] += 1
            # First chunk is instant so the consumer can dequeue + interrupt;
            # later chunks lag so the producer is still working when the
            # consumer's drain loop runs.
            if idx >= 1:
                time.sleep(0.3)
            with open(out_path, "wb") as f:
                f.write(b"\x00" * 64)
            written_paths.append(out_path)
            return True

        motor._piper = MagicMock()
        motor._piper.is_available.return_value = True
        motor._piper.synthesize.side_effect = fake_synthesize

        def load_and_interrupt(path):
            with motor._lock:
                motor._speaking = False

        motor.pygame.mixer.music.load.side_effect = load_and_interrupt
        motor.pygame.mixer.music.get_busy.return_value = False

        done = threading.Event()

        def run():
            motor._hablar(
                "Sentence one drain test. "
                "Sentence two drain test. "
                "Sentence three drain test."
            )
            done.set()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert done.wait(timeout=8.0), "Consumer did not exit within timeout"

        assert len(written_paths) > 0, "synthesize() was never called — test setup broken"
        surviving = [p for p in written_paths if os.path.exists(p)]
        assert surviving == [], (
            f"Temp files leaked after interrupt: {surviving}\n"
            "the producer must be joined BEFORE the drain so a late-written "
            "chunk cannot survive the queue drain"
        )

    def test_single_prequeued_file_deleted_on_post_dequeue_interrupt(self, tmp_path):
        """The post-dequeue _speaking guard must delete the just-dequeued file.

        The synthesize() side-effect writes to out_path (a TEMP_DIR path).
        We track that path and assert it is deleted by the post-dequeue guard or
        drain loop after the interrupt fires.
        """
        import os

        motor, _ui = _make_motor()
        motor.voz_referencia = None
        motor.motor_tts = "ligero"
        motor.tts_local_only = False
        motor._edge_tts_offline = True

        written_paths: list[str] = []

        def fake_synthesize_and_interrupt(text, out_path):
            with open(out_path, "wb") as f:
                f.write(b"\x00" * 64)
            written_paths.append(out_path)
            # Interrupt immediately after first synthesis so post-dequeue guard fires
            with motor._lock:
                motor._speaking = False
            return True

        motor._piper = MagicMock()
        motor._piper.is_available.return_value = True
        motor._piper.synthesize.side_effect = fake_synthesize_and_interrupt
        motor.pygame.mixer.music.get_busy.return_value = False

        done = threading.Event()

        def run():
            motor._hablar("Single sentence interrupt test.")
            done.set()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        assert done.wait(timeout=6.0), "Consumer did not exit within timeout"

        assert len(written_paths) > 0, "synthesize() was never called — test setup broken"
        surviving = [p for p in written_paths if os.path.exists(p)]
        assert surviving == [], (
            f"Temp file not deleted after post-dequeue _speaking guard fired: {surviving}"
        )


# ===========================================================================
# Fix 2 — broken hasattr guard: emergency_stop with no motor_ia attribute
# ===========================================================================


class TestFix2HasattrGuardForMotorIa:
    """emergency_stop must not raise AttributeError when motor_ia is absent."""

    def _make_shell_without_motor(self):
        """Return a shell stub that has NO motor_ia attribute at all."""
        import types
        from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController
        from opencohost.ui import app_shell as _mod

        shell = types.SimpleNamespace()
        bed, _ch, _track = _make_audio_bed()
        shell.audio_bed = bed
        # Deliberately do NOT set shell.motor_ia
        shell._kira_agenda_tick_id = None
        shell._kira_agenda_prefetched_action = None
        shell._kira_agenda_update_status = MagicMock()
        shell._on_stream_admin_log = MagicMock()
        shell._kira_agenda_restore_chat_filter = MagicMock()
        shell._clear_obs_joyita = MagicMock()
        shell._enqueue_kira_agenda_action = MagicMock()
        shell.after_cancel = MagicMock()
        shell.kira_agenda = KiraAgendaController()

        shell._kira_agenda_emergency_stop = (
            lambda: _mod.VocalAIApp._kira_agenda_emergency_stop(shell)
        )
        return shell, bed

    def test_emergency_stop_no_motor_ia_does_not_raise(self):
        """emergency_stop must complete without AttributeError when motor_ia is absent."""
        shell, _bed = self._make_shell_without_motor()
        assert not hasattr(shell, "motor_ia"), "Precondition: motor_ia must be absent"

        try:
            shell._kira_agenda_emergency_stop()
        except AttributeError as exc:
            pytest.fail(
                f"emergency_stop raised AttributeError when motor_ia is absent: {exc}"
            )

    def test_emergency_stop_no_motor_ia_still_stops_audio_bed(self):
        """Even without motor_ia, the audio bed must be stopped on emergency."""
        shell, bed = self._make_shell_without_motor()

        shell._kira_agenda_emergency_stop()

        assert bed.current_track is None, (
            "audio_bed must be stopped even when motor_ia is absent"
        )

    def test_emergency_stop_with_motor_ia_still_calls_drop_pending(self):
        """With motor_ia present, drop_pending_sources must still be called."""
        motor, _ui = _make_motor()
        motor.drop_pending_sources = MagicMock()
        bed, _ch, _track = _make_audio_bed()
        shell = _make_shell_stub(bed, motor=motor)

        shell._kira_agenda_emergency_stop()

        motor.drop_pending_sources.assert_called_once()


# ===========================================================================
# Fix 3 — soft_stop must defer audio_bed.stop() until speaking ends
# ===========================================================================


class TestFix3SoftStopDeferredAudioBedStop:
    """soft_stop must NOT stop the audio bed immediately; must defer until speech ends."""

    def _make_shell_with_speaking_end(self, bed, motor=None):
        """Return a shell stub with _on_motor_speaking_end_deferred_stop extracted.

        We test the deferred-stop logic directly via the helper that
        _on_motor_speaking_end calls, rather than invoking the full method which
        requires the entire UI widget tree (ptt, switch_ptt, voice_panel, etc.).
        """
        import types
        from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController
        from opencohost.ui import app_shell as _mod

        shell = types.SimpleNamespace()
        shell.audio_bed = bed
        if motor is not None:
            shell.motor_ia = motor
        else:
            stub_motor = MagicMock()
            stub_motor._lock = threading.Lock()
            stub_motor._speaking = False
            shell.motor_ia = stub_motor

        shell._kira_agenda_tick_id = None
        shell._kira_agenda_prefetched_action = None
        shell._kira_agenda_update_status = MagicMock()
        shell._on_stream_admin_log = MagicMock()
        shell._kira_agenda_restore_chat_filter = MagicMock()
        shell._clear_obs_joyita = MagicMock()
        shell._enqueue_kira_agenda_action = MagicMock()
        shell.after_cancel = MagicMock()
        shell.kira_agenda = KiraAgendaController()
        shell.kira_agenda.enable()
        shell.kira_agenda.stop_requested = True

        shell._kira_agenda_emergency_stop = (
            lambda: _mod.VocalAIApp._kira_agenda_emergency_stop(shell)
        )
        shell._kira_agenda_soft_stop = (
            lambda: _mod.VocalAIApp._kira_agenda_soft_stop(shell)
        )
        # Expose the deferred-stop helper directly for isolated testing
        shell._check_pending_audio_bed_stop = (
            lambda: _mod.VocalAIApp._check_pending_audio_bed_stop(shell)
        )
        return shell

    def test_soft_stop_does_not_stop_audio_bed_immediately_while_speaking(self):
        """soft_stop must NOT call audio_bed.stop() synchronously while agenda is SPEAKING.

        Round 2: when agenda is already OFF the synchronous path fires.
        This test covers the deferred branch — agenda state is SPEAKING.
        """
        from opencohost.smart_aggregator.kira_agenda_controller import AgendaState

        bed, ch, _track = _make_audio_bed()
        shell = _make_shell_stub(bed)
        shell.kira_agenda.enable()
        shell.kira_agenda.state = AgendaState.SPEAKING

        shell._kira_agenda_soft_stop()

        # audio_bed.stop() via channel.fadeout or channel.stop must NOT fire yet
        ch.stop.assert_not_called()
        ch.fadeout.assert_not_called()

    def test_soft_stop_sets_pending_audio_bed_stop_flag_while_speaking(self):
        """soft_stop must set _pending_audio_bed_stop = True while agenda is SPEAKING.

        Round 2: flag is set and immediately consumed when agenda is already OFF.
        This test covers the SPEAKING branch where the flag stays True.
        """
        from opencohost.smart_aggregator.kira_agenda_controller import AgendaState

        bed, _ch, _track = _make_audio_bed()
        shell = _make_shell_stub(bed)
        shell.kira_agenda.enable()
        shell.kira_agenda.state = AgendaState.SPEAKING

        shell._kira_agenda_soft_stop()

        assert getattr(shell, "_pending_audio_bed_stop", False) is True, (
            "soft_stop must set shell._pending_audio_bed_stop = True "
            "so the deferred stop fires when speaking ends"
        )

    def test_pending_stop_fires_when_agenda_is_off(self):
        """_check_pending_audio_bed_stop must call stop() when agenda is OFF and flag is set."""
        from opencohost.smart_aggregator.kira_agenda_controller import AgendaState

        bed, ch, _track = _make_audio_bed()
        shell = self._make_shell_with_speaking_end(bed)

        # Simulate: soft_stop was called, flag is set, agenda is now OFF (speech finished)
        shell._pending_audio_bed_stop = True
        shell.kira_agenda.state = AgendaState.OFF

        shell._check_pending_audio_bed_stop()

        # After speaking ends with agenda OFF and flag set, graceful stop must fire
        ch.fadeout.assert_called_once()
        assert getattr(shell, "_pending_audio_bed_stop", True) is False, (
            "_pending_audio_bed_stop must be cleared after the deferred stop fires"
        )

    def test_pending_stop_does_not_fire_while_agenda_still_speaking(self):
        """_check_pending_audio_bed_stop must NOT fire stop if agenda is still SPEAKING."""
        from opencohost.smart_aggregator.kira_agenda_controller import AgendaState

        bed, ch, _track = _make_audio_bed()
        shell = self._make_shell_with_speaking_end(bed)

        # Flag set but agenda still SPEAKING (closing speech not yet finished)
        shell._pending_audio_bed_stop = True
        shell.kira_agenda.state = AgendaState.SPEAKING

        shell._check_pending_audio_bed_stop()

        # Must NOT fire yet
        ch.fadeout.assert_not_called()
        ch.stop.assert_not_called()

    def test_soft_stop_fires_immediately_when_agenda_already_off(self):
        """Round 2 CRITICAL: soft_stop when agenda is already OFF must stop music synchronously.

        Scenario: operator queue drains → agenda auto-transitions to OFF → operator
        clicks soft-stop.  soft_stop() fast-exits (sets state=OFF, returns none()),
        so _on_motor_speaking_end never fires.  Without the synchronous
        _check_pending_audio_bed_stop() call, the music bed plays FOREVER.

        Fix: _kira_agenda_soft_stop() must call _check_pending_audio_bed_stop()
        immediately AFTER setting _pending_audio_bed_stop=True.
        """
        from opencohost.smart_aggregator.kira_agenda_controller import AgendaState

        bed, ch, _track = _make_audio_bed()
        shell = self._make_shell_with_speaking_end(bed)

        # Precondition: agenda is already OFF (queue drained, auto-stopped)
        shell.kira_agenda.state = AgendaState.OFF

        shell._kira_agenda_soft_stop()

        # The graceful stop MUST have fired synchronously — no speech will ever
        # enqueue, so the deferred _on_motor_speaking_end path never triggers.
        ch.fadeout.assert_called_once_with(0), (
            "audio_bed.stop(emergency=False) must fire synchronously when "
            "agenda is already OFF at the time soft_stop() is called"
        )
        assert getattr(shell, "_pending_audio_bed_stop", True) is False, (
            "_pending_audio_bed_stop must be cleared after the synchronous stop"
        )

    def test_soft_stop_deferred_path_still_works_when_speaking(self):
        """Deferred path must still work: soft_stop while SPEAKING does NOT stop immediately.

        When the agenda is mid-closing-speech (state SPEAKING), setting the flag
        and immediately calling _check_pending_audio_bed_stop() must NOT stop the
        music — the guard inside the helper will see state != OFF and return early.
        The stop fires later via _on_motor_speaking_end.
        """
        from opencohost.smart_aggregator.kira_agenda_controller import AgendaState

        bed, ch, _track = _make_audio_bed()
        shell = self._make_shell_with_speaking_end(bed)

        # Precondition: agenda is actively speaking (closing speech in progress)
        shell.kira_agenda.state = AgendaState.SPEAKING

        shell._kira_agenda_soft_stop()

        # Must NOT stop immediately — the deferred path handles it
        ch.fadeout.assert_not_called()
        ch.stop.assert_not_called()
        # Flag must remain set so the deferred path fires later
        assert getattr(shell, "_pending_audio_bed_stop", False) is True, (
            "_pending_audio_bed_stop must remain True so the deferred "
            "_on_motor_speaking_end path fires once speech ends"
        )


# ===========================================================================
# Fix 4 (secondary) — emergency_stop must clear stale _pending_audio_bed_stop
# ===========================================================================


class TestFix4EmergencyStopClearsPendingFlag:
    """emergency_stop must clear any stale _pending_audio_bed_stop flag.

    Scenario: soft_stop was called, _pending_audio_bed_stop=True was set, but
    before _on_motor_speaking_end fired the operator clicked emergency stop.
    Without clearing the flag, the NEXT speaking_end (from a future session or
    stale callback) would trigger an unwanted graceful stop.
    """

    def test_emergency_stop_clears_pending_audio_bed_stop_flag(self):
        """After emergency_stop, _pending_audio_bed_stop must be False."""
        bed, _ch, _track = _make_audio_bed()
        shell = _make_shell_stub(bed)

        # Simulate: a prior soft_stop set the flag
        shell._pending_audio_bed_stop = True

        shell._kira_agenda_emergency_stop()

        assert getattr(shell, "_pending_audio_bed_stop", True) is False, (
            "_kira_agenda_emergency_stop must clear _pending_audio_bed_stop "
            "so a stale True flag cannot trigger a spurious graceful stop later"
        )

    def test_emergency_stop_does_not_double_stop_audio_bed(self):
        """emergency_stop clears the flag so the audio bed is not stopped twice.

        The immediate hard stop has already fired; the stale pending flag must
        not cause a second graceful stop on the next speaking_end event.
        """
        from opencohost.smart_aggregator.kira_agenda_controller import AgendaState
        from opencohost.ui import app_shell as _mod
        import types

        bed, ch, _track = _make_audio_bed()

        # Build a shell that also exposes _check_pending_audio_bed_stop
        shell = types.SimpleNamespace()
        shell.audio_bed = bed
        stub_motor = MagicMock()
        stub_motor._lock = threading.Lock()
        stub_motor._speaking = False
        shell.motor_ia = stub_motor
        shell._kira_agenda_tick_id = None
        shell._kira_agenda_prefetched_action = None
        shell._kira_agenda_update_status = MagicMock()
        shell._on_stream_admin_log = MagicMock()
        shell._kira_agenda_restore_chat_filter = MagicMock()
        shell._clear_obs_joyita = MagicMock()
        shell._enqueue_kira_agenda_action = MagicMock()
        shell.after_cancel = MagicMock()
        from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController
        shell.kira_agenda = KiraAgendaController()

        shell._kira_agenda_emergency_stop = (
            lambda: _mod.VocalAIApp._kira_agenda_emergency_stop(shell)
        )
        shell._check_pending_audio_bed_stop = (
            lambda: _mod.VocalAIApp._check_pending_audio_bed_stop(shell)
        )

        # Simulate: stale pending flag from a prior soft_stop
        shell._pending_audio_bed_stop = True

        shell._kira_agenda_emergency_stop()

        # emergency_stop performs the hard stop — reset the channel mock
        # so we can verify _check_pending_audio_bed_stop doesn't fire again
        ch.reset_mock()

        # Now simulate a stale speaking_end callback arriving after emergency stop
        # kira_agenda.state is OFF after emergency_stop, so the guard must NOT fire
        # because the flag was already cleared by emergency_stop
        shell._check_pending_audio_bed_stop()

        ch.fadeout.assert_not_called(), (
            "After emergency_stop clears the flag, a stale speaking_end "
            "must NOT trigger a second graceful audio_bed.stop()"
        )


# ===========================================================================
# FR1 (ADR-AUD-005) — interrupt_speaking() public method on MotorVocalIA
# ===========================================================================


class TestFR1InterruptSpeaking:
    """FR1: MotorVocalIA must expose interrupt_speaking() as the canonical
    public interrupt; app_shell must not reach into _lock/_speaking directly.
    """

    def test_interrupt_speaking_clears_flag_under_lock(self):
        """interrupt_speaking() must atomically set _speaking=False.

        RED: AttributeError before the method exists.
        GREEN: flag cleared after calling the method.
        """
        motor, _ui = _make_motor()
        motor._speaking = True

        motor.interrupt_speaking()

        assert motor._speaking is False, (
            "interrupt_speaking() must set motor._speaking to False so the "
            "in-flight _hablar consumer loop exits immediately"
        )

    def test_emergency_stop_uses_public_interrupt_not_private_reach_in(self):
        """kira_agenda_emergency_stop must use interrupt_speaking(), not reach
        into motor_ia._lock / motor_ia._speaking directly.

        Phase 7 (app_shell_agenda_audio_decomposition_20260624): the emergency
        stop body moved to agenda_audio_controller.py, so the public-interrupt
        assertion now targets that module. The Demeter reach-in ban is asserted
        against BOTH files (strengthens the ADR-AUD-005 intent — neither the
        shell nor the extracted module may reach in).

        RED: the private reach-in is still present in source — assertion fails.
        GREEN: source uses the public method only.
        """
        import pathlib

        repo_root = pathlib.Path(__file__).resolve().parents[1]
        app_shell = (repo_root / "opencohost" / "ui" / "app_shell.py").read_text(
            encoding="utf-8"
        )
        controller = (
            repo_root / "opencohost" / "ui" / "agenda_audio_controller.py"
        ).read_text(encoding="utf-8")

        assert "motor_ia.interrupt_speaking()" in controller, (
            "agenda_audio_controller.kira_agenda_emergency_stop must call "
            "motor_ia.interrupt_speaking() (ADR-AUD-005 FR1)"
        )
        for source in (app_shell, controller):
            assert "motor_ia._lock" not in source, (
                "must NOT reach into motor_ia._lock directly "
                "(ADR-AUD-005 Demeter violation)"
            )
            assert "motor_ia._speaking" not in source, (
                "must NOT reach into motor_ia._speaking directly "
                "(ADR-AUD-005 Demeter violation)"
            )
