"""T3 (memory-grounding-and-recall-budget-20261016): session topics no longer
depend on the episodic DB stage finishing inside the recall budget.

The topic lookup is in-memory dot products, so it only needs the turn's query
vector. That vector is exposed as ``RecallOutcome.query_vec`` whether or not
fact ranking got one (cold index, timeout) and whether or not the episodic
outcome was discarded.
"""
from __future__ import annotations

import dataclasses
import logging

from opencohost.config import settings
from opencohost.core.memory import episodic_merge
from tests.test_memory_v5_query_embed_once import RECALL_QUERY, _rig
from tests.test_memory_fact_semantic_rank import PROFILE, FakeWorker, _vec
from tests.test_session_topics_injection import (
    OLD_USER,
    VEC,
    _flags,  # noqa: F401  (autouse fixture)
    _hist,
    _motor,
    _seed,
)


def _discarded_recall(query_vec=tuple(VEC)):
    base = episodic_merge.RecallOutcome(
        (), "NO_CANDIDATES", "EPISODIC_TOPIC", None, 640,
        episodic_merge.FALLBACK_TIMEOUT,
    )
    return dataclasses.replace(
        base, fact_query_vec=None, fact_fallback="timeout", query_vec=query_vec
    )


def _topics_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("[SESSION_TOPICS]")]


def test_topics_recalled_when_episodic_discarded(caplog):
    m = _motor()
    _seed(m, (OLD_USER, "Sounds like shaders."))

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        block = m._session_topics_block("direct", _discarded_recall(), _hist("something else"))

    assert block.startswith("<session_topics>")
    (line,) = _topics_lines(caplog)
    assert "result=hit" in line and "no_vec" not in line


def test_fact_vector_still_wins_when_present(caplog):
    m = _motor()
    _seed(m, (OLD_USER, "Sounds like shaders."))
    recall = dataclasses.replace(_discarded_recall(query_vec=None), fact_query_vec=tuple(VEC))

    assert m._session_topics_block("direct", recall, _hist("x")).startswith("<session_topics>")


def test_no_vec_only_when_no_vector_of_any_kind(caplog):
    m = _motor()
    _seed(m, (OLD_USER, "x"))

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        assert m._session_topics_block("direct", _discarded_recall(query_vec=None), []) == ""

    assert "reason=no_vec" in _topics_lines(caplog)[0]


def test_turn_vector_is_stashed_for_commit_when_only_query_vec_exists():
    m = _motor()
    _seed(m, (OLD_USER, "x"))
    m._session_topics_block("direct", _discarded_recall(), [], query_text="hola que tal")
    assert getattr(m, "_session_topics_turn_vec", None) is not None


def test_end_to_end_cold_fact_index_and_budget_overrun(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(settings, "MEMORY_RECALL_BUDGET_MS", 1)
    motor, index, worker = _rig(
        monkeypatch, tmp_path, FakeWorker(), retrieval_delay_s=0.05, reconcile=False
    )
    motor._session_topics_index().add_fragment(_vec(RECALL_QUERY), OLD_USER, "ok.", 1)

    recall = motor._run_episodic_recall(motor._memory, RECALL_QUERY, PROFILE)
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        block = motor._session_topics_block("direct", recall, [])

    # f491516: a recall that finished is no longer relabelled "timeout" after the
    # fact. The retriever returned nothing, so it resolves to no_hit; the budget
    # overrun is recorded as telemetry only.
    assert recall.fallback == episodic_merge.FALLBACK_NO_HIT and recall.over_budget == 1
    assert recall.fact_query_vec is None
    assert block.startswith("<session_topics>")
    assert "no_vec" not in " ".join(_topics_lines(caplog))
