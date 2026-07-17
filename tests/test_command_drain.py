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


def test_process_context_blocks_later_control_commands_from_jumping_ahead(monkeypatch):
    # WU5 — FIFO fix: a set_profile queued AFTER an earlier process_context
    # must not be applied first. Applying it first would wipe historial /
    # switch system_prompt before the earlier-queued direct turn runs,
    # answering it under the wrong profile.
    motor, _ = _make_motor()
    call_log: list = []

    def fake_infer(payload, source="direct", history_text=None):
        call_log.append(payload)

    monkeypatch.setattr(motor, "_ejecutar_inferencia", fake_infer)
    motor.system_prompt = "original-prompt"

    motor._priority_queue = [_agenda_item("agenda-turn")]
    motor.command_queue.put(("process_context", "x"))
    motor.command_queue.put(("set_profile", {"prompt": "new-prompt", "_profile_name": "P2"}))

    motor._process_priority_queue()

    # Neither command was touched — both stay queued, in original order,
    # for run() to consume. The profile switch did not jump ahead.
    assert motor.command_queue.get_nowait() == ("process_context", "x")
    assert motor.command_queue.get_nowait() == ("set_profile", {"prompt": "new-prompt", "_profile_name": "P2"})
    assert motor.system_prompt == "original-prompt"
    assert "x" not in call_log
    assert call_log == ["agenda-turn"]


def test_run_tolerant_unpacks_2tuple_and_3tuple_commands(monkeypatch):
    # A1 (memoria_quality_20260717): run()'s command consumer must tolerate BOTH
    # the legacy 2-tuple (chat + control verbs, app_shell) and the 3-tuple
    # (command, payload, history_text) put by the F10 PTT Dispatcher and the
    # legacy voice_control.py idle path — mirroring the priority-queue 4->5-tuple
    # tolerant unpack. history_text reaches _dispatch_command as None for a
    # 2-tuple and as the 3rd element for a 3-tuple. Tested via the extracted
    # _consume_command seam (run() itself does heavy pygame/ollama init; the
    # sibling _dispatch_command was likewise "extracted from run() for
    # testability").
    motor, _ = _make_motor()
    captured: list = []
    monkeypatch.setattr(
        motor,
        "_dispatch_command",
        lambda tipo, payload, history_text=None: captured.append((tipo, payload, history_text)),
    )

    motor._consume_command(("process_context", "hola"))  # legacy 2-tuple
    motor._consume_command(
        (
            "process_context",
            "El streamer acaba de decir (PTT): hola",
            "El streamer dijo (PTT): hola",
        )
    )  # 3-tuple with honest history_text

    assert captured == [
        ("process_context", "hola", None),
        (
            "process_context",
            "El streamer acaba de decir (PTT): hola",
            "El streamer dijo (PTT): hola",
        ),
    ]


def test_dispatch_command_process_context_forwards_history_text_idle_and_busy(monkeypatch):
    # A1 (memoria_quality_20260717): _dispatch_command must forward history_text
    # down BOTH process_context branches — idle -> _ejecutar_inferencia,
    # busy -> enqueue — so the honest PTT text reaches _commit_history whether or
    # not the motor was already speaking/processing.

    # Idle branch forwards to _ejecutar_inferencia.
    motor, _ = _make_motor()
    infer_calls: list = []
    monkeypatch.setattr(
        motor,
        "_ejecutar_inferencia",
        lambda payload, source="direct", history_text=None: infer_calls.append((payload, source, history_text)),
    )
    monkeypatch.setattr(motor, "_complete_processing_cycle", lambda *a, **k: None)
    motor._processing = False
    motor._speaking = False
    motor._dispatch_command("process_context", "ptt-wrapped", history_text="El streamer dijo (PTT): x")
    assert infer_calls == [("ptt-wrapped", "direct", "El streamer dijo (PTT): x")]

    # Busy branch forwards to enqueue (re-enqueued at chat priority today).
    motor2, _ = _make_motor()
    enqueue_calls: list = []
    monkeypatch.setattr(
        motor2,
        "enqueue",
        lambda payload, priority=1, source="chat", history_text=None: enqueue_calls.append(
            (payload, priority, source, history_text)
        ),
    )
    motor2._processing = True
    motor2._dispatch_command("process_context", "ptt-wrapped", history_text="El streamer dijo (PTT): x")
    assert enqueue_calls == [("ptt-wrapped", 1, "direct", "El streamer dijo (PTT): x")]


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
