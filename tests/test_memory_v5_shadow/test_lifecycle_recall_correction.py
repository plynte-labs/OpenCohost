"""
Lifecycle + recall correction tests (synthetic data only, no real corpus).

Covers the single coherent correction:
  A. Lifecycle ownership / crash-startup recovery (MemorySubsystem.shutdown,
     EngineHost.stop facade routing, startup episode recovery).
  B. Retrieval correctness + natural opportunistic recall (NONE preflight,
     anchor-based corroboration).
  C. Deterministic query understanding (verb families, absolute dates, scopes).
  D. Retrieval scope (EPISODIC_TOPIC / SESSION_RECALL / PROFILE_SYNTHESIS).
  E. v4/v5 arbitration (+ OFF-zero preservation).

Strict-TDD: this file is written FIRST and must show RED before the fix.
Names that do not exist yet (RecallScope, startup_episode_recall,
recall_decision) are imported INSIDE the tests so the module still collects
pre-GREEN and each such test reports its own ERROR instead of killing the run.
"""
from __future__ import annotations

import queue
import sqlite3
import subprocess
import sys
import textwrap
from datetime import datetime, timezone
from pathlib import Path

import pytest

from opencohost.core.memory_v5_shadow.episodic_recall import (
    EpisodicRecallCoordinator,
    RecallMode,
)
from opencohost.core.memory_v5_shadow.evidence import CommittedTurnSnapshot
from opencohost.core.memory_v5_shadow.query_analyzer import RecallIntent
from opencohost.core.memory_v5_shadow.semantic_cache import (
    EpisodeEmbeddingRecord,
    ExchangeEmbeddingRecord,
    SemanticCacheStore,
)
from opencohost.core.memory_v5_shadow.semantic_indexer import (
    IncrementalSemanticIndexer,
)
from opencohost.core.memory_v5_shadow.subsystem import MemorySubsystem

REF = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Helpers (synthetic only)
# ---------------------------------------------------------------------------

_SHADOW_DDL = """
CREATE TABLE evidence_journal (
    event_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    stream_sequence INTEGER NOT NULL,
    role TEXT NOT NULL,
    source TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    content TEXT NOT NULL,
    is_private INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE TABLE sessions (
    session_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    opened_reason TEXT NOT NULL,
    closure_reason TEXT,
    event_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE episodes (
    episode_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    state TEXT NOT NULL,
    formation_policy_id TEXT NOT NULL,
    formation_policy_version TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    opened_reason TEXT NOT NULL,
    closure_reason TEXT,
    event_count INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE episode_membership (
    episode_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    sequence_index INTEGER NOT NULL,
    PRIMARY KEY (episode_id, event_id)
);
"""


class AxisWorker:
    """Deterministic keyword-routed embedding mock with call counting."""

    def __init__(self, routes=None, default=None):
        self.routes = dict(routes or {})
        self.default = list(default) if default is not None else [0.05] * 384
        self.embed_query_calls = 0
        self.shutdown_calls = 0

    def _vec(self, text):
        t = text.lower()
        for kw, vec in self.routes.items():
            if kw in t:
                return list(vec)
        return list(self.default)

    def embed_query(self, text, timeout_s=0.5):
        self.embed_query_calls += 1
        return self._vec(text)

    def embed_batch(self, texts, timeout_s=1.5):
        return [self._vec(t) for t in texts]

    def shutdown(self):
        self.shutdown_calls += 1


def _seed_full(conn, cache, profile, episode_id, session_id, started_at,
               user_text, asst_text, ex_vec, ep_vec, lex_tokens):
    """Insert one CLOSED episode (shadow rows) + manual cache rows."""
    conn.execute(
        "INSERT INTO episodes VALUES (?, ?, ?, 'CLOSED', 'p', 'v1', ?, ?, 'S', 'C', 2)",
        (episode_id, profile, session_id, started_at, started_at),
    )
    conn.execute(
        "INSERT INTO evidence_journal VALUES ('u_%s', ?, 'r1', 1, 'user', 'direct', ?, ?, 0, ?)" % episode_id,
        (profile, started_at, user_text, started_at),
    )
    conn.execute(
        "INSERT INTO evidence_journal VALUES ('a_%s', ?, 'r1', 2, 'assistant', 'direct', ?, ?, 0, ?)" % episode_id,
        (profile, started_at, asst_text, started_at),
    )
    conn.execute("INSERT INTO episode_membership VALUES (?, ?, 0)", (episode_id, "u_%s" % episode_id))
    conn.execute("INSERT INTO episode_membership VALUES (?, ?, 1)", (episode_id, "a_%s" % episode_id))
    conn.execute(
        "INSERT INTO sessions VALUES (?, 'r1', ?, 'CLOSED', ?, ?, 'STARTUP', 'SHUTDOWN', 2)",
        (session_id, profile, started_at, started_at),
    )
    conn.commit()
    cache.insert_exchange_embedding(
        ExchangeEmbeddingRecord("u_%s:a_%s" % (episode_id, episode_id), profile,
                                session_id, episode_id, "h", "m", "v1",
                                len(ex_vec), list(ex_vec), started_at, lex_tokens)
    )
    cache.insert_episode_embedding(
        EpisodeEmbeddingRecord(episode_id, profile, "m", "v1",
                               list(ep_vec), 0.9, 0.8, "fp", started_at)
    )


