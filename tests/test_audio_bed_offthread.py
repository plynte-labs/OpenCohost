"""Concurrency tests for FR3 — pygame Sound decode off-thread and off-lock.

Strict TDD: these tests are written BEFORE the implementation is in place.
They are expected to be RED (fail) against the original audio_bed.py and GREEN
after the Phase-3 restructure lands.

Tests:
  1. test_lock_not_held_during_decode
     While a play's decode is blocked in Phase 2 (off-lock), another thread must
     be able to acquire bed._lock.  RED before restructure because today the WHOLE
     _play_selected body runs under self._lock.

  2. test_stop_supersedes_inflight_decode
     Start a play whose decode blocks; call bed.stop(); release the decode; assert
     current_track is None (stale decode must NOT publish).

  3. test_newer_request_supersedes_inflight_decode
     First play's decode blocks; a second play completes fully; release the first;
     assert current_track is the SECOND track (stale first must be dropped).

  4. test_music_play_mood_dispatches_off_calling_thread
     _music_play_mood must invoke audio_bed.request_mood on a worker thread, NOT on
     the calling thread.  Also asserts _music_update_panel is called on the caller.
"""

from __future__ import annotations

import importlib
import sys
import threading
import time
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from opencohost.core.audio_bed import AudioBedEngine, AudioBedPolicy
from opencohost.core.music_library import MusicLibrary, MusicTrack


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _two_track_lib() -> tuple[MagicMock, list[MusicTrack]]:
    """Return a MusicLibrary stub with two distinct valid tracks."""
    lib = MagicMock(spec=MusicLibrary)
    tracks = [
        MusicTrack(
            id=f"t{i}",
            original_name=f"track{i}.wav",
            path=f"/fake/track{i}.wav",
            mood="normal",
            label=f"normal-{i}",
            variant_index=i,
        )
        for i in range(2)
    ]
    lib.valid_tracks.return_value = tracks
    return lib, tracks


def _make_bed(num_tracks: int = 2) -> tuple[AudioBedEngine, MagicMock, list[MusicTrack]]:
    """Return AudioBedEngine with pygame mocked and no current track."""
    lib, tracks = _two_track_lib()
    if num_tracks == 1:
        lib.valid_tracks.return_value = [tracks[0]]
    policy = AudioBedPolicy(
        min_play_seconds=1.0,
        max_play_seconds=2.0,
        idle_loop_limit=2,
        idle_check_interval=0.5,
        fade_ms=0,
    )
    bed = AudioBedEngine(library=lib, policy=policy)
    mock_pygame = MagicMock()
    mock_channel = MagicMock()
    mock_pygame.mixer.find_channel.return_value = mock_channel
    # Default Sound() returns a trivial mock — tests override this per-scenario
    mock_pygame.mixer.Sound.return_value = MagicMock()
    bed._pygame = mock_pygame
    return bed, mock_pygame, tracks


class _BlockingSound:
    """Fake pygame.mixer.Sound whose constructor blocks until released.

    Instantiating this class blocks the calling thread on `gate.wait()`.
    The test controls when the decode unblocks by calling `gate.set()`.
    After unblocking it returns a MagicMock sound object.
    """

    def __new__(cls, path: str):  # type: ignore[override]
        # Block until the gate is set
        cls._gate.wait()
        obj = MagicMock()
        return obj


# ---------------------------------------------------------------------------
# Test 1 — lock not held during decode
# ---------------------------------------------------------------------------

