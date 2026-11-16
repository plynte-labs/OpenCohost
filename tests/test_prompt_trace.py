"""prompt-trace-20261007 (T1 + T2): per-call prompt traceability.

T1: one content-free ``[PROMPT_TRACE]`` INFO line per LLM call in the main log.
T2: opt-in (``OPENCOHOST_PROMPT_TRACE=1``) JSONL record with the full messages
and raw reply in a SEPARATE file under ``<data_root>/logs/prompt_trace``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import queue
import re
import time
from unittest.mock import MagicMock

import pytest

from opencohost.config import settings
from opencohost.core import llm_engine
from opencohost.core.context.prompt_assembler import PromptContextAssembler
from opencohost.core.engine import prompt_trace

SECRET_USER = "mi tarjeta secreta es 4444-ZEBRA-9"
SECRET_REPLY = "respuesta confidencial QUOKKA"


class FakeMessage:
    def __init__(self, content="", thinking=None):
        self.content = content
        self.thinking = thinking


class FakeChunk:
    def __init__(self, content="", done=False, **stats):
        self.message = FakeMessage(content)
        self.done = done
        for key, value in stats.items():
            setattr(self, key, value)

    def get(self, key, default=None):
        return getattr(self, key, default)


class FakeRouter:
    def __init__(self):
        self.jobs = []

    def submit_streaming(self, source, priority):
        job = type("J", (), {"job_id": len(self.jobs) + 1, "sealed": False})()
        self.jobs.append(job)
        return job

    def append_chunks(self, job, chunks):
        return True

    def seal(self, job):
        job.sealed = True


@pytest.fixture
def trace_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path))
    prompt_trace._reset_for_tests()
    return tmp_path / "prompt_trace"


@pytest.fixture
def flag_on(monkeypatch, trace_dir):
    monkeypatch.setattr(settings, "PROMPT_TRACE_ENABLED", True)
    return trace_dir


@pytest.fixture
def flag_off(monkeypatch, trace_dir):
    monkeypatch.setattr(settings, "PROMPT_TRACE_ENABLED", False)
    return trace_dir


@pytest.fixture
def motor(monkeypatch):
    m = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    m.current_model = "llama3"
    m.use_system_role = True
    m.ollama = MagicMock()
    m._speech_router_enabled = True
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", True)
    router = FakeRouter()
    m._ensure_router = lambda: router
    m._speech_router = router
    return m


def _buffered(motor, content="Hola, aqui estoy.", **stats):
    resp = FakeChunk(content, done=True, **stats)
    resp.message.thinking = "pienso en voz baja"
    motor._ollama_chat_with_watchdog = MagicMock(return_value=resp)
    motor._speech_router_enabled = False  # not stream-eligible


def _streamed(motor, chunks):
    def _streaming(*, timeout, **kwargs):
        def _gen():
            yield from chunks

        return _gen()

    motor._ollama_chat_streaming = _streaming


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if "[PROMPT_TRACE]" in r.getMessage()]


def _records(trace_dir):
    out = []
    for f in sorted(trace_dir.glob("*.jsonl")):
        for line in f.read_text(encoding="utf-8").splitlines():
            out.append(json.loads(line))
    return out


# ------------------------------------------------------------------ T1


def test_t1_line_has_sections_and_no_text(motor, flag_off, caplog):
    _buffered(motor, prompt_eval_count=50, eval_count=12, done_reason="stop")
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._generar_dialogo(SECRET_USER, source="direct", commit_history=True)
    lines = _lines(caplog)
    assert len(lines) == 1
    line = lines[0]
    print(line)
    assert re.search(
        r"turn=[0-9a-f]{8} source=direct model=llama3 sections=persona:\d+#[0-9a-f]{6}", line
    )
    for key in (
        "history_msgs=", "history_chars=", "num_predict=", "think=",
        "prompt_eval=50", "eval=12", "done=stop", "cache_reused=",
    ):
        assert key in line
    assert SECRET_USER not in line
    assert "ZEBRA" not in line
    assert "Hola, aqui estoy." not in line
    assert "pienso en voz baja" not in line


def test_flag_off_creates_no_file(motor, flag_off):
    _buffered(motor)
    motor._generar_dialogo("hola", source="direct", commit_history=True)
    assert not flag_off.exists()


def test_flag_on_buffered_writes_record(motor, flag_on, caplog):
    _buffered(motor, content=SECRET_REPLY, prompt_eval_count=40, eval_count=9, done_reason="stop")
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._generar_dialogo(SECRET_USER, source="direct", commit_history=True)
    recs = _records(flag_on)
    assert len(recs) == 1
    rec = recs[0]
    assert set(rec) >= {
        "turn", "ts", "source", "model", "options", "sections", "messages",
        "reply", "thinking", "done_reason", "prompt_eval_count", "eval_count", "path",
    }
    assert rec["source"] == "direct" and rec["model"] == "llama3"
    assert rec["path"] == "buffered"
    assert rec["reply"] == SECRET_REPLY
    assert rec["thinking"] == "pienso en voz baja"
    assert rec["done_reason"] == "stop" and rec["eval_count"] == 9
    assert set(rec["options"]) >= {"num_predict", "temperature", "num_ctx"}
    assert any(SECRET_USER in str(m["content"]) for m in rec["messages"])
    assert {"name", "chars", "hash"} <= set(rec["sections"][0])
    assert f"turn={rec['turn']}" in _lines(caplog)[0]
    assert SECRET_USER not in caplog.text and SECRET_REPLY not in caplog.text


def test_flag_on_streamed_writes_accumulated_reply(motor, flag_on, caplog):
    _streamed(motor, [
        FakeChunk("Primera frase limpia. "),
        FakeChunk("Segunda frase tranquila."),
        FakeChunk("", done=True, eval_count=20, prompt_eval_count=33, done_reason="stop"),
    ])
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._generar_dialogo(SECRET_USER, source="direct", commit_history=True)
    recs = _records(flag_on)
    assert len(recs) == 1
    assert recs[0]["path"] == "stream"
    assert recs[0]["reply"] == "Primera frase limpia. Segunda frase tranquila."
    assert recs[0]["prompt_eval_count"] == 33
    assert f"turn={recs[0]['turn']}" in _lines(caplog)[0]
    assert "prompt_eval=33" in _lines(caplog)[0]


# ------------------------------------------------------------------ unit


def _call(**over):
    kw = dict(
        source="direct", model="m", is_local=True,
        messages=[{"role": "system", "content": "SYS"}, {"role": "user", "content": "U"}],
        options={"num_predict": 10, "num_ctx": 100, "temperature": 0.5, "think": False},
        sections=[("persona", "SYS")], path="buffered",
    )
    kw.update(over)
    return prompt_trace.begin(**kw)


def test_pruning_removes_old_files_once(flag_on):
    flag_on.mkdir(parents=True)
    old = flag_on / "20200101.jsonl"
    old.write_text("{}\n", encoding="utf-8")
    fresh = flag_on / "20990101.jsonl"
    fresh.write_text("{}\n", encoding="utf-8")
    old_ts = time.time() - 5 * 86400
    os.utime(old, (old_ts, old_ts))
    _call().finish({"message": {"content": "r"}})
    assert not old.exists()
    assert fresh.exists()


def test_io_error_is_tolerated_with_one_warning(flag_on, monkeypatch, caplog):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(prompt_trace.os, "makedirs", boom)
    with caplog.at_level(logging.WARNING, logger="OpenCohost"):
        _call().finish({"message": {"content": "SECRETREPLY"}})
        _call().finish({"message": {"content": "SECRETREPLY"}})
    warns = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "PROMPT_TRACE" in r.getMessage()
    ]
    assert len(warns) >= 1
    assert "SECRETREPLY" not in caplog.text


def test_fail_records_error_without_content_in_log(flag_on, caplog):
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        _call().fail(TimeoutError("watchdog_timeout:45s"), partial="parcial")
    recs = _records(flag_on)
    assert recs[0]["error"] == "TimeoutError" and recs[0]["reply"] == "parcial"
    assert "parcial" not in caplog.text


def test_cache_reuse_estimate_per_model(flag_off, caplog):
    msgs = [{"role": "user", "content": "x" * 400}]
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        _call(model="cm", messages=msgs).finish({"prompt_eval_count": 100, "message": {"content": ""}})
        _call(model="cm", messages=msgs).finish({"prompt_eval_count": 10, "message": {"content": ""}})
    first, second = _lines(caplog)
    assert "cache_reused=0" in first
    assert "cache_reused=~90" in second


def test_cloud_call_reports_na(flag_off, caplog):
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        _call(is_local=False).finish({"message": {"content": ""}})
    assert "cache_reused=n/a" in _lines(caplog)[0]


# ------------------------------------------------------------------ assembler


def test_assembler_manifest_matches_system_prompt():
    setup = PromptContextAssembler().assemble(
        "hola", "direct",
        system_prompt="PERSONA TEXT",
        use_system_role=True, is_local=True, provider_cfg={}, request_model="llama3",
        history_snapshot=[
            {"role": "user", "content": "a", "source": "direct"},
            {"role": "assistant", "content": "b", "source": "direct"},
        ],
        personalization_enabled=False, memorias_enabled=False,
        warmth="empathetic",
    )
    names = [n for n, _ in setup.sections]
    assert names[0] == "persona"
    assert "grounding" in names and "owner_policy" in names and "warmth" in names
    system = setup.messages[0]["content"]
    system_parts = [t for n, t in setup.sections if n in setup.system_section_names]
    assert "\n\n".join(system_parts) == system
    user_msg = setup.messages[-1]["content"]
    for name, text in setup.sections:
        assert text in system or text in user_msg


def test_hash_is_sha1_prefix():
    assert prompt_trace.short_hash("abc") == hashlib.sha1(b"abc").hexdigest()[:6]