def _snap(pid, run, role, text, ts):
    return CommittedTurnSnapshot(
        committed_turn_id="", profile_id=pid, run_id=run, stream_sequence=0,
        role=role, source="direct", occurred_at=ts, content=text, is_private=False,
    )


# ---------------------------------------------------------------------------
# A. Lifecycle ownership and crash/startup recovery
# ---------------------------------------------------------------------------

class TestSubsystemShutdownOwnership:
    def test_shutdown_uses_real_apis_in_order_and_is_idempotent(self):
        calls = []

        class FakeRuntime:
            run_id = "fake-run"

            def shutdown(self, timeout=2.0):
                calls.append(("runtime", timeout))

        class FakeWorker:
            # NOTE: real SemanticWorkerService exposes shutdown(), not stop().
            def shutdown(self):
                calls.append(("worker",))

        class FakeCache:
            def close(self):
                calls.append(("cache",))

        sub = MemorySubsystem(mode="SHADOW", runtime=FakeRuntime(),
                              cache_store=FakeCache(), worker=FakeWorker())
        sub.shutdown(timeout_s=1.0)
        # Correct order: quiesce runtime first, then worker, then cache.
        assert calls == [("runtime", 1.0), ("worker",), ("cache",)]
        # Idempotent: second call is a safe no-op.
        sub.shutdown(timeout_s=1.0)
        assert calls == [("runtime", 1.0), ("worker",), ("cache",)]


class TestEngineHostStopFacade:
    def test_stop_routes_through_memory_facade(self):
        from opencohost.api.engine_host import EngineHost

        facade_calls = []

        class FakeFacade:
            def shutdown(self, timeout_s=2.0):
                facade_calls.append(timeout_s)

        def _tripwire(_self=None):
            raise AssertionError("legacy memory field must not be touched")

        class FakeMotor:
            _memory = FakeFacade()
            # Legacy fields are tripwires: any access fails loudly.
            _memory_runtime = property(_tripwire)
            _semantic_worker = property(_tripwire)
            _semantic_cache_store = property(_tripwire)
            is_processing = False
            current_model = None

            def flush_memorias(self):
                return None

            @property
            def command_queue(self):
                return queue.Queue()

        import tempfile
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            host = EngineHost(lock_path=str(Path(tmp) / "api.lock"))
            host.motor = FakeMotor()
            host.monitor = None
            host.aggregator = None
            host.stop()
        assert facade_calls == [1.0]


