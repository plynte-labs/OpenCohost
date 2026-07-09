"""Characterization tests for the agenda/audio cluster — pinned BEFORE the
Phase 7 extraction move (track app_shell_agenda_audio_decomposition_20260624).

These tests assert EXISTING behavior of the agenda/audio cluster in
opencohost/ui/app_shell.py. They must be GREEN against the CURRENT code —
they are the proof that the upcoming move to
opencohost/ui/agenda_audio_controller.py (function-module + thin same-named
delegate, mirroring obs_lifecycle.py / motor_event_handlers.py) is
behavior-neutral. If any test here contradicts intended product behavior,
that is a separate bug to report — do not "fix" it silently in this slice.

Harness mirrors tests/test_app_shell_obs_resilience.py and
tests/test_kira_agenda_tick.py: `object.__new__(VocalAIApp)` shells stubbed
with the minimum attributes needed to reach the path under test, driving
unbound `VocalAIApp._method(shell, ...)` calls directly.

Cluster map (see the Phase 7 blueprint / characterization plan):
  A — tick loop / idle suggestions (auto-exit, paused/hard-paused, exceptions)
  B — prefetch (consume-pending-chat, prefetch-while-speaking)
  C — audio coordination / enable
  D — chat-filter lifecycle (save-once guard)
  E — status / aggregator glue (_on_smart_aggregated_context routing)
"""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Pre-import so lazy/deferred imports inside app_shell methods resolve against
# the same module objects mock.patch will later target (see
# test_app_shell_obs_resilience.py header comment for the eviction gotcha).
import opencohost.ui.app_shell  # noqa: F401
import opencohost.smart_aggregator.chat_input_contract  # noqa: F401


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
    with patch.dict(sys.modules, modules):
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


# ===========================================================================
# Cluster A — tick loop / idle suggestions
# ===========================================================================


def test_tick_auto_exit_declares_session_complete():
    """A1: none-action + IDLE + no active topic + empty queue -> auto-exit to
    OFF, 'Sesión completada' logged, chat filter restored, status refreshed,
    OBS joyita cleared, and NO reschedule."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        AgendaState = app_shell.AgendaState
        AgendaAction = app_shell.AgendaAction

        app = object.__new__(app_shell.VocalAIApp)
        agenda = MagicMock()
        agenda.state = AgendaState.IDLE
        agenda.active_topic = None
        agenda.queued_topics.return_value = []
        agenda.next_action.return_value = AgendaAction.none()
        app.kira_agenda = agenda
        app.motor_ia = MagicMock(is_processing=False, is_speaking=False)
        app._enqueue_kira_agenda_action = MagicMock()
        app._on_stream_admin_log = MagicMock()
        app._kira_agenda_restore_chat_filter = MagicMock()
        app._kira_agenda_update_status = MagicMock()
        app._kira_agenda_schedule_tick = MagicMock()
        app._clear_obs_joyita = MagicMock()

        app_shell.VocalAIApp._kira_agenda_tick(app)

        assert agenda.state == AgendaState.OFF
        logged = [c.args[0] for c in app._on_stream_admin_log.call_args_list]
        assert any("Sesión completada" in msg for msg in logged)
        app._kira_agenda_restore_chat_filter.assert_called_once()
        app._kira_agenda_update_status.assert_called_once()
        app._clear_obs_joyita.assert_called_once_with("KiraJoyita")
        app._kira_agenda_schedule_tick.assert_not_called()
    finally:
        _restore_app_shell_module(old_module)


def test_tick_paused_not_ready_reschedules_4500():
    """A2: PAUSED_NEEDS_OPERATOR + can_auto_resume()=False (state unchanged)
    -> update_status + schedule_tick(4500); next_action NOT called.

    motor_ia is intentionally left unset: the early return must happen
    before _kira_agenda_tick ever reads motor_ia (an AttributeError here
    would mean that guarantee broke)."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        AgendaState = app_shell.AgendaState

        app = object.__new__(app_shell.VocalAIApp)
        agenda = MagicMock()
        agenda.state = AgendaState.PAUSED_NEEDS_OPERATOR
        agenda.can_auto_resume.return_value = False
        app.kira_agenda = agenda
        app._on_stream_admin_log = MagicMock()
        app._kira_agenda_update_status = MagicMock()
        app._kira_agenda_schedule_tick = MagicMock()

        app_shell.VocalAIApp._kira_agenda_tick(app)

        agenda.next_action.assert_not_called()
        app._kira_agenda_update_status.assert_called_once()
        app._kira_agenda_schedule_tick.assert_called_once_with(4500)
    finally:
        _restore_app_shell_module(old_module)


