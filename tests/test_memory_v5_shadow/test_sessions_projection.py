from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import pytest

from opencohost.core.memory_v5_shadow.evidence import CommittedTurnSnapshot
from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime
from opencohost.core.memory_v5_shadow.sessions import (
    SessionFormationReducer,
    SessionRecord,
    compute_canonical_sessions_hash,
)


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


def test_session_id_deterministic_and_pure():
    run_id = "test_run_1"
    pid = "profile_alpha"
    seq = 10
    opened_reason = "STARTUP"
    sid1 = SessionFormationReducer.compute_session_id(run_id, pid, seq, opened_reason)
    sid2 = SessionFormationReducer.compute_session_id(run_id, pid, seq, opened_reason)
    assert sid1 == sid2
    assert len(sid1) == 24
    expected = hashlib.sha256(f"{run_id}:{pid}:{seq}:{opened_reason}".encode()).hexdigest()[:24]
    assert sid1 == expected


def test_startup_session_opened_on_first_evidence():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db, queue_maxsize=100)
        t0 = "2026-09-01T10:00:00Z"
        u = _snap(rt.run_id, 1, "p1", t0, "hi user", role="user")
        a = _snap(rt.run_id, 2, "p1", t0, "hi asst", role="assistant")
        rt.record_turn_exchange(u, a)
        rt.shutdown(timeout=2.0)

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        sessions = conn.execute("SELECT * FROM sessions ORDER BY started_at").fetchall()
        conn.close()

        assert len(sessions) == 1
        s = sessions[0]
        assert s["profile_id"] == "p1"
        assert s["opened_reason"] == "STARTUP"
        assert s["state"] == "CLOSED"
        assert s["closure_reason"] == "SHUTDOWN"
        assert s["event_count"] == 2


def test_hard_boundaries_profile_switch_and_shutdown():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db, queue_maxsize=100)
        t1 = "2026-09-01T10:00:00Z"
        t2 = "2026-09-01T10:05:00Z"
        # Turn in pA
        rt.record_turn_exchange(
            _snap(rt.run_id, 1, "pA", t1, "q1"),
            _snap(rt.run_id, 2, "pA", t1, "a1", role="assistant"),
        )
        # Switch pA -> pB
        rt.on_profile_switch("pA", "pB")
        # Turn in pB
        rt.record_turn_exchange(
            _snap(rt.run_id, 5, "pB", t2, "q2"),
            _snap(rt.run_id, 6, "pB", t2, "a2", role="assistant"),
        )
        rt.shutdown(timeout=2.0)

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        sessions = conn.execute("SELECT * FROM sessions ORDER BY started_at").fetchall()
        conn.close()

        assert len(sessions) == 2
        s1, s2 = sessions[0], sessions[1]
        assert s1["profile_id"] == "pA"
        assert s1["opened_reason"] == "STARTUP"
        assert s1["closure_reason"] == "PROFILE_SWITCH_OUT"
        assert s1["event_count"] == 2
        assert s1["state"] == "CLOSED"

        assert s2["profile_id"] == "pB"
        assert s2["opened_reason"] == "PROFILE_SWITCH_IN"
        assert s2["closure_reason"] == "SHUTDOWN"
        assert s2["event_count"] == 2
        assert s2["state"] == "CLOSED"


def test_soft_boundary_idle_gap_30min():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db, queue_maxsize=100)
        t1 = "2026-09-01T10:00:00Z"
        t2 = "2026-09-01T10:31:00Z"  # 31 min gap (> 30 min)

        rt.record_turn_exchange(
            _snap(rt.run_id, 1, "p1", t1, "morning q"),
            _snap(rt.run_id, 2, "p1", t1, "morning a", role="assistant"),
        )
        rt.record_turn_exchange(
            _snap(rt.run_id, 3, "p1", t2, "later q"),
            _snap(rt.run_id, 4, "p1", t2, "later a", role="assistant"),
        )
        rt.shutdown(timeout=2.0)

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        sessions = conn.execute("SELECT * FROM sessions ORDER BY started_at").fetchall()
        conn.close()

        assert len(sessions) == 2
        s1, s2 = sessions[0], sessions[1]
        assert s1["closure_reason"] == "IDLE_GAP"
        assert s1["event_count"] == 2
        assert s2["opened_reason"] == "IDLE_GAP"
        assert s2["closure_reason"] == "SHUTDOWN"
        assert s2["event_count"] == 2


def test_soft_boundaries_max_turns_and_max_duration():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db, queue_maxsize=2000)
        t_base = datetime(2026, 9, 1, 10, 0, 0, tzinfo=timezone.utc)

        # 250 exchanges = 500 evidence records
        for i in range(250):
            t_str = (t_base + timedelta(seconds=i)).isoformat().replace("+00:00", "Z")
            rt.record_turn_exchange(
                _snap(rt.run_id, 0, "p1", t_str, f"u {i}"),
                _snap(rt.run_id, 0, "p1", t_str, f"a {i}", role="assistant"),
            )
        # 1 additional exchange = 2 evidence records -> triggers rollover to session 2
        t_extra = (t_base + timedelta(seconds=251)).isoformat().replace("+00:00", "Z")
        rt.record_turn_exchange(
            _snap(rt.run_id, 0, "p1", t_extra, "u extra"),
            _snap(rt.run_id, 0, "p1", t_extra, "a extra", role="assistant"),
        )
        rt.shutdown(timeout=3.0)

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        sessions = conn.execute("SELECT * FROM sessions ORDER BY started_at").fetchall()
        conn.close()

        assert len(sessions) == 2
        assert sessions[0]["event_count"] == 500
        assert sessions[0]["closure_reason"] == "MAX_TURNS"
        assert sessions[1]["event_count"] == 2
        assert sessions[1]["opened_reason"] == "IDLE_GAP"


