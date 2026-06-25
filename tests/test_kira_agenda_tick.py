"""FR2 — idle-tick suggestion recompute off the Tk main thread.

Strict TDD: tests are written BEFORE the implementation. They are expected to
be RED against the current app_shell.py (synchronous inline recompute) and
GREEN after FR2 lands (_dispatch_suggestion_recompute + _apply_idle_suggestions).

Test coverage:
  1. test_idle_recompute_runs_off_calling_thread
     Heavy reads (summarize / compute_vibe / get_recent_context_snapshots) must
     NOT run on the calling thread — they must run on a worker thread.

  2. test_recompute_applied_via_safe_after_on_success
     On success, suggest_topics must be called (via _safe_after marshal) after
     the worker completes, not directly from the worker.

  3. test_stale_recompute_superseded_by_newer_generation
     If _suggestion_gen advances before the worker's apply runs, suggest_topics
     must NOT be called (supersession guard).

  4. test_recompute_dropped_when_agenda_leaves_idle
     If agenda state is no longer IDLE when _apply_idle_suggestions runs,
     suggest_topics must NOT be called.

  5. test_tick_reschedules_synchronously
     _kira_agenda_schedule_tick(4500) must still be called on the calling thread
     within _kira_agenda_tick even when a recompute is dispatched.

  6. test_worker_exception_is_logged_not_swallowed
     If summarize() raises, no exception propagates to the caller and
     logger.exception (or logger.error) is invoked. suggest_topics is NOT called.
"""

from __future__ import annotations

import importlib
import sys
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Harness helpers (mirror test_app_shell_obs_resilience pattern)
# ---------------------------------------------------------------------------

def _import_app_shell_with_ui_deps_mocked():
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


def _make_app_with_idle_tick_conditions(app_shell_module):
    """Return a VocalAIApp shell pre-configured for the idle recompute trigger.

    Conditions that must hold in _kira_agenda_tick for the recompute path:
      - kira_agenda.state == AgendaState.IDLE
      - kira_agenda.queued_topics() returns falsy
      - _idle_ticks == 2 so this tick increments to 3 (>= 3 triggers)
      - kira_agenda.can_suggest() returns True
      - smart_agg is set with a valid _session_id

    Also stubs the full set of attributes that _kira_agenda_tick touches
    (motor_ia, next_action returning a "none" action, _on_stream_admin_log, etc.)
    so the tick can reach the idle-recompute branch without blowing up.
    """
    AgendaState = app_shell_module.AgendaState
    AgendaAction = app_shell_module.AgendaAction

    app = object.__new__(app_shell_module.VocalAIApp)

    # Minimal agenda mock — next_action returns "none" so we reach idle branch
    agenda = MagicMock()
    agenda.state = AgendaState.IDLE
    agenda.queued_topics.return_value = []
    agenda.can_suggest.return_value = True
    agenda.topics = []
    agenda.last_outputs = []
    # active_topic must be non-None so the auto-exit block does NOT fire
    # (the auto-exit triggers when: kind==none AND state==IDLE AND active_topic is None AND empty queue).
    # By keeping a non-None active_topic we stay in the flow that reaches the idle-recompute branch.
    agenda.active_topic = MagicMock()
    agenda.next_action.return_value = AgendaAction.none()
    app.kira_agenda = agenda

    # Idle tick counter: start at 2 so the next increment hits 3
    app._idle_ticks = 2
    # Generation counter (FR2 field — may not exist yet before implementation)
    app._suggestion_gen = 0

    # Smart aggregator mock
    smart_agg = MagicMock()
    smart_agg._session_id = "test-session-123"
    smart_agg.intent_aggregator = MagicMock()
    smart_agg.thermometer = MagicMock()
    smart_agg.history = MagicMock()
    app.smart_agg = smart_agg

    # motor_ia stub — _kira_agenda_tick reads is_processing and is_speaking
    app.motor_ia = MagicMock()
    app.motor_ia.is_processing = False
    app.motor_ia.is_speaking = False

    # Stub _enqueue_kira_agenda_action (called after next_action)
    app._enqueue_kira_agenda_action = MagicMock()

    # Stub _on_stream_admin_log to avoid cascading attribute errors
    app._on_stream_admin_log = MagicMock()

    # Synchronous _safe_after (so _apply_idle_suggestions runs deterministically)
    # Records calls for the "applied via _safe_after" test
    safe_after_calls = []

    def sync_safe_after(fn, delay_ms=0):
        safe_after_calls.append(fn)
        fn()

    app._safe_after = sync_safe_after
    app._safe_after_calls = safe_after_calls

    # _kira_agenda_update_status and _kira_agenda_schedule_tick as mocks
    app._kira_agenda_update_status = MagicMock()
    app._kira_agenda_schedule_tick = MagicMock()

    # Closing guard
    app._closing = False

    return app


