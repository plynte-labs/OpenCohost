from __future__ import annotations

import sqlite3
from pathlib import Path
import pytest

from opencohost.core.memory_v5_shadow.semantic_cache import SemanticCacheStore
from opencohost.core.memory_v5_shadow.semantic_indexer import (
    IncrementalSemanticIndexer,
)
from opencohost.core.memory_v5_shadow.semantic_worker import SemanticWorkerService


def test_incremental_semantic_indexer_indexes_episode_and_exchanges(tmp_path: Path):
    shadow_db = tmp_path / "shadow.db"
    cache_db = tmp_path / "cache.db"

    # Setup dummy shadow DB with 1 closed episode and 2 exchanges
    conn = sqlite3.connect(shadow_db)
    conn.executescript("""
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
    """)

    # Evidence: 2 exchanges (4 events)
    events = [
        ("ev1", "prof_1", "run1", 1, "user", "PTT", "2026-09-02T10:00:00Z", "¿Qué opinas de los audífonos?", "2026-09-02T10:00:00Z"),
        ("ev2", "prof_1", "run1", 2, "assistant", "PTT", "2026-09-02T10:00:05Z", "Los Sonos Ace tienen buen diseño pero poco grave.", "2026-09-02T10:00:05Z"),
        ("ev3", "prof_1", "run1", 3, "user", "PTT", "2026-09-02T10:02:00Z", "¿Y el precio?", "2026-09-02T10:02:00Z"),
        ("ev4", "prof_1", "run1", 4, "assistant", "PTT", "2026-09-02T10:02:05Z", "Cuestan alrededor de 450 dólares.", "2026-09-02T10:02:05Z"),
    ]
    for ev in events:
        conn.execute("INSERT INTO evidence_journal VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)", ev)

    conn.execute(
        "INSERT INTO episodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("ep_100", "prof_1", "sess_100", "CLOSED", "policy_v1", "v1", "2026-09-02T10:00:00Z", "2026-09-02T10:02:05Z", "SESSION_START", "SESSION_CLOSED", 4),
    )

    for idx, ev_id in enumerate(["ev1", "ev2", "ev3", "ev4"]):
        conn.execute("INSERT INTO episode_membership VALUES (?, ?, ?)", ("ep_100", ev_id, idx))
    conn.commit()

    # Indexer
    cache_store = SemanticCacheStore(cache_db)
    cache_store.initialize()
    worker = SemanticWorkerService(use_dummy=True)
    worker.start()

    indexer = IncrementalSemanticIndexer(shadow_conn=conn, cache_store=cache_store, worker=worker)
    res = indexer.index_episode("ep_100")
    assert res is True

    # Verify cache
    ex_records = cache_store.get_exchange_embeddings_by_profile("prof_1")
    assert len(ex_records) == 2
    assert ex_records[0].episode_id == "ep_100"
    assert ex_records[1].episode_id == "ep_100"

    ep_records = cache_store.get_episode_embeddings_by_profile("prof_1")
    assert len(ep_records) == 1
    assert ep_records[0].episode_id == "ep_100"
    assert ep_records[0].cohesion_mean > 0.0

    worker.shutdown()
    conn.close()
    cache_store.close()


def test_reconcile_unindexed_episodes(tmp_path: Path):
    shadow_db = tmp_path / "shadow.db"
    cache_db = tmp_path / "cache.db"

    conn = sqlite3.connect(shadow_db)
    conn.executescript("""
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
    """)

    # 1 closed episode with 1 exchange
    conn.execute("INSERT INTO evidence_journal VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)", ("e1", "p1", "r1", 1, "user", "PTT", "2026-09-02T10:00:00Z", "hola", "2026-09-02T10:00:00Z"))
    conn.execute("INSERT INTO evidence_journal VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)", ("e2", "p1", "r1", 2, "assistant", "PTT", "2026-09-02T10:00:01Z", "buenas", "2026-09-02T10:00:01Z"))
    conn.execute("INSERT INTO episodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", ("ep_unindexed", "p1", "s1", "CLOSED", "policy_v1", "v1", "2026-09-02T10:00:00Z", "2026-09-02T10:00:01Z", "SESSION_START", "SESSION_CLOSED", 2))
    conn.execute("INSERT INTO episode_membership VALUES (?, ?, ?)", ("ep_unindexed", "e1", 0))
    conn.execute("INSERT INTO episode_membership VALUES (?, ?, ?)", ("ep_unindexed", "e2", 1))
    conn.commit()

    cache_store = SemanticCacheStore(cache_db)
    cache_store.initialize()
    worker = SemanticWorkerService(use_dummy=True)
    worker.start()

    indexer = IncrementalSemanticIndexer(shadow_conn=conn, cache_store=cache_store, worker=worker)
    reconciled_count = indexer.reconcile_unindexed_episodes()
    assert reconciled_count == 1

    # Second run finds 0 unindexed
    assert indexer.reconcile_unindexed_episodes() == 0

    worker.shutdown()
    conn.close()
    cache_store.close()


