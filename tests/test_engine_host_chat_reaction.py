"""Tests for the headless chat->Kira feeder (EngineHost._on_aggregated_context).

The bug this pins: the API host wired only `on_filtered_message` /
`on_source_changed` on its Aggregator, so viewer chat rendered in the Tauri UI
but `on_aggregated_context` stayed None and Kira never reacted to chat. The
fix mirrors the CTK routing (`agenda_audio_controller.on_smart_aggregated_context`,
now extracted to `opencohost.smart_aggregator.chat_reaction` so the api package
can import it without violating its never-imports-opencohost.ui contract).

House pattern for start()-level wiring mirrors tests/test_chat_feed_sink.py:
monkeypatch the heavy constructors, run start(), assert on the wiring/behavior.
"""

import queue
import threading
from unittest.mock import MagicMock

import pytest

import opencohost.api.engine_host as engine_host_mod
from opencohost.smart_aggregator.kira_agenda_controller import (
    AgendaAction,
    AgendaState,
    wrap_untrusted_chat,
)


def _started_host(tmp_path, monkeypatch):
    """EngineHost.start() with every heavy collaborator faked out.

    KiraAgendaController raises so `host.agenda` lands on the resilient None
    path (no agenda driver thread) -- tests that need an agenda install a
    MagicMock afterwards, exactly like the CTK characterization tests do.
    """
    eh = engine_host_mod

    fake_motor = MagicMock()
    fake_motor.command_queue = queue.Queue()
    fake_motor.current_model = None
    fake_motor.is_processing = False
    fake_motor.is_speaking = False
    fake_motor.historial = []

    monkeypatch.setattr(eh, "MotorVocalIA", lambda *a, **kw: fake_motor)
    monkeypatch.setattr(eh, "HealthMonitor", lambda: MagicMock())
    monkeypatch.setattr(eh, "ObsRuntime", MagicMock())
    monkeypatch.setattr(eh, "Aggregator", MagicMock())
    monkeypatch.setattr(
        eh,
        "KiraAgendaController",
        lambda: (_ for _ in ()).throw(RuntimeError("no agenda in this test")),
    )
    monkeypatch.setattr(eh, "AgendaPersistence", MagicMock())
    monkeypatch.setattr(eh, "EditorialCardStore", MagicMock())
    monkeypatch.setattr(eh, "EditorialAgendaBridge", MagicMock())
    monkeypatch.setattr(eh, "MusicLibrary", MagicMock())
    monkeypatch.setattr(eh, "ollama", MagicMock())

    host = eh.EngineHost(lock_path=str(tmp_path / "eh.lock"))
    host._wake_ollama_eager = lambda: None  # no daemon wake thread in tests
    host.start()
    return host, fake_motor


# ──────────────────────────────────────────────────────────────────────────
# start() wiring -- the line whose absence WAS the bug
# ──────────────────────────────────────────────────────────────────────────


def test_start_wires_on_aggregated_context(tmp_path, monkeypatch):
    """Reverting the fix leaves on_aggregated_context a bare Mock attribute
    (in prod: None), which is exactly the payload-dropped-on-the-floor bug."""
    host, _ = _started_host(tmp_path, monkeypatch)
    try:
        assert host.aggregator.on_aggregated_context == host._on_aggregated_context
    finally:
        host.stop()


# ──────────────────────────────────────────────────────────────────────────
# Standalone RF3 path (agenda None/inert): chat must reach motor.enqueue
# ──────────────────────────────────────────────────────────────────────────


def test_rf3_chat_reaches_motor_headless(tmp_path, monkeypatch):
    host, motor = _started_host(tmp_path, monkeypatch)
    try:
        assert host.agenda is None  # resilient path from the fixture
        host._on_aggregated_context(
            {
                "trigger": {},
                "context": [{"user": "Alice", "text": "does Kira read the chat?"}],
                "intent_summary": {},
            }
        )
        motor.enqueue.assert_called_once()
        args, kwargs = motor.enqueue.call_args
        prompt = args[0]
        assert "Alice" in prompt
        # Tier split (tauri_stream_chat_20260812): chat_reaction no longer
        # pins a priority — enqueue() resolves the stream tier from `source`
        # at the live STREAM_OVER_AGENDA setting. A pinned literal here is
        # exactly how viewer chat ended up in the direct tier.
        assert "priority" not in kwargs
        assert kwargs.get("source") == "chat"
        # SECURITY: raw viewer chat must sit inside the read-only data fence
        # (kira_agenda_controller.wrap_untrusted_chat), never bare in the prompt.
        fenced = wrap_untrusted_chat(
            "Mensajes recientes del chat:\n- Alice: does Kira read the chat?"
        )
        assert fenced in prompt
    finally:
        host.stop()