# ---------------------------------------------------------------------------
# Test 1 — heavy reads run off the calling thread
# ---------------------------------------------------------------------------

class TestIdleRecomputeRunsOffCallingThread:
    """summarize(), compute_vibe(), and get_recent_context_snapshots() must NOT
    run on the calling (main/test) thread — they must run on a worker thread.

    RED reason: current implementation calls them inline in _kira_agenda_tick
    on the calling thread.
    """

    def test_idle_recompute_runs_off_calling_thread(self):
        app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
        try:
            app = _make_app_with_idle_tick_conditions(app_shell)

            calling_thread = threading.current_thread()
            worker_thread_seen = [None]
            worker_started = threading.Event()
            worker_done = threading.Event()

            # Patch _safe_after to capture-and-invoke synchronously but let
            # the worker run first.  We need to wait for the worker to finish
            # before checking.
            def sync_safe_after(fn, delay_ms=0):
                # Just invoke — makes _apply_idle_suggestions run synchronously
                # after the worker's lambda posts it.
                fn()

            app._safe_after = sync_safe_after

            def recording_summarize():
                worker_thread_seen[0] = threading.current_thread()
                worker_started.set()
                worker_done.set()
                return "intent summary"

            app.smart_agg.intent_aggregator.summarize.side_effect = recording_summarize
            app.smart_agg.thermometer.compute_vibe.return_value = {"temperature": 60}
            app.smart_agg.history.get_recent_context_snapshots.return_value = []

            with patch.object(app_shell, "generate_suggestions", return_value=[]):
                app_shell.VocalAIApp._kira_agenda_tick(app)

            # Wait for worker
            assert worker_done.wait(timeout=3.0), "Worker (summarize) never ran"

            assert worker_thread_seen[0] is not None
            assert worker_thread_seen[0] is not calling_thread, (
                "summarize() must run on a worker thread, not the calling (UI) thread. "
                "FR2 is not yet implemented — _kira_agenda_tick still does the recompute inline."
            )
        finally:
            _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# Test 2 — suggest_topics applied via _safe_after marshal
# ---------------------------------------------------------------------------

class TestRecomputeAppliedViaSafeAfterOnSuccess:
    """On success, suggest_topics must be called only after marshaling back
    through _safe_after (not called directly from the worker thread).

    We verify this by replacing _safe_after with a capturing mock that records
    callables and invokes them, then checking suggest_topics was not called
    BEFORE _safe_after posted the callback.
    """

    def test_recompute_applied_via_safe_after_on_success(self):
        app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
        try:
            app = _make_app_with_idle_tick_conditions(app_shell)

            suggest_topics_called_before_safe_after = [False]
            safe_after_received_fn = threading.Event()
            pending_fn = [None]

            def deferred_safe_after(fn, delay_ms=0):
                # Record the fn but DON'T call it yet — we verify suggest_topics
                # has NOT been called at this point.
                pending_fn[0] = fn
                safe_after_received_fn.set()

            app._safe_after = deferred_safe_after

            suggestions_stub = ["test topic"]
            app.smart_agg.intent_aggregator.summarize.return_value = "summary"
            app.smart_agg.thermometer.compute_vibe.return_value = {"temperature": 50}
            app.smart_agg.history.get_recent_context_snapshots.return_value = []

            with patch.object(app_shell, "generate_suggestions", return_value=suggestions_stub):
                app_shell.VocalAIApp._kira_agenda_tick(app)

            # Wait for worker to post to _safe_after
            assert safe_after_received_fn.wait(timeout=3.0), (
                "_safe_after was never called with the apply callback. "
                "FR2 must marshal the result back via _safe_after."
            )

            # At this point, suggest_topics must NOT have been called yet
            # (the callback is pending but not invoked)
            suggest_topics_not_called_yet = not app.kira_agenda.suggest_topics.called
            assert suggest_topics_not_called_yet, (
                "suggest_topics must NOT be called directly from the worker — "
                "it must go through _safe_after first."
            )

            # Now invoke the pending callback (simulating Tkinter after() dispatch)
            pending_fn[0]()

            # Now suggest_topics MUST have been called
            app.kira_agenda.suggest_topics.assert_called_once_with(suggestions_stub)
        finally:
            _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# Test 3 — stale recompute superseded by newer generation
# ---------------------------------------------------------------------------

