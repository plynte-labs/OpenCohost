"""Judge round 1 (memory-grounding-and-recall-budget-20261016): regressions.

Covers the engine-level fixes that have no home in an existing suite: a recall
turn never feeds the session-topic index with Kira's side, a recall turn that is
later corrected is superseded (not duplicated), the active-card vector reuses
the turn vector when the fact index is cold, and the worker telemetry never
reads a stale/None stat after a lock timeout.
"""
from __future__ import annotations

import dataclasses
import threading
import time

import pytest

from opencohost.config import settings
from opencohost.core.memory import episodic_merge
from opencohost.core.memory.memoria_store import MemoriaStore
from opencohost.core.memory_v5_shadow.semantic_worker import SemanticWorkerService
from tests.test_memory_correction_supersede import PROFILE, _motor as _corr_motor, _rows
from tests.test_session_topics_injection import _flags, _motor  # noqa: F401


# ---- 6. recall turns never index Kira's side --------------------------------

def _capture_submits(monkeypatch, motor):
    calls = []
    monkeypatch.setattr(
        motor, "_session_topics_job",
        lambda worker, idx, clean, kira, seq, generation, vec=None: calls.append((clean, kira)),
    )
    monkeypatch.setattr(motor, "_session_topics_take_turn_vec", lambda clean: [1.0, 0.0, 0.0])
    monkeypatch.setattr(motor, "_session_topics_shutdown", lambda *a, **k: None, raising=False)
    return calls


def _submit(motor, user, kira):
    from types import SimpleNamespace

    motor._memory = SimpleNamespace(runtime=None, run_id=None, worker=object())
    motor._session_topics_submit("direct", False, user, kira)
    ex = getattr(motor, "_session_topics_executor", None)
    if ex is not None:
        ex.submit(lambda: None).result(timeout=5)


def test_recall_turn_is_indexed_without_kira_side(monkeypatch):
    motor = _motor()
    calls = _capture_submits(monkeypatch, motor)

    _submit(motor, "te acuerdas del tesla que comentamos la sesion pasada",
            "Claro, un Tesla Model 3 rojo con 480 caballos de fuerza.")

    assert len(calls) == 1
    assert calls[0][1] == ""


def test_normal_turn_keeps_kira_side_in_the_topic_index(monkeypatch):
    motor = _motor()
    calls = _capture_submits(monkeypatch, motor)

    _submit(motor, "hoy voy a jugar hollow knight toda la tarde en stream",
            "Buena eleccion para la tarde.")

    assert len(calls) == 1
    assert calls[0][1].startswith("Buena eleccion")


# ---- 7. worker telemetry never reads stale/None after a lock timeout ---------

def _hold_lock(worker):
    held, release = threading.Event(), threading.Event()

    def _run():
        with worker._call_lock:
            held.set()
            release.wait(5)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    held.wait(5)
    return release, t


def test_lock_timeout_records_wait_and_zero_call_time():
    worker = SemanticWorkerService(use_dummy=True)
    worker._record_call("embed_query", 11.0, 22.0)  # the previous turn's stats
    release, t = _hold_lock(worker)
    try:
        assert worker.embed_query("hola", timeout_s=0.05) is None
    finally:
        release.set()
        t.join(5)

    stats = worker.last_call_stats()
    assert stats is not None
    assert stats["call_ms"] == 0
    assert stats["wait_ms"] >= 30  # waited ~50 ms, not the previous turn's 11


def test_nonblocking_skip_also_resets_the_previous_stats():
    worker = SemanticWorkerService(use_dummy=True)
    worker._record_call("embed_query", 11.0, 22.0)
    release, t = _hold_lock(worker)
    try:
        assert worker.embed_query("hola", block=False) is None
    finally:
        release.set()
        t.join(5)

    stats = worker.last_call_stats()
    assert stats is not None and stats["call_ms"] == 0 and stats["wait_ms"] < 11


# ---- 9. a corrected recall turn is superseded, not duplicated -----------------

RECALL_USER = "te acuerdas del tesla model tres"
RECALL_KIRA = "Claro, tienes un Tesla Model 3 rojo con ocho mil kilómetros."
FIX_USER = "me equivoqué, el tesla model tres era blanco y no rojo"
FIX_KIRA = "Corregido, blanco."


def test_correction_after_a_recall_turn_supersedes_it(monkeypatch, tmp_path):
    motor = _corr_motor(monkeypatch, tmp_path)
    motor._commit_history(RECALL_USER, RECALL_KIRA, source="direct")
    [first] = _rows(tmp_path)

    motor._commit_history(FIX_USER, FIX_KIRA, source="direct")

    rows = _rows(tmp_path)
    assert len(rows) == 1
    assert rows[0]["id"] == first["id"]
    assert rows[0]["revision"] == first["revision"] + 1


# ---- 10. active cards reuse the turn vector when the fact index is cold ------

class _CardService:
    def __init__(self):
        self.bound = None

    def bind_turn_vector(self, text, vec, attempted=False):
        self.bound = (text, vec, attempted)


def _recall(**kw):
    base = episodic_merge.RecallOutcome.disabled()
    return dataclasses.replace(base, **kw)


def test_active_cards_bind_the_turn_vector_when_fact_vector_is_cold():
    motor = _motor()
    motor.active_card_service = _CardService()

    motor._bind_active_card_vector("hola", _recall(query_vec=(1.0, 0.0), fact_query_vec=None))

    assert motor.active_card_service.bound == ("hola", (1.0, 0.0), True)


def test_active_cards_prefer_the_fact_vector_when_present():
    motor = _motor()
    motor.active_card_service = _CardService()

    motor._bind_active_card_vector(
        "hola", _recall(query_vec=(1.0, 0.0), fact_query_vec=(0.0, 1.0)))

    assert motor.active_card_service.bound[1] == (0.0, 1.0)
