"""Judge round 1 (owner-reflection-memory-20261015): one RED test per finding."""
from __future__ import annotations

import json
import logging
import threading
from types import SimpleNamespace

import pytest

import opencohost.core.llm_engine  # noqa: F401  (first: breaks the mixin import cycle)
import opencohost.core.engine.llm_engine_reflection as reflection_mod  # noqa: E402
from opencohost.core.memory.memoria_store import MemoriaStore, derive_stable_key
from tests.owner_reflection_support import (
    PROFILE,
    FakeHandle,
    all_rows,
    fact_json,
    feed,
    install_stream,
    make_motor,
    ndjson,
    owner_reflection_lines,
    run_job,
    seed_row,
    spy_capture,
)
from tests.test_memoria_supersede import _Recorder

Q = "What is the owner's dog called?"
A = "The owner's dog is called Rex."
LONG_Q = ("In the long running conversation about the games that the owner likes to play "
          "on the stream during the weekly evening show, which game does the owner play")
G1 = "The owner plays Elden Ring."
G2 = "The owner plays Hollow Knight."


def _clock(monkeypatch, start=1000.0):
    box = [start]
    monkeypatch.setattr(reflection_mod, "_monotonic", lambda: box[0])
    return box


def _line(caplog):
    lines = owner_reflection_lines(caplog)
    assert len(lines) == 1, lines
    return lines[0]


# 1. purge / delete must clear the reflection state ---------------------------------

def test_purge_clears_the_ring(monkeypatch, tmp_path):
    motor = make_motor(monkeypatch, tmp_path)
    ring = feed(motor, 8)
    spy = install_stream(monkeypatch, motor, fact_json((Q, A, (1,))))
    motor.reset_session_topics()
    run_job(motor)
    assert ring.snapshot() == () and spy.calls == []


def test_purge_mid_job_cancels_and_writes_nothing(monkeypatch, tmp_path):
    motor = make_motor(monkeypatch, tmp_path)
    feed(motor, 8)
    handle = FakeHandle(ndjson(fact_json((Q, A, (1,)))), hold=True, keep_yielding=True)
    install_stream(monkeypatch, motor, handle=handle)
    calls = spy_capture(monkeypatch, motor)
    motor._owner_reflection_tick()
    assert handle.started.wait(2)
    motor.reset_session_topics()
    assert handle.closed.is_set()
    handle.release.set()
    motor._owner_reflection_join(3)
    assert calls == []


# 2. the counter reconciles after ring eviction -----------------------------------------

def test_eviction_does_not_leave_the_job_due_forever(monkeypatch, tmp_path):
    motor = make_motor(monkeypatch, tmp_path)
    ring = feed(motor, 30)  # the ring keeps 16
    install_stream(monkeypatch, motor, fact_json((Q, A, (1,))))
    run_job(motor)
    run_job(motor)
    assert ring.pending_count() == 0
    assert not ring.due(8)
    ps = motor.ollama.Client.return_value.ps
    ps.reset_mock()
    motor._owner_reflection_tick()
    assert ps.call_count == 0


def test_due_with_an_empty_batch_backs_off_and_reconciles(monkeypatch, tmp_path):
    now = 1000.0
    motor = make_motor(monkeypatch, tmp_path, now=now)
    ring = motor._owner_reflection_ring()
    ring.owner_turns_since_reflection = 9  # stale counter, nothing left in the ring
    ps = motor.ollama.Client.return_value.ps
    motor._owner_reflection_tick()
    assert ps.call_count == 0
    assert not ring.due(8)
    assert motor._owner_reflection_next_check > now


# 3. the key and the corrections ---------------------------------------------------------

def test_long_question_correction_reaches_the_judge(monkeypatch, tmp_path, caplog):
    motor = make_motor(monkeypatch, tmp_path)
    feed(motor, 8)
    spy = install_stream(monkeypatch, motor, fact_json((LONG_Q, G1, (1,))))
    calls = spy_capture(monkeypatch, motor)
    run_job(motor)
    assert len(calls) == 1
    feed(motor, 8, start=9)
    spy._factory = lambda: FakeHandle(ndjson(fact_json((LONG_Q, G2, (1,)))))
    caplog.set_level(logging.INFO)
    caplog.clear()
    run_job(motor)
    assert len(calls) == 2, "the correction must not be dropped as a repeat"
    assert "repeated=0" in _line(caplog)