def test_crash_recovery_cross_profile_ownership():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt1 = MemoryRuntime(db_path=db, queue_maxsize=100)
        t0 = "2026-09-01T10:00:00Z"
        rt1.record_turn_exchange(
            _snap(rt1.run_id, 1, "profile_orphan", t0, "unclosed turn"),
            _snap(rt1.run_id, 2, "profile_orphan", t0, "unclosed turn ans", role="assistant"),
        )
        deadline = time.time() + 2
        while time.time() < deadline and rt1._queue.qsize() > 0:
            time.sleep(0.02)
        time.sleep(0.2)
        rt1._running = False
        rt1._store.close()

        rt2 = MemoryRuntime(db_path=db, queue_maxsize=100)
        rt2.shutdown(timeout=2.0)

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        recovered_sessions = conn.execute(
            "SELECT * FROM sessions WHERE profile_id='profile_orphan'"
        ).fetchall()
        recovery_events = conn.execute(
            "SELECT * FROM lifecycle_events WHERE kind='STARTUP_RECOVERY'"
        ).fetchall()
        conn.close()

        assert len(recovered_sessions) == 1
        s = recovered_sessions[0]
        assert s["state"] == "CLOSED"
        assert s["closure_reason"] == "CRASH_RECOVERY_CLOSED"
        assert s["ended_at"] == t0

        assert len(recovery_events) == 1
        rec = recovery_events[0]
        assert rec["owner_profile_id"] == "profile_orphan"


def test_deterministic_rebuild_byte_identical_hash():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db, queue_maxsize=100)
        t_base = datetime(2026, 9, 1, 12, 0, 0, tzinfo=timezone.utc)

        for i in range(10):
            t_str = (t_base + timedelta(minutes=i * 5)).isoformat().replace("+00:00", "Z")
            rt.record_turn_exchange(
                _snap(rt.run_id, 0, "prof_a", t_str, f"q {i}"),
                _snap(rt.run_id, 0, "prof_a", t_str, f"a {i}", role="assistant"),
            )
        rt.on_profile_switch("prof_a", "prof_b")
        for i in range(5):
            t_str = (t_base + timedelta(minutes=60 + i * 5)).isoformat().replace("+00:00", "Z")
            rt.record_turn_exchange(
                _snap(rt.run_id, 0, "prof_b", t_str, f"b_q {i}"),
                _snap(rt.run_id, 0, "prof_b", t_str, f"b_a {i}", role="assistant"),
            )
        rt.shutdown(timeout=2.0)

        # 1. Capture live sessions hash
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        live_rows = conn.execute("SELECT * FROM sessions ORDER BY started_at, session_id").fetchall()
        live_sessions = [dict(r) for r in live_rows]
        live_hash = compute_canonical_sessions_hash(live_sessions)
        assert len(live_sessions) >= 2

        # 2. Execute rebuild using the pure reducer
        reducer = SessionFormationReducer()
        rebuilt_sessions = reducer.rebuild_from_db(db)
        rebuilt_hash = compute_canonical_sessions_hash(rebuilt_sessions)
        conn.close()

        # Check parity
        assert live_hash == rebuilt_hash, f"Hash mismatch: live={live_hash} vs rebuilt={rebuilt_hash}"


def test_retention_purge_rebuilds_affected_sessions():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db, queue_maxsize=100)
        t0 = "2026-09-01T10:00:00Z"
        rt.record_turn_exchange(
            _snap(rt.run_id, 0, "p1", t0, "q1"),
            _snap(rt.run_id, 0, "p1", t0, "a1", role="assistant"),
        )
        rt.record_turn_exchange(
            _snap(rt.run_id, 0, "p1", t0, "q2"),
            _snap(rt.run_id, 0, "p1", t0, "a2", role="assistant"),
        )
        rt.shutdown(timeout=2.0)

        # Purge first turn from evidence
        conn = sqlite3.connect(str(db))
        conn.execute("DELETE FROM evidence_journal WHERE stream_sequence=1")
        conn.commit()
        conn.close()

        # Rebuild after retention purge
        reducer = SessionFormationReducer()
        rebuilt = reducer.rebuild_from_db(db)
        assert rebuilt[0]["event_count"] == 3


def test_startup_reconciles_sessions_after_crash_between_journal_and_upsert():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt1 = MemoryRuntime(db_path=db, queue_maxsize=100)
        t0 = "2026-09-01T10:00:00Z"
        # Turn inserted
        rt1.record_turn_exchange(
            _snap(rt1.run_id, 1, "p_recon", t0, "turn 1"),
            _snap(rt1.run_id, 2, "p_recon", t0, "turn 1 asst", role="assistant"),
        )
        time.sleep(0.3)
        rt1.shutdown(timeout=2.0)

        # Simulate crash that wiped sessions table while keeping evidence journal
        conn = sqlite3.connect(str(db))
        conn.execute("DELETE FROM sessions")
        conn.commit()
        conn.close()

        # Next runtime startup must reconcile sessions from authoritative journals
        rt2 = MemoryRuntime(db_path=db, queue_maxsize=100)
        rt2.shutdown(timeout=2.0)

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        sessions = conn.execute("SELECT * FROM sessions WHERE profile_id='p_recon'").fetchall()
        conn.close()

        assert len(sessions) == 1
        assert sessions[0]["profile_id"] == "p_recon"
        assert sessions[0]["event_count"] == 2
