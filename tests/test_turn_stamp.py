"""Phase C1 (refactor_core_api_20260802) — TurnStamp dataclass.

Pins the frozen `TurnStamp` shape and its threading through the private
seams that used to hand-duplicate the submitted_at/submitted_under_provider
conditional-kwargs-forwarding idiom: tuple -> `_consume_command` ->
`_dispatch_command` -> `enqueue`/`_ejecutar_inferencia`. The stamp collapses
what used to be a 2-3-way branch at each site into a single "stamp is None
or not" check; every existing byte-identical behavior (queue_wait_ms math,
dialogue_callback call shapes, the TURN_LATENCY/DIRECT_WAIT_EXCEEDED log
lines) is pinned by the pre-existing suites (test_queue_wait_stamp.py,
test_direct_bounded_wait.py) and left untouched here — this file is scoped
to the stamp object itself and its threading mechanics.

Only `motor._hablar` and `motor._ollama_chat` are mocked — the repo's
established idiom (tests/test_dialogue_callback.py:40).
"""
from __future__ import annotations

import dataclasses
import logging
import queue
import time
from unittest.mock import MagicMock

import pytest

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
# 1. The dataclass itself
# ---------------------------------------------------------------------------


def test_turn_stamp_is_frozen():
    stamp = TurnStamp(submitted_at=123.0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        stamp.submitted_at = 456.0


def test_turn_stamp_defaults_to_none_for_optional_fields():
    stamp = TurnStamp(submitted_at=1.0)
    assert stamp.submitted_under_provider is None


def test_turn_stamp_carries_the_provider_tag_when_given():
    stamp = TurnStamp(submitted_at=1.0, submitted_under_provider="nvidia_nim")
    assert stamp.submitted_at == 1.0
    assert stamp.submitted_under_provider == "nvidia_nim"


# ---------------------------------------------------------------------------
# 2. _consume_command builds the stamp and threads it, unconditionally, to
#    _dispatch_command
# ---------------------------------------------------------------------------


def test_consume_command_builds_stamp_and_threads_to_dispatch_command(monkeypatch):
    motor = _motor()
    captured = {}
    monkeypatch.setattr(
        motor, "_dispatch_command",
        lambda tipo, payload, history_text=None, source="direct", stamp=None: captured.update(
            tipo=tipo, payload=payload, history_text=history_text, source=source, stamp=stamp,
        ),
    )

    motor._consume_command(
        ("process_context", "wrapped", "honest", "ptt", 111.5, "nvidia_nim")
    )

    assert captured["stamp"] == TurnStamp(submitted_at=111.5, submitted_under_provider="nvidia_nim")


def test_consume_command_unstamped_tuple_threads_stamp_none(monkeypatch):
    motor = _motor()
    captured = {}
    monkeypatch.setattr(
        motor, "_dispatch_command",
        lambda tipo, payload, history_text=None, source="direct", stamp=None: captured.update(stamp=stamp),
    )

    motor._consume_command(("process_context", "hola"))  # legacy 2-tuple

    assert captured["stamp"] is None


# ---------------------------------------------------------------------------
# 3. _dispatch_command threads the stamp to both process_context branches
# ---------------------------------------------------------------------------


def test_dispatch_command_busy_path_threads_stamp_fields_to_enqueue(monkeypatch):
    motor = _motor()
    enqueue_calls = []
    monkeypatch.setattr(
        motor, "enqueue",
        lambda payload, priority=1, source="chat", history_text=None,
        submitted_at=None, submitted_under_provider=None: enqueue_calls.append(
            (payload, priority, source, history_text, submitted_at, submitted_under_provider)
        ),
    )
    motor._processing = True
    stamp = TurnStamp(submitted_at=222.0, submitted_under_provider="local")

    motor._dispatch_command("process_context", "hola", source="direct", stamp=stamp)

    assert enqueue_calls == [("hola", 1, "direct", None, 222.0, "local")]


def test_dispatch_command_busy_path_unstamped_omits_the_kwargs(monkeypatch):
    """A stub with the pre-C1 enqueue() signature (no submitted_at/
    submitted_under_provider params) must still work for an unstamped turn —
    the collapse never forces the kwargs onto a caller that has nothing to
    say."""
    motor = _motor()
    enqueue_calls = []
    monkeypatch.setattr(
        motor, "enqueue",
        lambda payload, priority=1, source="chat", history_text=None: enqueue_calls.append(
            (payload, priority, source, history_text)
        ),
    )
    motor._processing = True

    motor._dispatch_command("process_context", "hola", source="direct", stamp=None)

    assert enqueue_calls == [("hola", 1, "direct", None)]


def test_dispatch_command_idle_path_threads_stamp_to_ejecutar_inferencia(monkeypatch):
    motor = _motor()
    infer_calls = []
    monkeypatch.setattr(
        motor, "_ejecutar_inferencia",
        lambda payload, source="direct", history_text=None, stamp=None: infer_calls.append(
            (payload, source, history_text, stamp)
        ),
    )
    monkeypatch.setattr(motor, "_complete_processing_cycle", lambda *a, **k: None)
    motor._processing = False
    motor._speaking = False
    stamp = TurnStamp(submitted_at=333.0)

    motor._dispatch_command("process_context", "hola", source="direct", stamp=stamp)

    assert infer_calls == [("hola", "direct", None, stamp)]


# ---------------------------------------------------------------------------
# 4. The priority-queue worker builds the stamp at unpack time (enqueue's own
#    tuple storage is unchanged) and threads it to _ejecutar_inferencia
# ---------------------------------------------------------------------------


def test_priority_queue_worker_builds_stamp_from_tuple_and_threads_it(monkeypatch):
    motor = _motor()
    infer_calls = []
    monkeypatch.setattr(
        motor, "_ejecutar_inferencia",
        lambda payload, source="direct", history_text=None, stamp=None: infer_calls.append(
            (payload, source, history_text, stamp)
        ),
    )
    # 7-tuple produced by enqueue(): (priority, ts, payload, source,
    # history_text, submitted_at, submitted_under_provider).
    motor._priority_queue = [(1, time.time(), "hola", "direct", None, 444.0, "nvidia_nim")]

    motor._process_priority_queue()

    assert infer_calls == [("hola", "direct", None, TurnStamp(submitted_at=444.0, submitted_under_provider="nvidia_nim"))]


def test_priority_queue_worker_unstamped_item_threads_stamp_none(monkeypatch):
    motor = _motor()
    infer_calls = []
    monkeypatch.setattr(
        motor, "_ejecutar_inferencia",
        lambda payload, source="direct", history_text=None: infer_calls.append(
            (payload, source, history_text)
        ),
    )
    # 5-tuple, no submitted_at/submitted_under_provider (legacy/agenda shape).
    motor._priority_queue = [(2, time.time(), "bloque", "kira-agenda", None)]

    motor._process_priority_queue()

    assert infer_calls == [("bloque", "kira-agenda", None)]


# ---------------------------------------------------------------------------
# 5. queue_wait_ms computed from a stamp matches the pre-C1 math exactly
# ---------------------------------------------------------------------------


def test_queue_wait_ms_from_stamp_matches_old_math(monkeypatch, caplog):
    clock = {"t": 1000.0}
    monkeypatch.setattr("opencohost.core.llm_engine.time.monotonic", lambda: clock["t"])

    motor = _motor()
    motor._ollama_chat = MagicMock(return_value=_resp("Todo tranquilo por acá."))

    submitted_at = clock["t"]
    clock["t"] += 60.0  # the item sat queued for 60s

    with caplog.at_level(logging.DEBUG, logger="OpenCohost"):
        motor._ejecutar_inferencia("hola", source="direct", stamp=TurnStamp(submitted_at=submitted_at))

    records = _latency_records(caplog)
    assert len(records) == 1
    line = records[0]
    assert "queue_wait_ms=60000" in line
    assert "request_to_tts_total_ms=60000" in line


def test_unstamped_turn_threads_stamp_none_end_to_end(monkeypatch):
    """Tuple -> _consume_command -> _dispatch_command -> _ejecutar_inferencia,
    all the way to dialogue_callback, with no stamp at any point: the
    pre-C1 2-positional-arg dialogue_callback shape is preserved."""
    emitted = MagicMock()
    motor = _motor(dialogue_callback=emitted)
    motor._ollama_chat = MagicMock(return_value=_resp("che, todo bien"))
    motor._processing = False
    motor._speaking = False
    # Isolate the threading-through-_dispatch_command assertion from the
    # unrelated post-turn cascade (command drain / model-switch check /
    # queue re-processing) — mirrors test_command_drain.py's own idiom.
    monkeypatch.setattr(motor, "_complete_processing_cycle", lambda *a, **k: None)

    motor._consume_command(("process_context", "hola"))  # legacy 2-tuple, no stamp

    emitted.assert_called_once_with("che, todo bien", "kira")