def test_rf3_empty_context_enqueues_nothing(tmp_path, monkeypatch):
    host, motor = _started_host(tmp_path, monkeypatch)
    try:
        host._on_aggregated_context({"trigger": {}, "context": [], "intent_summary": {}})
        motor.enqueue.assert_not_called()
    finally:
        host.stop()


# ──────────────────────────────────────────────────────────────────────────
# Agenda-active path: chat routes into next_action(compact_chat=...)
# ──────────────────────────────────────────────────────────────────────────


def test_agenda_active_chat_routes_to_next_action(tmp_path, monkeypatch):
    host, motor = _started_host(tmp_path, monkeypatch)
    try:
        agenda = MagicMock()
        agenda.state = AgendaState.WAITING_SIGNAL
        agenda.active_topic = MagicMock()
        action = AgendaAction(kind="enqueue", prompt="agenda prompt", source="kira-agenda", priority=2)
        agenda.next_action.return_value = action
        host.agenda = agenda

        host._on_aggregated_context(
            {"trigger": {}, "context": [], "intent_summary": {"prompt": "hola"}}
        )

        agenda.next_action.assert_called_once_with(
            motor_busy=False, kira_speaking=False, compact_chat="hola"
        )
        # kira-agenda source goes through replace_pending (autonomous turns
        # never stack), mirroring agenda_driver.enqueue_agenda_action.
        motor.replace_pending.assert_called_once_with(
            "agenda prompt", priority=2, source="kira-agenda"
        )
        motor.enqueue.assert_not_called()
    finally:
        host.stop()


def test_agenda_inert_falls_through_to_rf3(tmp_path, monkeypatch):
    host, motor = _started_host(tmp_path, monkeypatch)
    try:
        agenda = MagicMock()
        agenda.state = AgendaState.OFF
        host.agenda = agenda

        host._on_aggregated_context(
            {
                "trigger": {},
                "context": [{"user": "Bob", "text": "kira are you alive?"}],
                "intent_summary": {},
            }
        )

        agenda.next_action.assert_not_called()
        motor.enqueue.assert_called_once()
        assert motor.enqueue.call_args.kwargs.get("source") == "chat"
    finally:
        host.stop()


# ──────────────────────────────────────────────────────────────────────────
# Busy stash + speaking_end consume (CTK parity: motor_event_handlers 409-415)
# ──────────────────────────────────────────────────────────────────────────


def test_busy_chat_stash_consumed_at_agenda_speaking_end(tmp_path, monkeypatch):
    host, motor = _started_host(tmp_path, monkeypatch)
    try:
        agenda = MagicMock()
        agenda.state = AgendaState.SPEAKING
        agenda.active_topic = MagicMock()
        host.agenda = agenda
        motor.is_processing = True

        host._on_aggregated_context(
            {"trigger": {}, "context": [], "intent_summary": {"prompt": "  hola  "}}
        )

        # Stashed (stripped), not spoken over the in-flight turn.
        assert host._pending_compact_chat == "hola"
        agenda.next_action.assert_not_called()
        motor.enqueue.assert_not_called()

        # Agenda speaking_end: the stashed chat signal outranks any prefetch
        # draft and turns into a chat-responsive agenda action.
        motor.is_processing = False
        action = AgendaAction(kind="enqueue", prompt="chat turn", source="kira-agenda", priority=2)
        agenda.next_action.return_value = action
        agenda.chat_signal_due.return_value = True

        host._handle_agenda_motor_event("speaking_end")

        assert host._pending_compact_chat == ""
        agenda.next_action.assert_called_once_with(compact_chat="hola")
        motor.replace_pending.assert_called_once_with(
            "chat turn", priority=2, source="kira-agenda"
        )
    finally:
        host.stop()


