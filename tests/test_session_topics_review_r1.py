"""Review round 1 (session-topic-folder-20261016, T5): fixes F1-F8."""
from __future__ import annotations

import dataclasses
import json
import logging
import math
import queue
import threading
import time
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import opencohost.core.llm_engine as llm_engine
from opencohost.config import settings
from opencohost.core.context.session_topics import SessionTopicIndex, fragment_key
from opencohost.core.memory import episodic_merge
from opencohost.core.memory.memory_digest import MemoryDigest
from opencohost.core.memory_v5_shadow.semantic_worker import SemanticWorkerService
from tests.test_session_topics_capture import FakeWorker, warm_memory

VEC = [1.0, 0.0, 0.0]
USER = "tell me more about the vulkan renderer stutter please"
OTHER = "what should we cook for dinner tonight my friend"
KIRA = "Sure thing. The stutter is probably shader compilation."


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_TOPICS_ENABLED", True)
    monkeypatch.setattr(settings, "MEMORY_SEMANTIC_ENABLED", True)
    monkeypatch.setattr(settings, "MEMORY_FACTS_ENABLED", True)


@pytest.fixture
def view_file(monkeypatch, tmp_path):
    path = tmp_path / "data" / "session_topics" / "session.md"
    monkeypatch.setattr(settings, "SESSION_TOPICS_VIEW_FILE", str(path))
    monkeypatch.setattr(settings, "SESSION_TOPICS_VIEW_DEBOUNCE_S", 30.0)
    return path


def _motor(worker=None, *, memory=None):
    m = llm_engine.MotorVocalIA(queue.Queue(), lambda e: None)
    m.current_model = "llama3"
    m.use_system_role = True
    m.ollama = MagicMock()
    m.pygame = MagicMock()
    m.is_ready = True
    m._current_profile_id = "profile-1"
    m._memory = memory if memory is not None else warm_memory(worker)
    return m


def _recall(vec=VEC):
    return dataclasses.replace(episodic_merge.RecallOutcome.disabled(), fact_query_vec=vec)


def _topics(m):
    m._session_topics_flush()
    idx = getattr(m, "_session_topics", None)
    return idx.snapshot() if idx is not None else ()


# ---------------------------------------------------------------- F1 ----

def test_digest_tracks_keys_in_fifo_order():
    d = MemoryDigest(max_chars=30)
    d.append("a" * 12, key="k1")
    d.append("b" * 12, key="k2")
    assert d.keys == {"k1", "k2"}
    d.append("c" * 12, key="k3")  # FIFO evicts the oldest line and its key
    assert d.keys == {"k2", "k3"}
    d.append("x")  # keyless callers are unchanged
    assert d.keys == {"k2", "k3"}
    d.clear()
    assert d.keys == frozenset()


def test_eviction_records_the_fragment_key_in_the_digest(on):
    m = _motor(FakeWorker())
    m.historial = deque(maxlen=2)
    m._commit_history(USER, KIRA, source="direct")
    m._commit_history(OTHER, KIRA, source="direct")
    assert fragment_key(USER) in m._memory_digest.keys


def test_pairs_still_in_the_digest_are_not_reinjected(on):
    m = _motor(FakeWorker())
    m._session_topics_index().add_fragment(VEC, USER, "Shaders.", 1)
    assert "<session_topics>" in m._session_topics_block("direct", _recall(), [])
    m._memory_digest.append("ledger line", key=fragment_key(USER))
    assert m._session_topics_block("direct", _recall(), []) == ""
    m._memory_digest.clear()
    assert "<session_topics>" in m._session_topics_block("direct", _recall(), [])


# ---------------------------------------------------------------- F2 ----

def test_worker_nonblocking_call_never_waits_for_a_busy_lock():
    svc = SemanticWorkerService(use_dummy=True)
    assert svc._call_lock.acquire(blocking=False)
    try:
        started = time.monotonic()
        out = svc.embed_query("x", timeout_s=2.0, block=False)
        assert out is None
        assert time.monotonic() - started < 0.5
    finally:
        svc._call_lock.release()