def test_indexer_reconciles_multi_profile_corpus_smoke(tmp_path: Path):
    """Reconcile a synthetic multi-profile corpus (hermetic).

    Formerly read the owner's local ``francisco-trial.db`` under ``.artifacts``,
    which drifts and holds several profiles, so the per-profile count never
    matched the corpus-wide CLOSED count. This builds an equivalent corpus:
    two profiles, several CLOSED episodes with multiple exchanges each, and one
    OPEN episode that must stay unindexed.
    """
    conn = sqlite3.connect(tmp_path / "shadow.db")
    conn.executescript("""
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
    """)

    profile_id = "prof_main"
    other_profile = "prof_other"
    # (episode_id, profile, state): 3 closed + 1 open for the main profile,
    # 2 closed for another profile.
    episodes = [
        ("ep_a1", profile_id, "CLOSED"),
        ("ep_a2", profile_id, "CLOSED"),
        ("ep_a3", profile_id, "CLOSED"),
        ("ep_a4", profile_id, "OPEN"),
        ("ep_b1", other_profile, "CLOSED"),
        ("ep_b2", other_profile, "CLOSED"),
    ]
    seq = 0
    for ep_id, prof, state in episodes:
        for exchange in range(3):
            for role, text in (("user", f"pregunta {ep_id} {exchange}"),
                               ("assistant", f"respuesta {ep_id} {exchange}")):
                seq += 1
                event_id = f"ev{seq}"
                stamp = f"2026-09-02T10:{seq // 60:02d}:{seq % 60:02d}Z"
                conn.execute(
                    "INSERT INTO evidence_journal VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
                    (event_id, prof, "run1", seq, role, "PTT", stamp, text, stamp),
                )
                conn.execute(
                    "INSERT INTO episode_membership VALUES (?, ?, ?)",
                    (ep_id, event_id, seq),
                )
        conn.execute(
            "INSERT INTO episodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ep_id, prof, f"sess_{ep_id}", state, "policy_v1", "v1",
             "2026-09-02T10:00:00Z", None if state == "OPEN" else "2026-09-02T10:59:00Z",
             "SESSION_START", None if state == "OPEN" else "SESSION_CLOSED", 6),
        )
    conn.commit()

    cache_store = SemanticCacheStore(tmp_path / "trial_cache.db")
    cache_store.initialize()
    worker = SemanticWorkerService(use_dummy=True)
    worker.start()

    try:
        indexer = IncrementalSemanticIndexer(shadow_conn=conn, cache_store=cache_store, worker=worker)
        count = indexer.reconcile_unindexed_episodes()
        expected_closed = conn.execute("SELECT count(1) FROM episodes WHERE state='CLOSED'").fetchone()[0]
        assert expected_closed == 5
        assert count == expected_closed

        main_eps = cache_store.get_episode_embeddings_by_profile(profile_id)
        main_exs = cache_store.get_exchange_embeddings_by_profile(profile_id)
        other_eps = cache_store.get_episode_embeddings_by_profile(other_profile)
        assert {r.episode_id for r in main_eps} == {"ep_a1", "ep_a2", "ep_a3"}
        assert len(other_eps) == 2
        assert len(main_exs) == 9  # 3 closed episodes x 3 exchanges; the OPEN one is skipped

        # Idempotent: a second pass finds nothing left to index.
        assert indexer.reconcile_unindexed_episodes() == 0
    finally:
        worker.shutdown()
        conn.close()
        cache_store.close()
