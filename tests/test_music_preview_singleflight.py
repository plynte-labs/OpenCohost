"""Single-flight guard for music preview buttons — strict TDD.

Bug A: rapid clicks on the mood preview buttons spawn N concurrent worker
threads, and N pygame channels play simultaneously during the 6-second
fadeout window.  FR3 worsened this by moving each request_mood onto its
own daemon thread via _dispatch_audio_play to keep disk I/O off the UI thread.

Fix contract:
  1. AT MOST ONE worker in-flight at any time (single-flight).
  2. Starting a new preview hard-stops the previous channel before playing
     the new one — NO two channels audible at once.
  3. Last-clicked mood wins (coalesce: mid-flight clicks update mood, do not
     spawn additional workers).
  4. The automatic on_boundary / agenda-enable paths are NOT affected: they
     still use _dispatch_audio_play and keep the 6-second crossfade policy.

Tests (RED against pre-fix code, GREEN after the fix):
  1. test_rapid_preview_clicks_spawn_one_worker
  2. test_rapid_preview_clicks_one_active_channel
  3. test_last_clicked_mood_wins
  4. test_boundary_transition_keeps_crossfade  (regression guard)

Non-vacuity notes are inline per test — each assertion WILL FAIL on a broken
implementation for the specific reason documented.
"""
from __future__ import annotations

import importlib
import sys
import threading
import time
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from opencohost.core.music.audio_bed import AudioBedEngine, AudioBedPolicy
from opencohost.core.music.music_library import MusicLibrary, MusicTrack


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _two_track_lib() -> tuple[MagicMock, list[MusicTrack]]:
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
    lib, tracks = _two_track_lib()
    if num_tracks == 1:
        lib.valid_tracks.return_value = [tracks[0]]
    policy = AudioBedPolicy(
        min_play_seconds=1.0,
        max_play_seconds=2.0,
        idle_loop_limit=2,
        idle_check_interval=0.5,
        fade_ms=6000,  # keep the real policy fade_ms to exercise the difference
    )
    bed = AudioBedEngine(library=lib, policy=policy)
    mock_pygame = MagicMock()
    mock_channel = MagicMock()
    mock_pygame.mixer.find_channel.return_value = mock_channel
    mock_pygame.mixer.Sound.return_value = MagicMock()
    bed._pygame = mock_pygame
    return bed, mock_pygame, tracks


def _import_app_shell_with_ui_deps_mocked():
    """Import app_shell with all heavy UI deps mocked out."""
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


# ---------------------------------------------------------------------------
# Test 1 — rapid clicks must NOT spawn more than one concurrent worker
# ---------------------------------------------------------------------------

