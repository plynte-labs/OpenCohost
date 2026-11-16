"""T4 (session-topic-folder-20261016): the debounced, best-effort session.md view."""
from __future__ import annotations

import os
import queue
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import opencohost.core.llm_engine as llm_engine
from opencohost.config import settings
from opencohost.core.context.session_topics import SessionTopicIndex, render_session_markdown

USER = "tell me more about the vulkan renderer stutter please and thanks a lot"
KIRA = "Sure thing. The stutter is probably shader compilation."


class FakeWorker:
    def embed_query(self, text, timeout_s=0.5, *, block=True):
        return [1.0, 0.0, 0.0]


@pytest.fixture
def view(monkeypatch, tmp_path):
    path = tmp_path / "data" / "session_topics" / "session.md"
    monkeypatch.setattr(settings, "SESSION_TOPICS_ENABLED", True)
    monkeypatch.setattr(settings, "MEMORY_SEMANTIC_ENABLED", True)
    monkeypatch.setattr(settings, "SESSION_TOPICS_VIEW_FILE", str(path))
    monkeypatch.setattr(settings, "SESSION_TOPICS_VIEW_DEBOUNCE_S", 30.0)
    return path


def _motor():
    m = llm_engine.MotorVocalIA(queue.Queue(), lambda e: None)
    m.ollama = MagicMock()
    m.pygame = MagicMock()
    m.is_ready = True
    m._current_profile_id = "profile-1"
    m._memory = SimpleNamespace(
        runtime=None,
        run_id=None,
        worker=FakeWorker(),
        fact_index=SimpleNamespace(cache_store=SimpleNamespace(count_fact_embeddings=lambda pid: 3)),
    )
    return m


def _commit(m, user=USER, kira=KIRA):
    m._commit_history(user, kira, source="direct")
    m._session_topics_flush()


def test_render_is_compact_and_has_no_vectors_or_keys():
    idx = SessionTopicIndex()
    idx.add_fragment([1.0, 0.0], USER, "Sure thing", 1)
    idx.add_fragment([0.0, 1.0], "what should we cook for dinner tonight friend", "Pasta", 2)
    md = render_session_markdown(idx.snapshot(), generated_at="2026-10-16T10:00:00Z")
    assert md.startswith("# Session topics")
    assert "2026-10-16T10:00:00Z" in md
    assert md.count("## Topic ") == 2
    assert "tell me more about the vulkan renderer stutter" in md
    assert "tell me more about the vulkan renderer stutter please" not in md  # 8-word cap
    assert "centroid" not in md and "1.0" not in md


def test_render_empty_snapshot_says_so():
    assert "No topics yet" in render_session_markdown((), generated_at="t")


def test_capture_writes_the_view_on_flush(view):
    m = _motor()
    _commit(m)
    assert not view.exists()  # debounced: nothing yet
    m._session_topics_view_flush()
    text = view.read_text(encoding="utf-8")
    assert "## Topic 1" in text and "Sure thing" in text


def test_debounce_coalesces_many_captures_into_one_write(view, monkeypatch):
    writes = []
    import opencohost.core.engine.llm_engine_memorias as mod

    real = mod.atomic_write_text
    monkeypatch.setattr(mod, "atomic_write_text", lambda p, t: (writes.append(1), real(p, t))[1])
    m = _motor()
    for i in range(5):
        _commit(m, user=f"vulkan renderer stutter question number {i} right now", kira=f"Answer {i}.")
    assert writes == []
    m._session_topics_view_flush()
    assert len(writes) == 1


def test_trailing_write_happens_after_the_debounce_window(view, monkeypatch):
    monkeypatch.setattr(settings, "SESSION_TOPICS_VIEW_DEBOUNCE_S", 0.05)
    m = _motor()
    _commit(m)
    for _ in range(100):
        if view.exists():
            break
        threading.Event().wait(0.05)
    assert view.exists()


def test_write_failure_never_breaks_capture_or_turn(view, monkeypatch):
    import opencohost.core.engine.llm_engine_memorias as mod

    def boom(p, t):
        raise OSError("disk full")

    monkeypatch.setattr(mod, "atomic_write_text", boom)
    m = _motor()
    _commit(m)
    m._session_topics_view_flush()  # must not raise
    assert len(m.historial) == 2
    assert len(m._session_topics.snapshot()) == 1


def test_flag_off_writes_nothing(view, monkeypatch):
    monkeypatch.setattr(settings, "SESSION_TOPICS_ENABLED", False)
    m = _motor()
    _commit(m)
    m._session_topics_view_flush()
    assert not view.exists()


def test_reset_removes_the_view(view):
    m = _motor()
    _commit(m)
    m._session_topics_view_flush()
    assert view.exists()
    m._session_topics_reset()
    m._session_topics_view_flush()
    assert not view.exists()


def test_view_never_holds_private_viewer_or_agenda_turns(view):
    m = _motor()
    m._memorias_private = True
    _commit(m)
    m._memorias_private = False
    m._commit_history(USER, KIRA, source="chat")
    m._commit_history(USER, KIRA, source="kira-agenda-x")
    m._session_topics_flush()
    m._session_topics_view_flush()
    assert not view.exists()


def test_default_view_path_is_under_data_and_gitignored():
    # conftest redirects the setting for isolation; rebuild the module's real default.
    path = Path(os.path.join(str(settings.USER_DATA_DIR), "data", "session_topics", "session.md"))
    assert path.name == "session.md" and "session_topics" in path.parts
    repo = Path(__file__).resolve().parent.parent
    try:
        rel = path.resolve().relative_to(repo)
    except ValueError:
        pytest.skip("USER_DATA_DIR is outside the repo (frozen/explicit root)")
    result = subprocess.run(["git", "check-ignore", "-q", str(rel)], cwd=repo, capture_output=True)
    assert result.returncode == 0, "session.md must be gitignored"
