"""T2 (memory-grounding-and-recall-budget-20261016): one query embedding per turn.

Before: the episodic stage embedded the query, then the fact stage embedded the
same text again on the same worker. After: the episodic embedding is reused for
fact ranking, and survives the episodic outcome being discarded by the budget.
"""
from __future__ import annotations

import time

import pytest

from opencohost.config import settings
from opencohost.core.memory_v5_shadow.episodic_recall import (
    EpisodicRecallCoordinator,
    RecallMode,
)
from opencohost.core.memory_v5_shadow.fact_index import FACT_FALLBACK_COLD
from opencohost.core.memory_v5_shadow.subsystem import MemorySubsystem
from tests.test_memory_fact_semantic_rank import (
    FACT_CAR,
    FACT_PET,
    PROFILE,
    FakeWorker,
    _make_index,
    _make_motor,
    _recall_records,
    _rows_from,
    _prompt,
    _block_lines,
    _vec,
)

RECALL_QUERY = "recuerdas zqquery como se llama tu mascota"


class _SlowEmptyRetriever:
    def __init__(self, delay_s=0.0):
        self.delay_s = delay_s

    def get_candidates(self, *a, **kw):
        time.sleep(self.delay_s)
        return []


def _cache_stub():
    class _Cache:
        def get_exchange_embeddings_by_profile(self, profile_id):
            return []

    return _Cache()


def _rig(monkeypatch, tmp_path, worker, retrieval_delay_s=0.0, reconcile=True):
    rows = [{"id": "pet", "content": FACT_PET}, {"id": "car", "content": FACT_CAR}]
    motor, db = _make_motor(monkeypatch, tmp_path, rows)
    index, cache, _ = _make_index(tmp_path, worker=worker)
    if reconcile:
        index.reconcile(PROFILE, _rows_from(db))
    worker.queries.clear()
    coord = EpisodicRecallCoordinator(
        shadow_conn=None, cache_store=_cache_stub(), worker=worker, mode=RecallMode.ACTIVE
    )
    coord.retriever = _SlowEmptyRetriever(retrieval_delay_s)
    motor._memory = MemorySubsystem(mode="ACTIVE", coordinator=coord, fact_index=index)
    return motor, index, worker


def test_one_embed_call_per_turn(monkeypatch, tmp_path, caplog):
    import logging

    motor, index, worker = _rig(monkeypatch, tmp_path, FakeWorker())

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        lines = _block_lines(_prompt(motor, RECALL_QUERY))

    assert len(worker.queries) == 1, worker.queries
    # Fact ranking still ran on the reused vector.
    (rec,) = _recall_records(caplog)
    assert "fact_fallback=none" in rec.getMessage()
    assert FACT_PET in lines


def test_fact_vector_survives_slow_episodic_recall(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "MEMORY_RECALL_BUDGET_MS", 1)
    motor, index, worker = _rig(monkeypatch, tmp_path, FakeWorker(), retrieval_delay_s=0.05)

    outcome = motor._run_episodic_recall(motor._memory, RECALL_QUERY, PROFILE)

    assert outcome.fallback != "timeout"  # T3: a finished recall is not discarded post hoc
    assert outcome.over_budget == 1
    assert outcome.fact_query_vec is not None
    assert outcome.fact_fallback == "none"
    assert len(worker.queries) == 1


def test_turn_vector_exposed_even_when_fact_index_is_cold(monkeypatch, tmp_path):
    motor, index, worker = _rig(monkeypatch, tmp_path, FakeWorker(), reconcile=False)

    outcome = motor._run_episodic_recall(motor._memory, RECALL_QUERY, PROFILE)

    assert outcome.fact_fallback == FACT_FALLBACK_COLD
    assert outcome.fact_query_vec is None
    assert outcome.query_vec is not None
    assert len(worker.queries) == 1


def test_query_vector_with_precomputed_never_calls_the_worker(tmp_path):
    index, cache, worker = _make_index(tmp_path)
    index.reconcile(PROFILE, [])
    pre = _vec("zqquery")

    vec, code = index.query_vector(PROFILE, "zqquery", 0.0, precomputed=pre)
    assert worker.queries == []
    assert code == FACT_FALLBACK_COLD and vec is None  # no fact embeddings yet: cold

    fresh_motor_rows = [{"id": "pet", "content": FACT_PET, "profile_id": PROFILE, "revision": 1, "status": "promoted"}]
    index.reconcile(PROFILE, fresh_motor_rows)
    worker.queries.clear()
    vec, code = index.query_vector(PROFILE, "zqquery", 0.0, precomputed=pre)
    assert worker.queries == []
    assert code == "none" and list(vec) == list(pre)


def test_episodic_packet_carries_the_embedded_vector():
    worker = FakeWorker()
    coord = EpisodicRecallCoordinator(
        shadow_conn=None, cache_store=_cache_stub(), worker=worker, mode=RecallMode.ACTIVE
    )
    coord.retriever = _SlowEmptyRetriever()
    packet = coord.process_query(RECALL_QUERY, profile_id=PROFILE)
    assert packet.query_vec is not None and len(packet.query_vec) == 4
    assert len(worker.queries) == 1