def test_speaking_end_without_due_chat_signal_keeps_stash(tmp_path, monkeypatch):
    host, motor = _started_host(tmp_path, monkeypatch)
    try:
        agenda = MagicMock()
        agenda.state = AgendaState.SPEAKING
        agenda.active_topic = MagicMock()
        agenda.chat_signal_due.return_value = False
        host.agenda = agenda
        motor.is_processing = True

        host._on_aggregated_context(
            {"trigger": {}, "context": [], "intent_summary": {"prompt": "hola"}}
        )
        assert host._pending_compact_chat == "hola"

        host._handle_agenda_motor_event("speaking_end")

        # Not yet due -- the stash survives for a later boundary.
        assert host._pending_compact_chat == "hola"
        agenda.next_action.assert_not_called()
    finally:
        host.stop()


# ──────────────────────────────────────────────────────────────────────────
# Thread-safety shape: the handler must serialize on agenda_lock (the
# controller is not thread-safe and this callback fires on the chat source
# reader thread, not the engine thread)
# ──────────────────────────────────────────────────────────────────────────


def test_on_aggregated_context_takes_agenda_lock(tmp_path, monkeypatch):
    host, motor = _started_host(tmp_path, monkeypatch)
    try:
        agenda = MagicMock()
        agenda.state = AgendaState.WAITING_SIGNAL
        agenda.active_topic = MagicMock()
        agenda.next_action.return_value = AgendaAction(kind="none", source="none", priority=99)
        host.agenda = agenda

        seen = {}

        def _spy_next_action(**kwargs):
            seen["locked"] = host.agenda_lock.locked()
            return AgendaAction(kind="none", source="none", priority=99)

        agenda.next_action.side_effect = _spy_next_action

        host._on_aggregated_context(
            {"trigger": {}, "context": [], "intent_summary": {"prompt": "hola"}}
        )

        assert seen.get("locked") is True
        assert not host.agenda_lock.locked()  # released afterwards
    finally:
        host.stop()


# ──────────────────────────────────────────────────────────────────────────
# log_accion allowlist: one of chat_reaction's two InputContract lines is
# chat-derived and must never reach the log.
# ──────────────────────────────────────────────────────────────────────────


def test_stay_silent_diagnostic_is_logged(tmp_path, monkeypatch, caplog):
    """When Input Contract keeps Kira quiet, a quiet Kira is the CORRECT
    outcome -- without this line the operator cannot tell it apart from a
    dead pipeline. Pure metadata: counts, a fixed event enum, a float."""
    host, _ = _started_host(tmp_path, monkeypatch)
    try:
        with caplog.at_level("INFO", logger=engine_host_mod._logger.name):
            host._log_chat_reaction(
                "[InputContract] stay_silent: msgs=2 users=1 "
                "event=low_signal_noise confidence=0.00"
            )
        assert "stay_silent" in caplog.text
    finally:
        host.stop()


def test_using_packet_line_never_reaches_the_log(tmp_path, monkeypatch, caplog):
    """chat_reaction's OTHER InputContract line interpolates
    `old_compact[:80]!r` -- intent_summary["prompt"], which is viewer-derived.
    Project rule: never expose raw chat in logs. Drop the whole line."""
    host, _ = _started_host(tmp_path, monkeypatch)
    try:
        with caplog.at_level("INFO", logger=engine_host_mod._logger.name):
            # caplog already holds the fixture's deliberate agenda-construction
            # ERROR, so assert on absence of THIS line, not on an empty log.
            caplog.clear()
            host._log_chat_reaction(
                "[InputContract] using packet: event=direct_question "
                "goal=answer_direct_question highlight=yes clusters=1 "
                "old_compact='APA Y LISBETH????'"
            )
        assert "InputContract" not in caplog.text
        assert "LISBETH" not in caplog.text
    finally:
        host.stop()


def test_allowlist_drops_an_unrecognized_line(tmp_path, monkeypatch, caplog):
    """Allowlist, not denylist: a log_accion() call added to chat_reaction.py
    later must NOT start leaking through here by default."""
    host, _ = _started_host(tmp_path, monkeypatch)
    try:
        with caplog.at_level("INFO", logger=engine_host_mod._logger.name):
            caplog.clear()  # see the sibling test: the fixture logs on purpose
            host._log_chat_reaction("[SomethingNew] viewer said: hola patron")
        assert "SomethingNew" not in caplog.text
        assert "hola patron" not in caplog.text
    finally:
        host.stop()
