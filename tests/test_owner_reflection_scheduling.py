"""T2 (owner-reflection-memory-20261015, ADR-065): flag, idle predicate, runner yielding."""
from __future__ import annotations

import logging
import threading
import time
from types import SimpleNamespace

import pytest

import opencohost.core.engine.llm_inference_service as inference_service
from opencohost.config import settings
from tests.owner_reflection_support import (
    FakeHandle,
    fact_json,
    feed,
    install_stream,
    make_motor,
    ndjson,
    owner_reflection_lines,
    run_job,
    spy_capture,
)

FACT = fact_json(("What is the owner's dog called?", "The owner's dog is called Rex.", (1,)))


def _due(monkeypatch, tmp_path, **kw):
    motor = make_motor(monkeypatch, tmp_path, **kw)
    feed(motor, 8)
    spy = install_stream(monkeypatch, motor, FACT)
    return motor, spy


def _reflection_threads():
    return [t for t in threading.enumerate() if t.name.startswith("owner-reflection")]


def test_flag_off_is_inert(monkeypatch, tmp_path):
    motor = make_motor(monkeypatch, tmp_path, enabled=False)
    spy = install_stream(monkeypatch, motor, FACT)
    before = set(vars(motor))
    for i in range(10):
        motor._commit_history(f"owner statement {i} about gardening tomatoes", "ok then", source="direct")
    motor.note_owner_turn_arrival()
    motor._owner_reflection_tick()
    motor._owner_reflection_join(1.0)
    assert not [k for k in set(vars(motor)) - before if "owner_reflection" in k]
    assert spy.calls == []
    assert _reflection_threads() == []
    assert getattr(motor, "_owner_reflection", None) is None


@pytest.mark.parametrize(
    "raw,expected",
    [("on", True), ("1", True), ("true", True), ("yes", True), ("ON", True),
     ("", False), ("0", False), ("off", False), ("garbage", False)],
)
def test_flag_resolution_values(monkeypatch, raw, expected):
    monkeypatch.setenv("OPENCOHOST_OWNER_REFLECTION", raw)
    assert settings._resolve_owner_reflection() is expected


def test_flag_unset_disables(monkeypatch):
    monkeypatch.delenv("OPENCOHOST_OWNER_REFLECTION", raising=False)
    assert settings._resolve_owner_reflection() is False


def test_flag_defaults_match_the_adr():
    assert settings.OWNER_REFLECTION_EVERY_N == 8
    assert settings.OWNER_REFLECTION_IDLE_S == 30
    assert settings.OWNER_REFLECTION_MAX_FACTS == 3
    assert settings.OWNER_REFLECTION_REPEAT_THRESHOLD == 0.90
    assert settings.OWNER_REFLECTION_JOB_TIMEOUT_S == 20


def test_due_and_idle_baseline_starts_the_job(monkeypatch, tmp_path):
    motor, spy = _due(monkeypatch, tmp_path)
    run_job(motor)
    assert len(spy.calls) == 1


def test_not_due_below_n(monkeypatch, tmp_path):
    motor = make_motor(monkeypatch, tmp_path)
    feed(motor, 7)
    spy = install_stream(monkeypatch, motor, FACT)
    run_job(motor)
    assert spy.calls == []


@pytest.mark.parametrize("blocker", ["processing", "queue", "generating", "pending_turn", "accumulation"])
def test_job_never_starts_while_turn_pending(monkeypatch, tmp_path, blocker):
    motor, spy = _due(monkeypatch, tmp_path)
    if blocker == "processing":
        motor._processing = True
    elif blocker == "queue":
        motor.command_queue.put(("process_context", "hello there", None, "direct"))
    elif blocker == "generating":
        motor._llm_generating = True
    elif blocker == "pending_turn":
        motor.enqueue("hello there friend", source="direct")
    elif blocker == "accumulation":
        motor._accumulation_buffer.append((time.time(), "pending", "chat"))
    run_job(motor)
    assert spy.calls == []


@pytest.mark.parametrize("blocker", ["speaking", "ptt_hold"])
def test_job_never_starts_during_tts_or_ptt_hold(monkeypatch, tmp_path, blocker):
    motor, spy = _due(monkeypatch, tmp_path)
    if blocker == "speaking":
        motor._speaking = True
    else:
        motor._promotion_ptt_held = True
    run_job(motor)
    assert spy.calls == []


def test_idle_requires_thirty_seconds_since_last_turn(monkeypatch, tmp_path):
    motor, spy = _due(monkeypatch, tmp_path, now=1000.0)
    motor._last_turn_done_monotonic = 1000.0 - 29.0
    run_job(motor)
    assert spy.calls == []
    motor._last_turn_done_monotonic = 1000.0 - 30.0
    run_job(motor)
    assert len(spy.calls) == 1


def test_complete_processing_cycle_stamps_last_turn_done(monkeypatch, tmp_path):
    motor = make_motor(monkeypatch, tmp_path, now=4242.0)
    motor._last_turn_done_monotonic = None
    motor._complete_processing_cycle(process_queue=False)
    assert motor._last_turn_done_monotonic == 4242.0