class TestCleanStopRestartRecovery:
    TS1 = "2026-09-02T10:00:00Z"
    TS2 = "2026-09-02T10:01:00Z"

    def _converse(self, sub, run, pid):
        sub.record_turn_exchange(
            _snap(pid, run, "user", "hablamos del puente y el faro del puerto", self.TS1),
            _snap(pid, run, "assistant", "el puente y el faro se ven desde el puerto", self.TS1),
        )
        sub.record_turn_exchange(
            _snap(pid, run, "user", "el faro alumbra el puente por la noche", self.TS2),
            _snap(pid, run, "assistant", "si, el faro alumbra el puente", self.TS2),
        )
        sub.runtime._queue.join()

    def test_converse_stop_restart_closed_indexed_retrievable(self, tmp_path):
        from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime

        pid = "prof_lifecycle"
        vec_p = [0.0, 0.0, 1.0] + [0.0] * 381
        worker = AxisWorker(routes={"puente": vec_p, "faro": vec_p})

        rt = MemoryRuntime(db_path=tmp_path / "shadow.db", queue_maxsize=100)
        cache = SemanticCacheStore(tmp_path / "cache.db")
        cache.initialize()
        sub = MemorySubsystem(mode="SHADOW", runtime=rt, cache_store=cache, worker=worker)
        self._converse(sub, rt.run_id, pid)
        sub.shutdown(timeout_s=2.0)

        # Prior session + episode must be CLOSED after a clean stop.
        con = sqlite3.connect(tmp_path / "shadow.db")
        states = {r[0] for r in con.execute("SELECT DISTINCT state FROM sessions").fetchall()}
        ep_states = {r[0] for r in con.execute("SELECT DISTINCT state FROM episodes").fetchall()}
        con.close()
        assert states == {"CLOSED"}, states
        assert ep_states == {"CLOSED"}, ep_states

        # Restart on the same DB: recovery keeps CLOSED, reconciliation indexes.
        # NEW-API (RED pre-GREEN): recovered via the subsystem facade.
        rt2 = MemoryRuntime(db_path=tmp_path / "shadow.db", queue_maxsize=100)
        worker2 = AxisWorker(routes={"puente": vec_p, "faro": vec_p})
        cache2 = SemanticCacheStore(tmp_path / "cache.db")
        cache2.initialize()
        recovery = MemorySubsystem.startup_episode_recovery(str(tmp_path / "shadow.db"))
        assert recovery["closed_episodes"] >= 1
        indexer = IncrementalSemanticIndexer(rt2._store._conn, cache2, worker2)
        assert indexer.reconcile_unindexed_episodes() >= 1

        coord = EpisodicRecallCoordinator(rt2._store._conn, cache2, worker2,
                                          mode=RecallMode.ACTIVE)
        packet = coord.process_query("hablamos sobre el puente o el faro?",
                                     profile_id=pid, reference_time=REF)
        assert packet is not None and len(packet.retrieved_episodes) == 1
        rt2.shutdown(timeout=2.0)
        cache2.close()

    def test_crash_restart_recovery_closes_orphan_open_state(self, tmp_path):
        from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime

        pid = "prof_crash"
        vec_p = [0.0, 0.0, 1.0] + [0.0] * 381
        rt1 = MemoryRuntime(db_path=tmp_path / "shadow.db", queue_maxsize=100)
        sub1 = MemorySubsystem(mode="SHADOW", runtime=rt1,
                               worker=AxisWorker(routes={"puente": vec_p}))
        self._converse(sub1, rt1.run_id, pid)
        # Simulate a crash: kill the worker thread WITHOUT the SHUTDOWN write.
        rt1._running = False
        rt1._worker.join(timeout=2.0)

        con = sqlite3.connect(tmp_path / "shadow.db")
        open_before = con.execute(
            "SELECT COUNT(1) FROM sessions WHERE state='OPEN'").fetchone()[0]
        con.close()
        assert open_before >= 1  # crash left OPEN state behind

        rt2 = MemoryRuntime(db_path=tmp_path / "shadow.db", queue_maxsize=100)
        recovery = MemorySubsystem.startup_episode_recovery(str(tmp_path / "shadow.db"))
        assert recovery["closed_episodes"] >= 1

        cache2 = SemanticCacheStore(tmp_path / "cache.db")
        cache2.initialize()
        worker2 = AxisWorker(routes={"puente": vec_p, "faro": vec_p})
        # A live CURRENT open episode must never be indexed, even when the
        # reconciler runs right over it.
        live_con = sqlite3.connect(tmp_path / "shadow.db")
        live_con.execute(
            "INSERT INTO episodes VALUES ('ep_live_open', ?, 's_live', 'OPEN', "
            "'deterministic-temporal', 'v1', '2026-09-03T11:00:00Z', NULL, "
            "'SESSION_START', NULL, 1)",
            (pid,),
        )
        live_con.commit()
        live_con.close()
        indexer = IncrementalSemanticIndexer(rt2._store._conn, cache2, worker2)
        assert indexer.reconcile_unindexed_episodes() >= 1
        assert cache2.get_episode_by_id("ep_live_open") is None
        coord = EpisodicRecallCoordinator(rt2._store._conn, cache2, worker2,
                                          mode=RecallMode.ACTIVE)
        packet = coord.process_query("hablamos sobre el puente o el faro?",
                                     profile_id=pid, reference_time=REF)
        assert packet is not None and len(packet.retrieved_episodes) == 1

        # Live/current OPEN episodes must never be indexed or recallable.
        con = sqlite3.connect(tmp_path / "shadow.db")
        live_open = con.execute(
            "SELECT episode_id FROM episodes WHERE state='OPEN'").fetchall()
        con.close()
        for (eid,) in live_open:
            assert cache2.get_episode_by_id(eid) is None
        rt2.shutdown(timeout=2.0)
        cache2.close()


