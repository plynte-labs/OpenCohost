from __future__ import annotations

import math
from pathlib import Path
import pytest
import numpy as np

from opencohost.core.memory_v5_shadow.semantic import (
    EpisodeCohesionDiagnostics,
    EpisodeSemanticRepresentation,
    EpisodeSimilarityNeighbor,
    EpisodeSimilarityReport,
    build_episode_semantic_representation,
    compute_episode_similarities,
)


def test_build_episode_semantic_representation_centroid_and_cohesion():
    # Two orthogonal 3-D vectors: [1, 0, 0] and [0, 1, 0]
    v1 = [1.0, 0.0, 0.0]
    v2 = [0.0, 1.0, 0.0]
    
    rep = build_episode_semantic_representation(
        episode_id="ep_01",
        session_id="sess_01",
        profile_id="prof_01",
        exchange_vectors=[v1, v2],
    )
    assert rep is not None
    assert rep.episode_id == "ep_01"
    assert rep.session_id == "sess_01"
    assert rep.exchange_count == 2

    # Centroid: [0.5, 0.5, 0] -> normalized: [1/sqrt(2), 1/sqrt(2), 0]
    expected_val = 1.0 / math.sqrt(2.0)
    assert math.isclose(rep.vector[0], expected_val, abs_tol=1e-4)
    assert math.isclose(rep.vector[1], expected_val, abs_tol=1e-4)
    assert math.isclose(rep.vector[2], 0.0, abs_tol=1e-4)

    # Norm must be exactly 1.0
    norm = math.sqrt(sum(x * x for x in rep.vector))
    assert math.isclose(norm, 1.0, abs_tol=1e-5)

    # Cohesion: each vector dot centroid = 1/sqrt(2) ≈ 0.7071
    assert math.isclose(rep.cohesion.mean_exchange_to_centroid, expected_val, abs_tol=1e-4)
    assert math.isclose(rep.cohesion.min_exchange_to_centroid, expected_val, abs_tol=1e-4)
    assert rep.cohesion.exchange_count == 2


def test_compute_episode_similarities_cross_session_filtering():
    # 3 episodes
    # Ep 1 (Session A): [1, 0, 0]
    # Ep 2 (Session A, same session): [0.8, 0.6, 0] -> cosine with Ep 1 = 0.8
    # Ep 3 (Session B, cross session): [0.6, 0.8, 0] -> cosine with Ep 1 = 0.6
    
    rep1 = EpisodeSemanticRepresentation(
        episode_id="ep_1",
        session_id="sess_A",
        profile_id="p1",
        vector=[1.0, 0.0, 0.0],
        cohesion=EpisodeCohesionDiagnostics(1.0, 1.0, 1),
        exchange_count=1,
    )
    rep2 = EpisodeSemanticRepresentation(
        episode_id="ep_2",
        session_id="sess_A",
        profile_id="p1",
        vector=[0.8, 0.6, 0.0],
        cohesion=EpisodeCohesionDiagnostics(1.0, 1.0, 1),
        exchange_count=1,
    )
    rep3 = EpisodeSemanticRepresentation(
        episode_id="ep_3",
        session_id="sess_B",
        profile_id="p1",
        vector=[0.6, 0.8, 0.0],
        cohesion=EpisodeCohesionDiagnostics(1.0, 1.0, 1),
        exchange_count=1,
    )

    timestamps = {
        "ep_1": "2026-09-02T10:00:00Z",
        "ep_2": "2026-09-02T10:30:00Z",
        "ep_3": "2026-09-02T12:00:00Z",
    }

    reports = compute_episode_similarities([rep1, rep2, rep3], episode_timestamps=timestamps)
    assert len(reports) == 3

    r1 = [r for r in reports if r.episode_id == "ep_1"][0]
    
    # Nearest overall: Ep 2 (sim 0.8) then Ep 3 (sim 0.6)
    assert len(r1.nearest_overall) == 2
    assert r1.nearest_overall[0].neighbor_episode_id == "ep_2"
    assert math.isclose(r1.nearest_overall[0].cosine_similarity, 0.8, abs_tol=1e-3)
    assert r1.nearest_overall[0].is_same_session is True
    assert r1.nearest_overall[0].time_delta_seconds == 1800.0

    assert r1.nearest_overall[1].neighbor_episode_id == "ep_3"
    assert math.isclose(r1.nearest_overall[1].cosine_similarity, 0.6, abs_tol=1e-3)
    assert r1.nearest_overall[1].is_same_session is False
    assert r1.nearest_overall[1].time_delta_seconds == 7200.0

    # Nearest cross-session: Ep 2 MUST BE EXCLUDED! Only Ep 3 can be present
    assert len(r1.nearest_cross_session) == 1
    assert r1.nearest_cross_session[0].neighbor_episode_id == "ep_3"
    assert r1.nearest_cross_session[0].is_same_session is False