def test_tick_next_action_raising_falls_back_to_none_and_reschedules():
    """A3: next_action() raising is caught, logged (logger.exception +
    operator log line), and the tick still reschedules — Kira must never go
    permanently silent because of an internal exception."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        AgendaState = app_shell.AgendaState

        app = object.__new__(app_shell.VocalAIApp)
        agenda = MagicMock()
        agenda.state = AgendaState.IDLE
        agenda.active_topic = MagicMock()  # non-None: skip the auto-exit block
        agenda.queued_topics.return_value = ["queued"]  # skip idle-suggestion path
        agenda.next_action.side_effect = RuntimeError("boom")
        app.kira_agenda = agenda
        app.motor_ia = MagicMock(is_processing=False, is_speaking=False)
        app._idle_ticks = 0
        app._enqueue_kira_agenda_action = MagicMock()
        app._on_stream_admin_log = MagicMock()
        app._kira_agenda_update_status = MagicMock()
        app._kira_agenda_schedule_tick = MagicMock()

        with patch.object(app_shell.logger, "exception") as mock_log_exc:
            app_shell.VocalAIApp._kira_agenda_tick(app)

        mock_log_exc.assert_called_once()
        logged = [c.args[0] for c in app._on_stream_admin_log.call_args_list]
        assert any("next_action" in msg for msg in logged)
        enqueued_action = app._enqueue_kira_agenda_action.call_args.args[0]
        assert enqueued_action.kind == "none"
        app._kira_agenda_schedule_tick.assert_called_once_with(4500)
    finally:
        _restore_app_shell_module(old_module)


def test_tick_hard_paused_after_failed_auto_resume_stops_ticking():
    """A4: can_auto_resume() returns False AND flips state to HARD_PAUSED as
    a side effect -> HARD_PAUSED log + update_status + NO reschedule. Pins
    the nested elif that is only reachable via can_auto_resume()'s internal
    state mutation — pin it, do not "fix" it."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        AgendaState = app_shell.AgendaState

        app = object.__new__(app_shell.VocalAIApp)
        agenda = MagicMock()
        agenda.state = AgendaState.PAUSED_NEEDS_OPERATOR
        agenda.recovery = SimpleNamespace(last_failure_reason="fallos repetidos")

        def _fail_to_hard_paused():
            agenda.state = AgendaState.HARD_PAUSED
            return False

        agenda.can_auto_resume.side_effect = _fail_to_hard_paused
        app.kira_agenda = agenda
        app._on_stream_admin_log = MagicMock()
        app._kira_agenda_update_status = MagicMock()
        app._kira_agenda_schedule_tick = MagicMock()

        app_shell.VocalAIApp._kira_agenda_tick(app)

        agenda.next_action.assert_not_called()
        logged = [c.args[0] for c in app._on_stream_admin_log.call_args_list]
        assert any("HARD_PAUSED" in msg for msg in logged)
        app._kira_agenda_update_status.assert_called_once()
        app._kira_agenda_schedule_tick.assert_not_called()
    finally:
        _restore_app_shell_module(old_module)


# ===========================================================================
# Cluster B — prefetch
# ===========================================================================