# ---------------------------------------------------------------------------
# B. Retrieval correctness and natural opportunistic recall
# ---------------------------------------------------------------------------

class TestNoneOpportunisticRecall:
    def _world(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "shadow.db")
        conn.executescript(_SHADOW_DDL)
        cache = SemanticCacheStore(tmp_path / "cache.db")
        cache.initialize()
        vec_h = [0.0, 0.0, 1.0] + [0.0] * 381
        _seed_full(conn, cache, "p1", "ep_heap", "s1", "2026-09-02T10:00:00Z",
                   "el heap y el stack del servicio",
                   "heap y stack configurados en el servicio",
                   vec_h, vec_h, "heap servicio stack")
        conn.commit()
        return conn, cache, vec_h

    def test_none_with_anchor_overlap_proceeds_to_strict_recall(self, tmp_path):
        conn, cache, vec_h = self._world(tmp_path)
        worker = AxisWorker(routes={"heap": vec_h, "stack": vec_h})
        coord = EpisodicRecallCoordinator(conn, cache, worker, mode=RecallMode.ACTIVE)
        # No recall verb -> NONE intent, but strong historical anchors exist.
        packet = coord.process_query("el heap y el stack en produccion",
                                     profile_id="p1", reference_time=REF)
        assert packet is not None
        assert len(packet.retrieved_episodes) == 1
        assert packet.retrieved_episodes[0].episode_id == "ep_heap"
        conn.close()
        cache.close()

    def test_none_without_overlap_returns_no_recall_without_embedding(self, tmp_path):
        conn, cache, _ = self._world(tmp_path)
        worker = AxisWorker(routes={})
        coord = EpisodicRecallCoordinator(conn, cache, worker, mode=RecallMode.ACTIVE)
        packet = coord.process_query("¿Cuál es la capital de Francia?",
                                     profile_id="p1", reference_time=REF)
        assert packet is not None
        assert packet.retrieved_episodes == []
        assert packet.formatted_block == ""
        assert packet.reason_code == "NO_HISTORICAL_ANCHOR"
        assert worker.embed_query_calls == 0
        conn.close()
        cache.close()


class TestAnchorCorroboration:
    def test_stale_episode_with_only_generic_overlap_is_rejected(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "shadow.db")
        conn.executescript(_SHADOW_DDL)
        cache = SemanticCacheStore(tmp_path / "cache.db")
        cache.initialize()
        vec_a = [1.0] + [0.0] * 383
        vec_s = [0.9, 0.4359] + [0.0] * 382
        # Authoritative heap/stack episode (moderate cosine 0.9).
        _seed_full(conn, cache, "p1", "ep_heap", "s1", "2026-09-02T10:00:00Z",
                   "el heap y el stack del servicio",
                   "heap y stack configurados",
                   vec_a, vec_a, "heap servicio stack")
        # Stale episode: highest cosine (1.0) but shares ONLY generic tokens.
        _seed_full(conn, cache, "p1", "ep_stale", "s2", "2026-09-02T11:00:00Z",
                   "hablamos sobre ello en la reunion",
                   "si, lo comentamos en la reunion",
                   vec_s, vec_s, "comentamos hablamos reunion sobre")
        conn.commit()
        worker = AxisWorker(routes={}, default=vec_s)  # query vector == stale vector
        coord = EpisodicRecallCoordinator(conn, cache, worker, mode=RecallMode.ACTIVE)
        packet = coord.process_query("hablamos sobre el heap o stack en el sistema?",
                                     profile_id="p1", reference_time=REF)
        assert packet is not None
        # The wrong episode must not count as success: authoritative only.
        assert [e.episode_id for e in packet.retrieved_episodes] == ["ep_heap"]
        assert "ep_stale" not in packet.formatted_block
        conn.close()
        cache.close()


# ---------------------------------------------------------------------------
# C. Deterministic query understanding (NEW-API: RecallScope, absolute dates)
# ---------------------------------------------------------------------------

