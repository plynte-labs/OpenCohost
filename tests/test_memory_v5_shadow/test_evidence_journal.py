import hashlib
import tempfile
import time
from pathlib import Path


def test_deterministic_event_id():
    from opencohost.core.memory_v5_shadow.evidence import CommittedTurnSnapshot
    from opencohost.core.memory_v5_shadow.store import ShadowStore

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "shadow.db"
        s = ShadowStore(db_path=db)
        run = "abc123run"
        seq = 42
        committed = f"{run}:{seq}"
        exp = hashlib.sha256(committed.encode()).hexdigest()[:24]
        snap = CommittedTurnSnapshot(
            committed_turn_id=committed,
            profile_id="p1",
            run_id=run,
            stream_sequence=seq,
            role="user",
            source="direct",
            occurred_at="2026-09-01T00:00:00Z",
            content="synthetic hello world",
            is_private=False,
        )
        s.insert_snapshot(snap)
        rows = s.list_evidence(profile_id="p1")
        assert rows[0]["event_id"] == exp
        assert rows[0]["content_hash"] == hashlib.sha256(b"synthetic hello world").hexdigest()
        s.close()


def test_idempotent_insert():
    from opencohost.core.memory_v5_shadow.evidence import CommittedTurnSnapshot
    from opencohost.core.memory_v5_shadow.store import ShadowStore

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "shadow.db"
        st = ShadowStore(db_path=db)
        snap = CommittedTurnSnapshot(
            committed_turn_id="run1:1",
            profile_id="p1",
            run_id="run1",
            stream_sequence=1,
            role="user",
            source="direct",
            occurred_at="2026-09-01T00:00:00Z",
            content="synthetic dup",
            is_private=False,
        )
        r1 = st.insert_snapshot(snap)
        assert r1.outcome == "INSERTED"
        r2 = st.insert_snapshot(snap)
        assert r2.outcome == "EXACT_DUPLICATE"
        assert st.count_evidence() == 1
        st.close()


def test_source_allowlist_constant():
    from opencohost.core.memory_v5_shadow.evidence import ALLOWED_SOURCES

    assert ALLOWED_SOURCES == frozenset({"direct", "ptt", "owner-bundle"})


def test_user_assistant_both_persisted_with_consecutive_seq():
    from opencohost.core.memory_v5_shadow.evidence import CommittedTurnSnapshot
    from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime

    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db)
        run = rt.run_id
        snap_u = CommittedTurnSnapshot(
            committed_turn_id=f"{run}:10",
            profile_id="p1",
            run_id=run,
            stream_sequence=10,
            role="user",
            source="direct",
            occurred_at="2026-09-01T00:00:00Z",
            content="synthetic user turn with enough tokens for capture",
            is_private=False,
        )
        snap_a = CommittedTurnSnapshot(
            committed_turn_id=f"{run}:11",
            profile_id="p1",
            run_id=run,
            stream_sequence=11,
            role="assistant",
            source="direct",
            occurred_at="2026-09-01T00:00:00Z",
            content="synthetic assistant reply with enough tokens for capture",
            is_private=False,
        )
        rt.record_turn(snap_u)
        rt.record_turn(snap_a)
        deadline = time.time() + 2.0
        while time.time() < deadline and rt._store.count_evidence(profile_id="p1") < 2:
            time.sleep(0.02)
        rows = rt._store.list_evidence(profile_id="p1")
        assert len(rows) == 2
        assert {r["role"] for r in rows} == {"user", "assistant"}
        seqs = sorted(r["stream_sequence"] for r in rows)
        assert seqs[1] == seqs[0] + 1
        for r in rows:
            assert r["event_id"] == hashlib.sha256(r["committed_turn_id"].encode()).hexdigest()[:24]
        rt.shutdown()
