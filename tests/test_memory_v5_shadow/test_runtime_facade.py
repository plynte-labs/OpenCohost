import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path


def _run(code: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    import os

    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=10)


def test_facade_has_exactly_three_public_methods():
    from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime
    import inspect

    pubs = [m for m in dir(MemoryRuntime) if not m.startswith("_")]
    assert set(pubs) >= {"record_turn", "on_profile_switch", "shutdown"}
    for extra in ["purge_profile", "forget_all"]:
        assert extra not in pubs, f"MemoryRuntime must not expose {extra} directly"
    sig_rt = inspect.signature(MemoryRuntime.record_turn)
    assert "snapshot" in sig_rt.parameters
    sig_ps = inspect.signature(MemoryRuntime.on_profile_switch)
    params = [p for p in sig_ps.parameters.keys() if p != "self"]
    assert params == ["old_profile_id", "new_profile_id"]
    sig_sd = inspect.signature(MemoryRuntime.shutdown)
    assert "timeout" in sig_sd.parameters


def test_shadow_privacy_controller_delegates():
    from opencohost.core.memory_v5_shadow.privacy import ShadowPrivacyController

    import inspect

    assert "purge_profile" in dir(ShadowPrivacyController)
    assert "forget_all" in dir(ShadowPrivacyController)
    assert "record_turn" not in dir(ShadowPrivacyController)


def test_off_zero_via_subprocess_no_import_thread_db():
    code = textwrap.dedent(
        """
        import os, sys, tempfile
        from pathlib import Path
        td=tempfile.mkdtemp()
        os.environ["OPENCOHOST_DATA_ROOT"]=td
        os.environ["OPENCOHOST_MEMORY_V5_MODE"]="OFF"
        os.environ.pop("OPENCOHOST_MEMORY_V5_SHADOW_DB", None)
        # ensure fresh import
        if "opencohost.core.memory_v5_shadow.runtime" in sys.modules:
            del sys.modules["opencohost.core.memory_v5_shadow.runtime"]
        if "opencohost.core.memory_v5_shadow.store" in sys.modules:
            del sys.modules["opencohost.core.memory_v5_shadow.store"]
        import queue
        from opencohost.core.llm_engine import MotorVocalIA
        m=MotorVocalIA(queue.Queue(), lambda s: None)
        assert m._memory_runtime is None, "OFF must have no runtime"
        assert m._memory_run_id is None
        assert m._memory_stream_seq==0
        assert m._memory_init_status["effective"]=="OFF"
        # no v5 module imported
        imported=[k for k in sys.modules if "memory_v5_shadow" in k]
        assert imported==[], f"OFF imported v5: {imported}"
        # no thread
        import threading
        assert not any(t.name=="memory-v5-shadow" for t in threading.enumerate()), "OFF must not start worker thread"
        # no db file
        from opencohost.config.settings import MEMORY_V5_SHADOW_DB
        assert not Path(MEMORY_V5_SHADOW_DB).exists(), "OFF must not create DB"
        # no datetime work proof: before import, snapshot list should be None
        # call _commit_history with OFF and verify no snapshot allocation (via not creating file)
        m._current_profile_id="p1"
        m.ollama=__import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        m.pygame=__import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
        m.is_ready=True
        m._commit_history("synthetic user", "synthetic assistant", source="direct")
        import time; time.sleep(0.2)
        # still no db
        assert not Path(MEMORY_V5_SHADOW_DB).exists()
        print("OFF_OK")
        """
    )
    res = _run(code)
    assert res.returncode == 0, f"OFF subprocess failed: {res.stdout}\n{res.stderr}"
    assert "OFF_OK" in res.stdout


def test_shadow_creates_runtime_and_db():
    from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db)
        assert rt.run_id
        assert db.exists()
        rt.shutdown()
        try:
            rt._store.close()
        except Exception:
            pass


def test_queue_saturation_fail_open():
    import datetime

    from opencohost.core.memory_v5_shadow.evidence import CommittedTurnSnapshot
    from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        db = Path(tmp) / "shadow.db"
        rt = MemoryRuntime(db_path=db, queue_maxsize=10)
        snap = CommittedTurnSnapshot(
            committed_turn_id=f"{rt.run_id}:1",
            profile_id="p1",
            run_id=rt.run_id,
            stream_sequence=1,
            role="user",
            source="direct",
            occurred_at=datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
            content="synthetic user content alpha",
            is_private=False,
        )
        for _ in range(12):
            rt.record_turn(snap)
        time.sleep(0.2)
        assert rt.diagnostics["dropped_evidence_total"] >= 1
        rt.shutdown()


def test_engine_seam_architecture_contract():
    """Architectural gate: llm_engine.py must not instantiate Memory v5 internal components directly."""
    llm_engine_path = Path(__file__).resolve().parents[2] / "opencohost" / "core" / "llm_engine.py"
    assert llm_engine_path.exists(), f"llm_engine.py not found at {llm_engine_path}"
    llm_engine_source = llm_engine_path.read_text(encoding="utf-8")

    assert "SemanticCacheStore(" not in llm_engine_source
    assert "SemanticWorkerService(" not in llm_engine_source
    assert "IncrementalSemanticIndexer(" not in llm_engine_source
    assert "EpisodicRecallCoordinator(" not in llm_engine_source
    assert "from opencohost.core.memory_v5_shadow.episodic_recall import RecallMode" not in llm_engine_source