class TestQueryUnderstandingCorrection:
    def _analyze(self, text):
        from opencohost.core.memory_v5_shadow.query_analyzer import (
            EpisodicQueryAnalyzer,
        )
        return EpisodicQueryAnalyzer().analyze(text, profile_id="p1",
                                               reference_time=REF)

    def test_spanish_verb_families_are_explicit(self):
        from opencohost.core.memory_v5_shadow.query_analyzer import RecallScope
        cases = [
            "que opino de la IA segun mis memorias?",
            "te acuerdas lo que hablamos?",
            "hablamos sobre el heap o stack?",
            "hablabamos de aquel servicio?",
            "que discutimos la sesion pasada?",
            "que discutiamos ayer?",
            "platicamos sobre el plan?",
            "conversamos del error?",
            "charlamos de la migracion?",
            "tocamos el tema del despliegue?",
            "que opinamos de la IA y el test de turing?",
            "que pensabamos del diseno?",
            "que concluimos sobre el presupuesto?",
            "que dijimos del contrato?",
            "segun mis recuerdos, que decidimos?",
            "segun mis memorias, que paso?",
            "que tenemos registrado del incidente?",
            "de que hablamos la conversacion anterior?",
            "que vimos la ultima sesion?",
            "lo de la otra vez, que fue?",
        ]
        for text in cases:
            res = self._analyze(text)
            assert res.recall_intent == RecallIntent.EXPLICIT, text
            assert res.scope == RecallScope.EPISODIC_TOPIC or "sesion" in text or "conversacion" in text or "otra vez" in text, text

    def test_absolute_temporal_references(self):
        from opencohost.core.memory_v5_shadow.query_analyzer import (
            TemporalConstraintType,
        )
        res = self._analyze("las sesiones del 2 de septiembre que estudiamos?")
        assert res.temporal_constraint is not None
        assert res.temporal_constraint.constraint_type == TemporalConstraintType.ABSOLUTE_DAY
        assert res.temporal_constraint.matches("2026-09-02T10:00:00Z", reference_time=REF) is True
        assert res.temporal_constraint.matches("2026-09-03T10:00:00Z", reference_time=REF) is False

        res_m = self._analyze("que vimos en septiembre del modulo?")
        assert res_m.temporal_constraint is not None
        assert res_m.temporal_constraint.constraint_type == TemporalConstraintType.MONTH
        assert res_m.temporal_constraint.matches("2026-09-01T10:00:00Z", reference_time=REF) is True
        assert res_m.temporal_constraint.matches("2026-08-27T10:00:00Z", reference_time=REF) is False

    def test_session_past_scope(self):
        from opencohost.core.memory_v5_shadow.query_analyzer import RecallScope
        for text in ["que discutimos la sesion pasada?",
                     "de que se hablo la ultima sesion?",
                     "lo de la conversacion anterior?"]:
            res = self._analyze(text)
            assert res.scope == RecallScope.SESSION_RECALL, text
            assert res.recall_intent == RecallIntent.EXPLICIT, text

    def test_profile_synthesis_scope(self):
        from opencohost.core.memory_v5_shadow.query_analyzer import RecallScope
        for text in ["A que me dedico, cuales son mis hobbies?",
                     "que sabes de mi?",
                     "cuales son mis intereses?"]:
            res = self._analyze(text)
            assert res.scope == RecallScope.PROFILE_SYNTHESIS, text

    def test_general_knowledge_has_no_scope_recall(self):
        from opencohost.core.memory_v5_shadow.query_analyzer import RecallScope
        res = self._analyze("¿Cuál es la capital de Francia?")
        assert res.recall_intent == RecallIntent.NONE
        assert res.scope == RecallScope.EPISODIC_TOPIC


# ---------------------------------------------------------------------------
# D. Retrieval scope
# ---------------------------------------------------------------------------