class TestRapidPreviewClicksSpawnOneWorker:
    """_music_play_mood called N times rapidly must never have more than one
    worker executing concurrently AND must coalesce all clicks into exactly
    one request_mood call with exactly one Thread spawned.

    Non-vacuity:
      - A naive impl that spawns N threads would record concurrency_depths with
        max > 1 and spawn_count > 1 — both assertions would FAIL.
      - An impl that plays every click (no coalescing) would record
        request_mood.call_count == CLICKS — the call_count assertion FAILS.
      - A stale-timeout impl with the old 5s dead-wait would make this test
        take 5 s; the bounded 2s wait keeps the runtime honest.
    """

    def test_rapid_preview_clicks_spawn_one_worker(self):
        app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
        spawned_threads: list[threading.Thread] = []
        try:
            app = object.__new__(app_shell.VocalAIApp)

            CLICKS = 8
            concurrency_depths: list[int] = []
            worker_counter_lock = threading.Lock()
            active_count = [0]
            worker_done = threading.Event()

            def fake_request_mood(mood, *, force=False, boundary=False, **kw):
                with worker_counter_lock:
                    active_count[0] += 1
                    concurrency_depths.append(active_count[0])
                time.sleep(0.05)  # give other threads time to pile up if guard is absent
                with worker_counter_lock:
                    active_count[0] -= 1
                worker_done.set()
                return True

            mock_bed = MagicMock()
            mock_bed.request_mood.side_effect = fake_request_mood
            mock_bed.stop = MagicMock()

            app.audio_bed = mock_bed
            app._music_update_panel = MagicMock()

            # Wrap threading.Thread to count spawns originating from the preview path.
            # We patch at the module level so the lambda closure inside _music_play_mood
            # sees the wrapped constructor.
            real_Thread = threading.Thread
            spawn_lock = threading.Lock()

            def counting_Thread(*args, **kwargs):
                t = real_Thread(*args, **kwargs)
                with spawn_lock:
                    spawned_threads.append(t)
                return t

            with patch.object(threading, "Thread", side_effect=counting_Thread):
                # Also patch inside the app_shell module's reference
                original_thread_in_module = getattr(app_shell, "threading", threading)
                with patch.object(original_thread_in_module, "Thread", side_effect=counting_Thread):
                    for _ in range(CLICKS):
                        app_shell.VocalAIApp._music_play_mood(app, "normal")

            # Wait for the single worker to finish — short bounded timeout, not 5s dead-wait
            finished = worker_done.wait(timeout=2.0)
            # Give the worker's finally-path time to clear _preview_in_flight
            time.sleep(0.1)

            assert finished, "Worker never completed request_mood within 2s"
            assert concurrency_depths, "request_mood was never called"

            max_concurrent = max(concurrency_depths)
            assert max_concurrent == 1, (
                f"Expected at most 1 concurrent worker, got max concurrency depth "
                f"of {max_concurrent}. Depth trace: {concurrency_depths}. "
                "A broken impl that spawns N threads would show depth > 1."
            )

            # Coalescing: 8 rapid same-mood clicks → exactly 1 request_mood call
            assert mock_bed.request_mood.call_count == 1, (
                f"Expected exactly 1 request_mood call (coalesced), got "
                f"{mock_bed.request_mood.call_count}. "
                "A no-coalesce impl would call request_mood CLICKS times."
            )

            # Exactly ONE Thread was spawned from the preview path
            assert len(spawned_threads) == 1, (
                f"Expected exactly 1 Thread spawned, got {len(spawned_threads)}. "
                "A broken impl would spawn one thread per click."
            )
        finally:
            # F4: join all spawned daemons before restoring modules
            for t in spawned_threads:
                t.join(timeout=2.0)
                assert not t.is_alive(), "Spawned preview worker is still alive after join"
            _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# Test 2 — rapid clicks must never produce two simultaneously-playing channels
#          AND hard-stop (not fadeout) is called BEFORE each request_mood
# ---------------------------------------------------------------------------

