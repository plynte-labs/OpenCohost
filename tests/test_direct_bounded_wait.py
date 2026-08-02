"""Unit 4.2 (runtime_findings_batch_20260731, D3b) — bounded wait for
source=direct, and the F12 provider-tag closure.

Root cause (see MotorVocalIA._drain_pending_direct_into_priority_queue's own
docstring in llm_engine.py): WU2's consume-at-event adopts the NEXT agenda
block straight into `_priority_queue` SYNCHRONOUSLY, on the engine thread,
before `run()` ever gets a chance to read `command_queue` again. As long as
that keeps succeeding, a `source=direct` command sitting in `command_queue`
(put there by `Dispatcher.dispatch`) is never even looked at — this is what
made four direct questions wait 13.8-29.1 minutes on 2026-07-30, not a
priority-sort bug (`_priority_queue`'s own sort, 0=PTT/1=chat-direct/2=agenda,
was always correct once an item actually reached it).

Only `motor._hablar` and `motor._ollama_chat` are mocked — the repo's
established idiom (tests/test_dialogue_callback.py:40, mirrored by
tests/test_queue_wait_stamp.py).
"""
from __future__ import annotations

import logging
import queue
import time
from unittest.mock import MagicMock

from opencohost.api.dispatch import Dispatcher
from opencohost.config.settings import DIRECT_ANSWER_MAX_WAIT_SECONDS
from opencohost.core.llm_engine import MotorVocalIA


def _resp(text):
    return {"message": {"content": text}}


def _motor(dialogue_callback=None):
    motor = MotorVocalIA(queue.Queue(), lambda event: None,
                          dialogue_callback=dialogue_callback)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor.current_model = "llama3"
    motor._reasoning_model_cache["llama3"] = False
    motor._hablar = MagicMock()
    return motor


def _agenda_item(payload: str, priority: int = 2):
    # 7-tuple produced by enqueue() post-4.2:
    # (priority, ts, payload, source, history_text, submitted_at, submitted_under_provider)
    return (priority, time.time(), payload, "kira-agenda", None, None, None)


# ---------------------------------------------------------------------------
# 1. A queued direct item is served at the next boundary, ahead of any
#    further agenda action (the mechanical heart of D3b).
# ---------------------------------------------------------------------------


def test_direct_command_drained_into_priority_queue_ahead_of_agenda():
    """Unit test of the drain method itself: an EXPLICITLY tagged direct
    command (Dispatcher shape, 4+ tuple) sitting in command_queue is moved
    into _priority_queue at priority=1, sorting ahead of an already-queued
    agenda item (priority=2)."""
    motor = MotorVocalIA(queue.Queue(), lambda e: None)
    motor._priority_queue = [_agenda_item("agenda-turn")]
    motor.command_queue.put(("process_context", "pregunta directa", "hist", "direct"))

    motor._drain_pending_direct_into_priority_queue()

    assert motor.command_queue.empty()
    assert len(motor._priority_queue) == 2
    assert motor._priority_queue[0][0] == 1  # priority
    assert motor._priority_queue[0][2] == "pregunta directa"
    assert motor._priority_queue[0][3] == "direct"
    assert motor._priority_queue[1][2] == "agenda-turn"


def test_legacy_bare_process_context_tuple_stays_deferred():
    """Regression guard for WU1 (tests/test_command_drain.py): a plain
    (command, payload) 2-tuple has no EXPLICIT source tag — it must stay
    deferred exactly as before, not get swept into the priority queue by
    the tolerant-unpack default."""
    motor = MotorVocalIA(queue.Queue(), lambda e: None)
    motor.command_queue.put(("process_context", "x"))

    motor._drain_pending_direct_into_priority_queue()

    assert motor.command_queue.get_nowait() == ("process_context", "x")
    assert motor._priority_queue == []


