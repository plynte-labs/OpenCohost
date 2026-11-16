"""T2 (owner-reflection-memory-20261015, ADR-065): the distilled fact goes through the existing
memory write path, so ranking and supersession apply."""
from __future__ import annotations

import json
import logging

from opencohost.core.memory.memoria_store import MemoriaStore
from tests.owner_reflection_support import (
    PROFILE,
    all_rows,
    fact_json,
    feed,
    install_stream,
    make_motor,
    owner_reflection_lines,
    run_job,
    seed_row,
    spy_capture,
)
from tests.test_memoria_supersede import _FakeWorker, _Recorder, _record, _row, _wire

Q = "What is the owner's dog called?"
A_OLD = "The owner's dog is called Rex."
A_NEW = "The owner's dog is called Max."
FACT_NEW = fact_json((Q, A_NEW, (1,)))


def _due(monkeypatch, tmp_path, reply=FACT_NEW):
    motor = make_motor(monkeypatch, tmp_path)
    feed(motor, 8)
    install_stream(monkeypatch, motor, reply)
    return motor


def test_distilled_fact_written_through_capture_only(monkeypatch, tmp_path):
    motor = _due(monkeypatch, tmp_path)
    calls = spy_capture(monkeypatch, motor)
    store = motor._get_memoria_store()
    upserts, others = [], []
    orig_upsert = store.upsert_draft
    monkeypatch.setattr(store, "upsert_draft", lambda *a, **k: (upserts.append(1), orig_upsert(*a, **k))[1])
    for name in ("supersede_draft", "apply_promotion_batch", "set_flags"):
        if hasattr(store, name):
            monkeypatch.setattr(store, name, lambda *a, _n=name, **k: others.append(_n))
    run_job(motor)
    assert len(calls) == 1
    assert calls[0][0][0] == PROFILE and calls[0][0][2] == Q and calls[0][0][3] == A_NEW
    assert upserts == [1] and others == []


def test_distilled_draft_is_unjudged_and_not_injected(monkeypatch, tmp_path):
    motor = _due(monkeypatch, tmp_path)
    run_job(motor)
    rows = [r for r in all_rows(tmp_path / "memorias.db") if r["title"] == Q]
    assert len(rows) == 1 and rows[0]["status"] == "draft" and rows[0]["judged_at"] == ""
    store = MemoriaStore(tmp_path / "memorias.db")
    assert rows[0]["id"] not in [r["id"] for r in store.list_injection_candidates(PROFILE)]


def test_sweep_is_rearmed_after_write(monkeypatch, tmp_path):
    motor = _due(monkeypatch, tmp_path)
    motor._promotion_swept = True
    run_job(motor)
    motor._owner_reflection_tick()  # the engine thread consumes the worker's re-arm request when idle
    assert motor._promotion_swept is False


def test_sweep_is_not_rearmed_when_nothing_was_written(monkeypatch, tmp_path):
    motor = _due(monkeypatch, tmp_path, reply=fact_json())
    motor._promotion_swept = True
    run_job(motor)
    assert motor._promotion_swept is True


def test_run_advances_the_cursor_and_resets_the_counter(monkeypatch, tmp_path):
    motor = _due(monkeypatch, tmp_path)
    ring = motor._owner_reflection_ring()
    run_job(motor)
    assert ring.last_distilled_seq == 8 and ring.owner_turns_since_reflection == 0


def test_contradicting_fact_supersedes_the_old_row_through_the_judge(monkeypatch, tmp_path):
    """The judge's ``update`` action (ADR-061/F2) supersedes the old durable row and promotes
    the new one. No new mechanism: the old row is kept, inactive, ``superseded_by`` the new row."""
    motor = _due(monkeypatch, tmp_path)
    db = tmp_path / "memorias.db"
    seed_row(db, "old", Q, A_OLD)
    judged = "The owner's dog is called Max."
    worker = _FakeWorker({A_NEW: [1.0, 1.0], judged: [0.0, 1.0]})
    _wire(motor, worker, [_record("old", PROFILE, 1, A_OLD, [1.0, 0.5])])
    run_job(motor)
    draft = next(r for r in all_rows(db) if r["title"] == Q and r["id"] != "old")
    reply = json.dumps({"decisions": [{"i": 1, "keep": True, "text": judged, "origin": "self_stated",
                                       "action": "update", "target": "E1"}]})
    counts = motor.promote_pending_drafts(chat_callable=_Recorder(reply))
    assert counts["kept"] == 1
    new_row, old_row = _row(tmp_path, draft["id"]), _row(tmp_path, "old")
    assert new_row["status"] == "promoted" and new_row["inactive"] == 0 and new_row["content"] == judged
    assert old_row["inactive"] == 1 and old_row["superseded_by"] == draft["id"]
    assert old_row["content"] == A_OLD  # never deleted
    assert len(all_rows(db)) == 2  # the old row and the promoted new one: no third row


def test_ranking_order_unchanged_after_update(monkeypatch, tmp_path):
    motor = _due(monkeypatch, tmp_path)
    db = tmp_path / "memorias.db"
    seed_row(db, "old", Q, A_OLD, updated="2026-03-01")
    seed_row(db, "other", "What does the owner grow?", "The owner grows tomatoes.", updated="2026-02-01")
    judged = "The owner's dog is called Max."
    worker = _FakeWorker({A_NEW: [1.0, 1.0], judged: [0.0, 1.0]})
    _wire(motor, worker, [_record("old", PROFILE, 1, A_OLD, [1.0, 0.5])])
    run_job(motor)
    draft = next(r for r in all_rows(db) if r["title"] == Q and r["id"] != "old")
    reply = json.dumps({"decisions": [{"i": 1, "keep": True, "text": judged, "origin": "self_stated",
                                       "action": "update", "target": "E1"}]})
    motor.promote_pending_drafts(chat_callable=_Recorder(reply))
    ids = [r["id"] for r in MemoriaStore(db).list_injection_candidates(PROFILE)]
    assert "old" not in ids
    assert ids == [draft["id"], "other"]  # durable first, then most recently updated first


def test_model_index_outside_batch_is_rejected(monkeypatch, tmp_path, caplog):
    motor = _due(monkeypatch, tmp_path, reply=fact_json((Q, A_NEW, (9,))))
    calls = spy_capture(monkeypatch, motor)
    caplog.set_level(logging.INFO)
    run_job(motor)
    assert calls == []
    lines = owner_reflection_lines(caplog)
    assert len(lines) == 1, lines
    line = lines[0]
    assert "schema_rejected=1" in line and "written=0" in line


def test_partial_valid_facts_still_write(monkeypatch, tmp_path):
    reply = fact_json((Q, A_NEW, (1,)), ("What does the owner grow?", "The owner grows tomatoes.", (99,)))
    motor = _due(monkeypatch, tmp_path, reply=reply)
    calls = spy_capture(monkeypatch, motor)
    run_job(motor)
    assert len(calls) == 1


def test_facts_per_run_are_capped(monkeypatch, tmp_path):
    facts = [(f"Question number {i} about gardening?", f"The owner states answer number {i} clearly.", (1,))
             for i in range(6)]
    motor = _due(monkeypatch, tmp_path, reply=fact_json(*facts))
    calls = spy_capture(monkeypatch, motor)
    run_job(motor)
    assert len(calls) == 3
