"""Memory v5 write path: visible init/drop/skip logging and closure embedding.

T1 of the memory semantic-recall work. Memory v5 wrote nothing in live
Tauri sessions and every failure path was silent (a bare ``except`` at engine
init, swallowed per-turn drops, module loggers that never reach
``logs/opencohost_*.log``). These tests pin:

- one INFO startup line on the ``OpenCohost`` logger (the one routed to the
  owner's log file), carrying the exception TYPE only on failure;
- a WARNING (once per reason) when a committed turn is dropped;
- an INFO (once per reason per session) when a turn is skipped by a gate;
- a committed direct turn reaching the evidence journal in a temp DB;
- episode closure at shutdown triggering the embedding worker.

No test ever touches the real ``data/`` DBs and no log line may carry content.
"""
from __future__ import annotations

import logging
import math
import queue
import re
import sqlite3

import pytest

import opencohost.core.llm_engine as engine_mod
from opencohost.config import settings as settings_mod
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.core.memory_v5_shadow import subsystem as subsystem_mod
from opencohost.core.memory_v5_shadow.evidence import CommittedTurnSnapshot
from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime
from opencohost.core.memory_v5_shadow.subsystem import MemorySubsystem

SECRET = "SECRET-user-words-never-logged"


class FakeWorker:
    """In-process stand-in for SemanticWorkerService (no child process)."""

    def __init__(self, *args, **kwargs):
        self.embed_batch_calls = 0
        self.started = False
        self.stopped = False

    def start(self, timeout: float = 5.0) -> None:
        self.started = True

    def is_alive(self) -> bool:
        return self.started and not self.stopped

    @staticmethod
    def _vec(text: str) -> list[float]:
        raw = [((hash((text, i)) % 1000) + 1) / 1000.0 for i in range(384)]
        norm = math.sqrt(sum(x * x for x in raw))
        return [x / norm for x in raw]

    def embed_query(self, text, timeout_s=0.5):
        return self._vec(text)

    def embed_batch(self, texts, timeout_s=10.0):
        self.embed_batch_calls += 1
        return [self._vec(t) for t in texts]

    def shutdown(self) -> None:
        self.stopped = True


def _memory_v5_records(caplog, level=None):
    out = []
    for r in caplog.records:
        if r.name != "OpenCohost":
            continue
        if "memory_v5:" not in r.getMessage():
            continue
        if level is not None and r.levelno != level:
            continue
        out.append(r)
    return out


@pytest.fixture
def temp_v5(tmp_path, monkeypatch):
    shadow = tmp_path / "shadow.db"
    cache = tmp_path / "cache.db"
    monkeypatch.setenv("OPENCOHOST_MEMORY_V5_SHADOW_DB", str(shadow))
    monkeypatch.setenv("OPENCOHOST_MEMORY_V5_SEMANTIC_CACHE_DB", str(cache))
    monkeypatch.setattr(settings_mod, "MEMORY_V5_SHADOW_DB", str(shadow))
    monkeypatch.setattr(settings_mod, "MEMORY_V5_SEMANTIC_CACHE_DB", str(cache))
    monkeypatch.setattr(subsystem_mod, "SemanticWorkerService", FakeWorker)
    return shadow, cache


@pytest.fixture
def motor(monkeypatch):
    # conftest keeps MEMORY_V5_MODE OFF, so construction never spawns a worker;
    # each test attaches the subsystem it needs.
    monkeypatch.setattr(engine_mod, "MEMORIAS_ENABLED", False, raising=False)
    monkeypatch.setattr(settings_mod, "MEMORIAS_ENABLED", False, raising=False)
    m = MotorVocalIA(queue.Queue(), lambda *a, **k: None)
    yield m
    mem = getattr(m, "_memory", None)
    if mem is not None:
        mem.shutdown(timeout_s=2.0)


def _count(db, table):
    con = sqlite3.connect(str(db))
    try:
        return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
    finally:
        con.close()


# ── engine init line ────────────────────────────────────────────────────────


def test_init_success_logs_one_info_line(monkeypatch, caplog, temp_v5):
    monkeypatch.setattr(settings_mod, "MEMORY_V5_MODE", "ACTIVE")
    m = MotorVocalIA.__new__(MotorVocalIA)
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        m._init_memory_v5()
    try:
        recs = [
            r for r in _memory_v5_records(caplog, logging.INFO)
            if r.getMessage().startswith("memory_v5: mode=")
        ]
        assert [_strip_db_suffix(r.getMessage()) for r in recs] == [
            "memory_v5: mode=ACTIVE effective=ACTIVE init=ok reason=ok"
        ]
        # Startup recovery/reconcile are visible too, metadata counts only.
        others = [r.getMessage() for r in _memory_v5_records(caplog)]
        assert "memory_v5: startup_reconcile indexed=0" in others
        assert m._memory is not None and m._memory.runtime is not None
    finally:
        m._memory.shutdown(timeout_s=2.0)


