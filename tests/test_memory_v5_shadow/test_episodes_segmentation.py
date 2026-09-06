from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path

import pytest

from opencohost.core.memory_v5_shadow.evidence import CommittedTurnSnapshot
from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime
from opencohost.core.memory_v5_shadow.episodes import (
    EpisodeSegmentationEngine,
    EpisodeRecord,
    compute_canonical_episodes_hash,
)
from tools.memory_v5_shadow.evaluate_episodes import compute_consolidation_metrics


def _snap(
    run_id: str,
    seq: int,
    profile_id: str = "p1",
    occurred_at: str = "2026-09-01T10:00:00Z",
    content: str = "hello",
    role: str = "user",
) -> CommittedTurnSnapshot:
    return CommittedTurnSnapshot(
        committed_turn_id=f"{run_id}:{seq}",
        profile_id=profile_id,
        run_id=run_id,
        stream_sequence=seq,
        role=role,
        source="direct",
        occurred_at=occurred_at,
        content=content,
        is_private=False,
    )


def test_episode_id_deterministic_and_pure():
    session_id = "sess_12345"
    start_event_id = "ev_001"
    policy_id = "deterministic-temporal"
    policy_version = "v1"

    eid1 = EpisodeSegmentationEngine.compute_episode_id(
        session_id, start_event_id, policy_id, policy_version
    )
    eid2 = EpisodeSegmentationEngine.compute_episode_id(
        session_id, start_event_id, policy_id, policy_version
    )
    assert eid1 == eid2
    assert len(eid1) == 24
    expected = hashlib.sha256(
        f"{session_id}:{start_event_id}:{policy_id}:{policy_version}".encode()
    ).hexdigest()[:24]
    assert eid1 == expected


def test_session_boundary_creates_episode_and_memberships():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db, queue_maxsize=100)
        t1 = "2026-09-01T10:00:00Z"
        t2 = "2026-09-01T10:01:00Z"

        # Exchange 1
        rt.record_turn_exchange(
            _snap(rt.run_id, 1, "p1", t1, "user turn 1"),
            _snap(rt.run_id, 2, "p1", t1, "assistant turn 1", role="assistant"),
        )
        # Exchange 2
        rt.record_turn_exchange(
            _snap(rt.run_id, 3, "p1", t2, "user turn 2"),
            _snap(rt.run_id, 4, "p1", t2, "assistant turn 2", role="assistant"),
        )
        rt.shutdown(timeout=2.0)

        # Run segmentation
        engine = EpisodeSegmentationEngine()
        episodes, memberships = engine.segment_all_from_db(db)

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        ep_rows = conn.execute("SELECT * FROM episodes ORDER BY started_at").fetchall()
        mem_rows = conn.execute("SELECT * FROM episode_membership ORDER BY sequence_index").fetchall()
        conn.close()

        assert len(ep_rows) == 1
        ep = ep_rows[0]
        assert ep["event_count"] == 4
        assert ep["closure_reason"] == "SESSION_CLOSED"
        assert ep["opened_reason"] == "SESSION_START"
        assert len(mem_rows) == 4
        for i, m in enumerate(mem_rows):
            assert m["sequence_index"] == i


def test_idle_gap_and_max_window_boundaries():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db, queue_maxsize=100)
        t_base = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)

        # 4 turns (2 exchanges)
        for i in range(2):
            t_str = (t_base + timedelta(seconds=i * 30)).isoformat().replace("+00:00", "Z")
            rt.record_turn_exchange(
                _snap(rt.run_id, 0, "p1", t_str, f"q {i}"),
                _snap(rt.run_id, 0, "p1", t_str, f"a {i}", role="assistant"),
            )

        # Intra-session idle gap of 15 min (900s >= 600s episode idle gap, but < 1800s session gap)
        t_gap = (t_base + timedelta(minutes=15)).isoformat().replace("+00:00", "Z")
        rt.record_turn_exchange(
            _snap(rt.run_id, 0, "p1", t_gap, "post gap q"),
            _snap(rt.run_id, 0, "p1", t_gap, "post gap a", role="assistant"),
        )
        rt.shutdown(timeout=2.0)

        engine = EpisodeSegmentationEngine()
        episodes, memberships = engine.segment_all_from_db(db)

        assert len(episodes) == 2
        assert episodes[0]["event_count"] == 4
        assert episodes[0]["closure_reason"] == "IDLE_GAP_EPISODE"
        assert episodes[1]["event_count"] == 2
        assert episodes[1]["opened_reason"] == "IDLE_GAP_EPISODE"
        assert episodes[1]["closure_reason"] == "SESSION_CLOSED"


def test_deterministic_episodes_rebuild_hash_parity():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db, queue_maxsize=100)
        t_base = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)

        for i in range(12):
            t_str = (t_base + timedelta(minutes=i * 2)).isoformat().replace("+00:00", "Z")
            rt.record_turn_exchange(
                _snap(rt.run_id, 0, "prof_x", t_str, f"topic q {i}"),
                _snap(rt.run_id, 0, "prof_x", t_str, f"topic a {i}", role="assistant"),
            )
        rt.shutdown(timeout=2.0)

        engine = EpisodeSegmentationEngine()
        live_eps, _ = engine.segment_all_from_db(db)
        live_hash = compute_canonical_episodes_hash(live_eps)

        # Clear episodes in DB
        conn = sqlite3.connect(str(db))
        conn.execute("DELETE FROM episodes")
        conn.commit()
        conn.close()

        # Rebuild from DB journals
        rebuilt_eps, _ = engine.segment_all_from_db(db)
        rebuilt_hash = compute_canonical_episodes_hash(rebuilt_eps)

        assert live_hash == rebuilt_hash


def test_consolidation_pressure_metrics_evaluation():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db, queue_maxsize=100)
        t_base = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)

        # Repeated content turns across multiple segmented windows
        for i in range(6):
            t_str = (t_base + timedelta(minutes=i * 12)).isoformat().replace("+00:00", "Z")
            rt.record_turn_exchange(
                _snap(rt.run_id, 0, "p1", t_str, "Hablamos del Cityray precio y motor"),
                _snap(rt.run_id, 0, "p1", t_str, "El Cityray cuesta 450k con motor turbo", role="assistant"),
            )
        rt.shutdown(timeout=2.0)

        engine = EpisodeSegmentationEngine()
        engine.segment_all_from_db(db)

        metrics = compute_consolidation_metrics(db)
        assert metrics["total_episodes"] >= 1
        assert "exact_content_repetition_rate" in metrics
        assert metrics["human_evaluation_status"] == "PENDING_REAL_CORPUS"
        assert metrics["consolidation_pressure"] == "INSUFFICIENT_EVIDENCE"