def test_consume_pending_chat_if_due_enqueues_and_clears():
    """B1 positive: pending compact chat + chat_signal_due()=True + an
    enqueue-kind action -> enqueued, pending cleared, status refreshed,
    returns True."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        agenda = MagicMock()
        agenda.chat_signal_due.return_value = True
        action = app_shell.AgendaAction(kind="enqueue", prompt="hola chat")
        agenda.next_action.return_value = action
        app.kira_agenda = agenda
        app._kira_agenda_pending_compact_chat = "hola chat"
        app._enqueue_kira_agenda_action = MagicMock()
        app._kira_agenda_update_status = MagicMock()

        result = app_shell.VocalAIApp._kira_agenda_consume_pending_chat_if_due(app)

        assert result is True
        agenda.next_action.assert_called_once_with(compact_chat="hola chat")
        app._enqueue_kira_agenda_action.assert_called_once_with(action)
        assert app._kira_agenda_pending_compact_chat == ""
        app._kira_agenda_update_status.assert_called_once()
    finally:
        _restore_app_shell_module(old_module)


@pytest.mark.parametrize(
    "pending, due, action_kind",
    [
        ("", True, "enqueue"),        # nothing pending
        ("hola", False, "enqueue"),   # signal not due yet
        ("hola", True, "none"),       # action resolves to non-enqueue
    ],
    ids=["empty-pending", "not-due", "non-enqueue-action"],
)
def test_consume_pending_chat_if_due_preserves_pending_when_not_consumed(pending, due, action_kind):
    """B1 counter-cases: returns False, nothing enqueued. Pins the exact
    current semantics that pending is NOT cleared even on the non-enqueue
    path (the clear only happens after the kind check)."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        agenda = MagicMock()
        agenda.chat_signal_due.return_value = due
        agenda.next_action.return_value = app_shell.AgendaAction(kind=action_kind, prompt="hola")
        app.kira_agenda = agenda
        app._kira_agenda_pending_compact_chat = pending
        app._enqueue_kira_agenda_action = MagicMock()
        app._kira_agenda_update_status = MagicMock()

        result = app_shell.VocalAIApp._kira_agenda_consume_pending_chat_if_due(app)

        assert result is False
        app._enqueue_kira_agenda_action.assert_not_called()
        app._kira_agenda_update_status.assert_not_called()
        assert app._kira_agenda_pending_compact_chat == pending
    finally:
        _restore_app_shell_module(old_module)


def test_prefetch_while_speaking_ignores_non_agenda_source():
    """B2 negative: current_speech_source not starting with 'kira-agenda'
    -> no prefetch_agenda call, no prefetched action stored."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app.kira_agenda = MagicMock()
        app.motor_ia = MagicMock()
        app.motor_ia.current_speech_source = "ptt"
        app.motor_ia.prefetch_agenda = MagicMock()

        app_shell.VocalAIApp._kira_agenda_prefetch_while_speaking(app)

        app.kira_agenda.prefetch_action_after_current_speech.assert_not_called()
        app.motor_ia.prefetch_agenda.assert_not_called()
        assert not hasattr(app, "_kira_agenda_prefetched_action")
    finally:
        _restore_app_shell_module(old_module)


def test_prefetch_while_speaking_stores_action_only_for_agenda_source():
    """B2 positive: agenda source + enqueue action + motor accepts the
    prefetch -> the action is stored on _kira_agenda_prefetched_action."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        action = app_shell.AgendaAction(
            kind="enqueue", prompt="siguiente turno", priority=2, source="kira-agenda"
        )
        agenda = MagicMock()
        agenda.prefetch_action_after_current_speech.return_value = action
        app.kira_agenda = agenda
        app.motor_ia = MagicMock()
        app.motor_ia.current_speech_source = "kira-agenda-tick"
        app.motor_ia.prefetch_agenda = MagicMock(return_value=True)

        app_shell.VocalAIApp._kira_agenda_prefetch_while_speaking(app)

        app.motor_ia.prefetch_agenda.assert_called_once_with(
            "siguiente turno", priority=2, source="kira-agenda"
        )
        assert app._kira_agenda_prefetched_action is action
    finally:
        _restore_app_shell_module(old_module)


