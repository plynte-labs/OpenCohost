"""WU1 — drain safe control commands at engine turn boundaries.

Strict TDD for the command-starvation bug: control commands posted while the
engine is busy running back-to-back agenda turns (the recursive
_process_priority_queue -> _complete_processing_cycle cycle) sit unread in
command_queue until the priority queue empties. These tests reproduce the
starvation deterministically without threads, then pin the fixed behavior:
whitelisted commands apply at the NEXT turn boundary, non-whitelisted verbs
and the None shutdown sentinel stay deferred.
"""
from __future__ import annotations

import queue
import time
import types

from unittest.mock import MagicMock

from opencohost.core import llm_engine
from opencohost.core.llm_engine import MotorVocalIA


def _make_motor():
    log_q: queue.Queue = queue.Queue()
    events: list[str] = []
    motor = MotorVocalIA(log_q, events.append)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    return motor, events


def _agenda_item(payload: str):
    # 5-tuple produced by enqueue(): (priority, ts, payload, source, history_text)
    return (2, time.time(), payload, "kira-agenda", None)


def test_control_command_applies_between_agenda_turns(monkeypatch):
    motor, _ = _make_motor()
    call_log: list = []

    def fake_infer(payload, source="direct", history_text=None):
        call_log.append(("turn", payload))

    monkeypatch.setattr(motor, "_ejecutar_inferencia", fake_infer)
    motor._piper = types.SimpleNamespace(
        set_length_scale=lambda v: call_log.append(("speed", v))
    )
    monkeypatch.setattr(llm_engine, "save_tts_speed", lambda *_a, **_k: None)

    motor._priority_queue = [_agenda_item("t1"), _agenda_item("t2")]
    motor.command_queue.put(("set_tts_speed", 1.30))

    motor._process_priority_queue()

    assert call_log == [("turn", "t1"), ("speed", 1.30), ("turn", "t2")]
    # Command was consumed, not left sitting in the queue.
    assert motor.command_queue.empty()


def test_process_context_stays_deferred(monkeypatch):
    motor, _ = _make_motor()
    call_log: list = []

    def fake_infer(payload, source="direct", history_text=None):
        call_log.append(payload)

    monkeypatch.setattr(motor, "_ejecutar_inferencia", fake_infer)

    motor._priority_queue = [_agenda_item("agenda-turn")]
    motor.command_queue.put(("process_context", "x"))

    motor._process_priority_queue()

    assert motor.command_queue.get_nowait() == ("process_context", "x")
    # "x" never ran as a turn — only the agenda item did.
    assert "x" not in call_log
    assert call_log == ["agenda-turn"]


def test_none_sentinel_survives_drain(monkeypatch):
    motor, _ = _make_motor()
    monkeypatch.setattr(
        motor, "_ejecutar_inferencia", lambda *a, **k: None
    )

    motor._priority_queue = [_agenda_item("agenda-turn")]
    motor.command_queue.put(None)

    motor._process_priority_queue()

    assert motor.command_queue.get_nowait() is None


def test_drain_emits_motor_event(monkeypatch):
    motor, events = _make_motor()

    monkeypatch.setattr(
        motor, "_ejecutar_inferencia", lambda *a, **k: None
    )
    motor._piper = types.SimpleNamespace(set_length_scale=lambda v: None)
    monkeypatch.setattr(llm_engine, "save_tts_speed", lambda *_a, **_k: None)

    motor._priority_queue = [_agenda_item("t1")]
    motor.command_queue.put(("set_tts_speed", 1.30))

    motor._process_priority_queue()

    assert "commands_drained" in events
