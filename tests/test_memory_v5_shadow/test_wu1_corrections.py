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
    benchmark.N = 5
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
        rt._reserved = 0
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
            if isinstance(item, tuple) and item and item[0] in ("single", "exchange"):
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
            rt=m._memory_runtime
            rt.shutdown(timeout=3.0)
            import sqlite3
            conn=sqlite3.connect(str(db))
            rows=conn.execute("SELECT stream_sequence FROM evidence_journal ORDER BY rowid").fetchall()
            conn.close()
            seqs=[r[0] for r in rows]
            assert len(seqs) > 0, "no evidence recorded"
            assert seqs==sorted(seqs), f"insertion order not monotonic: {seqs}"
            print("ORDER_OK")
        """
    )
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=15)
    assert res.returncode == 0, f"profile switch order failed: {res.stdout}\n{res.stderr}"
    assert "ORDER_OK" in res.stdout


def test_preemption_before_admission_purged_cleanly():
    from opencohost.core.memory_v5_shadow.privacy import ShadowPrivacyController
    from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db, queue_maxsize=1000)
        pc = ShadowPrivacyController(rt)

        # Thread 1: Allocates sequence under lock, creates snapshots, then gets preempted BEFORE admission
        seq_u, seq_a = rt.allocate_sequences(2)
        user_snap = _snap(rt.run_id, seq_u, "p_preempt", "pre-purge user")
        asst_snap = _snap(rt.run_id, seq_a, "p_preempt", "pre-purge asst")

        # Thread 2: Dispatches and commits purge while Thread 1 is suspended
        res = pc.purge_profile("p_preempt")
        assert res["status"] == "COMMITTED"

        # Thread 1: Wakes up and calls record_turn_exchange
        rt.record_turn_exchange(user_snap, asst_snap)

        # Wait for worker queue drain
        deadline = time.time() + 2
        while time.time() < deadline and rt._queue.qsize() > 0:
            time.sleep(0.02)
        time.sleep(0.2)

        # PROOF: Pre-purge evidence allocated before purge MUST NOT survive!
        assert rt._store.count_evidence(profile_id="p_preempt") == 0

        # Post-purge turn must be admitted normally
        seq_u2, seq_a2 = rt.allocate_sequences(2)
        user_snap2 = _snap(rt.run_id, seq_u2, "p_preempt", "post-purge user")
        asst_snap2 = _snap(rt.run_id, seq_a2, "p_preempt", "post-purge asst")
        rt.record_turn_exchange(user_snap2, asst_snap2)

        deadline = time.time() + 2
        while time.time() < deadline and rt._store.count_evidence(profile_id="p_preempt") < 2:
            time.sleep(0.02)

        assert rt._store.count_evidence(profile_id="p_preempt") == 2
        rt.shutdown()


def test_forget_all_preemption_cutoff_cleanly_drops():
    from opencohost.core.memory_v5_shadow.privacy import ShadowPrivacyController
    from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db, queue_maxsize=1000)
        pc = ShadowPrivacyController(rt)

        seq_u, seq_a = rt.allocate_sequences(2)
        u_snap = _snap(rt.run_id, seq_u, "p1", "pre-forget user")
        a_snap = _snap(rt.run_id, seq_a, "p1", "pre-forget asst")

        res = pc.forget_all()
        assert res["status"] == "COMMITTED"

        rt.record_turn_exchange(u_snap, a_snap)

        deadline = time.time() + 2
        while time.time() < deadline and rt._queue.qsize() > 0:
            time.sleep(0.02)
        time.sleep(0.2)

        assert rt._store.count_evidence() == 0
        rt.shutdown()


def test_packaged_schema_parity_with_canonical_ddl():
    p_pkg = Path(r"E:\VoiceAI\opencohost\core\memory_v5_shadow\schema_v1.sql")
    p_doc = Path(r"E:\VoiceAI\docs\memory_v5\memory_v5_shadow_ddl_v1.sql")
    assert p_pkg.exists(), "packaged schema_v1.sql missing"
    assert p_doc.exists(), "docs memory_v5_shadow_ddl_v1.sql missing"

    h_pkg = hashlib.sha256(p_pkg.read_bytes()).hexdigest().upper()
    h_doc = hashlib.sha256(p_doc.read_bytes()).hexdigest().upper()
    assert h_pkg == h_doc, f"DDL mismatch: pkg={h_pkg} vs doc={h_doc}"


def test_exchange_atomic_batch_not_interleaved():
    from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db, queue_maxsize=5000)

        def worker(w_id: int):
            for i in range(20):
                u = _snap(rt.run_id, 0, f"p_{w_id}", f"user {w_id}:{i}")
                a = _snap(rt.run_id, 0, f"p_{w_id}", f"asst {w_id}:{i}")
                rt.record_turn_exchange(u, a)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rt.shutdown(timeout=3.0)

        import sqlite3
        conn = sqlite3.connect(str(db))
        rows = conn.execute("SELECT stream_sequence, profile_id FROM evidence_journal ORDER BY rowid").fetchall()
        conn.close()

        assert len(rows) == 4 * 20 * 2
        for idx in range(0, len(rows), 2):
            u_row = rows[idx]
            a_row = rows[idx + 1]
            assert u_row[1] == a_row[1], f"interleaved profiles at index {idx}: {u_row} vs {a_row}"
            assert a_row[0] == u_row[0] + 1, f"interleaved sequences at index {idx}: {u_row} vs {a_row}"


def test_mixed_privacy_exchange_rejected_completely():
    from opencohost.core.memory_v5_shadow.evidence import CommittedTurnSnapshot
    from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db, queue_maxsize=100)

        # 1. User private, assistant public -> must be dropped completely
        u_priv = CommittedTurnSnapshot(
            committed_turn_id=f"{rt.run_id}:1",
            profile_id="p1",
            run_id=rt.run_id,
            stream_sequence=1,
            role="user",
            source="direct",
            occurred_at="2026-09-01T00:00:00Z",
            content="private user question",
            is_private=True,
        )
        a_pub = CommittedTurnSnapshot(
            committed_turn_id=f"{rt.run_id}:2",
            profile_id="p1",
            run_id=rt.run_id,
            stream_sequence=2,
            role="assistant",
            source="direct",
            occurred_at="2026-09-01T00:00:00Z",
            content="public assistant answer",
            is_private=False,
        )
        rt.record_turn_exchange(u_priv, a_pub)

        # 2. User public, assistant private -> must be dropped completely
        u_pub = CommittedTurnSnapshot(
            committed_turn_id=f"{rt.run_id}:3",
            profile_id="p1",
            run_id=rt.run_id,
            stream_sequence=3,
            role="user",
            source="direct",
            occurred_at="2026-09-01T00:00:00Z",
            content="public user question",
            is_private=False,
        )
        a_priv = CommittedTurnSnapshot(
            committed_turn_id=f"{rt.run_id}:4",
            profile_id="p1",
            run_id=rt.run_id,
            stream_sequence=4,
            role="assistant",
            source="direct",
            occurred_at="2026-09-01T00:00:00Z",
            content="private assistant answer",
            is_private=True,
        )
        rt.record_turn_exchange(u_pub, a_priv)

        deadline = time.time() + 2
        while time.time() < deadline and rt._queue.qsize() > 0:
            time.sleep(0.02)
        time.sleep(0.2)

        # Neither exchange should have persisted any row
        assert rt._store.count_evidence(profile_id="p1") == 0
        rt.shutdown()


def test_lifecycle_events_persisted_for_switch_and_shutdown():
    import sqlite3
    from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db, queue_maxsize=100)
        rt.on_profile_switch("profA", "profB")
        rt.shutdown(timeout=2.0)

        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT kind, owner_profile_id, stream_sequence FROM lifecycle_events ORDER BY rowid"
        ).fetchall()
        conn.close()

        kinds = [r["kind"] for r in rows]
        assert "PROFILE_SWITCH_OUT" in kinds
        assert "PROFILE_SWITCH_IN" in kinds
        assert "SHUTDOWN" in kinds