class TestSessionRecallScope:
    def test_previous_compatible_closed_session_wins_over_semantics(self, tmp_path):
        from opencohost.core.memory_v5_shadow.query_analyzer import RecallScope
        conn = sqlite3.connect(tmp_path / "shadow.db")
        conn.executescript(_SHADOW_DDL)
        cache = SemanticCacheStore(tmp_path / "cache.db")
        cache.initialize()
        vec_old = [1.0] + [0.0] * 383
        vec_new = [0.7, 0.714] + [0.0] * 382
        _seed_full(conn, cache, "p1", "ep_old", "s_old", "2026-09-01T10:00:00Z",
                   "discutimos el plan del servicio ayer",
                   "si, discutimos el plan",
                   vec_old, vec_old, "ayer discutimos plan servicio")
        _seed_full(conn, cache, "p1", "ep_new", "s_new", "2026-09-02T10:00:00Z",
                   "discutimos el plan del servicio hoy",
                   "cerramos el plan",
                   vec_new, vec_new, "cerramos discutimos hoy plan")
        # A live OPEN session exists but must never supply recall.
        conn.execute(
            "INSERT INTO sessions VALUES ('s_live', 'r9', 'p1', 'OPEN', "
            "'2026-09-03T10:00:00Z', NULL, 'STARTUP', NULL, 0)")
        conn.commit()
        worker = AxisWorker(routes={"discutimos": vec_old})
        coord = EpisodicRecallCoordinator(conn, cache, worker, mode=RecallMode.ACTIVE)
        packet = coord.process_query("que discutimos la sesion pasada?",
                                     profile_id="p1", reference_time=REF)
        assert packet is not None
        assert packet.scope == RecallScope.SESSION_RECALL
        # Previous compatible CLOSED session is s_new, even though s_old
        # scores higher semantically.
        assert [e.episode_id for e in packet.retrieved_episodes] == ["ep_new"]
        assert packet.retrieved_episodes[0].session_id == "s_new"
        conn.close()
        cache.close()


class TestProfileSynthesisScope:
    def _world(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "shadow.db")
        conn.executescript(_SHADOW_DDL)
        cache = SemanticCacheStore(tmp_path / "cache.db")
        cache.initialize()
        q = [1.0] + [0.0] * 383
        e1 = [0.9, 0.4359] + [0.0] * 382
        e2 = [0.9, -0.4359] + [0.0] * 382
        e3 = [0.85, 0.5268] + [0.0] * 382
        _seed_full(conn, cache, "p1", "ep_work", "s1", "2026-09-01T10:00:00Z",
                   "me dedico a la enfermeria desde hace anos",
                   "la enfermeria es tu profesion",
                   e1, e1, "anos dedico enfermeria profesion")
        _seed_full(conn, cache, "p1", "ep_hobby", "s2", "2026-09-02T10:00:00Z",
                   "mis hobbies son el futbol y la cocina",
                   "futbol y cocina los fines de semana",
                   e2, e2, "cocina fines futbol hobbies semana")
        _seed_full(conn, cache, "p1", "ep_interest", "s3", "2026-09-02T11:00:00Z",
                   "me interesa la astronomia y los libros",
                   "astronomia y libros de divulgacion",
                   e3, e3, "astronomia divulgacion interesa libros")
        conn.commit()
        return conn, cache, q

    def test_profile_synthesis_returns_diverse_evidence_with_provenance(self, tmp_path):
        from opencohost.core.memory_v5_shadow.query_analyzer import RecallScope
        conn, cache, q = self._world(tmp_path)
        worker = AxisWorker(routes={"dedico": q, "hobbie": q, "interesa": q})
        coord = EpisodicRecallCoordinator(conn, cache, worker, mode=RecallMode.ACTIVE)
        packet = coord.process_query(
            "A que me dedico, cuales son mis hobbies y que me interesa?",
            profile_id="p1", reference_time=REF)
        assert packet is not None
        assert packet.scope == RecallScope.PROFILE_SYNTHESIS
        ids = [e.episode_id for e in packet.retrieved_episodes]
        assert len(ids) >= 2
        assert len(set(ids)) == len(ids)
        # Provenance explicit: short episode IDs travel in the block.
        for eid in ids:
            assert eid[:8] in packet.formatted_block
        conn.close()
        cache.close()

    def test_profile_synthesis_abstains_without_evidence(self, tmp_path):
        conn, cache, _ = self._world(tmp_path)
        worker = AxisWorker(routes={})
        coord = EpisodicRecallCoordinator(conn, cache, worker, mode=RecallMode.ACTIVE)
        packet = coord.process_query("cual es mi pelicula favorita?",
                                     profile_id="p1", reference_time=REF)
        assert packet is not None
        assert packet.retrieved_episodes == []
        assert packet.formatted_block == ""
        conn.close()
        cache.close()

    def test_turing_query_recalls_supported_evidence_only(self, tmp_path):
        conn = sqlite3.connect(tmp_path / "shadow.db")
        conn.executescript(_SHADOW_DDL)
        cache = SemanticCacheStore(tmp_path / "cache.db")
        cache.initialize()
        vec = [0.0, 0.0, 0.0, 1.0] + [0.0] * 380
        _seed_full(conn, cache, "p1", "ep_ia", "s1", "2026-09-02T10:00:00Z",
                   "opino que la IA ayuda en el trabajo",
                   "la IA como herramienta util",
                   vec, vec, "herramienta ia opino trabajo util")
        conn.commit()
        worker = AxisWorker(routes={"turing": vec, "opinamos": vec})
        coord = EpisodicRecallCoordinator(conn, cache, worker, mode=RecallMode.ACTIVE)
        packet = coord.process_query("que opinamos de la IA y el test de turing?",
                                     profile_id="p1", reference_time=REF)
        assert packet is not None
        assert [e.episode_id for e in packet.retrieved_episodes] == ["ep_ia"]
        conn.close()
        cache.close()