def _startup_line(caplog):
    lines = [
        r.getMessage() for r in _memory_v5_records(caplog)
        if r.getMessage().startswith("memory_v5: mode=")
    ]
    assert len(lines) == 1
    return lines[0]


_DB_SUFFIX = re.compile(r" shadow_db=\S+ cache_db=\S+ override=(none|shadow|cache|both)$")


def _strip_db_suffix(line):
    """Drop the appended DB-identity fields; they have dedicated tests."""
    assert _DB_SUFFIX.search(line), line
    return _DB_SUFFIX.sub("", line)


def _run_init(monkeypatch, caplog, mode="OFF"):
    monkeypatch.setattr(settings_mod, "MEMORY_V5_MODE", mode)
    m = MotorVocalIA.__new__(MotorVocalIA)
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        m._init_memory_v5()
    return m


def test_startup_line_default_paths_report_basenames_no_override(monkeypatch, caplog, tmp_path):
    monkeypatch.delenv("OPENCOHOST_MEMORY_V5_SHADOW_DB", raising=False)
    monkeypatch.delenv("OPENCOHOST_MEMORY_V5_SEMANTIC_CACHE_DB", raising=False)
    monkeypatch.setattr(
        settings_mod, "MEMORY_V5_SHADOW_DB", str(tmp_path / "data" / "memory_v5_shadow.db")
    )
    monkeypatch.setattr(
        settings_mod, "MEMORY_V5_SEMANTIC_CACHE_DB", str(tmp_path / "data" / "memory_v5_cache.db")
    )
    _run_init(monkeypatch, caplog)
    line = _startup_line(caplog)
    assert line.endswith("shadow_db=memory_v5_shadow.db cache_db=memory_v5_cache.db override=none")
    assert "/" not in line and "\\" not in line
    assert str(tmp_path) not in line


def test_startup_line_shadow_override_is_flagged(monkeypatch, caplog, tmp_path):
    monkeypatch.setenv("OPENCOHOST_MEMORY_V5_SHADOW_DB", str(tmp_path / "old_trial.db"))
    monkeypatch.delenv("OPENCOHOST_MEMORY_V5_SEMANTIC_CACHE_DB", raising=False)
    monkeypatch.setattr(settings_mod, "MEMORY_V5_SHADOW_DB", str(tmp_path / "old_trial.db"))
    monkeypatch.setattr(settings_mod, "MEMORY_V5_SEMANTIC_CACHE_DB", str(tmp_path / "c.db"))
    _run_init(monkeypatch, caplog)
    line = _startup_line(caplog)
    assert "shadow_db=old_trial.db" in line
    assert line.endswith("override=shadow")
    assert str(tmp_path) not in line


def test_startup_line_cache_override_is_flagged(monkeypatch, caplog, tmp_path):
    monkeypatch.delenv("OPENCOHOST_MEMORY_V5_SHADOW_DB", raising=False)
    monkeypatch.setenv("OPENCOHOST_MEMORY_V5_SEMANTIC_CACHE_DB", str(tmp_path / "old_cache.db"))
    monkeypatch.setattr(settings_mod, "MEMORY_V5_SHADOW_DB", str(tmp_path / "s.db"))
    monkeypatch.setattr(settings_mod, "MEMORY_V5_SEMANTIC_CACHE_DB", str(tmp_path / "old_cache.db"))
    _run_init(monkeypatch, caplog)
    line = _startup_line(caplog)
    assert "cache_db=old_cache.db" in line
    assert line.endswith("override=cache")


def test_startup_line_both_overrides_flagged(monkeypatch, caplog, tmp_path):
    monkeypatch.setenv("OPENCOHOST_MEMORY_V5_SHADOW_DB", str(tmp_path / "a.db"))
    monkeypatch.setenv("OPENCOHOST_MEMORY_V5_SEMANTIC_CACHE_DB", str(tmp_path / "b.db"))
    monkeypatch.setattr(settings_mod, "MEMORY_V5_SHADOW_DB", str(tmp_path / "a.db"))
    monkeypatch.setattr(settings_mod, "MEMORY_V5_SEMANTIC_CACHE_DB", str(tmp_path / "b.db"))
    _run_init(monkeypatch, caplog)
    line = _startup_line(caplog)
    assert "shadow_db=a.db cache_db=b.db override=both" in line
    assert "/" not in line and "\\" not in line


