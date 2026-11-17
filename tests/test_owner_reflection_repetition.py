"""T2 (owner-reflection-memory-20261015, ADR-065 section 6): the repetition check."""
from __future__ import annotations

import logging

import pytest

from opencohost.config import settings
from opencohost.core.context.owner_reflection import FactCandidate, repeats_existing, repeats_spoken, similarity
from opencohost.core.memory.memoria_store import MemoriaStore, derive_stable_key
from tests.owner_reflection_support import (
    PROFILE,
    fact_json,
    feed,
    install_stream,
    make_motor,
    owner_reflection_lines,
    run_job,
    seed_row,
    spy_capture,
)

Q = "What is the owner's dog called?"
A_REX = "The owner's dog is called Rex."
A_MAX = "The owner's dog is called Max."


def _line(motor, caplog):
    lines = owner_reflection_lines(caplog)
    assert len(lines) == 1, lines
    return lines[0]


def _due(monkeypatch, tmp_path, answer, *, kira_lines=None, question=Q):
    motor = make_motor(monkeypatch, tmp_path)
    ring = motor._owner_reflection_ring()
    for i in range(1, 9):
        kira = (kira_lines or {}).get(i, f"kira answer number {i} about compost heaps")
        ring.offer("direct", False, f"owner statement number {i} about gardening tomatoes", kira)
    install_stream(monkeypatch, motor, fact_json((question, answer, (1,))))
    return motor


def _jac(a, b):
    return similarity(a, b)


def test_repeated_fact_is_rejected(monkeypatch, tmp_path, caplog):
    motor = _due(monkeypatch, tmp_path, A_REX)
    seed_row(tmp_path / "memorias.db", "existing", Q, A_REX)
    calls = spy_capture(monkeypatch, motor)
    caplog.set_level(logging.INFO)
    run_job(motor)
    assert calls == []
    line = _line(motor, caplog)
    assert "repeated=1" in line and "written=0" in line


def test_fact_repeating_recent_kira_line_is_rejected(monkeypatch, tmp_path, caplog):
    spoken = {3: "As you said, the owner's dog is called Rex."}
    motor = _due(monkeypatch, tmp_path, "As you said, the owner's dog is called Rex.", kira_lines=spoken)
    calls = spy_capture(monkeypatch, motor)
    caplog.set_level(logging.INFO)
    run_job(motor)
    assert calls == []
    assert "repeated=1" in _line(motor, caplog)


def test_kira_lines_older_than_the_window_do_not_count(monkeypatch, tmp_path, caplog):
    spoken = {1: "As you said, the owner's dog is called Rex."}  # 8th-newest: outside the last 6
    motor = _due(monkeypatch, tmp_path, "As you said, the owner's dog is called Rex.", kira_lines=spoken)
    calls = spy_capture(monkeypatch, motor)
    caplog.set_level(logging.INFO)
    run_job(motor)
    assert len(calls) == 1 and "repeated=0" in _line(motor, caplog)


def test_slot_change_passes_repetition_check(monkeypatch, tmp_path, caplog):
    assert _jac(A_REX, A_MAX) < 0.90  # the fixture really is below the threshold
    motor = _due(monkeypatch, tmp_path, A_MAX)
    seed_row(tmp_path / "memorias.db", "existing", Q, A_REX)
    calls = spy_capture(monkeypatch, motor)
    caplog.set_level(logging.INFO)
    run_job(motor)
    assert len(calls) == 1
    line = _line(motor, caplog)
    assert "repeated=0" in line and "written=1" in line


def test_stable_key_collision_is_rejected(monkeypatch, tmp_path, caplog):
    motor = _due(monkeypatch, tmp_path, A_MAX)
    key = derive_stable_key(PROFILE, f"{A_MAX} {Q}")  # the key is derived answer-first
    assert key is not None
    # An UNJUDGED draft with the same key and the same content is a repeat (a different content
    # would be a correction and is revised instead: see test_owner_reflection_judge_r1).
    MemoriaStore(tmp_path / "memorias.db").upsert_draft(PROFILE, key, "earlier title", A_MAX)
    calls = spy_capture(monkeypatch, motor)
    caplog.set_level(logging.INFO)
    run_job(motor)
    assert calls == []
    line = _line(motor, caplog)
    assert "repeated=1" in line and "unkeyed=0" in line and "outcome=ok" in line


def test_unkeyable_fact_is_counted_not_written(monkeypatch, tmp_path, caplog):
    motor = _due(monkeypatch, tmp_path, "ok", question="Hm?")
    calls = spy_capture(monkeypatch, motor)
    caplog.set_level(logging.INFO)
    run_job(motor)
    assert calls == []
    assert "unkeyed=1" in _line(motor, caplog)


def test_threshold_is_configurable(monkeypatch, tmp_path, caplog):
    existing = A_REX
    j = _jac(existing, A_MAX)
    assert 0.0 < j < 1.0
    # The pure function decides at the boundary.
    cand = FactCandidate(Q, A_MAX, (1,))
    assert repeats_existing(cand, [existing], j - 0.001) is True
    assert repeats_existing(cand, [existing], j + 0.001) is False
    # And the engine passes the live setting through.
    monkeypatch.setattr(settings, "OWNER_REFLECTION_REPEAT_THRESHOLD", j - 0.001)
    motor = _due(monkeypatch, tmp_path, A_MAX)
    seed_row(tmp_path / "memorias.db", "existing", Q, A_REX)
    calls = spy_capture(monkeypatch, motor)
    caplog.set_level(logging.INFO)
    run_job(motor)
    assert calls == [] and "repeated=1" in _line(motor, caplog)


def test_repeats_spoken_uses_the_answer_only():
    cand = FactCandidate("A very different question here?", "Your dog is called Rex, remember.", (1,))
    assert repeats_spoken(cand, ["Your dog is called Rex, remember."], 0.90) is True
    assert repeats_spoken(cand, ["Something about compost heaps entirely."], 0.90) is False
    assert repeats_spoken(cand, [], 0.90) is False


def test_duplicate_facts_inside_one_reply_write_once(monkeypatch, tmp_path, caplog):
    motor = make_motor(monkeypatch, tmp_path)
    feed(motor, 8)
    install_stream(monkeypatch, motor, fact_json((Q, A_MAX, (1,)), (Q, A_MAX, (2,))))
    calls = spy_capture(monkeypatch, motor)
    caplog.set_level(logging.INFO)
    run_job(motor)
    assert len(calls) == 1
    line = _line(motor, caplog)
    assert "distilled=2" in line and "repeated=1" in line and "written=1" in line
