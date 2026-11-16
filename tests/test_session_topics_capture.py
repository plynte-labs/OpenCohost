"""T2 (session-topic-folder-20261016): settings + owner-only capture + background embed."""
from __future__ import annotations

import queue
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import opencohost.core.llm_engine as llm_engine
from opencohost.config import settings

USER = "tell me more about the vulkan renderer stutter please"
KIRA = "Sure thing. The stutter is probably shader compilation."


class FakeWorker:
    def __init__(self, vec=(1.0, 0.0, 0.0), raises=False, gate=None):
        self.calls = []
        self.blocks = []
        self.vec = list(vec) if vec is not None else None
        self.raises = raises
        self.gate = gate

    def embed_query(self, text, timeout_s=0.5, *, block=True):
        self.calls.append((text, threading.get_ident()))
        self.blocks.append(block)
        if self.gate is not None:
            self.gate.wait(5)
        if self.raises:
            raise RuntimeError("worker boom")
        return self.vec


def warm_memory(worker, fact_embeddings=3):
    """A memory stub whose profile has fact embeddings, i.e. one where the
    turn's recall CAN produce a query vector (the F3 capture precondition)."""
    return SimpleNamespace(
        runtime=None,
        run_id=None,
        worker=worker,
        fact_index=SimpleNamespace(
            cache_store=SimpleNamespace(count_fact_embeddings=lambda pid: fact_embeddings)
        ),
    )


def _motor(worker=None):
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda e: None)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor._current_profile_id = "profile-1"
    motor._memory = warm_memory(worker)
    return motor


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_TOPICS_ENABLED", True)
    monkeypatch.setattr(settings, "MEMORY_SEMANTIC_ENABLED", True)


def _topics(motor):
    motor._session_topics_flush()
    idx = getattr(motor, "_session_topics", None)
    return idx.snapshot() if idx is not None else ()


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", True),
        ("garbage", True),
        ("on", True),
        ("ON", True),
        ("1", True),
        ("true", True),
        ("off", False),
        ("OFF", False),
        ("0", False),
        ("false", False),
        ("no", False),
    ],
)
def test_env_resolution_defaults_on(monkeypatch, raw, expected):
    monkeypatch.setenv("OPENCOHOST_SESSION_TOPICS", raw)
    assert settings._resolve_session_topics() is expected


def test_env_resolution_unset_is_on(monkeypatch):
    monkeypatch.delenv("OPENCOHOST_SESSION_TOPICS", raising=False)
    assert settings._resolve_session_topics() is True


def test_threshold_settings_exist():
    assert 0 < settings.SESSION_TOPICS_ASSIGN_THRESHOLD < settings.SESSION_TOPICS_RETRIEVE_THRESHOLD < 1
    assert settings.SESSION_TOPICS_MIN_WORDS == 4
    assert settings.SESSION_TOPICS_MAX_TOPICS == 24


def test_flag_off_never_embeds(monkeypatch):
    monkeypatch.setattr(settings, "SESSION_TOPICS_ENABLED", False)
    worker = FakeWorker()
    motor = _motor(worker)
    motor._commit_history(USER, KIRA, source="direct")
    assert _topics(motor) == ()
    assert worker.calls == []


def test_owner_turn_is_captured_off_the_calling_thread(on):
    worker = FakeWorker()
    motor = _motor(worker)
    motor._commit_history(USER, KIRA, source="direct")
    assert len(_topics(motor)) == 1
    assert len(worker.calls) == 1
    assert worker.calls[0][1] != threading.get_ident()


@pytest.mark.parametrize("source", ["ptt", "owner-bundle"])
def test_other_owner_sources_are_captured(on, source):
    motor = _motor(FakeWorker())
    motor._commit_history(USER, KIRA, source=source)
    assert len(_topics(motor)) == 1


@pytest.mark.parametrize("source", ["chat", "kira-agenda-intro"])
def test_viewer_chat_and_agenda_are_never_captured(on, source):
    worker = FakeWorker()
    motor = _motor(worker)
    motor._commit_history(USER, KIRA, source=source)
    assert _topics(motor) == ()
    assert worker.calls == []


def test_private_turn_is_never_captured(on):
    worker = FakeWorker()
    motor = _motor(worker)
    motor._memorias_private = True
    motor._commit_history(USER, KIRA, source="direct")
    assert _topics(motor) == ()
    assert worker.calls == []


def test_trivial_turn_skips_the_embed(on):
    worker = FakeWorker()
    motor = _motor(worker)
    motor._commit_history("ok thanks", "np", source="direct")
    assert _topics(motor) == ()
    assert worker.calls == []


def test_semantic_off_skips_capture(on, monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_SEMANTIC_ENABLED", False)
    worker = FakeWorker()
    motor = _motor(worker)
    motor._commit_history(USER, KIRA, source="direct")
    assert _topics(motor) == ()
    assert worker.calls == []


def test_no_worker_is_a_silent_skip(on):
    motor = _motor(None)
    motor._commit_history(USER, KIRA, source="direct")
    assert _topics(motor) == ()


@pytest.mark.parametrize("worker", [FakeWorker(raises=True), FakeWorker(vec=None)])
def test_worker_failure_never_breaks_the_turn(on, worker):
    motor = _motor(worker)
    motor._commit_history(USER, KIRA, source="direct")
    assert len(motor.historial) == 2
    assert _topics(motor) == ()


def test_slow_worker_does_not_block_the_commit(on):
    gate = threading.Event()
    worker = FakeWorker(gate=gate)
    motor = _motor(worker)
    done = threading.Event()

    def run():
        motor._commit_history(USER, KIRA, source="direct")
        done.set()

    threading.Thread(target=run, daemon=True).start()
    assert done.wait(2.0), "commit blocked on the embed"
    gate.set()
    assert len(_topics(motor)) == 1


def test_reset_clears_the_index(on):
    motor = _motor(FakeWorker())
    motor._commit_history(USER, KIRA, source="direct")
    assert len(_topics(motor)) == 1
    motor._session_topics_reset()
    assert _topics(motor) == ()


def test_blocked_guardrail_line_is_not_captured(on):
    worker = FakeWorker()
    motor = _motor(worker)
    line = next(iter(llm_engine.i18n_active.guardrail_fallback_lines()))
    motor._commit_history(USER, line, source="direct")
    assert _topics(motor) == ()
