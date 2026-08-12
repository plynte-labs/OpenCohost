"""Tests for ChatFeedSink (RF3 -- GET /api/stream/chat-live/messages,
conductor/tracks/tauri_stream_chat_20260812/proposal.md 5).

ChatFeedSink is `EngineHost.chat_feed`, wired directly as
`Aggregator.on_filtered_message` at construction time. It only ever receives
messages that already survived MessageFilter, the spam gate, and the input
safety floor (`runtime_screen`, design.md 3.1/8) -- see
TestInputSafetyFloor in tests/test_smart_aggregator.py for that guarantee.
This file covers the sink itself (shape mirrors EventLogSink, see
tests/test_api_events.py) plus the EngineHost wiring.
"""

import threading
from unittest.mock import MagicMock

import opencohost.api.engine_host as engine_host_mod


# ──────────────────────────────────────────────────────────────────────────
# ChatFeedSink -- pure unit (seq monotonicity, since() filtering, backfill)
# ──────────────────────────────────────────────────────────────────────────


def test_chat_feed_sink_seq_monotonic_and_since_filters_correctly():
    sink = engine_host_mod.ChatFeedSink()

    empty = sink.since(0)
    assert empty["messages"] == []
    assert empty["cursor"] == 0

    sink.record({"user": "viewer1", "text": "hola", "timestamp": 111.0})
    sink.record({"user": "viewer2", "text": "que tal", "timestamp": 222.0})

    backfill = sink.since(0)  # since=0 -- full backfill
    assert [m["seq"] for m in backfill["messages"]] == [1, 2]
    assert [m["author"] for m in backfill["messages"]] == ["viewer1", "viewer2"]
    assert [m["text"] for m in backfill["messages"]] == ["hola", "que tal"]
    assert backfill["cursor"] == 2

    only_new = sink.since(1)  # cursor filtering -- only seq > 1
    assert [m["seq"] for m in only_new["messages"]] == [2]

    caught_up = sink.since(2)
    assert caught_up["messages"] == []
    assert caught_up["cursor"] == 2  # cursor never regresses


def test_chat_feed_sink_ring_cap_evicts_oldest():
    sink = engine_host_mod.ChatFeedSink(maxlen=5)
    for i in range(8):
        sink.record({"user": f"u{i}", "text": f"msg{i}", "timestamp": float(i)})

    result = sink.since(0)
    assert [m["seq"] for m in result["messages"]] == [4, 5, 6, 7, 8]
    assert [m["author"] for m in result["messages"]] == ["u3", "u4", "u5", "u6", "u7"]
    assert result["cursor"] == 8  # cursor is not rolled back by eviction


def test_chat_feed_sink_thread_safety_smoke():
    """Smoke-tests the lock: concurrent record() calls from many threads must
    not lose updates or hand out duplicate/out-of-order seq numbers."""
    sink = engine_host_mod.ChatFeedSink(maxlen=1000)

    def _hammer():
        for _ in range(50):
            sink.record({"user": "u", "text": "t", "timestamp": 1.0})

    threads = [threading.Thread(target=_hammer) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    seqs = [m["seq"] for m in sink.since(0)["messages"]]
    assert len(seqs) == 500
    assert seqs == list(range(1, 501))  # no duplicates, no gaps, strictly increasing


def test_chat_feed_sink_restart_resets_seq_and_changes_boot(monkeypatch):
    monkeypatch.setattr(engine_host_mod.time, "time", lambda: 1000.0)
    sink1 = engine_host_mod.ChatFeedSink()
    sink1.record({"user": "u", "text": "t", "timestamp": 1.0})
    assert sink1.since(0)["cursor"] == 1
    assert sink1.boot == 1000.0

    monkeypatch.setattr(engine_host_mod.time, "time", lambda: 2000.0)
    sink2 = engine_host_mod.ChatFeedSink()  # new process boot
    assert sink2.boot == 2000.0
    assert sink2.boot != sink1.boot
    assert sink2.since(0)["cursor"] == 0  # seq reset


def test_chat_feed_sink_record_stores_only_render_fields():
    """PRIVACY: only author/text/ts/seq may reach the ring -- no quality
    score or other filter-internal keys, even if present on the message."""
    sink = engine_host_mod.ChatFeedSink()

    sink.record({"user": "viewer1", "text": "hola", "timestamp": 111.0, "quality": 0.9})

    [stored] = sink.since(0)["messages"]
    assert set(stored.keys()) == {"seq", "author", "text", "ts"}


def test_chat_feed_sink_record_falls_back_to_wall_clock_when_timestamp_missing(monkeypatch):
    monkeypatch.setattr(engine_host_mod.time, "time", lambda: 555.0)
    sink = engine_host_mod.ChatFeedSink()

    sink.record({"user": "viewer1", "text": "hola"})  # no "timestamp" key

    [stored] = sink.since(0)["messages"]
    assert stored["ts"] == 555.0


# ──────────────────────────────────────────────────────────────────────────
# EngineHost wiring (B2) -- chat_feed exists pre-start, callback is wired
# ──────────────────────────────────────────────────────────────────────────


def test_engine_host_constructs_chat_feed_before_start(tmp_path):
    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "eh.lock"))
    assert isinstance(host.chat_feed, engine_host_mod.ChatFeedSink)


def test_engine_host_wires_chat_feed_record_as_on_filtered_message(tmp_path, monkeypatch):
    """B2: at Aggregator construction time, on_filtered_message must be
    assigned directly to chat_feed.record (no lambda/wrapper indirection)."""
    eh = engine_host_mod

    def _fake_motor(log_queue, ui_callback=None, dialogue_callback=None):
        m = MagicMock()
        m.command_queue = __import__("queue").Queue()
        m.current_model = None
        return m

    monkeypatch.setattr(eh, "MotorVocalIA", _fake_motor)
    monkeypatch.setattr(eh, "HealthMonitor", lambda: MagicMock())
    monkeypatch.setattr(eh, "ObsRuntime", MagicMock())
    monkeypatch.setattr(eh, "Aggregator", MagicMock())
    monkeypatch.setattr(eh, "KiraAgendaController", MagicMock())
    monkeypatch.setattr(eh, "AgendaPersistence", MagicMock())
    monkeypatch.setattr(eh, "MusicLibrary", MagicMock())
    monkeypatch.setattr(eh, "ollama", MagicMock())

    host = eh.EngineHost(lock_path=str(tmp_path / "eh.lock"))
    host._wake_ollama_eager = lambda: None  # no daemon wake thread in tests
    host.start()
    try:
        assert host.aggregator is not None
        assert host.aggregator.on_filtered_message == host.chat_feed.record
    finally:
        host.stop()
