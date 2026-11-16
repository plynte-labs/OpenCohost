"""T1 (memory-grounding-and-recall-budget-20261016): additive recall stage telemetry.

The `memory_recall:` line gains embed_ms / worker_wait_ms / retrieval_ms /
device so the budget owner can see which stage consumes the 400 ms. Numbers and
fixed codes only; a line without stage data is byte-identical to the old one.
"""
from __future__ import annotations

import re
import threading
import time
from unittest.mock import MagicMock

from opencohost.core.memory import episodic_merge
from opencohost.core.memory_v5_shadow.episodic_recall import (
    EpisodicRecallCoordinator,
    RecallMode,
)
from opencohost.core.memory_v5_shadow.semantic_worker import SemanticWorkerService
from opencohost.core.memory_v5_shadow.subsystem import MemorySubsystem

_STAGE_RE = re.compile(
    r" embed_ms=\d+ worker_wait_ms=\d+ retrieval_ms=\d+ device=[a-z0-9_]+$"
)


def _outcome(**stage):
    return episodic_merge.RecallOutcome(
        (), "NO_CANDIDATES", "EPISODIC_TOPIC", None, 640,
        episodic_merge.FALLBACK_TIMEOUT, **stage,
    )


def test_recall_line_carries_stage_fields():
    line = episodic_merge.format_recall_telemetry(
        _outcome(embed_ms=78, worker_wait_ms=3, retrieval_ms=512, device="cpu"),
        lexical=1, episodes=0,
    )
    assert _STAGE_RE.search(line), line
    assert "embed_ms=78 worker_wait_ms=3 retrieval_ms=512 device=cpu" in line


def test_stage_fields_are_codes_only_never_free_text():
    line = episodic_merge.format_recall_telemetry(
        _outcome(embed_ms=1, worker_wait_ms=0, retrieval_ms=2, device="cpu secret chat text!"),
        lexical=0, episodes=0,
    )
    assert "secret" not in line and "chat" not in line and "!" not in line
    assert _STAGE_RE.search(line), line


def test_line_without_stage_data_is_unchanged():
    line = episodic_merge.format_recall_telemetry(_outcome(), lexical=1, episodes=0)
    assert "embed_ms" not in line and "device=" not in line
    assert " fact_fallback=" in line and line.endswith(" over_budget=0")


def test_worker_records_embed_timing_and_device():
    worker = SemanticWorkerService(use_dummy=True)
    worker.start()
    try:
        assert worker.embed_query("hola") is not None
        stats = worker.last_call_stats()
        assert stats is not None
        assert stats["method"] == "embed_query"
        assert stats["call_ms"] >= 0 and stats["wait_ms"] >= 0
        assert worker.device == "dummy"
    finally:
        worker.shutdown()


def test_worker_wait_ms_measures_lock_contention():
    worker = SemanticWorkerService(use_dummy=True)
    worker.start()
    try:
        worker._call_lock.acquire()
        threading.Timer(0.2, worker._call_lock.release).start()
        assert worker.embed_query("hola", timeout_s=2.0) is not None
        stats = worker.last_call_stats()
        assert stats["wait_ms"] >= 150
    finally:
        worker.shutdown()


def test_last_call_stats_is_per_thread():
    worker = SemanticWorkerService(use_dummy=True)
    worker.start()
    try:
        worker.embed_query("hola")
        seen = {}
        t = threading.Thread(target=lambda: seen.update(s=worker.last_call_stats()))
        t.start()
        t.join()
        assert seen["s"] is None
    finally:
        worker.shutdown()


class _StubWorker:
    device = "cpu"

    def __init__(self):
        self._stats = None

    def embed_query(self, text, timeout_s=0.5, **kw):
        time.sleep(0.02)
        self._stats = {"method": "embed_query", "wait_ms": 4, "call_ms": 20}
        return [0.1] * 384

    def last_call_stats(self):
        return self._stats


class _EmptyRetriever:
    def get_candidates(self, *a, **kw):
        return []


def test_packet_and_decision_carry_stage_fields():
    cache = MagicMock()
    cache.get_exchange_embeddings_by_profile.return_value = []
    coord = EpisodicRecallCoordinator(
        shadow_conn=None, cache_store=cache, worker=_StubWorker(), mode=RecallMode.ACTIVE
    )
    coord.retriever = _EmptyRetriever()
    packet = coord.process_query(
        "te acuerdas de lo que hablamos sobre audifonos", profile_id="p1"
    )
    assert packet.reason_code == "NO_CANDIDATES"
    assert packet.embed_ms == 20 and packet.worker_wait_ms == 4
    assert packet.retrieval_ms is not None and packet.retrieval_ms >= 0
    assert packet.device == "cpu"

    mem = MemorySubsystem(mode="ACTIVE", coordinator=coord)
    decision = mem.recall_decision("te acuerdas de lo que hablamos sobre audifonos", "p1")
    assert decision.embed_ms == 20 and decision.device == "cpu"


def test_resolve_recall_outcome_forwards_stage_fields():
    from types import SimpleNamespace

    decision = SimpleNamespace(
        reason="NO_CANDIDATES", scope="EPISODIC_TOPIC", top_score=None, episodes=(),
        hit=False, embed_ms=11, worker_wait_ms=2, retrieval_ms=300, device="cpu",
    )
    out = episodic_merge.resolve_recall_outcome(decision, 640, 400, 2)
    assert (out.embed_ms, out.worker_wait_ms, out.retrieval_ms, out.device) == (11, 2, 300, "cpu")