class TestStaleRecomputeSupersededByNewerGeneration:
    """If _suggestion_gen advances before the worker's apply callback runs,
    suggest_topics must NOT be called (supersession guard 1).
    """

    def test_stale_recompute_superseded_by_newer_generation(self):
        app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
        try:
            app = _make_app_with_idle_tick_conditions(app_shell)

            pending_fn = [None]
            safe_after_received = threading.Event()

            def deferred_safe_after(fn, delay_ms=0):
                pending_fn[0] = fn
                safe_after_received.set()

            app._safe_after = deferred_safe_after

            app.smart_agg.intent_aggregator.summarize.return_value = "summary"
            app.smart_agg.thermometer.compute_vibe.return_value = {"temperature": 50}
            app.smart_agg.history.get_recent_context_snapshots.return_value = []

            with patch.object(app_shell, "generate_suggestions", return_value=["a topic"]):
                app_shell.VocalAIApp._kira_agenda_tick(app)

            assert safe_after_received.wait(timeout=3.0), "Worker never posted to _safe_after"

            # Simulate a newer tick: bump _suggestion_gen before the apply runs
            app._suggestion_gen += 1

            # Now invoke the (stale) apply callback
            pending_fn[0]()

            # suggest_topics must NOT have been called (superseded)
            app.kira_agenda.suggest_topics.assert_not_called()
        finally:
            _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# Test 4 — agenda left IDLE before apply runs
# ---------------------------------------------------------------------------

class TestRecomputeDroppedWhenAgendaLeavesIdle:
    """If agenda state is no longer IDLE when _apply_idle_suggestions runs,
    suggest_topics must NOT be called (supersession guard 2).
    """

    def test_recompute_dropped_when_agenda_leaves_idle(self):
        app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
        try:
            AgendaState = app_shell.AgendaState
            app = _make_app_with_idle_tick_conditions(app_shell)

            pending_fn = [None]
            safe_after_received = threading.Event()

            def deferred_safe_after(fn, delay_ms=0):
                pending_fn[0] = fn
                safe_after_received.set()

            app._safe_after = deferred_safe_after

            app.smart_agg.intent_aggregator.summarize.return_value = "summary"
            app.smart_agg.thermometer.compute_vibe.return_value = {"temperature": 50}
            app.smart_agg.history.get_recent_context_snapshots.return_value = []

            with patch.object(app_shell, "generate_suggestions", return_value=["a topic"]):
                app_shell.VocalAIApp._kira_agenda_tick(app)

            assert safe_after_received.wait(timeout=3.0), "Worker never posted to _safe_after"

            # Simulate agenda leaving IDLE (e.g. went OFF or SPEAKING) before apply runs
            app.kira_agenda.state = AgendaState.OFF

            # Now invoke the apply callback
            pending_fn[0]()

            # suggest_topics must NOT have been called
            app.kira_agenda.suggest_topics.assert_not_called()
        finally:
            _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# Test 5 — tick reschedules synchronously even when recompute is dispatched
# ---------------------------------------------------------------------------

class TestTickReschedulesSynchronously:
    """_kira_agenda_schedule_tick(4500) must be called synchronously within
    _kira_agenda_tick regardless of whether a recompute worker is running.

    The tick loop must not block waiting for the worker.
    """

    def test_tick_reschedules_synchronously(self):
        app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
        try:
            app = _make_app_with_idle_tick_conditions(app_shell)

            # Make summarize block briefly to confirm the tick doesn't wait
            worker_unblocked = threading.Event()

            def slow_summarize():
                # Block until we tell it to proceed (we'll set this AFTER checking)
                worker_unblocked.wait(timeout=5.0)
                return "summary"

            app.smart_agg.intent_aggregator.summarize.side_effect = slow_summarize
            app.smart_agg.thermometer.compute_vibe.return_value = {"temperature": 50}
            app.smart_agg.history.get_recent_context_snapshots.return_value = []

            with patch.object(app_shell, "generate_suggestions", return_value=[]):
                app_shell.VocalAIApp._kira_agenda_tick(app)

            # _kira_agenda_schedule_tick must have been called on THIS thread
            # BEFORE the worker has a chance to finish (since summarize is still blocking)
            app._kira_agenda_schedule_tick.assert_called_once_with(4500)

            # Unblock the worker so it can finish cleanly
            worker_unblocked.set()
        finally:
            _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# Test 6 — worker exception is logged, not swallowed
# ---------------------------------------------------------------------------