# ===========================================================================
# Cluster C — audio coordination / enable
# ===========================================================================


def test_enable_with_empty_queue_refuses_before_touching_filter():
    """C1: no queued topics + no active topic -> honest refusal logged,
    status refreshed; enable()/force_strict_chat_filter/audio dispatch/tick
    are never touched."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        agenda = MagicMock()
        agenda.queued_topics.return_value = []
        agenda.active_topic = None
        app.kira_agenda = agenda
        app._on_stream_admin_log = MagicMock()
        app._kira_agenda_update_status = MagicMock()
        app._kira_agenda_force_strict_chat_filter = MagicMock()
        app._dispatch_audio_play = MagicMock()
        app._kira_agenda_tick = MagicMock()

        app_shell.VocalAIApp._kira_agenda_enable(app)

        logged = [c.args[0] for c in app._on_stream_admin_log.call_args_list]
        assert any("No se activa: no hay temas en cola" in msg for msg in logged)
        app._kira_agenda_update_status.assert_called_once()
        agenda.enable.assert_not_called()
        app._kira_agenda_force_strict_chat_filter.assert_not_called()
        app._dispatch_audio_play.assert_not_called()
        app._kira_agenda_tick.assert_not_called()
    finally:
        _restore_app_shell_module(old_module)


def test_enable_success_order():
    """C2: the success path runs in a fixed order — force_strict -> enable ->
    log -> dispatch_audio_play -> update_status -> tick."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        AgendaState = app_shell.AgendaState
        app = object.__new__(app_shell.VocalAIApp)
        agenda = MagicMock()
        agenda.queued_topics.return_value = ["t1"]
        agenda.active_topic = None
        agenda.state = AgendaState.WAITING_SIGNAL  # not OFF -> guardrails passed
        app.kira_agenda = agenda
        app.audio_bed = MagicMock()

        order = []
        app._kira_agenda_force_strict_chat_filter = MagicMock(
            side_effect=lambda: order.append("force_strict")
        )
        agenda.enable = MagicMock(side_effect=lambda: order.append("enable"))
        app._on_stream_admin_log = MagicMock(side_effect=lambda *a, **k: order.append("log"))
        app._dispatch_audio_play = MagicMock(
            side_effect=lambda fn: order.append("dispatch_audio_play")
        )
        app._kira_agenda_update_status = MagicMock(
            side_effect=lambda: order.append("update_status")
        )
        app._kira_agenda_tick = MagicMock(side_effect=lambda: order.append("tick"))

        app_shell.VocalAIApp._kira_agenda_enable(app)

        assert order == [
            "force_strict", "enable", "log", "dispatch_audio_play", "update_status", "tick",
        ]
    finally:
        _restore_app_shell_module(old_module)


# ===========================================================================
# Cluster D — chat-filter lifecycle
# ===========================================================================


def test_force_strict_saves_policy_once():
    """D1: two consecutive forces -> get_filter_policy() called exactly once,
    the previous-policy guard keeps the FIRST value, set_filter_policy is
    still called 'strict' each time."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app.smart_agg = MagicMock()
        app.smart_agg.get_filter_policy.return_value = "loose"

        app_shell.VocalAIApp._kira_agenda_force_strict_chat_filter(app)
        app_shell.VocalAIApp._kira_agenda_force_strict_chat_filter(app)

        app.smart_agg.get_filter_policy.assert_called_once()
        assert app._kira_agenda_previous_filter_policy == "loose"
        assert app.smart_agg.set_filter_policy.call_args_list == [
            (("strict",), {}), (("strict",), {}),
        ]
    finally:
        _restore_app_shell_module(old_module)


def test_restore_sets_previous_and_clears_guard():
    """D2: restore -> set_filter_policy(<saved>), attribute deleted; a
    second restore is a no-op (no second set call)."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app.smart_agg = MagicMock()
        app._kira_agenda_previous_filter_policy = "loose"

        app_shell.VocalAIApp._kira_agenda_restore_chat_filter(app)

        app.smart_agg.set_filter_policy.assert_called_once_with("loose")
        assert not hasattr(app, "_kira_agenda_previous_filter_policy")

        app_shell.VocalAIApp._kira_agenda_restore_chat_filter(app)
        app.smart_agg.set_filter_policy.assert_called_once_with("loose")
    finally:
        _restore_app_shell_module(old_module)