class TestLockNotHeldDuringDecode:
    """While _play_selected is blocked in the off-lock decode phase, another
    thread must be able to acquire bed._lock immediately (non-blocking).

    This is the core contract: Phase 2 (decode) runs WITHOUT the lock.
    RED before restructure because today the lock wraps the whole method.
    """

    def test_lock_not_held_during_decode(self):
        bed, mock_pygame, tracks = _make_bed()

        decode_started = threading.Event()
        decode_gate = threading.Event()
        lock_acquired_during_decode = threading.Event()

        def slow_sound_constructor(path):
            decode_started.set()
            decode_gate.wait(timeout=5.0)
            return MagicMock()

        mock_pygame.mixer.Sound.side_effect = slow_sound_constructor

        # Start a play on a background thread — it will block in Sound()
        play_thread = threading.Thread(
            target=bed.request_mood, args=("normal",), kwargs={"force": True}, daemon=True
        )
        play_thread.start()

        # Wait for decode to start (Sound() entered)
        assert decode_started.wait(timeout=3.0), "Decode never started"

        # Now try to acquire the lock from THIS thread — must not block
        acquired = bed._lock.acquire(blocking=False)
        if acquired:
            bed._lock.release()
            lock_acquired_during_decode.set()

        # Unblock the decode so the thread can finish
        decode_gate.set()
        play_thread.join(timeout=5.0)

        assert lock_acquired_during_decode.is_set(), (
            "bed._lock was held during mixer.Sound() decode — "
            "lock must be released before Phase 2 begins"
        )


# ---------------------------------------------------------------------------
# Test 2 — stop() supersedes an in-flight decode
# ---------------------------------------------------------------------------

class TestStopSupersedes:
    """After stop() is called while a play's decode is in progress, the stale
    decode must NOT publish (current_track must remain None after release).
    """

    def test_stop_supersedes_inflight_decode(self):
        bed, mock_pygame, tracks = _make_bed()

        decode_started = threading.Event()
        decode_gate = threading.Event()

        def slow_sound_constructor(path):
            decode_started.set()
            decode_gate.wait(timeout=5.0)
            return MagicMock()

        mock_pygame.mixer.Sound.side_effect = slow_sound_constructor

        # Capture the channel mock BEFORE stop() potentially swaps it
        channel_before_stop = mock_pygame.mixer.find_channel.return_value

        play_thread = threading.Thread(
            target=bed.request_mood, args=("normal",), kwargs={"force": True}, daemon=True
        )
        play_thread.start()

        # Wait for decode to start
        assert decode_started.wait(timeout=3.0), "Decode never started"

        # Record play call count before stop — nothing must have been played yet
        play_calls_at_stop = channel_before_stop.play.call_count

        # Call stop() — this must supersede the in-flight decode
        bed.stop(emergency=True)

        # Release the decode
        decode_gate.set()
        play_thread.join(timeout=5.0)

        # (a) current_track must remain None — the stale decode must not publish
        assert bed.current_track is None, (
            "stop() called during in-flight decode must prevent publish; "
            "current_track must remain None"
        )

        # (b) FIX 4: the superseded decode must NOT have called channel.play
        # (the guard must drop it BEFORE channel mutation, not after)
        play_calls_after = channel_before_stop.play.call_count
        assert play_calls_after == play_calls_at_stop, (
            f"Superseded decode must NOT call channel.play; "
            f"play count was {play_calls_at_stop} before stop, "
            f"{play_calls_after} after stale decode was released. "
            "The Phase-3 guard must fire BEFORE channel.play."
        )


# ---------------------------------------------------------------------------
# Test 3 — newer request supersedes in-flight decode
# ---------------------------------------------------------------------------

