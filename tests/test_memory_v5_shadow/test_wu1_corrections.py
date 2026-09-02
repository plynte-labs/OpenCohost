import hashlib
import importlib.util
import queue
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from pathlib import Path


def _snap(run: str, seq: int, pid: str, content: str = "synthetic") -> object:
    from opencohost.core.memory_v5_shadow.evidence import CommittedTurnSnapshot

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


def test_benchmark_samples_combined_snapshot_pair_and_admissions():
    benchmark_path = Path("docs/memory_v5/wu1_benchmark_shadow.py")
    spec = importlib.util.spec_from_file_location("wu1_benchmark_shadow", benchmark_path)
    assert spec is not None and spec.loader is not None
    benchmark = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(benchmark)
    benchmark.SAMPLES = 5
    benchmark.WARMUPS = 1

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        component = benchmark._run("component", Path(tmp))

    assert len(component["combined"]) == 5
    assert len(component["snapshot"]) == 5
    assert len(component["enqueue"]) == 5


def test_one_fifo_single_owner_and_admission():
    from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db, queue_maxsize=20)
        assert rt._queue is not None
        qid = id(rt._queue)
        rt.record_turn(_snap(rt.run_id, 1, "p1"))
        rt.on_profile_switch("p1", "p2")
        rt.record_turn(_snap(rt.run_id, 2, "p1"))
        assert id(rt._queue) == qid
        assert rt._queue.qsize() >= 2
        d = rt.diagnostics
        assert "worker_alive" in d
        assert "admission_closed" in d
        rt.shutdown()


def test_privacy_linearizability_epochs_deterministic():
    from opencohost.core.memory_v5_shadow.privacy import ShadowPrivacyController
    from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db, queue_maxsize=1000)
        pc = ShadowPrivacyController(rt)
        orig_store = rt._store.insert_snapshot

        def slow(s):
            time.sleep(0.15)
            return orig_store(s)

        rt._store.insert_snapshot = slow
        rt.record_turn(_snap(rt.run_id, 1, "p1", "before1"))
        rt.record_turn(_snap(rt.run_id, 2, "p1", "before2"))
        res = pc.purge_profile("p1")
        assert res["status"] == "COMMITTED"
        deadline = time.time() + 3
        while time.time() < deadline and rt._queue.qsize() > 0:
            time.sleep(0.02)
        time.sleep(0.2)
        assert rt._store.count_evidence(profile_id="p1") == 0
        rt._store.insert_snapshot = orig_store
        rt.record_turn(_snap(rt.run_id, 10, "p1", "after"))
        deadline = time.time() + 2
        while time.time() < deadline and rt._store.count_evidence(profile_id="p1") == 0:
            time.sleep(0.02)
        assert rt._store.count_evidence(profile_id="p1") == 1
        rt.shutdown()


def test_queue_full_and_timeout_fail_closed_no_direct_fallback():
    from opencohost.core.memory_v5_shadow.privacy import ShadowPrivacyController
    from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db, queue_maxsize=3)
        orig = rt._store.insert_snapshot

        def block(s):
            time.sleep(1.2)
            return orig(s)

        rt._store.insert_snapshot = block
        for i in range(3):
            rt.record_turn(_snap(rt.run_id, 100 + i, "p1", f"fill{i}"))
        time.sleep(0.1)
        # queue is now full (3/3) and worker blocked for 1.2s
        pc = ShadowPrivacyController(rt)
        res = pc.purge_profile("p1")
        assert res["status"] == "FAILED"
        assert res["reason"] in ("queue_full", "barrier_timeout_cancelled", "admission_closed")
        assert rt.diagnostics["degraded"] is True
        assert rt.diagnostics["capture_enabled"] is False
        rt._store.insert_snapshot = orig
        rt.shutdown(timeout=0.05)
        assert rt.diagnostics["control_failures_total"] >= 1
        # cleanup with ignore errors - file may be locked
        try:
            rt._store.close()
        except Exception:
            pass