def test_restore_with_falsy_previous_is_noop_and_keeps_attr():
    """D3: saved policy is falsy ("") -> no set_filter_policy call AND the
    attribute is NOT deleted. Pins the exact `if previous:` gate — a falsy
    stored policy neither restores nor clears."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app.smart_agg = MagicMock()
        app._kira_agenda_previous_filter_policy = ""

        app_shell.VocalAIApp._kira_agenda_restore_chat_filter(app)

        app.smart_agg.set_filter_policy.assert_not_called()
        assert hasattr(app, "_kira_agenda_previous_filter_policy")
        assert app._kira_agenda_previous_filter_policy == ""
    finally:
        _restore_app_shell_module(old_module)


def test_force_strict_without_smart_agg_is_noop():
    """D4: smart_agg absent, then explicitly None -> both are a silent
    no-op (no previous-policy guard is ever set)."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)  # no smart_agg attribute at all

        app_shell.VocalAIApp._kira_agenda_force_strict_chat_filter(app)
        assert not hasattr(app, "_kira_agenda_previous_filter_policy")

        app.smart_agg = None
        app_shell.VocalAIApp._kira_agenda_force_strict_chat_filter(app)
        assert not hasattr(app, "_kira_agenda_previous_filter_policy")
    finally:
        _restore_app_shell_module(old_module)


# ===========================================================================
# Cluster E — status / aggregator glue (_on_smart_aggregated_context)
# ===========================================================================


def test_chat_routes_to_agenda_when_active():
    """E1: agenda state SPEAKING-family + intent prompt -> next_action(...)
    + enqueue + update_status; the RF3 smart_agg_ui path is NOT taken."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        AgendaState = app_shell.AgendaState
        AgendaAction = app_shell.AgendaAction
        app = object.__new__(app_shell.VocalAIApp)
        agenda = MagicMock()
        agenda.state = AgendaState.SPEAKING
        agenda.active_topic = MagicMock()
        action = AgendaAction(kind="enqueue", prompt="hello")
        agenda.next_action.return_value = action
        app.kira_agenda = agenda
        app.motor_ia = MagicMock(is_processing=False, is_speaking=False)
        app._enqueue_kira_agenda_action = MagicMock()
        app._kira_agenda_update_status = MagicMock()
        app.smart_agg_ui = MagicMock()

        data = {"intent_summary": {"prompt": "hello"}, "context": []}
        app_shell.VocalAIApp._on_smart_aggregated_context(app, data)

        agenda.next_action.assert_called_once_with(
            motor_busy=False, kira_speaking=False, compact_chat="hello"
        )
        app._enqueue_kira_agenda_action.assert_called_once_with(action)
        app._kira_agenda_update_status.assert_called_once()
        app.smart_agg_ui.on_aggregated_context.assert_not_called()
    finally:
        _restore_app_shell_module(old_module)


def test_chat_stashed_when_motor_busy():
    """E2: motor is_processing=True -> the (stripped) chat is stashed on
    _kira_agenda_pending_compact_chat, status refreshed, next_action NOT
    called."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        AgendaState = app_shell.AgendaState
        app = object.__new__(app_shell.VocalAIApp)
        agenda = MagicMock()
        agenda.state = AgendaState.SPEAKING
        agenda.active_topic = MagicMock()
        app.kira_agenda = agenda
        app.motor_ia = MagicMock(is_processing=True, is_speaking=False)
        app._kira_agenda_update_status = MagicMock()
        app.smart_agg_ui = MagicMock()

        data = {"intent_summary": {"prompt": "  hello  "}, "context": []}
        app_shell.VocalAIApp._on_smart_aggregated_context(app, data)

        assert app._kira_agenda_pending_compact_chat == "hello"
        app._kira_agenda_update_status.assert_called_once()
        agenda.next_action.assert_not_called()
        app.smart_agg_ui.on_aggregated_context.assert_not_called()
    finally:
        _restore_app_shell_module(old_module)