# ---------------------------------------------------------------------------
# E. v4/v5 arbitration
# ---------------------------------------------------------------------------

_V4_CANNED = ('<memorias_guardadas nota="canned">v4 line</memorias_guardadas>')


def _motor_with_v5(tmp_path, monkeypatch, query_routes, seed_fn):
    import queue as _q
    from unittest.mock import MagicMock
    import opencohost.core.llm_engine as llm_engine

    conn = sqlite3.connect(tmp_path / "shadow.db")
    conn.executescript(_SHADOW_DDL)
    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()
    worker = AxisWorker(routes=query_routes)
    seed_fn(conn, cache, worker)
    conn.commit()
    indexer = IncrementalSemanticIndexer(conn, cache, worker)
    indexer.reconcile_unindexed_episodes()
    coord = EpisodicRecallCoordinator(conn, cache, worker, mode=RecallMode.ACTIVE)

    motor = llm_engine.MotorVocalIA(_q.Queue(), lambda e: None)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor._current_profile_id = "p1"
    motor._memory = MemorySubsystem(mode="ACTIVE", coordinator=coord)
    calls = []
    canned = _V4_CANNED

    def fake_v4(profile_id, contexto):
        calls.append((profile_id, contexto))
        return canned

    monkeypatch.setattr(motor, "_build_memorias_injection_block", fake_v4)
    return motor, calls, conn, cache


def _seed_heap(conn, cache, worker):
    conn.execute(
        "INSERT INTO episodes VALUES ('ep_heap', 'p1', 's1', 'CLOSED', 'p', 'v1', "
        "'2026-09-02T10:00:00Z', '2026-09-02T10:00:00Z', 'S', 'C', 2)")
    conn.execute(
        "INSERT INTO evidence_journal VALUES ('uh', 'p1', 'r1', 1, 'user', 'direct', "
        "'2026-09-02T10:00:00Z', 'el heap y el stack del servicio', 0, '2026-09-02T10:00:00Z')")
    conn.execute(
        "INSERT INTO evidence_journal VALUES ('ah', 'p1', 'r1', 2, 'assistant', 'direct', "
        "'2026-09-02T10:00:00Z', 'heap y stack configurados', 0, '2026-09-02T10:00:00Z')")
    conn.execute("INSERT INTO episode_membership VALUES ('ep_heap', 'uh', 0)")
    conn.execute("INSERT INTO episode_membership VALUES ('ep_heap', 'ah', 1)")