def test_persistence_outcomes_matrix():
    from opencohost.core.memory_v5_shadow.store import ShadowStore

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        s = ShadowStore(db_path=db)
        snap = _snap("run1", 1, "p1", "hello")
        r1 = s.insert_snapshot(snap)
        assert r1.outcome == "INSERTED"
        r2 = s.insert_snapshot(snap)
        assert r2.outcome == "EXACT_DUPLICATE"
        snap_diff = _snap("run1", 1, "p1", "different content")
        r3 = s.insert_snapshot(snap_diff)
        assert r3.outcome == "INTEGRITY_COLLISION"
        assert "collision" in r3.reason_code
        snap_seq = _snap("run1", 1, "p2", "other profile same seq")
        r4 = s.insert_snapshot(snap_seq)
        assert r4.outcome == "INTEGRITY_COLLISION"
        s.close()


def test_init_observability_requested_vs_effective():
    code_off = textwrap.dedent(
        """
        import os, tempfile, queue
        from pathlib import Path
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            os.environ["OPENCOHOST_DATA_ROOT"]=tmp
            os.environ["OPENCOHOST_MEMORY_V5_MODE"]="OFF"
            os.environ.pop("OPENCOHOST_MEMORY_V5_SHADOW_DB", None)
            from opencohost.core.llm_engine import MotorVocalIA
            m=MotorVocalIA(queue.Queue(), lambda s: None)
            assert m._memory_init_status["requested"] in ("OFF","off")
            assert m._memory_init_status["effective"]=="OFF"
            assert m._memory_runtime is None
            print("OFF_OK")
        """
    )
    code_shadow_fail = textwrap.dedent(
        """
        import os, tempfile, queue
        from pathlib import Path
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            os.environ["OPENCOHOST_DATA_ROOT"]=tmp
            os.environ["OPENCOHOST_MEMORY_V5_MODE"]="SHADOW"
            os.environ["OPENCOHOST_MEMORY_V5_SHADOW_DB"]=str(Path(tmp)/"bad.db")
            import opencohost.core.memory_v5_shadow.runtime as rtmod
            orig=rtmod.MemoryRuntime
            def boom(*a, **k):
                raise RuntimeError("init boom")
            rtmod.MemoryRuntime=boom
            try:
                from opencohost.core.llm_engine import MotorVocalIA
                m2=MotorVocalIA(queue.Queue(), lambda s: None)
                assert m2._memory_runtime is None
                assert m2._memory_init_status["requested"]=="SHADOW"
                assert m2._memory_init_status["effective"]=="INIT_FAILED"
                assert m2._memory_init_status["reason_code"]=="init_exception"
                assert "boom" not in str(m2._memory_init_status.values())
            finally:
                rtmod.MemoryRuntime=orig
            print("SHADOW_FAIL_OK")
        """
    )
    env = __import__("os").environ.copy()
    import os as _os

    env_off = _os.environ.copy()
    env_off["OPENCOHOST_MEMORY_V5_MODE"] = "OFF"
    res = subprocess.run([sys.executable, "-c", code_off], capture_output=True, text=True, env=env_off, timeout=10)
    assert res.returncode == 0, f"OFF observability failed: {res.stdout}\n{res.stderr}"
    assert "OFF_OK" in res.stdout
    env_sh = _os.environ.copy()
    env_sh["OPENCOHOST_MEMORY_V5_MODE"] = "SHADOW"
    res2 = subprocess.run([sys.executable, "-c", code_shadow_fail], capture_output=True, text=True, env=env_sh, timeout=10)
    assert res2.returncode == 0, f"SHADOW fail observability failed: {res2.stdout}\n{res2.stderr}"
    assert "SHADOW_FAIL_OK" in res2.stdout


def test_shutdown_ownership_rejects_post_quiesce_and_no_caller_close():
    from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db, queue_maxsize=100)
        orig = rt._store.insert_snapshot

        def slow(s):
            time.sleep(0.3)
            return orig(s)

        rt._store.insert_snapshot = slow
        rt.record_turn(_snap(rt.run_id, 1, "p1", "before"))
        rt.record_turn(_snap(rt.run_id, 2, "p1", "before2"))
        rt.shutdown(timeout=0.05)
        d = rt.diagnostics
        assert d["degraded"] is True
        assert d["control_failures_total"] >= 1
        dropped_before = d["dropped_evidence_total"]
        rt.record_turn(_snap(rt.run_id, 99, "p1", "post"))
        assert rt.diagnostics["dropped_evidence_total"] >= dropped_before
        rt2 = MemoryRuntime(db_path=Path(tmp) / "shadow2.db")
        assert rt2.run_id != rt.run_id
        assert rt2._worker is not rt._worker
        rt2.shutdown()
        rt._store.insert_snapshot = orig
        try:
            rt._store.close()
        except Exception:
            pass