class TestNewerRequestSupersedes:
    """When a second request_mood() completes while the first is still decoding,
    the first (stale) decode must be dropped — current_track must be the second.
    """

    def test_newer_request_supersedes_inflight_decode(self):
        bed, mock_pygame, tracks = _make_bed(num_tracks=2)

        # Ensure there are exactly two different tracks to pick from
        assert len(tracks) == 2

        first_decode_started = threading.Event()
        first_decode_gate = threading.Event()
        call_count = [0]

        def selective_sound_constructor(path):
            call_count[0] += 1
            if call_count[0] == 1:
                # First call: block until released
                first_decode_started.set()
                first_decode_gate.wait(timeout=5.0)
            # Return a distinct mock per path so we can identify which track was set
            mock = MagicMock()
            mock._path = path
            return mock

        mock_pygame.mixer.Sound.side_effect = selective_sound_constructor

        # First play — blocks in decode
        play1 = threading.Thread(
            target=bed.request_mood, args=("normal",), kwargs={"force": True}, daemon=True
        )
        play1.start()

        # Wait for first decode to start
        assert first_decode_started.wait(timeout=3.0), "First decode never started"

        # Force a second request that picks a DIFFERENT track.
        # Pin _mood_last_index so the second call picks the other track.
        # (Both tracks are "normal"; request_mood with force=True will pick
        # whichever index is next — we just need the second to finish first.)
        second_play_done = threading.Event()
        second_track_path = [None]

        def second_play():
            # Override Sound to NOT block, and record path
            original_side_effect = mock_pygame.mixer.Sound.side_effect

            def instant_sound(path):
                # If this is the second call, don't block
                call_count[0] += 1
                mock = MagicMock()
                mock._path = path
                second_track_path[0] = path
                return mock

            mock_pygame.mixer.Sound.side_effect = instant_sound
            bed.request_mood("normal", force=True)
            mock_pygame.mixer.Sound.side_effect = original_side_effect
            second_play_done.set()

        t2 = threading.Thread(target=second_play, daemon=True)
        t2.start()

        # Let second play complete before releasing first
        assert second_play_done.wait(timeout=3.0), "Second play never finished"
        track_after_second = bed.current_track

        # Now release the first decode — it should be superseded
        first_decode_gate.set()
        play1.join(timeout=5.0)

        assert bed.current_track is track_after_second, (
            "Stale first decode must not overwrite the current_track set by the "
            "second (more recent) request_mood() call"
        )
        assert bed.current_track is not None, (
            "current_track must not be None after a successful second play"
        )


# ---------------------------------------------------------------------------
# Test 4 — _music_play_mood dispatches off the calling thread
# ---------------------------------------------------------------------------

def _import_app_shell_with_ui_deps_mocked():
    """Import app_shell with all heavy UI deps mocked out (mirrors obs_resilience harness)."""
    class DummyWidget:
        pass

    class DummyCustomTkinter(SimpleNamespace):
        def __getattr__(self, _name):
            return DummyWidget

    modules = {
        "customtkinter": DummyCustomTkinter(CTk=DummyWidget, CTkToplevel=DummyWidget),
        "numpy": MagicMock(),
        "sounddevice": MagicMock(),
        "soundfile": MagicMock(),
        "pynput": MagicMock(),
        "pynput.keyboard": MagicMock(),
        "pynput.mouse": MagicMock(),
    }
    old_module = sys.modules.pop("opencohost.ui.app_shell", None)
    with __import__("unittest.mock", fromlist=["patch"]).patch.dict(sys.modules, modules):
        module = importlib.import_module("opencohost.ui.app_shell")
    return module, old_module


def _restore_app_shell_module(old_module) -> None:
    if old_module is not None:
        sys.modules["opencohost.ui.app_shell"] = old_module
        return
    sys.modules.pop("opencohost.ui.app_shell", None)
    ui_module = sys.modules.get("opencohost.ui")
    if ui_module is not None and hasattr(ui_module, "app_shell"):
        delattr(ui_module, "app_shell")