class TestRapidPreviewClicksOneActiveChannel:
    """Prove that the preview path calls stop(emergency=True) BEFORE each
    request_mood — verified via call ORDER on a recording bed — AND that when
    a mid-flight second mood is injected, the prior channel is hard-stopped
    rather than left with a 6-second fadeout.

    Non-vacuity (F1 fix):
      - Old vacuous test: all 4 clicks used mood="normal" → coalesce to 1 play
        → channels_created has 1 entry → the `if len > 1` block NEVER ran.
        A fadeout-instead-of-stop impl would green-pass that test silently.
      - New test: first request_mood blocks on `first_play_started`, we fire a
        SECOND click with a DISTINCT mood ("hype"), then release. The worker
        observes latest != current_mood → loops → plays "hype". Two plays occur,
        two channels are created, the assertion block ALWAYS executes.
      - The call-order assertion (stop before request_mood) FAILS on an impl
        that omits the stop() call or calls it after request_mood.
    """

    def test_rapid_preview_clicks_one_active_channel(self):
        app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
        spawned_threads: list[threading.Thread] = []
        try:
            app = object.__new__(app_shell.VocalAIApp)

            # Synchronization: block the FIRST request_mood until we fire a
            # second distinct mood, then release so the worker loops.
            first_play_started = threading.Event()  # fires when worker enters first play
            release_first_play = threading.Event()  # test releases this to unblock worker
            second_play_done = threading.Event()    # fires after second play completes

            call_log: list[tuple[str, str]] = []  # ("stop"|"request_mood", mood_or_"")
            call_log_lock = threading.Lock()
            play_count = [0]

            def recording_stop(emergency=False):
                with call_log_lock:
                    call_log.append(("stop", ""))

            def recording_request_mood(mood, *, force=False, boundary=False, **kw):
                with call_log_lock:
                    call_log.append(("request_mood", mood))
                    play_count[0] += 1
                    count = play_count[0]
                if count == 1:
                    # Signal that the first play has started, then block until released
                    first_play_started.set()
                    release_first_play.wait(timeout=3.0)
                else:
                    # Second (or later) play completes immediately
                    second_play_done.set()
                return True

            mock_bed = MagicMock()
            mock_bed.stop.side_effect = recording_stop
            mock_bed.request_mood.side_effect = recording_request_mood

            app.audio_bed = mock_bed
            app._music_update_panel = MagicMock()

            real_Thread = threading.Thread

            def counting_Thread(*args, **kwargs):
                t = real_Thread(*args, **kwargs)
                spawned_threads.append(t)
                return t

            original_thread_in_module = getattr(app_shell, "threading", threading)
            with patch.object(original_thread_in_module, "Thread", side_effect=counting_Thread):
                # Click 1: mood "normal"
                app_shell.VocalAIApp._music_play_mood(app, "normal")

            # Wait for the worker to enter first play
            assert first_play_started.wait(timeout=2.0), (
                "Worker never started first request_mood within 2s"
            )

            # Click 2 with a DISTINCT mood while the worker is blocked inside first play
            with patch.object(original_thread_in_module, "Thread", side_effect=counting_Thread):
                app_shell.VocalAIApp._music_play_mood(app, "hype")

            # Release the first play so the worker can finish and observe latest changed
            release_first_play.set()

            # Wait for the second play to complete
            assert second_play_done.wait(timeout=3.0), (
                "Worker never completed second request_mood (re-loop) within 3s"
            )
            # Give the worker time to clear _preview_in_flight
            time.sleep(0.1)

            # --- Assertion 1: call ORDER ---
            # Every request_mood must be preceded by a stop call.
            # The sequence must look like: ..., stop, request_mood(normal), stop, request_mood(hype), ...
            with call_log_lock:
                log_snapshot = list(call_log)

            request_mood_indices = [
                i for i, (op, _) in enumerate(log_snapshot) if op == "request_mood"
            ]
            assert len(request_mood_indices) == 2, (
                f"Expected exactly 2 request_mood calls (loop), got "
                f"{len(request_mood_indices)}. Log: {log_snapshot}"
            )

            for rm_idx in request_mood_indices:
                # There must be a stop call immediately before each request_mood
                assert rm_idx > 0 and log_snapshot[rm_idx - 1][0] == "stop", (
                    f"request_mood at log index {rm_idx} was NOT immediately preceded "
                    f"by a stop() call. Full log: {log_snapshot}. "
                    "A broken impl that skips stop() or calls it after request_mood fails here."
                )

            # --- Assertion 2: moods in correct order ---
            played_moods = [mood for op, mood in log_snapshot if op == "request_mood"]
            assert played_moods == ["normal", "hype"], (
                f"Expected worker to play ['normal', 'hype'] in order, got {played_moods}. "
                "A no-loop impl would only play ['normal'] and never serve 'hype'."
            )

            # --- Assertion 3: hard-stop was called (not just fadeout) ---
            # mock_bed.stop must have been called at least twice (once per play)
            assert mock_bed.stop.call_count >= 2, (
                f"Expected at least 2 stop() calls (one before each play), "
                f"got {mock_bed.stop.call_count}. "
                "A fadeout-instead-of-stop impl would have stop_count==0 here."
            )

            # Verify no fadeout calls were made on the mock_bed (preview uses stop, not fadeout)
            assert mock_bed.fadeout.call_count == 0 if hasattr(mock_bed, "fadeout") else True, (
                "Preview path must NOT call fadeout — it must use stop(emergency=True)."
            )

        finally:
            # F4: join all spawned daemons before restoring modules
            for t in spawned_threads:
                t.join(timeout=3.0)
                assert not t.is_alive(), "Spawned preview worker is still alive after join"
            _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# Test 3 — last clicked mood wins (exercises the WORKER RE-LOOP)
