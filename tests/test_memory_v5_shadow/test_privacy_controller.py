import tempfile
import time
from pathlib import Path

from opencohost.core.memory_v5_shadow.evidence import CommittedTurnSnapshot
from opencohost.core.memory_v5_shadow.privacy import ShadowPrivacyController
from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime


def _snap(run: str, seq: int, pid: str, content: str = "synthetic") -> CommittedTurnSnapshot:
    return CommittedTurnSnapshot(
        committed_turn_id=f"{run}:{seq}",
        profile_id=pid,
        run_id=run,
        stream_sequence=seq,
        role="user",
        source="direct",
        occurred_at="2026-09-01T00:00:00Z",
        content=content,
        is_private=False,
    )


def test_purge_profile_atomic_via_controller():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db)
        pc = ShadowPrivacyController(rt)
        run = rt.run_id
        for pid in ["p1", "p2"]:
            for i in range(2):
                rt.record_turn(_snap(run, 10 + i if pid == "p1" else 20 + i, pid, f"user {pid} {i}"))
        deadline = time.time() + 2
        while time.time() < deadline and rt._store.count_evidence() < 4:
            time.sleep(0.02)
        assert rt._store.count_evidence(profile_id="p1") == 2
        assert rt._store.count_evidence(profile_id="p2") == 2
        res = pc.purge_profile("p1")
        assert res["status"] == "COMMITTED"
        assert rt._store.count_evidence(profile_id="p1") == 0
        assert rt._store.count_evidence(profile_id="p2") == 2
        rt.shutdown()


def test_forget_all_atomic_via_controller():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db)
        pc = ShadowPrivacyController(rt)
        run = rt.run_id
        rt.record_turn(_snap(run, 1, "p1", "synthetic user forget"))
        deadline = time.time() + 2
        while time.time() < deadline and rt._store.count_evidence() == 0:
            time.sleep(0.02)
        assert rt._store.count_evidence() > 0
        res = pc.forget_all()
        assert res["status"] == "COMMITTED"
        assert rt._store.count_evidence() == 0
        rt.shutdown()


def test_diagnostics_metadata_only():
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db)
        rt.record_turn(_snap(rt.run_id, 1, "p1", "synthetic user diag secret"))
        deadline = time.time() + 2
        while time.time() < deadline and rt._store.count_evidence() == 0:
            time.sleep(0.02)
        import json

        d = rt.diagnostics
        j = json.dumps(d)
        assert "synthetic user diag secret" not in j
        assert isinstance(d, dict)
        rt.shutdown()