def test_collision_with_an_unjudged_draft_revises_it(monkeypatch, tmp_path):
    motor = make_motor(monkeypatch, tmp_path)
    feed(motor, 8)
    install_stream(monkeypatch, motor, fact_json((Q, "The owner has a pet called Max.", (1,))))
    key = derive_stable_key(PROFILE, "The owner has a pet called Max. " + Q)
    store = MemoriaStore(tmp_path / "memorias.db")
    store.upsert_draft(PROFILE, key, "older title", "Something about a completely different subject.")
    calls = spy_capture(monkeypatch, motor)
    run_job(motor)
    assert len(calls) == 1
    rows = all_rows(tmp_path / "memorias.db")
    assert len(rows) == 1 and rows[0]["revision"] == 2 and rows[0]["content"].endswith("Max.")


def test_collision_with_a_durable_row_is_rejected(monkeypatch, tmp_path, caplog):
    motor = make_motor(monkeypatch, tmp_path)
    feed(motor, 8)
    install_stream(monkeypatch, motor, fact_json((Q, "The owner has a pet called Max.", (1,))))
    key = derive_stable_key(PROFILE, "The owner has a pet called Max. " + Q)
    seed_row(tmp_path / "memorias.db", "durable", "t", "Something about a completely different subject.",
             stable_key=key)
    calls = spy_capture(monkeypatch, motor)
    caplog.set_level(logging.INFO)
    run_job(motor)
    assert calls == [] and "repeated=1" in _line(caplog)


# 4/6. the re-armed sweep -----------------------------------------------------------------

def _written(monkeypatch, tmp_path, reply=None):
    motor = make_motor(monkeypatch, tmp_path)
    feed(motor, 8)
    install_stream(monkeypatch, motor, reply or fact_json((Q, A, (1,))))
    motor._promotion_swept = True
    run_job(motor)
    return motor


def test_worker_does_not_touch_the_sweep_flag_directly(monkeypatch, tmp_path):
    motor = _written(monkeypatch, tmp_path)
    assert motor._promotion_swept is True  # only the engine thread consumes the re-arm


def test_rearm_is_applied_by_the_engine_thread_when_idle(monkeypatch, tmp_path):
    motor = _written(monkeypatch, tmp_path)
    motor._owner_reflection_tick()
    assert motor._promotion_swept is False


def test_rearm_survives_a_sweep_finishing_after_the_write(monkeypatch, tmp_path):
    motor = _written(monkeypatch, tmp_path)
    motor._promotion_swept = True  # an engine-thread sweep that was mid-flight completes
    motor._owner_reflection_tick()
    assert motor._promotion_swept is False


@pytest.mark.parametrize("blocker", ["speaking", "ptt_hold", "queue", "recent_turn", "processing", "generating"])
def test_rearmed_sweep_waits_for_idle(monkeypatch, tmp_path, blocker):
    motor = _written(monkeypatch, tmp_path)
    if blocker == "speaking":
        motor._speaking = True
    elif blocker == "ptt_hold":
        motor._promotion_ptt_held = True
    elif blocker == "queue":
        motor.command_queue.put(("process_context", "hello there", None, "direct"))
    elif blocker == "recent_turn":
        motor._last_turn_done_monotonic = 1000.0 - 5.0
    elif blocker == "processing":
        motor._processing = True
    else:
        motor._llm_generating = True
    motor._owner_reflection_tick()
    assert motor._promotion_swept is True
    # and it is applied once the engine is idle again
    motor._speaking = False
    motor._promotion_ptt_held = False
    motor._processing = False
    motor._llm_generating = False
    while not motor.command_queue.empty():
        motor.command_queue.get_nowait()
    motor._last_turn_done_monotonic = 1000.0 - 60.0
    motor._owner_reflection_tick()
    assert motor._promotion_swept is False