def test_worker_default_call_still_waits_for_the_lock_until_its_timeout():
    svc = SemanticWorkerService(use_dummy=True)
    assert svc._call_lock.acquire(blocking=False)
    try:
        started = time.monotonic()
        assert svc.embed_query("x", timeout_s=0.3) is None
        assert time.monotonic() - started >= 0.25
    finally:
        svc._call_lock.release()


def test_worker_nonblocking_call_runs_when_the_lock_is_free(monkeypatch):
    svc = SemanticWorkerService(use_dummy=True)
    monkeypatch.setattr(svc, "_call_locked", lambda method, args, timeout_s: [1.0])
    assert svc.embed_query("x", block=False) == [1.0]
    assert not svc._call_lock.locked()


def test_commit_reuses_the_turns_recall_vector(on):
    worker = FakeWorker(vec=(0.0, 1.0, 0.0))
    m = _motor(worker)
    # First turn: the index is still empty, yet the turn's vector is stashed.
    assert m._session_topics_block("direct", _recall(VEC), [], query_text=USER) == ""
    m._commit_history(USER, KIRA, source="direct")
    snap = _topics(m)
    assert worker.calls == []
    assert len(snap) == 1 and list(snap[0]["centroid"]) == VEC


def test_a_stashed_vector_is_not_reused_for_a_different_text(on):
    worker = FakeWorker()
    m = _motor(worker)
    m._session_topics_block("direct", _recall(VEC), [], query_text=OTHER)
    m._commit_history(USER, KIRA, source="direct")
    _topics(m)
    assert len(worker.calls) == 1


def test_a_stashed_vector_is_consumed_once(on):
    worker = FakeWorker()
    m = _motor(worker)
    m._session_topics_block("direct", _recall(VEC), [], query_text=USER)
    m._commit_history(USER, KIRA, source="direct")
    m._commit_history(USER, KIRA, source="direct")
    _topics(m)
    assert len(worker.calls) == 1


def test_fallback_embed_never_waits_for_the_worker_lock(on):
    worker = FakeWorker()
    m = _motor(worker)
    m._commit_history(USER, KIRA, source="direct")
    _topics(m)
    assert worker.blocks == [False]


def test_busy_worker_means_a_skipped_fragment_not_a_wait(on):
    class Busy(FakeWorker):
        def embed_query(self, text, timeout_s=0.5, *, block=True):
            super().embed_query(text, timeout_s, block=block)
            return None if not block else self.vec

    worker = Busy()
    m = _motor(worker)
    m._commit_history(USER, KIRA, source="direct")
    assert _topics(m) == ()
    assert len(m.historial) == 2


# ---------------------------------------------------------------- F3 ----

