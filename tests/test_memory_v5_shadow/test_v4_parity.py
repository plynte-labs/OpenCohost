import subprocess
import sys
import textwrap


def _run(code: str) -> subprocess.CompletedProcess:
    import os

    env = os.environ.copy()
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=30)


def test_off_vs_shadow_prompt_byte_identical_when_suppressed():
    code = textwrap.dedent(
        """
        import os, tempfile, queue, json, sys
        from pathlib import Path
        from unittest.mock import MagicMock, patch
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            os.environ["OPENCOHOST_DATA_ROOT"]=tmp
            os.environ["OPENCOHOST_MEMORY_V5_MODE"]="OFF"
            os.environ.pop("OPENCOHOST_MEMORY_V5_SHADOW_DB", None)
            from opencohost.core.llm_engine import MotorVocalIA
            moff=MotorVocalIA(queue.Queue(), lambda s: None)
            moff.ollama=MagicMock(); moff.pygame=MagicMock(); moff.is_ready=True
            moff._current_profile_id="p1"
            moff.historial.append({"role":"user","content":"synthetic hello","source":"direct","private":False})
            moff.historial.append({"role":"assistant","content":"synthetic hi","source":"direct","private":False})
            moff._memory_digest.append("contexto: synthetic -> Kira: hi")
            cap={}
            def fake_off(**kw):
                cap["off"]=kw.get("messages",[])
                return {"message":{"content":"r"}}
            with patch.object(moff, "_ollama_chat_with_watchdog", side_effect=fake_off):
                moff._generar_dialogo("synthetic direct question", source="direct", commit_history=False)
            off=json.dumps(cap["off"], sort_keys=True, ensure_ascii=False)
            db=Path(tmp)/"shadow.db"
            os.environ["OPENCOHOST_MEMORY_V5_MODE"]="SHADOW"
            os.environ["OPENCOHOST_MEMORY_V5_SHADOW_DB"]=str(db)
            from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime
            msh=MotorVocalIA(queue.Queue(), lambda s: None)
            rt=MemoryRuntime(db_path=db)
            msh._memory_runtime=rt
            msh._memory_run_id=rt.run_id
            msh.ollama=MagicMock(); msh.pygame=MagicMock(); msh.is_ready=True
            msh._current_profile_id="p1"
            msh.historial.append({"role":"user","content":"synthetic hello","source":"direct","private":False})
            msh.historial.append({"role":"assistant","content":"synthetic hi","source":"direct","private":False})
            msh._memory_digest.append("contexto: synthetic -> Kira: hi")
            def fake_sh(**kw):
                cap["shadow"]=kw.get("messages",[])
                return {"message":{"content":"r"}}
            with patch.object(msh, "_ollama_chat_with_watchdog", side_effect=fake_sh):
                msh._generar_dialogo("synthetic direct question", source="direct", commit_history=False)
            rt.shutdown()
            shadow=json.dumps(cap["shadow"], sort_keys=True, ensure_ascii=False)
            assert off==shadow, f"prompt diverged OFF vs SHADOW: {off!r} vs {shadow!r}"
            print("PARITY_OK")
        """
    )
    res = _run(code)
    assert res.returncode == 0, f"parity subprocess failed:\nSTDOUT:{res.stdout}\nSTDERR:{res.stderr}"
    assert "PARITY_OK" in res.stdout


def test_off_no_shadow_side_effects_via_subprocess():
    code = textwrap.dedent(
        """
        import os, tempfile, queue
        os.environ["OPENCOHOST_MEMORY_V5_MODE"]="OFF"
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            os.environ["OPENCOHOST_DATA_ROOT"]=tmp
            from opencohost.core.llm_engine import MotorVocalIA
            m=MotorVocalIA(queue.Queue(), lambda s: None)
            assert m._memory_runtime is None
            assert m._memory_run_id is None
            assert m._memory_init_status["effective"]=="OFF"
            print("OFF_SIDE_OK")
        """
    )
    res = _run(code)
    assert res.returncode == 0, f"OFF side effects failed: {res.stdout}\n{res.stderr}"
    assert "OFF_SIDE_OK" in res.stdout