class TestWorkerExceptionIsLoggedNotSwallowed:
    """If summarize() raises inside the worker, no exception propagates to the
    caller and logger.exception (or logger.error) is invoked. suggest_topics
    is NOT called.
    """

    def test_worker_exception_is_logged_not_swallowed(self):
        app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
        try:
            app = _make_app_with_idle_tick_conditions(app_shell)

            worker_done = threading.Event()

            # Use deferred _safe_after: the worker exception path returns early
            # so _safe_after should NOT be called.
            safe_after_called = [False]

            def deferred_safe_after(fn, delay_ms=0):
                safe_after_called[0] = True
                fn()

            app._safe_after = deferred_safe_after

            def boom():
                raise RuntimeError("simulated aggregator failure")

            app.smart_agg.intent_aggregator.summarize.side_effect = boom

            with patch.object(app_shell.logger, "exception") as mock_log_exc:
                with patch.object(app_shell.logger, "error") as mock_log_err:
                    # Drive the tick
                    app_shell.VocalAIApp._kira_agenda_tick(app)

                    # Wait for the worker to run (it raises, then logs)
                    # We need a small poll since we can't join the daemon thread directly
                    import time
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline:
                        if mock_log_exc.called or mock_log_err.called:
                            break
                        time.sleep(0.05)

                    # At least one of logger.exception / logger.error must have fired
                    assert mock_log_exc.called or mock_log_err.called, (
                        "Worker exception must be logged via logger.exception or logger.error. "
                        "FR2 must NOT silently swallow the exception (confirmed review finding on FR3)."
                    )

            # suggest_topics must NOT have been called
            app.kira_agenda.suggest_topics.assert_not_called()

            # No exception must have reached this thread
            # (the test itself not exploding IS the evidence, but we verify via the log)
        finally:
            _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# Test 7 — recompute uses the REAL Aggregator attribute names (B2 regression)
# ---------------------------------------------------------------------------

class TestRecomputeUsesRealAggregatorAttributeNames:
    """The worker must read the attributes that the real Aggregator exposes:
    intent_aggregator, thermometer (NOT thermometer), history.

    Uses a SimpleNamespace mirroring the real Aggregator so a wrong attribute
    name raises AttributeError (instead of MagicMock auto-vivifying it), which
    would make the recompute a silent no-op. RED before the thermometer ->
    thermometer fix; GREEN after.
    """

    def test_recompute_uses_thermometer_attribute(self):
        app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
        try:
            app = _make_app_with_idle_tick_conditions(app_shell)

            # Real Aggregator attribute surface (no MagicMock auto-vivification):
            # thermometer (NOT thermometer), intent_aggregator, history.
            app.smart_agg = SimpleNamespace(
                _session_id="s1",
                intent_aggregator=SimpleNamespace(summarize=lambda: "summary"),
                thermometer=SimpleNamespace(compute_vibe=lambda: {"temperature": 55}),
                history=SimpleNamespace(
                    get_recent_context_snapshots=lambda sid, max_items=3: []
                ),
            )

            pending_fn = [None]
            posted = threading.Event()

            def deferred_safe_after(fn, delay_ms=0):
                pending_fn[0] = fn
                posted.set()

            app._safe_after = deferred_safe_after

            with patch.object(app_shell, "generate_suggestions", return_value=["topic"]):
                app_shell.VocalAIApp._kira_agenda_tick(app)
                # With the wrong attribute name the worker raises AttributeError,
                # logs, and returns WITHOUT posting to _safe_after.
                assert posted.wait(timeout=3.0), (
                    "recompute worker never posted to _safe_after — it likely hit "
                    "AttributeError on smart_agg.thermometer (must be .thermometer)"
                )
                pending_fn[0]()

            app.kira_agenda.suggest_topics.assert_called_once_with(["topic"])
        finally:
            _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# Test 8 — late apply is dropped after teardown (_closing guard, B3)
# ---------------------------------------------------------------------------

class TestClosingGuardDropsLateApply:
    """A worker that completes during/after on_closing must not touch Tk/agenda.
    _apply_idle_suggestions must drop when _closing is True (mirrors the
    established post-teardown guard used elsewhere in app_shell)."""

    def test_apply_idle_suggestions_drops_when_closing(self):
        app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
        try:
            app = _make_app_with_idle_tick_conditions(app_shell)
            app._closing = True  # window is tearing down

            # gen matches and state is IDLE, so only the _closing guard can stop it.
            app_shell.VocalAIApp._apply_idle_suggestions(app, app._suggestion_gen, ["topic"])

            app.kira_agenda.suggest_topics.assert_not_called()
        finally:
            _restore_app_shell_module(old_module)