def test_record_admission_enqueue_is_atomic_against_shutdown():
    from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db, queue_maxsize=20)
        entered = threading.Event()
        release = threading.Event()
        lock_held: list[bool] = []
        original_put = rt._queue.put_nowait

        def gated_put(item):
            if not (isinstance(item, tuple) and item and isinstance(item[0], str)):
                lock_held.append(rt._adm_lock.locked())
                entered.set()
                assert release.wait(2), "test did not release evidence enqueue"
            original_put(item)

        rt._queue.put_nowait = gated_put
        producer = threading.Thread(target=rt.record_turn, args=(_snap(rt.run_id, 1, "p1"),))
        producer.start()
        assert entered.wait(2), "record_turn did not reach queue admission"
        stopper = threading.Thread(target=rt.shutdown, kwargs={"timeout": 2})
        stopper.start()
        release.set()
        producer.join(2)
        stopper.join(2)

        assert not producer.is_alive()
        assert not stopper.is_alive()
        assert lock_held == [True], "admission decision and enqueue must share _adm_lock"
        assert not rt._worker.is_alive()
        from opencohost.core.memory_v5_shadow.store import ShadowStore

        reopened = ShadowStore(db_path=db)
        assert reopened.count_evidence(profile_id="p1") == 1
        reopened.close()


def test_full_queue_shutdown_is_bounded_and_drains_after_release():
    from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db, queue_maxsize=3)
        rt._reserved = 0
        entered = threading.Event()
        release = threading.Event()
        original_insert = rt._store.insert_snapshot

        def blocked_insert(snapshot):
            entered.set()
            assert release.wait(2), "test did not release blocked worker"
            return original_insert(snapshot)

        rt._store.insert_snapshot = blocked_insert
        rt.record_turn(_snap(rt.run_id, 1, "p1"))
        assert entered.wait(2), "worker did not begin first insert"
        for seq in range(2, 5):
            rt.record_turn(_snap(rt.run_id, seq, "p1"))
        assert rt._queue.full()

        rt.shutdown(timeout=0.01)
        assert rt._running is False
        assert rt._worker.is_alive()
        assert rt.diagnostics["degraded"] is True
        assert rt._store.count_evidence(profile_id="p1") == 0, "live worker must retain store ownership"

        release.set()
        rt.shutdown(timeout=2)
        assert not rt._worker.is_alive()
        from opencohost.core.memory_v5_shadow.store import ShadowStore

        reopened = ShadowStore(db_path=db)
        assert reopened.count_evidence(profile_id="p1") == 4
        reopened.close()


def test_queued_purge_timeout_cancels_before_worker_ownership():
    from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        rt = MemoryRuntime(db_path=Path(tmp) / "shadow.db", queue_maxsize=20)
        target = _snap(rt.run_id, 1, "target")
        assert rt._store.insert_snapshot(target).outcome == "INSERTED"
        entered = threading.Event()
        release = threading.Event()
        original_insert = rt._store.insert_snapshot

        def blocked_insert(snapshot):
            entered.set()
            assert release.wait(2), "test did not release queued evidence"
            return original_insert(snapshot)

        rt._store.insert_snapshot = blocked_insert
        rt.record_turn(_snap(rt.run_id, 2, "blocker"))
        assert entered.wait(2), "worker did not take blocker"
        result = rt._dispatch_barrier("purge", "target", timeout=0.01)
        assert result == {"status": "FAILED", "reason": "barrier_timeout_cancelled"}

        release.set()
        rt._queue.join()
        assert rt._store.count_evidence(profile_id="target") == 1
        rt.shutdown()