def test_queued_direct_item_dispatched_ahead_of_further_agenda_action(monkeypatch):
    """End-to-end ordering through the real drain loop
    (_process_priority_queue -> _complete_processing_cycle ->
    _drain_pending_direct_into_priority_queue).

    Simulates the EXACT 2026-07-30 mechanism: while agenda-block-1 is being
    "spoken" (here: _ejecutar_inferencia is stubbed), the driver's
    consume-at-event adopts agenda-block-2 into _priority_queue AND a direct
    question arrives via the API (command_queue) — both happening inside the
    same turn boundary. Despite agenda-block-2 being queued first, the direct
    item must be served next, ahead of it.
    """
    motor = MotorVocalIA(queue.Queue(), lambda e: None)
    call_log: list = []

    def fake_infer(payload, source="direct", history_text=None,
                    submitted_at=None, submitted_under_provider=None):
        call_log.append((source, payload))
        if source == "kira-agenda" and payload == "agenda-block-1":
            # WU2 consume-at-event: the driver adopts the NEXT agenda block
            # synchronously, inside THIS turn's tail.
            motor.enqueue("agenda-block-2", priority=2, source="kira-agenda")
            # The direct question arrives from the API right now, while the
            # motor is mid-turn — it lands in command_queue (mirrors
            # _dispatch_command's busy branch / Dispatcher.dispatch).
            motor.command_queue.put(("process_context", "pregunta directa", "hist", "direct"))

    monkeypatch.setattr(motor, "_ejecutar_inferencia", fake_infer)
    motor._priority_queue = [_agenda_item("agenda-block-1")]

    motor._process_priority_queue()

    assert call_log == [
        ("kira-agenda", "agenda-block-1"),
        ("direct", "pregunta directa"),
        ("kira-agenda", "agenda-block-2"),
    ]


# ---------------------------------------------------------------------------
# 2. The documented bound (DIRECT_ANSWER_MAX_WAIT_SECONDS) is NOT an
#    enforcement timer — it only drives a metadata-only WARNING.
# ---------------------------------------------------------------------------


def _latency_warnings(caplog):
    return [
        r.getMessage() for r in caplog.records
        if r.levelname == "WARNING" and "DIRECT_WAIT_EXCEEDED" in r.getMessage()
    ]


def test_direct_over_bound_wait_logs_warning_metadata_only(monkeypatch, caplog):
    clock = {"t": 1000.0}
    monkeypatch.setattr("opencohost.core.llm_engine.time.monotonic", lambda: clock["t"])

    motor = _motor()
    secret_text = "el usuario conto un secreto ultra confidencial"
    motor._ollama_chat = MagicMock(return_value=_resp("respuesta generica"))

    submitted_at = clock["t"]
    clock["t"] += DIRECT_ANSWER_MAX_WAIT_SECONDS + 30  # push past the documented bound

    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        motor._ejecutar_inferencia(secret_text, source="direct", submitted_at=submitted_at)

    warnings = _latency_warnings(caplog)
    assert len(warnings) == 1
    line = warnings[0]
    assert "queue_wait_ms=" in line
    assert "bound_ms=" in line
    # Metadata-only: never the raw turn text (owner rule, mirrors CLAUSE_SANITIZER).
    assert secret_text not in line


def test_direct_within_bound_wait_logs_no_warning(monkeypatch, caplog):
    clock = {"t": 2000.0}
    monkeypatch.setattr("opencohost.core.llm_engine.time.monotonic", lambda: clock["t"])

    motor = _motor()
    motor._ollama_chat = MagicMock(return_value=_resp("respuesta generica"))

    submitted_at = clock["t"]
    clock["t"] += 5.0

    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        motor._ejecutar_inferencia("hola", source="direct", submitted_at=submitted_at)

    assert _latency_warnings(caplog) == []