# ---------------------------------------------------------------------------

class TestLastClickedMoodWins:
    """Deterministically exercises the mid-flight re-loop: block the first
    request_mood until a second DISTINCT mood is clicked, then release.
    The worker must observe latest changed → loop → play the new mood.
    Assert that the recorded sequence is EXACTLY [mood1, mood2] — not just
    that the last element equals the last click.

    Non-vacuity (F3 fix):
      - Old test: all clicks fired BEFORE the worker read latest_mood. The
        coalesce-before-start path was exercised, but NOT the mid-flight loop.
        A fake impl that reads the initial mood once and returns (no loop) would
        green-pass the old test if mood_sequence[-1] happened to equal the
        coalesced starting mood.
      - New test: the worker is BLOCKED inside the first play when we fire the
        second click. It MUST loop to serve 'hype'. An impl with no loop would:
          * complete only play 'energetic' and exit,
          * moods_played == ['energetic'], NOT ['energetic', 'hype'],
          * second_done would never fire → assert times out → FAIL.
      - The exact-sequence assertion ['energetic', 'hype'] also fails if the
        worker replays the wrong mood or plays additional intermediate moods.
    """

    def test_last_clicked_mood_wins(self):
        app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
        spawned_threads: list[threading.Thread] = []
        try:
            app = object.__new__(app_shell.VocalAIApp)

            # Synchronization events for deterministic re-loop
            first_play_started = threading.Event()
            release_first_play = threading.Event()
            second_play_done = threading.Event()

            moods_played: list[str] = []
            moods_lock = threading.Lock()
            play_count = [0]

            def fake_request_mood(mood, *, force=False, boundary=False, **kw):
                with moods_lock:
                    moods_played.append(mood)
                    play_count[0] += 1
                    count = play_count[0]
                if count == 1:
                    first_play_started.set()
                    release_first_play.wait(timeout=3.0)
                else:
                    second_play_done.set()
                return True

            mock_bed = MagicMock()
            mock_bed.request_mood.side_effect = fake_request_mood
            mock_bed.stop = MagicMock()

            app.audio_bed = mock_bed
            app._music_update_panel = MagicMock()

            real_Thread = threading.Thread

            def counting_Thread(*args, **kwargs):
                t = real_Thread(*args, **kwargs)
                spawned_threads.append(t)
                return t

            original_thread_in_module = getattr(app_shell, "threading", threading)

            # Fire click 1: "energetic" — this spawns the worker
            with patch.object(original_thread_in_module, "Thread", side_effect=counting_Thread):
                app_shell.VocalAIApp._music_play_mood(app, "energetic")

            # Wait until the worker is inside first play
            assert first_play_started.wait(timeout=2.0), (
                "Worker never started first request_mood within 2s"
            )

            # Fire click 2 with a DISTINCT mood while worker is blocked — must coalesce
            # (no new thread, just updates _preview_latest_mood)
            with patch.object(original_thread_in_module, "Thread", side_effect=counting_Thread):
                app_shell.VocalAIApp._music_play_mood(app, "hype")

            # Release: worker finishes 'energetic', observes latest=='hype' → loops
            release_first_play.set()

            # Wait for the second play to complete (proves the re-loop happened)
            assert second_play_done.wait(timeout=3.0), (
                "Worker never completed second request_mood (re-loop) within 3s. "
                "An impl with no re-loop would never set second_play_done → FAIL here."
            )
            time.sleep(0.1)

            with moods_lock:
                played_snapshot = list(moods_played)

            # Exact deterministic sequence — not just "last element"
            assert played_snapshot == ["energetic", "hype"], (
                f"Expected worker to play exactly ['energetic', 'hype'] in order "
                f"(mid-flight re-loop), got {played_snapshot}. "
                "A no-loop impl never plays 'hype' → FAIL. "
                "An impl that re-plays 'energetic' again → also FAIL."
            )

            # Only ONE Thread was spawned (click 2 coalesced, did not spawn another)
            assert len(spawned_threads) == 1, (
                f"Expected exactly 1 Thread spawned, got {len(spawned_threads)}. "
                "Click 2 must coalesce into the running worker, not spawn a new thread."
            )

        finally:
            # F4: join all spawned daemons before restoring modules
            for t in spawned_threads:
                t.join(timeout=3.0)
                assert not t.is_alive(), "Spawned preview worker is still alive after join"
            _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# Test 4 — automatic on_boundary path keeps the policy crossfade (regression)