def test_rearmed_sweep_waits_for_a_live_job(monkeypatch, tmp_path):
    motor = _written(monkeypatch, tmp_path)
    feed(motor, 8, start=9)
    handle = FakeHandle(ndjson(fact_json()), hold=True)
    install_stream(monkeypatch, motor, handle=handle)
    motor._owner_reflection_tick()  # applies nothing yet: a job is starting
    assert handle.started.wait(2)
    assert motor._promotion_swept is True
    motor.note_owner_turn_arrival()
    motor._owner_reflection_join(3)


def test_partial_write_then_cancel_still_rearms(monkeypatch, tmp_path, caplog):
    motor = make_motor(monkeypatch, tmp_path)
    feed(motor, 8)
    facts = [("Question one about gardening?", "The owner grows tomatoes in the garden.", (1,)),
             ("Question two about cooking?", "The owner bakes sourdough bread weekly.", (2,))]
    install_stream(monkeypatch, motor, fact_json(*facts))
    real = motor._capture_memoria
    seen = []

    def capture_then_cancel(*a, **k):
        out = real(*a, **k)
        seen.append(1)
        motor.note_owner_turn_arrival()  # a turn arrives right after the first write
        return out

    monkeypatch.setattr(motor, "_capture_memoria", capture_then_cancel)
    motor._promotion_swept = True
    caplog.set_level(logging.INFO)
    run_job(motor)
    assert len(seen) == 1
    assert "outcome=cancelled" in _line(caplog) and "written=1" in _line(caplog)
    monkeypatch.setattr(motor, "_capture_memoria", real)
    motor._owner_reflection_tick()  # the cancelled run is retried first; the sweep waits for it
    motor._owner_reflection_join(3)
    motor._owner_reflection_tick()
    assert motor._promotion_swept is False


# 5. the watcher yields to every runner user -------------------------------------------

@pytest.mark.parametrize("flag", ["_llm_generating", "_speaking", "_promotion_ptt_held"])
def test_watcher_yields_to_pregen_speech_and_ptt(monkeypatch, tmp_path, flag):
    motor = make_motor(monkeypatch, tmp_path)
    feed(motor, 8)
    handle = FakeHandle(ndjson(fact_json()), hold=True)
    install_stream(monkeypatch, motor, handle=handle)
    motor._owner_reflection_tick()
    assert handle.started.wait(2)
    setattr(motor, flag, True)
    assert handle.closed.wait(2), f"{flag} must cancel the job"
    motor._owner_reflection_join(3)


def test_the_job_never_sets_the_runner_flags(monkeypatch, tmp_path):
    motor = make_motor(monkeypatch, tmp_path)
    feed(motor, 8)
    handle = FakeHandle(ndjson(fact_json()), hold=True)
    install_stream(monkeypatch, motor, handle=handle)
    motor._owner_reflection_tick()
    assert handle.started.wait(2)
    assert not motor.llm_generating and not motor.is_speaking and not motor._promotion_ptt_held
    assert not handle.closed.wait(0.3), "an idle engine must not cancel the job"
    handle.release.set()
    motor._owner_reflection_join(3)


# 7. the recent-facts check targets the rows production creates --------------------------

def _promote(motor, text):
    reply = json.dumps({"decisions": [{"i": 1, "keep": True, "text": text, "origin": "self_stated"}]})
    return motor.promote_pending_drafts(chat_callable=_Recorder(reply))


def test_promoted_distilled_fact_is_repeated_when_redistilled(monkeypatch, tmp_path, caplog):
    motor = make_motor(monkeypatch, tmp_path)
    feed(motor, 8)
    spy = install_stream(monkeypatch, motor, fact_json((Q, A, (1,))))
    run_job(motor)
    assert _promote(motor, A)["kept"] == 1
    row = next(r for r in all_rows(tmp_path / "memorias.db"))
    assert row["status"] == "promoted" and row["title"] != Q  # production rewrites the title
    feed(motor, 8, start=9)
    spy._factory = lambda: FakeHandle(ndjson(fact_json(("Dog name?", A, (1,)))))
    calls = spy_capture(monkeypatch, motor)
    caplog.set_level(logging.INFO)
    caplog.clear()
    run_job(motor)
    assert calls == [] and "repeated=1" in _line(caplog)