def test_turn_arrival_returns_without_waiting_for_job(monkeypatch, tmp_path):
    motor = make_motor(monkeypatch, tmp_path)
    feed(motor, 8)
    handle = FakeHandle(ndjson(FACT), hold=True)
    install_stream(monkeypatch, motor, handle=handle)
    motor._owner_reflection_tick()
    assert handle.started.wait(2), "the job must open its stream"
    t0 = time.perf_counter()
    motor.note_owner_turn_arrival()
    assert (time.perf_counter() - t0) < 0.5  # the hook must not wait for the job (judge r1: CI-safe budget)
    motor._owner_reflection_join(3)
    assert not _reflection_threads()


def test_arrival_closes_inflight_response(monkeypatch, tmp_path):
    motor = make_motor(monkeypatch, tmp_path)
    feed(motor, 8)
    handle = FakeHandle(ndjson(FACT), hold=True)
    install_stream(monkeypatch, motor, handle=handle)
    motor._owner_reflection_tick()
    assert handle.started.wait(2)
    motor.note_owner_turn_arrival()
    assert handle.close_threads.count(threading.get_ident()) == 1
    motor._owner_reflection_join(3)
    assert handle.close_threads.count(threading.get_ident()) == 1


def test_cancelled_result_is_discarded(monkeypatch, tmp_path, caplog):
    motor = make_motor(monkeypatch, tmp_path)
    ring = feed(motor, 8)
    handle = FakeHandle(ndjson(FACT), hold=True, keep_yielding=True)
    install_stream(monkeypatch, motor, handle=handle)
    calls = spy_capture(monkeypatch, motor)
    caplog.set_level(logging.INFO)
    motor._owner_reflection_tick()
    assert handle.started.wait(2)
    motor.note_owner_turn_arrival()
    handle.release.set()  # a late, complete result still arrives
    motor._owner_reflection_join(3)
    assert calls == []
    assert ring.last_distilled_seq == 0
    assert ring.owner_turns_since_reflection == 8
    assert any("outcome=cancelled" in line for line in owner_reflection_lines(caplog))


def test_cancelled_run_is_retried_at_the_next_idle_window(monkeypatch, tmp_path):
    motor = make_motor(monkeypatch, tmp_path)
    feed(motor, 8)
    handle = FakeHandle(ndjson(FACT), hold=True, keep_yielding=True)
    spy = install_stream(monkeypatch, motor, handle=handle)
    motor._owner_reflection_tick()
    assert handle.started.wait(2)
    motor.note_owner_turn_arrival()
    handle.release.set()
    motor._owner_reflection_join(3)
    spy._factory = lambda: FakeHandle(ndjson(FACT))
    run_job(motor)
    assert len(spy.calls) == 2


@pytest.mark.parametrize("entry", ["replace_pending", "enqueue_accumulation", "pause_speech_for_ptt", "enqueue"])
def test_every_turn_entry_point_notifies_arrival(monkeypatch, tmp_path, entry):
    motor = make_motor(monkeypatch, tmp_path)
    seen = []
    monkeypatch.setattr(motor, "note_owner_turn_arrival", lambda: seen.append(entry))
    if entry == "replace_pending":
        motor.replace_pending("hello there friend", source="direct")
    elif entry == "enqueue":
        motor.enqueue("hello there friend", source="direct")
    elif entry == "enqueue_accumulation":
        motor.enqueue_accumulation("hello there friend", source="chat")
    else:
        motor.pause_speech_for_ptt()
    assert seen, f"{entry} must notify the reflection job of a turn arrival"


def test_command_queue_arrival_is_noticed_while_the_job_runs(monkeypatch, tmp_path):
    """The API dispatcher puts straight on command_queue, bypassing every hook above."""
    motor = make_motor(monkeypatch, tmp_path)
    feed(motor, 8)
    handle = FakeHandle(ndjson(FACT), hold=True)
    install_stream(monkeypatch, motor, handle=handle)
    motor._owner_reflection_tick()
    assert handle.started.wait(2)
    motor.command_queue.put(("process_context", "hello there friend", None, "direct"))
    assert handle.closed.wait(2), "a queued turn must close the in-flight response"
    motor._owner_reflection_join(3)


def test_job_does_not_use_watchdog(monkeypatch, tmp_path):
    motor, _spy = _due(monkeypatch, tmp_path)

    def boom(*a, **k):
        raise AssertionError("the reflection job must not use the watchdog path")

    monkeypatch.setattr(inference_service, "call_with_watchdog", boom, raising=False)
    monkeypatch.setattr(motor, "_ollama_chat_with_watchdog", boom)
    calls = spy_capture(monkeypatch, motor)
    run_job(motor)
    assert len(calls) == 1


def test_model_not_resident_skips_without_load(monkeypatch, tmp_path, caplog):
    motor, spy = _due(monkeypatch, tmp_path)
    motor.ollama.Client.return_value.ps.side_effect = lambda: SimpleNamespace(models=[])
    caplog.set_level(logging.INFO)
    run_job(motor)
    assert spy.calls == []
    assert any("reason=model_not_resident" in line and "outcome=skipped" in line
               for line in owner_reflection_lines(caplog))
    assert not motor.ollama.Client.return_value.generate.called
    assert not motor.ollama.Client.return_value.chat.called


def test_skip_is_logged_once_per_changed_reason(monkeypatch, tmp_path, caplog):
    motor, _spy = _due(monkeypatch, tmp_path)
    motor.ollama.Client.return_value.ps.side_effect = lambda: SimpleNamespace(models=[])
    caplog.set_level(logging.INFO)
    for _ in range(5):
        motor._owner_reflection_tick()
    assert len(owner_reflection_lines(caplog)) == 1
