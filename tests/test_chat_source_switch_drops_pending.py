"""Regression: a chat-source switch must drop pending source="chat" motor
items (Finding 2, judgment day 2026-08-13).

Bug: `Aggregator.on_source_changed` was wired ONLY to `ChatFeedSink.new_session`
(the display-buffer reset). Nothing told the motor to drop pending
`source="chat"` items, so a reaction queued against #canalA could pop and
speak minutes later -- after the operator switched to #canalB -- and the
Tauri UI would route that stale reply into #canalB's Stream tab.

`_started_host` mirrors the fixture in test_engine_host_chat_reaction.py
(kept local/self-contained rather than imported, per that file's own
house pattern).
"""
import queue
from unittest.mock import MagicMock

import opencohost.api.engine_host as engine_host_mod


def _started_host(tmp_path, monkeypatch):
    eh = engine_host_mod

    fake_motor = MagicMock()
    fake_motor.command_queue = queue.Queue()
    fake_motor.current_model = None
    fake_motor.is_processing = False
    fake_motor.is_speaking = False
    fake_motor.historial = []

    monkeypatch.setattr(eh, "MotorVocalIA", lambda *a, **kw: fake_motor)
    monkeypatch.setattr(eh, "HealthMonitor", lambda: MagicMock())
    monkeypatch.setattr(eh, "ObsRuntime", MagicMock())
    monkeypatch.setattr(eh, "Aggregator", MagicMock())
    monkeypatch.setattr(
        eh,
        "KiraAgendaController",
        lambda: (_ for _ in ()).throw(RuntimeError("no agenda in this test")),
    )
    monkeypatch.setattr(eh, "AgendaPersistence", MagicMock())
    monkeypatch.setattr(eh, "EditorialCardStore", MagicMock())
    monkeypatch.setattr(eh, "EditorialAgendaBridge", MagicMock())
    monkeypatch.setattr(eh, "MusicLibrary", MagicMock())
    monkeypatch.setattr(eh, "ollama", MagicMock())

    host = eh.EngineHost(lock_path=str(tmp_path / "eh.lock"))
    host._wake_ollama_eager = lambda: None  # no daemon wake thread in tests
    host.start()
    return host, fake_motor


def test_source_changed_drops_pending_chat_items(tmp_path, monkeypatch):
    """The mechanism: firing the wired on_source_changed signal (what
    Aggregator.connect() does on every channel switch) must reach the
    motor's drop_pending_sources with the "chat" prefix."""
    host, motor = _started_host(tmp_path, monkeypatch)
    try:
        host.aggregator.on_source_changed()
        motor.drop_pending_sources.assert_called_once_with(("chat",))
    finally:
        host.stop()


def test_source_changed_still_clears_the_display_feed(tmp_path, monkeypatch):
    """Pre-existing behavior (B2) must survive the fix: the feed buffer
    still resets and bumps `session` on every source change."""
    host, motor = _started_host(tmp_path, monkeypatch)
    try:
        host.chat_feed.record({"user": "a", "text": "hi", "timestamp": 1.0})
        assert host.chat_feed.since(0)["messages"]

        host.aggregator.on_source_changed()

        state = host.chat_feed.since(0)
        assert state["messages"] == []
        assert state["session"] == 1
    finally:
        host.stop()