class TestMusicPlayMoodDispatchesOffCallingThread:
    """_music_play_mood must NOT invoke request_mood on the calling thread.

    The test stubs request_mood to record the thread it was called from,
    then asserts it differs from the test (calling) thread.
    Also asserts _music_update_panel is called (panel refresh must happen).
    """

    def test_music_play_mood_dispatches_off_calling_thread(self):
        app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
        try:
            app = object.__new__(app_shell.VocalAIApp)

            request_mood_thread = [None]
            request_mood_event = threading.Event()

            def fake_request_mood(mood, *, force=False, boundary=False):
                request_mood_thread[0] = threading.current_thread()
                request_mood_event.set()
                return True

            mock_audio_bed = MagicMock()
            mock_audio_bed.request_mood.side_effect = fake_request_mood

            app.audio_bed = mock_audio_bed
            app._music_update_panel = MagicMock()

            # Call _music_play_mood on THIS (test) thread
            app_shell.VocalAIApp._music_play_mood(app, "normal")

            # Wait for request_mood to be called (may be on worker thread)
            called = request_mood_event.wait(timeout=3.0)
            assert called, "request_mood was never called by _music_play_mood"

            # Must have been called on a DIFFERENT thread
            assert request_mood_thread[0] is not threading.current_thread(), (
                "_music_play_mood must dispatch request_mood to a worker thread, "
                "not call it on the UI (calling) thread"
            )

            # _music_update_panel must be called (on the calling thread, immediately)
            app._music_update_panel.assert_called_once()
        finally:
            _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# FIX 2 — request_mood only_if_idle guard (synchronous)
# ---------------------------------------------------------------------------

class TestRequestMoodOnlyIfIdle:
    """request_mood(only_if_idle=True) must skip if current_track is set."""

    def test_request_mood_only_if_idle_skips_when_playing(self):
        bed, mock_pygame, tracks = _make_bed()

        # Manually set a current_track to simulate "something is playing"
        bed.current_track = tracks[0]

        result = bed.request_mood("normal", force=True, boundary=True, only_if_idle=True)

        # Must return False and leave current_track unchanged
        assert result is False, (
            "request_mood(only_if_idle=True) must return False when current_track is set"
        )
        assert bed.current_track is tracks[0], (
            "current_track must not be changed when only_if_idle=True and track is set"
        )
        # Sound must never have been decoded
        mock_pygame.mixer.Sound.assert_not_called()

    def test_request_mood_only_if_idle_proceeds_when_idle(self):
        bed, mock_pygame, tracks = _make_bed()

        # No current track — engine is idle
        assert bed.current_track is None

        result = bed.request_mood("normal", force=True, boundary=True, only_if_idle=True)

        # Must proceed (return True — a track was published)
        assert result is True, (
            "request_mood(only_if_idle=True) must proceed when current_track is None"
        )
        assert bed.current_track is not None


# ---------------------------------------------------------------------------
# FIX 3 — worker exceptions are logged, not swallowed
# ---------------------------------------------------------------------------

class TestDispatchAudioPlayLogsException:
    """_dispatch_audio_play must log worker exceptions, not swallow them silently."""

    def test_dispatch_audio_play_logs_worker_exception(self):
        app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
        try:
            app = object.__new__(app_shell.VocalAIApp)

            done_event = threading.Event()
            logged_messages = []

            # Capture what gets logged on the module logger
            import logging
            original_warning = app_shell.logger.warning

            def capture_warning(msg, *args, **kwargs):
                logged_messages.append(msg % args if args else msg)
                done_event.set()

            app_shell.logger.warning = capture_warning

            try:
                def boom():
                    raise RuntimeError("simulated mixer failure")

                # Must not raise on the calling thread
                app_shell.VocalAIApp._dispatch_audio_play(app, boom)

                # Wait for the worker to run
                fired = done_event.wait(timeout=3.0)
                assert fired, "Worker exception was never logged — it was silently swallowed"

                # The logged message must mention the error
                assert any("simulated mixer failure" in m for m in logged_messages), (
                    f"Expected error text in logged messages, got: {logged_messages}"
                )
            finally:
                app_shell.logger.warning = original_warning
        finally:
            _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# FIX 1 — concurrent Phase-1 must not double-consume _last_looping_track_id
# ---------------------------------------------------------------------------