def test_no_fallback_embed_when_facts_are_disabled(on, monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_FACTS_ENABLED", False)
    worker = FakeWorker()
    m = _motor(worker)
    m._commit_history(USER, KIRA, source="direct")
    assert _topics(m) == () and worker.calls == []


def test_no_fallback_embed_for_a_cold_profile(on):
    worker = FakeWorker()
    m = _motor(memory=warm_memory(worker, fact_embeddings=0))
    m._commit_history(USER, KIRA, source="direct")
    assert _topics(m) == () and worker.calls == []


def test_no_fallback_embed_without_a_fact_index(on):
    worker = FakeWorker()
    m = _motor(memory=SimpleNamespace(runtime=None, run_id=None, worker=worker))
    m._commit_history(USER, KIRA, source="direct")
    assert _topics(m) == () and worker.calls == []


def test_no_fallback_embed_without_a_profile(on):
    worker = FakeWorker()
    m = _motor(worker)
    m._current_profile_id = None
    m._commit_history(USER, KIRA, source="direct")
    assert _topics(m) == () and worker.calls == []


def test_settings_comment_names_the_real_gate():
    src = Path(settings.__file__).read_text(encoding="utf-8")
    flat = " ".join(src.replace("# ", " ").split())
    assert "Needs MEMORY_SEMANTIC_ENABLED and MEMORY_FACTS_ENABLED" in flat


# ---------------------------------------------------------------- F4 ----

def test_startup_removes_a_stale_view_file(on, view_file):
    view_file.parent.mkdir(parents=True)
    view_file.write_text("# stale", encoding="utf-8")
    _motor(FakeWorker())
    assert not view_file.exists()


def test_startup_removes_a_stale_view_file_with_the_flag_off(monkeypatch, view_file):
    monkeypatch.setattr(settings, "SESSION_TOPICS_ENABLED", False)
    view_file.parent.mkdir(parents=True)
    view_file.write_text("# stale", encoding="utf-8")
    _motor(FakeWorker())
    assert not view_file.exists()


def test_flag_off_view_write_deletes_the_file(monkeypatch, view_file):
    monkeypatch.setattr(settings, "SESSION_TOPICS_ENABLED", False)
    m = _motor(FakeWorker())
    view_file.parent.mkdir(parents=True)
    view_file.write_text("# stale", encoding="utf-8")
    m._session_topics_view_write()
    assert not view_file.exists()


def test_reset_session_topics_clears_the_index_and_the_file(on, view_file):
    m = _motor(FakeWorker())
    m._commit_history(USER, KIRA, source="direct")
    _topics(m)
    m._session_topics_view_flush()
    assert view_file.exists()
    m.reset_session_topics()
    assert not view_file.exists()
    assert _topics(m) == ()


def test_reset_session_topics_never_raises(on, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "SESSION_TOPICS_VIEW_FILE", str(tmp_path))  # a directory: unlink fails
    m = _motor(FakeWorker())
    m._commit_history(USER, KIRA, source="direct")
    _topics(m)
    m.reset_session_topics()
    assert _topics(m) == ()


def test_clear_history_calls_the_reset(on):
    m = _motor(FakeWorker())
    spy = MagicMock()
    m._session_topics_reset = spy
    m._dispatch_command("clear_history", None)
    spy.assert_called_once()


def test_set_profile_calls_the_reset(on, monkeypatch):
    m = _motor(FakeWorker())
    spy = MagicMock()
    m._session_topics_reset = spy
    m._memory.on_profile_switch = lambda *a, **k: None
    monkeypatch.setattr(m, "_dispatch_switch_flush", lambda *a, **k: None)
    m._dispatch_command("set_profile", {"id": "profile-2", "prompt": "x", "_profile_name": "p2"})
    spy.assert_called_once()


def _api_app(spy):
    import opencohost.api.main as main_mod
    from tests.test_api_phase1 import FakeHost

    def factory():
        host = FakeHost()
        host.motor.reset_session_topics = spy
        return host

    return main_mod.create_app(host_factory=factory, cors_origins=["http://localhost:5173"])


@pytest.fixture
def api_env(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod
    import opencohost.core.profiles.profiles as profiles_mod
    from tests.test_api_memoria_mutations import _enable, _seed_memorias_db

    main_mod._host_active = False
    main_mod._memoria_store = None
    db_path = tmp_path / "memorias.db"
    _seed_memorias_db(db_path)
    _enable(monkeypatch, db_path)
    pfile = tmp_path / "perfiles.json"
    pfile.write_text(
        json.dumps({"a": {"id": "ia", "prompt": "p", "use_system": True}, "b": {"id": "ib", "prompt": "p", "use_system": True}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(profiles_mod, "PROFILES_FILE", str(pfile))
    yield
    main_mod._host_active = False
    main_mod._memoria_store = None


def test_memoria_purge_resets_session_topics(api_env):
    from fastapi.testclient import TestClient

    spy = MagicMock()
    with TestClient(_api_app(spy)) as client:
        assert client.post("/api/memoria/purge", json={"profile_id": "default"}).status_code == 200
    spy.assert_called()


def test_memoria_delete_resets_session_topics(api_env):
    from fastapi.testclient import TestClient

    spy = MagicMock()
    with TestClient(_api_app(spy)) as client:
        resp = client.post("/api/memoria/delete", json={"profile_id": "default", "id": "mem_a"})
        assert resp.status_code == 200
    spy.assert_called()


def test_profile_delete_resets_session_topics(api_env):
    from fastapi.testclient import TestClient

    spy = MagicMock()
    with TestClient(_api_app(spy)) as client:
        assert client.delete("/api/perfiles/b").status_code == 200
    spy.assert_called()


# ---------------------------------------------------------------- F5 ----

def test_stale_generation_is_rejected_inside_add_fragment():
    idx = SessionTopicIndex()
    gen = idx.generation
    idx.reset()
    assert idx.generation != gen
    assert idx.add_fragment(VEC, USER, "k", 1, generation=gen) is None
    assert idx.snapshot() == ()
    assert idx.add_fragment(VEC, USER, "k", 2, generation=idx.generation) is not None
    assert idx.add_fragment(VEC, OTHER, "k", 3) is not None  # no generation: unchanged


def test_in_flight_embed_during_reset_is_dropped(on):
    gate = threading.Event()
    worker = FakeWorker(gate=gate)
    m = _motor(worker)
    m._commit_history(USER, KIRA, source="direct")
    deadline = time.monotonic() + 2
    while not worker.calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert worker.calls, "embed never started"
    m._session_topics_reset()
    gate.set()
    assert _topics(m) == ()


# ---------------------------------------------------------------- F6 ----

def test_pending_counter_is_not_leaked_when_submit_fails(on):
    m = _motor(FakeWorker())
    m._session_topics_index()

    def boom(*a, **k):
        raise RuntimeError("pool is shut down")

    m._session_topics_executor = SimpleNamespace(submit=boom)
    m._commit_history(USER, KIRA, source="direct")
    assert m._session_topics_pending == 0
    assert len(m.historial) == 2


def test_pending_cap_drops_overflow(on):
    gate = threading.Event()
    worker = FakeWorker(gate=gate)
    m = _motor(worker)
    for i in range(12):
        m._commit_history(f"{USER} number {i}", KIRA, source="direct")
    assert m._session_topics_pending == 8
    gate.set()
    _topics(m)
    assert len(worker.calls) == 8
    assert m._session_topics_pending == 0


def test_timer_is_stored_only_after_start_succeeds(on, monkeypatch):
    class BadTimer:
        def __init__(self, *a, **k):
            self.daemon = False

        def start(self):
            raise RuntimeError("can't start new thread")

    m = _motor(FakeWorker())
    monkeypatch.setattr(threading, "Timer", BadTimer)
    m._session_topics_view_schedule()
    assert getattr(m, "_session_topics_view_timer", None) is None
    monkeypatch.undo()


def test_teardown_flushes_the_view_and_stops_the_executor(on, view_file):
    m = _motor(FakeWorker())
    m._commit_history(USER, KIRA, source="direct")
    _topics(m)
    assert not view_file.exists()  # debounce window still open
    m._session_topics_shutdown()
    assert view_file.exists()
    assert m._session_topics_executor is None
    m._commit_history(OTHER, KIRA, source="direct")  # a late commit never raises
    assert len(m.historial) == 4


def test_flush_memorias_runs_the_session_topics_teardown(on):
    m = _motor(FakeWorker())
    spy = MagicMock()
    m._session_topics_shutdown = spy
    m.flush_memorias()
    spy.assert_called_once()


# ---------------------------------------------------------------- F7 ----

def test_centroid_is_compared_as_a_unit_vector():
    idx = SessionTopicIndex()
    idx.add_fragment([3.0, 0.0, 0.0], USER, "k", 1)
    idx.add_fragment([2.0, 0.2, 0.0], USER + " again", "k", 2)
    topic = idx._topics[0]
    assert math.isclose(math.sqrt(sum(x * x for x in topic.unit)), 1.0, rel_tol=1e-9)
    assert topic.sum_vec[0] == pytest.approx(5.0)  # the running sum is kept
    q = [1.0, 0.1, 0.0]
    hit = idx.query(q)
    s = topic.sum_vec
    expected = sum(a * b for a, b in zip(s, q)) / (math.sqrt(sum(a * a for a in s)) * math.sqrt(sum(b * b for b in q)))
    assert hit is not None and hit.score == pytest.approx(expected)


def test_count_accessors_are_cheap_and_correct():
    idx = SessionTopicIndex()
    assert (idx.topic_count(), idx.fragment_count()) == (0, 0)
    idx.add_fragment(VEC, USER, "k", 1)
    idx.add_fragment(VEC, USER + " again", "k", 2)
    idx.add_fragment([0.0, 1.0, 0.0], OTHER, "k", 3)
    assert (idx.topic_count(), idx.fragment_count()) == (2, 3)


def test_retrieval_never_snapshots_the_index_per_turn(on):
    m = _motor(FakeWorker())
    idx = m._session_topics_index()
    idx.add_fragment(VEC, USER, "Shaders.", 1)

    def boom():
        raise AssertionError("snapshot() on the turn path")

    idx.snapshot = boom
    assert "<session_topics>" in m._session_topics_block("direct", _recall(), [])


def test_dropped_block_logs_dropped_not_hit(on, caplog):
    m = _motor(FakeWorker())
    m._session_topics_index().add_fragment(VEC, USER, "Shaders.", 1)
    with caplog.at_level(logging.INFO):
        out = m._session_topics_block("direct", _recall(), [], compact=True)
    text = " ".join(r.getMessage() for r in caplog.records if "[SESSION_TOPICS]" in r.getMessage())
    assert out == ""
    assert "result=dropped" in text and "result=hit" not in text


def test_compact_profile_turn_logs_dropped_end_to_end(on, caplog, monkeypatch):
    m = _motor(FakeWorker())
    m._session_topics_index().add_fragment(VEC, "the vulkan renderer stutters on the main scene", "Shaders.", 1)
    m._run_episodic_recall = lambda mem, text, pid: _recall()
    monkeypatch.setattr(m, "_resolve_prompt_profile", lambda *a, **k: SimpleNamespace(profile="compact"))
    kw = dict(is_local=True, provider_cfg={}, request_model="llama3", watchdog_timeout=None)
    with caplog.at_level(logging.INFO):
        setup = m._build_generation_request("vulkan renderer stutters again", "direct", **kw)
    text = " ".join(r.getMessage() for r in caplog.records if "[SESSION_TOPICS]" in r.getMessage())
    assert "result=dropped" in text and "result=hit" not in text
    assert "<session_topics>" not in json.dumps(setup.messages)


# ---------------------------------------------------------------- F8 ----

def _build_messages(monkeypatch, layout, *, flag, populated, bypass):
    monkeypatch.setattr(settings, "PROMPT_LAYOUT", layout)
    monkeypatch.setattr(settings, "SESSION_TOPICS_ENABLED", flag)
    monkeypatch.setattr(settings, "MEMORY_SEMANTIC_ENABLED", True)
    m = _motor(FakeWorker())
    for u in ("first old question about the renderer", "second question about shaders"):
        m.historial.append({"role": "user", "content": u, "source": "direct"})
        m.historial.append({"role": "assistant", "content": "ok.", "source": "direct"})
    if populated:
        m._session_topics_index().add_fragment(VEC, "the vulkan renderer stutters on the main scene", "Shaders.", 1)
    if bypass:  # the base code shape: no session-topics entry point at all
        m._session_topics_block = lambda *a, **k: ""
        m._session_topics_submit = lambda *a, **k: None
    m._run_episodic_recall = lambda mem, text, pid: _recall()
    m._clock_kwargs = lambda *a, **k: {}
    kw = dict(is_local=True, provider_cfg={}, request_model="llama3", watchdog_timeout=None)
    setup = m._build_generation_request("vulkan renderer stutters again", "direct", **kw)
    return json.dumps(setup.messages, sort_keys=True, ensure_ascii=False)


@pytest.mark.parametrize("layout", ["legacy", "v2"])
def test_flag_off_prompt_is_byte_identical_to_the_feature_bypassed(monkeypatch, layout):
    base = _build_messages(monkeypatch, layout, flag=False, populated=False, bypass=True)
    off = _build_messages(monkeypatch, layout, flag=False, populated=True, bypass=False)
    assert off == base
    assert "<session_topics>" not in off


@pytest.mark.parametrize("layout", ["legacy", "v2"])
def test_flag_on_with_an_empty_index_is_byte_identical_to_the_feature_bypassed(monkeypatch, layout):
    base = _build_messages(monkeypatch, layout, flag=False, populated=False, bypass=True)
    on_empty = _build_messages(monkeypatch, layout, flag=True, populated=False, bypass=False)
    assert on_empty == base


@pytest.mark.parametrize("layout", ["legacy", "v2"])
def test_flag_on_with_a_populated_index_differs_only_by_the_block(monkeypatch, layout):
    base = _build_messages(monkeypatch, layout, flag=False, populated=False, bypass=True)
    on = _build_messages(monkeypatch, layout, flag=True, populated=True, bypass=False)
    assert on != base and "<session_topics>" in on