def test_startup_line_unknown_paths_never_raise(monkeypatch, caplog):
    monkeypatch.delenv("OPENCOHOST_MEMORY_V5_SHADOW_DB", raising=False)
    monkeypatch.delenv("OPENCOHOST_MEMORY_V5_SEMANTIC_CACHE_DB", raising=False)
    monkeypatch.setattr(settings_mod, "MEMORY_V5_SHADOW_DB", None)
    monkeypatch.setattr(settings_mod, "MEMORY_V5_SEMANTIC_CACHE_DB", "")
    _run_init(monkeypatch, caplog)
    line = _startup_line(caplog)
    assert line.endswith("shadow_db=unknown cache_db=unknown override=none")


def test_init_engine_exception_logs_type_only(monkeypatch, caplog):
    monkeypatch.setattr(settings_mod, "MEMORY_V5_MODE", "ACTIVE")

    def _boom(*a, **k):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(MemorySubsystem, "from_settings", classmethod(_boom))
    m = MotorVocalIA.__new__(MotorVocalIA)
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        m._init_memory_v5()
    msgs = [_strip_db_suffix(r.getMessage()) for r in _memory_v5_records(caplog)]
    assert msgs == [
        "memory_v5: mode=ACTIVE effective=INIT_FAILED init=failed "
        "reason=init_exception exc=RuntimeError"
    ]
    assert _memory_v5_records(caplog)[0].levelno == logging.WARNING
    assert SECRET not in caplog.text
    assert m._memory is None
    assert m._memory_init_status["requested"] == "ACTIVE"
    assert m._memory_init_status["effective"] == "INIT_FAILED"


def test_init_subsystem_failure_surfaces_exception_type(monkeypatch, caplog, temp_v5):
    monkeypatch.setattr(settings_mod, "MEMORY_V5_MODE", "ACTIVE")

    def _locked(self):
        raise sqlite3.OperationalError(SECRET)

    monkeypatch.setattr(subsystem_mod.SemanticCacheStore, "initialize", _locked)
    m = MotorVocalIA.__new__(MotorVocalIA)
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        m._init_memory_v5()
    msgs = [_strip_db_suffix(r.getMessage()) for r in _memory_v5_records(caplog)]
    assert msgs == [
        "memory_v5: mode=ACTIVE effective=INIT_FAILED init=failed "
        "reason=init_exception exc=OperationalError"
    ]
    assert SECRET not in caplog.text
    assert m._memory.init_exception_type == "OperationalError"


def test_init_off_logs_mode_off(monkeypatch, caplog):
    monkeypatch.setattr(settings_mod, "MEMORY_V5_MODE", "OFF")
    m = MotorVocalIA.__new__(MotorVocalIA)
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        m._init_memory_v5()
    msgs = [_strip_db_suffix(r.getMessage()) for r in _memory_v5_records(caplog)]
    assert msgs == ["memory_v5: mode=OFF effective=OFF init=ok reason=off_default"]
    assert m._memory is None


# ── the write path under an EngineHost-like engine ──────────────────────────


def test_committed_direct_turn_reaches_journal_and_sessions(motor, temp_v5):
    shadow, _cache = temp_v5
    motor._memory = MemorySubsystem.from_settings(mode="ACTIVE")
    assert motor._memory.status["effective"] == "ACTIVE"
    motor._current_profile_id = "prof-write-path"

    motor._commit_history("me gusta el cafe negro", "anotado, cafe negro", source="direct")
    motor._memory.runtime._queue.join()

    assert _count(shadow, "evidence_journal") == 2
    assert _count(shadow, "sessions") >= 1


# ── per-turn skip (INFO once per reason) ────────────────────────────────────


def test_gate_skip_no_profile_logs_once(motor, temp_v5, caplog):
    shadow, _cache = temp_v5
    motor._memory = MemorySubsystem.from_settings(mode="ACTIVE")
    motor._current_profile_id = None
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._commit_history(SECRET, "respuesta uno", source="direct")
        motor._commit_history(SECRET, "respuesta dos", source="direct")
    msgs = [r.getMessage() for r in _memory_v5_records(caplog, logging.INFO)
            if r.getMessage().startswith("memory_v5: skip")]
    assert msgs == ["memory_v5: skip reason=no_profile"]
    assert SECRET not in caplog.text
    motor._memory.runtime._queue.join()
    assert _count(shadow, "evidence_journal") == 0


def test_gate_skip_reasons_are_distinct_and_once_each(motor, caplog):
    motor._memory = None
    motor._current_profile_id = "prof"
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._commit_history("a", "b", source="direct")
        motor._commit_history("c", "d", source="direct")
    msgs = [r.getMessage() for r in _memory_v5_records(caplog, logging.INFO)
            if r.getMessage().startswith("memory_v5: skip")]
    assert msgs == ["memory_v5: skip reason=no_runtime"]