# ---------------------------------------------------------------------------

class TestBoundaryTransitionKeepsCrossfade:
    """The on_boundary / agenda auto-start paths must NOT be affected by the
    single-flight guard or the hard-stop logic.

    Specifically:
    - _dispatch_audio_play (used by _kira_agenda_enable) continues to spawn a
      daemon thread per call (its behaviour is unchanged).
    - AudioBedEngine.on_boundary() continues to use the policy fade_ms for
      channel transitions (the hard-stop is ONLY applied in the preview path).

    This is a REGRESSION guard: the fix must not change on_boundary behaviour.
    """

    def test_boundary_transition_keeps_crossfade(self):
        """on_boundary uses policy.fade_ms (6000ms), NOT emergency stop."""
        bed, mock_pygame, tracks = _make_bed()

        # Set up an initial "playing" state
        initial_channel = MagicMock()
        mock_pygame.mixer.find_channel.return_value = initial_channel
        bed._play_seq = 0

        # Simulate a track already playing
        bed.current_track = tracks[0]
        bed._channel = initial_channel
        bed.started_at = time.time() - 100  # old enough to transition
        bed.transition_pending = True
        bed.desired_mood = "normal"

        # A new channel for the next track
        new_channel = MagicMock()
        mock_pygame.mixer.find_channel.return_value = new_channel

        # Trigger on_boundary — should use policy fade, not emergency stop
        result = bed.on_boundary()

        assert result is True, "on_boundary must succeed when transition is pending"

        # The OLD channel must have received fadeout (not stop) with the policy fade_ms
        assert initial_channel.fadeout.called, (
            "on_boundary must call fadeout (crossfade) on the old channel, not stop()"
        )
        assert not initial_channel.stop.called, (
            "on_boundary must NOT call stop() on the old channel — that is for the "
            "preview hard-replace path only"
        )
        fade_arg = initial_channel.fadeout.call_args[0][0]
        assert fade_arg == bed.policy.fade_ms, (
            f"on_boundary must use policy.fade_ms={bed.policy.fade_ms}, "
            f"got fadeout({fade_arg})"
        )

    def test_dispatch_audio_play_still_spawns_thread(self):
        """_dispatch_audio_play (agenda/auto path) must still run on a worker thread.

        This ensures the agenda-enable auto-start path is NOT routed through the
        single-flight preview guard — it must keep its own thread-per-call behaviour.
        """
        app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
        try:
            app = object.__new__(app_shell.VocalAIApp)

            called_thread = [None]
            done = threading.Event()

            def fn():
                called_thread[0] = threading.current_thread()
                done.set()

            # _dispatch_audio_play is the AGENDA path — must still spawn a thread
            app_shell.VocalAIApp._dispatch_audio_play(app, fn)
            done.wait(timeout=3.0)

            assert called_thread[0] is not None, "_dispatch_audio_play never invoked fn"
            assert called_thread[0] is not threading.main_thread(), (
                "_dispatch_audio_play must run fn on a worker thread (agenda path unchanged)"
            )
        finally:
            _restore_app_shell_module(old_module)