@pytest.mark.parametrize(
    "state_name",
    ["OFF", "PAUSED_NEEDS_OPERATOR", "HARD_PAUSED", "IDLE_EXHAUSTED"],
)
def test_chat_falls_through_when_paused_or_exhausted(state_name):
    """E3: PAUSED_NEEDS_OPERATOR / HARD_PAUSED / OFF / (IDLE + no active +
    empty queue) all fall through to the RF3 path (smart_agg_ui.
    on_aggregated_context); the agenda itself is left untouched."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        AgendaState = app_shell.AgendaState
        app = object.__new__(app_shell.VocalAIApp)
        agenda = MagicMock()
        if state_name == "IDLE_EXHAUSTED":
            agenda.state = AgendaState.IDLE
            agenda.active_topic = None
            agenda.queued_topics.return_value = []
        else:
            agenda.state = getattr(AgendaState, state_name)
        app.kira_agenda = agenda
        app.motor_ia = MagicMock(is_processing=False, is_speaking=False)
        app._enqueue_kira_agenda_action = MagicMock()
        app._kira_agenda_update_status = MagicMock()
        app.smart_agg_ui = MagicMock()

        data = {"intent_summary": {"prompt": "hello"}, "context": []}
        app_shell.VocalAIApp._on_smart_aggregated_context(app, data)

        agenda.next_action.assert_not_called()
        app._enqueue_kira_agenda_action.assert_not_called()
        app.smart_agg_ui.on_aggregated_context.assert_called_once_with(data)
        assert data["_source_used"] == "old_compact"
    finally:
        _restore_app_shell_module(old_module)


def test_chat_empty_prompt_joins_last_six_context_texts():
    """E4: empty intent prompt -> compact chat is built from the last 6
    context entries (sliced BEFORE the has-text filter is applied, pinning
    the exact SECURITY-commented fallback)."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        AgendaState = app_shell.AgendaState
        AgendaAction = app_shell.AgendaAction
        app = object.__new__(app_shell.VocalAIApp)
        agenda = MagicMock()
        agenda.state = AgendaState.SPEAKING
        agenda.active_topic = MagicMock()
        agenda.next_action.return_value = AgendaAction(kind="enqueue", prompt="x")
        app.kira_agenda = agenda
        app.motor_ia = MagicMock(is_processing=False, is_speaking=False)
        app._enqueue_kira_agenda_action = MagicMock()
        app._kira_agenda_update_status = MagicMock()
        app.smart_agg_ui = MagicMock()

        context = [
            {"text": "old1"},  # outside the last-6 window
            {"text": "old2"},  # outside the last-6 window
            {"text": "m1"},
            {},                # inside the window, no "text" -> filtered
            {"text": "m3"},
            {"text": "m4"},
            {"text": "m5"},
            {"text": "m6"},
        ]
        data = {"intent_summary": {}, "context": context}
        app_shell.VocalAIApp._on_smart_aggregated_context(app, data)

        agenda.next_action.assert_called_once_with(
            motor_busy=False, kira_speaking=False, compact_chat="m1\nm3\nm4\nm5\nm6"
        )
    finally:
        _restore_app_shell_module(old_module)