def test_gate_skip_ineligible_source_and_private(motor, temp_v5, caplog):
    motor._memory = MemorySubsystem.from_settings(mode="ACTIVE")
    motor._current_profile_id = "prof"
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._commit_history("x", "y", source="chat")
        motor._commit_history("x", "y", source="chat")
        motor._memorias_private = True
        motor._commit_history("x", "y", source="direct")
        motor._commit_history("x", "y", source="direct")
    msgs = [r.getMessage() for r in _memory_v5_records(caplog, logging.INFO)
            if r.getMessage().startswith("memory_v5: skip")]
    assert msgs == [
        "memory_v5: skip reason=source_not_eligible",
        "memory_v5: skip reason=private",
    ]


# ── per-turn drop (WARNING, type only, once per reason) ─────────────────────


def test_turn_enqueue_exception_warns_type_only_once(motor, temp_v5, caplog):
    motor._memory = MemorySubsystem.from_settings(mode="ACTIVE")
    motor._current_profile_id = "prof"

    def _explode(*a, **k):
        raise ValueError(SECRET)

    motor._memory.record_turn_exchange = _explode
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._commit_history("q1", "a1", source="direct")
        motor._commit_history("q2", "a2", source="direct")
    msgs = [r.getMessage() for r in _memory_v5_records(caplog, logging.WARNING)]
    assert msgs == [
        "memory_v5: drop reason=snapshot_construction_or_enqueue_exception exc=ValueError"
    ]
    assert SECRET not in caplog.text


def _snap(pid, run_id, role, text, ts="2026-09-23T10:00:00Z"):
    return CommittedTurnSnapshot(
        committed_turn_id="",
        profile_id=pid,
        run_id=run_id,
        stream_sequence=0,
        role=role,
        source="direct",
        occurred_at=ts,
        content=text,
        is_private=False,
    )


def test_runtime_capture_disabled_drop_warns_once(tmp_path, caplog):
    rt = MemoryRuntime(db_path=tmp_path / "shadow.db", queue_maxsize=50)
    try:
        rt._capture_enabled = False
        with caplog.at_level(logging.INFO, logger="OpenCohost"):
            for _ in range(3):
                assert rt.record_turn_exchange(
                    _snap("p", rt.run_id, "user", SECRET),
                    _snap("p", rt.run_id, "assistant", SECRET),
                ) is None
        msgs = [r.getMessage() for r in _memory_v5_records(caplog, logging.WARNING)]
        assert msgs == ["memory_v5: drop reason=capture_disabled"]
        assert SECRET not in caplog.text
    finally:
        rt.shutdown(timeout=2.0)


def test_subsystem_runtime_exception_warns_type(tmp_path, caplog):
    rt = MemoryRuntime(db_path=tmp_path / "shadow.db", queue_maxsize=50)
    sub = MemorySubsystem(mode="ACTIVE", runtime=rt)
    try:
        def _explode(*a, **k):
            raise KeyError(SECRET)

        rt.record_turn_exchange = _explode
        with caplog.at_level(logging.INFO, logger="OpenCohost"):
            sub.record_turn_exchange(
                _snap("p", rt.run_id, "user", "u"), _snap("p", rt.run_id, "assistant", "a")
            )
            sub.record_turn_exchange(
                _snap("p", rt.run_id, "user", "u"), _snap("p", rt.run_id, "assistant", "a")
            )
        msgs = [r.getMessage() for r in _memory_v5_records(caplog, logging.WARNING)]
        assert msgs == [
            "memory_v5: drop reason=snapshot_construction_or_enqueue_exception exc=KeyError"
        ]
        assert SECRET not in caplog.text
    finally:
        sub.shutdown(timeout_s=2.0)


# ── episode closure -> embedding ────────────────────────────────────────────


def test_episode_closed_at_shutdown_is_embedded(temp_v5):
    shadow, cache = temp_v5
    sub = MemorySubsystem.from_settings(mode="ACTIVE")
    worker = sub.worker
    assert isinstance(worker, FakeWorker)
    rt = sub.runtime
    pid = "prof-closure"
    sub.record_turn_exchange(
        _snap(pid, rt.run_id, "user", "hablamos del puente y el faro", "2026-09-23T10:00:00Z"),
        _snap(pid, rt.run_id, "assistant", "el faro alumbra el puente", "2026-09-23T10:00:00Z"),
    )
    sub.record_turn_exchange(
        _snap(pid, rt.run_id, "user", "y el puerto de noche", "2026-09-23T10:01:00Z"),
        _snap(pid, rt.run_id, "assistant", "el puerto brilla con el faro", "2026-09-23T10:01:00Z"),
    )
    rt._queue.join()
    assert worker.embed_batch_calls == 0

    sub.shutdown(timeout_s=5.0)

    con = sqlite3.connect(str(shadow))
    closed = con.execute("SELECT COUNT(*) FROM episodes WHERE state='CLOSED'").fetchone()[0]
    con.close()
    assert closed >= 1
    assert worker.embed_batch_calls >= 1
    assert _count(cache, "episode_embeddings") >= 1