class TestArbitration:
    def test_confident_v5_hit_suppresses_generic_v4(self, tmp_path, monkeypatch):
        vec_h = [0.0, 0.0, 1.0] + [0.0] * 381
        motor, calls, conn, cache = _motor_with_v5(
            tmp_path, monkeypatch, {"heap": vec_h, "stack": vec_h}, _seed_heap)
        setup = motor._build_generation_request(
            "hablamos sobre el heap o stack?", source="ptt", is_local=True,
            provider_cfg={}, request_model="m", watchdog_timeout=10.0)
        prompt = setup.messages[-1]["content"]
        assert "<episodic_memory>" in prompt
        # Suppression is proven by the builder never being called (the
        # literal marker also lives in the default system prompt text, so
        # marker absence cannot prove it).
        assert calls == []
        conn.close()
        cache.close()

    def test_v5_miss_keeps_v4_fallback(self, tmp_path, monkeypatch):
        """Guard: when v5 has no evidence, the v4 path still injects."""
        vec_h = [0.0, 0.0, 1.0] + [0.0] * 381
        motor, calls, conn, cache = _motor_with_v5(
            tmp_path, monkeypatch, {"heap": vec_h}, _seed_heap)
        setup = motor._build_generation_request(
            "¿Cuál es la capital de Francia?", source="ptt", is_local=True,
            provider_cfg={}, request_model="m", watchdog_timeout=10.0)
        prompt = setup.messages[-1]["content"]
        assert "<episodic_memory>" not in prompt
        assert len(calls) == 1
        conn.close()
        cache.close()

    def test_profile_synthesis_coexists_with_v4_section(self, tmp_path, monkeypatch):
        from opencohost.core.memory_v5_shadow.query_analyzer import RecallScope  # noqa
        q = [1.0] + [0.0] * 383
        e1 = [0.9, 0.4359] + [0.0] * 382

        def seed(conn, cache, worker):
            conn.execute(
                "INSERT INTO episodes VALUES ('ep_work', 'p1', 's1', 'CLOSED', 'p', 'v1', "
                "'2026-09-01T10:00:00Z', '2026-09-01T10:00:00Z', 'S', 'C', 2)")
            conn.execute(
                "INSERT INTO evidence_journal VALUES ('uw', 'p1', 'r1', 1, 'user', 'direct', "
                "'2026-09-01T10:00:00Z', 'me dedico a la enfermeria', 0, '2026-09-01T10:00:00Z')")
            conn.execute(
                "INSERT INTO evidence_journal VALUES ('aw', 'p1', 'r1', 2, 'assistant', 'direct', "
                "'2026-09-01T10:00:00Z', 'la enfermeria es tu profesion', 0, '2026-09-01T10:00:00Z')")
            conn.execute("INSERT INTO episode_membership VALUES ('ep_work', 'uw', 0)")
            conn.execute("INSERT INTO episode_membership VALUES ('ep_work', 'aw', 1)")

        motor, calls, conn, cache = _motor_with_v5(
            tmp_path, monkeypatch, {"dedico": q}, seed)
        # Force the indexed vectors onto the profile axis for determinism.
        cache.insert_exchange_embedding(
            ExchangeEmbeddingRecord("uw:aw", "p1", "s1", "ep_work", "h", "m", "v1",
                                    384, e1, "2026-09-01T10:00:00Z",
                                    "dedico enfermeria profesion"))
        cache.insert_episode_embedding(
            EpisodeEmbeddingRecord("ep_work", "p1", "m", "v1", e1, 0.9, 0.8,
                                   "fp", "2026-09-01T10:00:00Z"))
        setup = motor._build_generation_request(
            "A que me dedico?", source="ptt", is_local=True,
            provider_cfg={}, request_model="m", watchdog_timeout=10.0)
        prompt = setup.messages[-1]["content"]
        assert "<episodic_memory>" in prompt
        # PROFILE_SYNTHESIS coexists: v4 builder still runs (separate section).
        assert len(calls) == 1
        conn.close()
        cache.close()

    def test_off_zero_no_v5_side_effects(self):
        code = textwrap.dedent(
            """
            import os, sys, tempfile
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
                os.environ["OPENCOHOST_DATA_ROOT"] = tmp
                os.environ["OPENCOHOST_MEMORY_V5_MODE"] = "OFF"
                os.environ.pop("OPENCOHOST_MEMORY_V5_SHADOW_DB", None)
                os.environ.pop("OPENCOHOST_MEMORY_V5_SEMANTIC_CACHE_DB", None)
                import queue
                from unittest.mock import MagicMock
                from opencohost.core.llm_engine import MotorVocalIA
                m = MotorVocalIA(queue.Queue(), lambda s: None)
                assert m._memory is None
                assert m._memory_runtime is None
                assert m._memory_init_status["effective"] == "OFF"
                assert "opencohost.core.memory_v5_shadow.runtime" not in sys.modules
                assert "opencohost.core.memory_v5_shadow.subsystem" not in sys.modules
                import pathlib
                dbs = list(pathlib.Path(tmp).rglob("*.db")) + list(pathlib.Path(tmp).rglob("*.sqlite*"))
                assert dbs == [], dbs
                print("OFF_ZERO_OK")
            """
        )
        res = subprocess.run([sys.executable, "-c", code], capture_output=True,
                             text=True, timeout=60)
        assert res.returncode == 0, "OFF-zero subprocess failed:\n%s\n%s" % (res.stdout, res.stderr)
        assert "OFF_ZERO_OK" in res.stdout