def test_started_purge_timeout_is_indeterminate_until_completion():
    from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        rt = MemoryRuntime(db_path=Path(tmp) / "shadow.db", queue_maxsize=20)
        assert rt._store.insert_snapshot(_snap(rt.run_id, 1, "target")).outcome == "INSERTED"
        entered = threading.Event()
        release = threading.Event()
        original_purge = rt._store.purge_profile

        def blocked_purge(profile_id):
            entered.set()
            assert release.wait(2), "test did not release started purge"
            return original_purge(profile_id)

        rt._store.purge_profile = blocked_purge
        holder: dict = {}
        caller = threading.Thread(
            target=lambda: holder.update(rt._dispatch_barrier("purge", "target", timeout=0.01))
        )
        caller.start()
        assert entered.wait(2), "worker did not claim purge ownership"
        caller.join(2)
        assert not caller.is_alive()
        assert holder == {"status": "INDETERMINATE", "reason": "execution_started"}

        release.set()
        rt._queue.join()
        assert rt._store.count_evidence(profile_id="target") == 0
        rt.shutdown()


def test_profile_switch_order_frozen_allocator():
    code = textwrap.dedent(
        """
        import os, tempfile, queue, threading, time
        from pathlib import Path
        from unittest.mock import MagicMock
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            os.environ["OPENCOHOST_DATA_ROOT"]=tmp
            os.environ["OPENCOHOST_MEMORY_V5_MODE"]="SHADOW"
            db=Path(tmp)/"shadow.db"
            os.environ["OPENCOHOST_MEMORY_V5_SHADOW_DB"]=str(db)
            from opencohost.core.llm_engine import MotorVocalIA
            m=MotorVocalIA(queue.Queue(), lambda s: None)
            m.ollama=MagicMock(); m.pygame=MagicMock(); m.is_ready=True
            m._current_profile_id="pA"
            def committer():
                for i in range(10):
                    m._commit_history(f"user {i}", f"assistant {i}", source="direct")
            def switcher():
                for pid in ["pB","pC","pA"]:
                    m._dispatch_command("set_profile", {"id": pid, "_profile_name": pid})
                    time.sleep(0.01)
            t1=threading.Thread(target=committer)
            t2=threading.Thread(target=switcher)
            t1.start(); t2.start()
            t1.join(); t2.join()
            time.sleep(0.5)
            rt=m._memory_runtime
            rows=rt._store.list_evidence()
            seqs=[r["stream_sequence"] for r in rows]
            assert seqs==sorted(seqs), f"sequences not monotonic: {seqs}"
            rt.shutdown()
            print("ORDER_OK")
        """
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15)
    assert res.returncode == 0, f"profile switch order failed: {res.stdout}\n{res.stderr}"
    assert "ORDER_OK" in res.stdout


def test_live_v4_db_unchanged_via_subprocess():
    import hashlib

    live = Path(r"E:\VoiceAI\data\memorias\memorias.db")
    if not live.exists():
        return
    h0 = hashlib.sha256(live.read_bytes()).hexdigest()
    sz0 = live.stat().st_size
    code = textwrap.dedent(
        """
        import tempfile, queue, os, time
        from pathlib import Path
        from unittest.mock import MagicMock
        os.environ["OPENCOHOST_MEMORY_V5_MODE"]="SHADOW"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            db=Path(tmp)/"shadow.db"
            os.environ["OPENCOHOST_MEMORY_V5_SHADOW_DB"]=str(db)
            os.environ["OPENCOHOST_DATA_ROOT"]=tmp
            from opencohost.core.llm_engine import MotorVocalIA
            m=MotorVocalIA(queue.Queue(), lambda s: None)
            m.ollama=MagicMock(); m.pygame=MagicMock(); m.is_ready=True
            m._current_profile_id="p1"
            m._commit_history("synthetic user", "synthetic assistant", source="direct")
            time.sleep(0.3)
            m._memory_runtime.shutdown()
        """
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=10)
    assert res.returncode == 0, f"shadow subproc failed: {res.stderr}"
    h1 = hashlib.sha256(live.read_bytes()).hexdigest()
    sz1 = live.stat().st_size
    assert h0 == h1, "live v4 DB hash changed"
    assert sz0 == sz1, "live v4 DB size changed"