def test_unique_exchange_vector_reuse():
    from opencohost.core.memory_v5_shadow.semantic import (
        ConversationalExchange,
        batch_embed_unique_exchanges,
    )

    class CountingBackend:
        def __init__(self):
            self.embed_calls = 0
            self.embedded_texts: list[str] = []

        def embed_batch(self, texts):
            self.embed_calls += 1
            self.embedded_texts.extend(texts)
            return [[1.0, 0.0] for _ in texts]

    ex1 = ConversationalExchange(1, "s1", "u1", "a1", "u1", "a1", "duplicate content", "t1")
    ex2 = ConversationalExchange(2, "s1", "u2", "a2", "u2", "a2", "unique content", "t2")
    ex3 = ConversationalExchange(1, "s2", "u3", "a3", "u3", "a3", "duplicate content", "t3")

    backend = CountingBackend()
    vectors = batch_embed_unique_exchanges([ex1, ex2, ex3], backend=backend)

    # Only 2 unique texts should be embedded
    assert len(backend.embedded_texts) == 2
    assert backend.embed_calls == 1
    assert "duplicate content" in backend.embedded_texts
    assert "unique content" in backend.embedded_texts

    # Lookup mapping has all 3 exchange keys
    k1 = f"{ex1.user_event_id}:{ex1.assistant_event_id}"
    k3 = f"{ex3.user_event_id}:{ex3.assistant_event_id}"
    assert k1 in vectors
    assert k3 in vectors
    assert vectors[k1] == vectors[k3]


def test_load_episode_evidence_strict_membership_order():
    import sqlite3
    from opencohost.core.memory_v5_shadow.semantic import load_episode_evidence_events

    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row

    # Create tables
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

    # Insert evidence out of chronological order
    conn.execute(
        "INSERT INTO evidence_journal VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        ("ev1", "p1", "r1", 1, "user", "PTT", "2026-09-02T10:00:00Z", "first text", "2026-09-02T10:00:00Z"),
    )
    conn.execute(
        "INSERT INTO evidence_journal VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        ("ev2", "p1", "r1", 2, "assistant", "PTT", "2026-09-02T10:01:00Z", "second text", "2026-09-02T10:01:00Z"),
    )
    conn.execute(
        "INSERT INTO evidence_journal VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        ("ev3", "p1", "r1", 3, "user", "PTT", "2026-09-02T10:02:00Z", "third text", "2026-09-02T10:02:00Z"),
    )

    # Insert episode
    conn.execute(
        "INSERT INTO episodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("ep_target", "p1", "s1", "CLOSED", "policy_v1", "v1", "2026-09-02T10:00:00Z", "2026-09-02T10:02:00Z", "SESSION_START", "SESSION_CLOSED", 2),
    )

    # Insert memberships: sequence 0 is ev3 (chronologically latest), sequence 1 is ev1 (earliest). ev2 omitted.
    conn.execute("INSERT INTO episode_membership VALUES (?, ?, ?)", ("ep_target", "ev3", 0))
    conn.execute("INSERT INTO episode_membership VALUES (?, ?, ?)", ("ep_target", "ev1", 1))

    loaded = load_episode_evidence_events(conn, "ep_target")
    assert len(loaded) == 2
    # Must strictly match membership sequence_index, NOT chronological stream order
    assert loaded[0]["event_id"] == "ev3"
    assert loaded[0]["content"] == "third text"
    assert loaded[1]["event_id"] == "ev1"
    assert loaded[1]["content"] == "first text"
    # ev2 must be absent
    assert "ev2" not in [x["event_id"] for x in loaded]
    conn.close()