def test_unjudged_distilled_draft_counts_as_a_recent_fact(monkeypatch, tmp_path, caplog):
    motor = make_motor(monkeypatch, tmp_path)
    feed(motor, 8)
    spy = install_stream(monkeypatch, motor, fact_json((Q, A, (1,))))
    run_job(motor)
    feed(motor, 8, start=9)
    spy._factory = lambda: FakeHandle(ndjson(fact_json(("Dog name?", A, (1,)))))
    calls = spy_capture(monkeypatch, motor)
    caplog.set_level(logging.INFO)
    caplog.clear()
    run_job(motor)
    assert calls == [] and "repeated=1" in _line(caplog)


# 8. suggestions ---------------------------------------------------------------------------

def test_skip_dedupe_holds_across_back_off_windows(monkeypatch, tmp_path, caplog):
    box = _clock(monkeypatch)
    motor = make_motor(monkeypatch, tmp_path)
    monkeypatch.setattr(reflection_mod, "_monotonic", lambda: box[0])
    feed(motor, 8)
    install_stream(monkeypatch, motor, fact_json())
    motor.ollama.Client.return_value.ps.side_effect = lambda: SimpleNamespace(models=[])
    caplog.set_level(logging.INFO)
    for _ in range(4):
        box[0] += 10_000.0
        motor._last_turn_done_monotonic = box[0] - 60.0
        motor._owner_reflection_tick()
    assert len(owner_reflection_lines(caplog)) == 1
    motor._loaded_model = ""  # a different gate: model_not_loaded
    box[0] += 10_000.0
    motor._last_turn_done_monotonic = box[0] - 60.0
    motor._owner_reflection_tick()
    lines = owner_reflection_lines(caplog)
    assert len(lines) == 2 and "reason=model_not_loaded" in lines[1]


def test_back_off_is_exponential_with_a_cap_and_resets_on_success(monkeypatch, tmp_path):
    box = _clock(monkeypatch)
    motor = make_motor(monkeypatch, tmp_path)
    monkeypatch.setattr(reflection_mod, "_monotonic", lambda: box[0])
    feed(motor, 8)
    spy = install_stream(monkeypatch, motor, fact_json())
    resident = motor.ollama.Client.return_value
    resident.ps.side_effect = lambda: SimpleNamespace(models=[])
    waits = []
    for _ in range(8):
        motor._last_turn_done_monotonic = box[0] - 60.0
        motor._owner_reflection_tick()
        waits.append(motor._owner_reflection_next_check - box[0])
        box[0] = motor._owner_reflection_next_check + 1.0
    assert waits[:3] == [30.0, 60.0, 120.0]
    assert max(waits) == 600.0
    resident.ps.side_effect = lambda: SimpleNamespace(models=[SimpleNamespace(model=motor._loaded_model)])
    motor._last_turn_done_monotonic = box[0] - 60.0
    run_job(motor)
    assert len(spy.calls) == 1
    resident.ps.side_effect = lambda: SimpleNamespace(models=[])
    feed(motor, 8, start=9)
    motor._last_turn_done_monotonic = box[0] - 60.0
    motor._owner_reflection_tick()
    assert motor._owner_reflection_next_check - box[0] == 30.0


def test_repeated_failure_reason_is_logged_once(monkeypatch, tmp_path, caplog):
    box = _clock(monkeypatch)
    motor = make_motor(monkeypatch, tmp_path)
    monkeypatch.setattr(reflection_mod, "_monotonic", lambda: box[0])
    feed(motor, 8)

    def boom(**kwargs):
        raise OSError("down")

    monkeypatch.setattr(motor, "_owner_reflection_open_stream", boom)
    caplog.set_level(logging.INFO)
    waits = []
    for _ in range(3):
        motor._last_turn_done_monotonic = box[0] - 60.0
        run_job(motor)
        waits.append(motor._owner_reflection_next_check - box[0])
        box[0] = motor._owner_reflection_next_check + 1.0
    lines = owner_reflection_lines(caplog)
    assert len(lines) == 1 and "outcome=failed" in lines[0] and "reason=transport" in lines[0]
    assert waits == [30.0, 60.0, 120.0]