def test_over_bound_wait_for_non_direct_source_logs_no_warning(monkeypatch, caplog):
    """Scoped strictly to source=="direct" per D3 — an agenda/chat turn that
    happens to carry a long queue_wait_ms (out of this unit's scope) must
    never trip the direct-only WARNING."""
    clock = {"t": 3000.0}
    monkeypatch.setattr("opencohost.core.llm_engine.time.monotonic", lambda: clock["t"])

    motor = _motor()
    motor._ollama_chat = MagicMock(return_value=_resp("respuesta generica"))

    submitted_at = clock["t"]
    clock["t"] += DIRECT_ANSWER_MAX_WAIT_SECONDS + 30

    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        motor._ejecutar_inferencia("hola", source="chat", submitted_at=submitted_at)

    assert _latency_warnings(caplog) == []


# ---------------------------------------------------------------------------
# 3. F12 closure — provider tag: submit-time posture vs. the actual
#    answering provider, disclosed on the reply.
# ---------------------------------------------------------------------------


def test_direct_turn_discloses_provider_change_while_queued(monkeypatch):
    clock = {"t": 4000.0}
    monkeypatch.setattr("opencohost.core.llm_engine.time.monotonic", lambda: clock["t"])

    emitted = MagicMock()
    motor = _motor(dialogue_callback=emitted)
    motor._ollama_chat = MagicMock(return_value=_resp("todo tranquilo"))
    motor._provider_config = {"active_provider": "nvidia_nim", "profiles": {}}

    d = Dispatcher(motor.command_queue)
    d.dispatch(
        "process_context",
        "hola",
        history_text="El usuario dijo: hola",
        source="direct",
        submitted_at=clock["t"],
        submitted_under_provider="nvidia_nim",
    )

    # Motor is mid-agenda-turn when this would be drained — it lands in the
    # PRIORITY queue, not run immediately.
    motor._processing = True
    motor._consume_command(motor.command_queue.get_nowait())
    assert len(motor._priority_queue) == 1

    # Cloud fails while the item sits queued; the engine falls back to local
    # (F2/F3, same batch) — the reply must disclose the mismatch, not answer
    # silently under a different provider.
    clock["t"] += 5.0
    motor._cloud_fallback_active = True
    motor._processing = False
    motor._speaking = False
    motor._process_priority_queue()

    emitted.assert_called_once_with(
        "todo tranquilo", "kira", queue_wait_ms=5000,
        answered_by_provider="local", answered_by_transport="local",
        submitted_under_provider="nvidia_nim",
        provider_changed_while_queued=True,
    )


def test_direct_turn_same_provider_carries_no_mismatch(monkeypatch):
    clock = {"t": 5000.0}
    monkeypatch.setattr("opencohost.core.llm_engine.time.monotonic", lambda: clock["t"])

    emitted = MagicMock()
    motor = _motor(dialogue_callback=emitted)
    motor._ollama_chat = MagicMock(return_value=_resp("todo tranquilo"))
    # Stays local throughout — no fallback, no switch.
    motor._provider_config = {"active_provider": "local", "profiles": {}}

    d = Dispatcher(motor.command_queue)
    d.dispatch(
        "process_context",
        "hola",
        history_text="El usuario dijo: hola",
        source="direct",
        submitted_at=clock["t"],
        submitted_under_provider="local",
    )

    motor._processing = True
    motor._consume_command(motor.command_queue.get_nowait())
    clock["t"] += 2.0
    motor._processing = False
    motor._speaking = False
    motor._process_priority_queue()

    emitted.assert_called_once_with(
        "todo tranquilo", "kira", queue_wait_ms=2000,
        answered_by_provider="local", answered_by_transport="local",
        submitted_under_provider="local",
        provider_changed_while_queued=False,
    )


def test_untagged_turn_carries_no_provider_disclosure_kwargs():
    """A turn with no submitted_under_provider tag (agenda, accumulated, or
    any pre-4.2 caller) must call dialogue_callback with EXACTLY the 4.1
    shape — no new kwargs at all, never a fabricated None-filled disclosure."""
    emitted = MagicMock()
    motor = _motor(dialogue_callback=emitted)
    motor._ollama_chat = MagicMock(return_value=_resp("che, todo bien"))

    motor._ejecutar_inferencia("hola", source="chat")

    emitted.assert_called_once_with("che, todo bien", "kira")
