"""Unit 4.1 (runtime_findings_batch_20260731, F5) — stamp direct/chat/ptt
requests at submit time so `[TURN_LATENCY]` can report the FULL wait.

Before this unit, TURN_LATENCY measured engine-receipt -> TTS only. In the
2026-07-30 run the owner's four direct questions actually waited 13.8-29.1
MINUTES (queued behind an agenda turn), but the metric reported ~15s medians
because the queue wait before the engine ever dequeued the item was invisible.

Only `motor._hablar` and `motor._ollama_chat` are mocked — the repo's
established idiom (tests/test_dialogue_callback.py:40).
"""
from __future__ import annotations

import logging
import queue
import time
from unittest.mock import MagicMock

from opencohost.api.dispatch import Dispatcher
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.core.scheduling.turn_stamp import TurnStamp


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


def _latency_records(caplog):
    return [r.getMessage() for r in caplog.records if "[TURN_LATENCY]" in r.getMessage()]


# ---------------------------------------------------------------------------
# The honesty check (Layer 1) — the whole point of this unit
# ---------------------------------------------------------------------------

def test_queue_wait_ms_reports_the_full_wait_not_the_engine_span(monkeypatch, caplog):
    """Simulates the 13.8-29.1 min class from the 2026-07-30 run, scaled to
    60s for a fast suite. If submitted_at were accidentally stamped at
    dequeue instead of at submission, queue_wait_ms would read ~0 and this
    test would fail.
    """
    clock = {"t": 1000.0}
    monkeypatch.setattr("opencohost.core.llm_engine.time.monotonic", lambda: clock["t"])

    motor = _motor()
    motor._ollama_chat = MagicMock(return_value=_resp("Todo tranquilo por acá."))

    submitted_at = clock["t"]  # stamped at the API dispatch entry
    clock["t"] += 60.0  # the item sat queued (busy motor) for 60s

    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        motor._ejecutar_inferencia("hola", source="direct", stamp=TurnStamp(submitted_at=submitted_at))

    records = _latency_records(caplog)
    assert len(records) == 1
    line = records[0]
    assert "source=direct" in line
    assert "queue_wait_ms=60000" in line
    # Engine-only span stayed small: the fake clock never advanced during
    # generation (ollama_chat is mocked to return instantly).
    assert "request_to_tts_ms=0" in line
    assert "request_to_tts_total_ms=60000" in line


def test_immediate_idle_dispatch_reports_near_zero_wait_not_none(monkeypatch, caplog):
    """A direct question answered while the motor is idle still carries a
    real (tiny) queue_wait_ms — it must not be swallowed into null just
    because the wait was short."""
    motor = _motor()
    motor._ollama_chat = MagicMock(return_value=_resp("Todo bien."))
    motor._processing = False
    motor._speaking = False

    submitted_at = time.monotonic()
    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        motor._dispatch_command("process_context", "hola", source="direct", stamp=TurnStamp(submitted_at=submitted_at))

    records = _latency_records(caplog)
    assert len(records) == 1
    assert "queue_wait_ms=" in records[0]
    assert "queue_wait_ms=-" not in records[0]  # never negative


# ---------------------------------------------------------------------------
# Unstamped items never fake a value
# ---------------------------------------------------------------------------

def test_unstamped_agenda_turn_emits_no_fake_queue_wait(caplog):
    motor = _motor()
    motor._ollama_chat = MagicMock(return_value=_resp("bloque de agenda"))

    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        motor._ejecutar_inferencia("tema", source="kira-agenda")

    records = _latency_records(caplog)
    assert len(records) == 1
    assert "queue_wait_ms" not in records[0]
    assert "request_to_tts_total_ms" not in records[0]


def test_unstamped_turn_dialogue_callback_gets_no_queue_wait_kwarg():
    # Pinned: an unstamped turn must call dialogue_callback with EXACTLY the
    # pre-4.1 2-positional-arg shape (test_dialogue_callback.py's own pin).
    emitted = MagicMock()
    motor = _motor(dialogue_callback=emitted)
    motor._ollama_chat = MagicMock(return_value=_resp("Che, todo bien."))

    motor._ejecutar_inferencia("hola", source="chat")

    emitted.assert_called_once_with("Che, todo bien.", "kira")


# ---------------------------------------------------------------------------
# Existing TURN_LATENCY fields are unchanged (pin the old field names)
# ---------------------------------------------------------------------------

def test_existing_turn_latency_fields_unchanged_when_unstamped(caplog):
    motor = _motor()
    motor._ollama_chat = MagicMock(return_value=_resp("Todo tranquilo por acá."))

    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        motor._ejecutar_inferencia("hola", source="direct")

    records = _latency_records(caplog)
    assert len(records) == 1
    assert "source=direct" in records[0]
    assert "request_to_tts_ms=" in records[0]


# ---------------------------------------------------------------------------
# End-to-end: a stamped direct turn through dispatch -> priority queue ->
# dequeue -> the ChatReplySink seam 4.2 consumes.
# ---------------------------------------------------------------------------

def test_direct_turn_end_to_end_carries_queue_wait_from_dispatch_to_sink(monkeypatch):
    clock = {"t": 2000.0}
    monkeypatch.setattr("opencohost.core.llm_engine.time.monotonic", lambda: clock["t"])

    emitted = MagicMock()
    motor = _motor(dialogue_callback=emitted)
    motor._ollama_chat = MagicMock(return_value=_resp("todo tranquilo"))

    # Same seam /api/chat/turn uses (opencohost/api/main.py).
    d = Dispatcher(motor.command_queue)
    d.dispatch(
        "process_context",
        "hola",
        history_text="El usuario dijo: hola",
        source="direct",
        submitted_at=clock["t"],
    )

    # The motor is mid-agenda-turn when run() would drain this item — it must
    # land in the PRIORITY queue, not run immediately (mirrors _dispatch_command's
    # busy branch).
    motor._processing = True
    motor._consume_command(motor.command_queue.get_nowait())
    assert len(motor._priority_queue) == 1

    # Kira keeps talking for 60s before the item is ever dequeued.
    clock["t"] += 60.0
    motor._processing = False
    motor._speaking = False
    motor._process_priority_queue()

    # Unit 4.1's seam for 4.2: queue_wait_ms reaches dialogue_callback
    # (ChatReplySink.record in production), which is exactly where
    # GET /api/chat/last-reply reads it from.
    emitted.assert_called_once_with("todo tranquilo", "kira", queue_wait_ms=60000)