class TestConcurrentPhase1ConsumesLoopingIdOnce:
    """Two concurrent _play_selected Phase-1 sections must not both null
    _last_looping_track_id before either publishes.  Only the WINNING generation
    (the one that passes the Phase-3 guard) may mutate selection state.

    Setup: _last_looping_track_id = tracks[0].id (the track to skip).
    Two plays are started with interleaved Phase-1 sections (both read before
    either publishes) — the highest seq wins Phase 3.

    Assertions:
      (a) _last_looping_track_id is None after the winner publishes (consumed once
          by the winner).
      (b) current_track is NOT tracks[0] (the skipped looping track).
    """

    def test_concurrent_phase1_consumes_looping_id_once(self):
        bed, mock_pygame, tracks = _make_bed(num_tracks=2)
        assert len(tracks) == 2

        looping_track = tracks[0]
        other_track = tracks[1]

        # Set the looping track to be skipped
        bed._last_looping_track_id = looping_track.id

        # Two valid tracks so len(candidates) > 1 triggers the looping-filter.
        # The filter removes looping_track, leaving only other_track — so both
        # Phase-1s are forced to pick other_track (deterministic).
        bed.library.valid_tracks.return_value = [looping_track, other_track]

        # Strategy: t1 blocks in Phase 2 (decode) so t2 can complete fully first
        # (winning Phase 3 with seq=2).  Then t1 is released and its Phase 3
        # fails the guard (seq=1 != 2) without modifying selection state.
        #
        # Both Phase-1s run before t1's Phase-2 begins because t1's decode is the
        # only blocking call.  We use three events:
        #   t1_decode_started  -- set by t1 when it enters Sound()
        #   t2_done            -- set by t2 when request_mood returns
        #   t1_decode_gate     -- released by the test after t2 finishes
        t1_decode_started = threading.Event()
        t2_done = threading.Event()
        t1_decode_gate = threading.Event()
        call_lock = threading.Lock()
        call_count = [0]

        def controlled_sound(path):
            with call_lock:
                idx = call_count[0]
                call_count[0] += 1
            if idx == 0:
                # First decode (t1): signal entry, then block.
                t1_decode_started.set()
                t1_decode_gate.wait(timeout=5.0)
            # Second and subsequent: return immediately.
            return MagicMock()

        mock_pygame.mixer.Sound.side_effect = controlled_sound

        errors = []

        def play_t1():
            try:
                bed.request_mood("normal", force=True)
            except Exception as exc:
                errors.append(exc)

        def play_t2():
            try:
                bed.request_mood("normal", force=True)
            except Exception as exc:
                errors.append(exc)
            finally:
                t2_done.set()

        t1 = threading.Thread(target=play_t1, daemon=True)
        t1.start()

        # Wait for t1 to be blocked inside Sound()
        assert t1_decode_started.wait(timeout=3.0), "t1 decode never started"

        # Now t2 runs: its Phase-1 sees _last_looping_track_id still set (t1 has
        # not yet committed Phase 3), and its Phase-2 is instant.
        t2 = threading.Thread(target=play_t2, daemon=True)
        t2.start()

        # Wait for t2 to fully complete (it wins Phase 3, seq=2)
        assert t2_done.wait(timeout=3.0), "t2 never finished"

        # Release t1 -- its Phase 3 will find seq=1 != _play_seq=2, drop silently.
        t1_decode_gate.set()
        t1.join(timeout=5.0)
        t2.join(timeout=5.0)

        assert not errors, f"Unexpected exceptions: {errors}"

        # (a) looping id must be nulled by t2's winning Phase-3 commit
        assert bed._last_looping_track_id is None, (
            "_last_looping_track_id must be nulled by the winner's Phase-3 commit"
        )

        # (b) current_track must NOT be the looping track we meant to skip
        assert bed.current_track is not None, "current_track must not be None after plays"
        assert bed.current_track.id != looping_track.id, (
            f"current_track must NOT be {looping_track.id} (the looping track we skipped); "
            f"got {bed.current_track.id}"
        )
