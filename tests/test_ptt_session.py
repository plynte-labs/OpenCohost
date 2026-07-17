"""Unit tests for the headless PttSession (liveaudio_ptt_tauri_20260710, WU1).

STRICT TDD, RED first. The WhisperLive STT server is ALWAYS mocked — these
tests never open a real socket. `websockets.connect` is monkeypatched with a
fake async context manager whose ``recv()`` drains canned ``{"text": ...}``
frames from an in-memory deque the test feeds directly.

PRIVACY (hard rule 2): the operator transcript is host speech. It may never be
persisted. The final test drives a full mocked session with a canned sentinel
phrase and asserts the phrase is absent from EVERY log record — PttSession logs
char counts and state transitions ONLY, never previews (deliberately stricter
than CTK voice_control.py's ``[:30]`` truncated previews).
"""

import collections
import json
import logging
import threading
import time

import pytest
import websockets

import opencohost.api.ptt_session as ptt_session_mod
from opencohost.api.ptt_session import PttSession, PttUnreachable


# ──────────────────────────────────────────────────────────────────────────
# Mocked WhisperLive WS
# ──────────────────────────────────────────────────────────────────────────


class _FakeWS:
    """Recv-only WS stub. The test appends JSON frames to ``inbox`` and flips
    ``closed`` to simulate a mid-session drop. ``recv()`` returns the next
    frame immediately or awaits (yielding to the tick loop) while silent."""

    def __init__(self):
        self.inbox = collections.deque()
        self.closed = False

    def feed_text(self, text):
        self.inbox.append(json.dumps({"text": text}))

    async def recv(self):
        while True:
            if self.closed:
                raise websockets.exceptions.ConnectionClosed(None, None)
            if self.inbox:
                # No await between the check and popleft -> no cancellation
                # point -> a canceled tick never loses a queued frame.
                return self.inbox.popleft()
            import asyncio

            await asyncio.sleep(0.005)


class _FakeConnect:
    def __init__(self, ws=None, fail=False):
        self._ws = ws
        self._fail = fail

    async def __aenter__(self):
        if self._fail:
            raise ConnectionRefusedError("whisperlive down")
        return self._ws

    async def __aexit__(self, *exc):
        if self._ws is not None:
            self._ws.closed = True
        return False


def _patch_connect(monkeypatch, ws=None, fail=False):
    monkeypatch.setattr(
        ptt_session_mod.websockets,
        "connect",
        lambda uri, **kw: _FakeConnect(ws=ws, fail=fail),
    )


class _Recorder:
    """Thread-safe capture of on_flush / on_event / on_close callbacks."""

    def __init__(self):
        self.flushes = []
        self.events = []
        self.closes = []
        self._lock = threading.Lock()

    def on_flush(self, text):
        with self._lock:
            self.flushes.append(text)

    def on_event(self, action):
        with self._lock:
            self.events.append(action)

    def on_close(self, last_error):
        with self._lock:
            self.closes.append(last_error)


def _fast_session(rec, ws_open_timeout=2.0, **overrides):
    params = dict(
        grace=0.15,
        keepalive_timeout=0.3,
        watchdog_tick=0.03,
        ws_open_timeout=ws_open_timeout,
        max_chars=2000,
    )
    params.update(overrides)
    return PttSession(
        "ws://test/whisperlive",
        on_flush=rec.on_flush,
        on_event=rec.on_event,
        on_close=rec.on_close,
        **params,
    )


def _wait_state(session, target, timeout=3.0):
    end = time.time() + timeout
    while time.time() < end:
        if session.state == target:
            return
        time.sleep(0.01)
    raise AssertionError(f"state is {session.state!r}, expected {target!r}")


# ──────────────────────────────────────────────────────────────────────────
# start() — connect success / failure
# ──────────────────────────────────────────────────────────────────────────


def test_connect_failure_raises_unreachable_never_fake_listening(monkeypatch):
    rec = _Recorder()
    _patch_connect(monkeypatch, fail=True)
    session = _fast_session(rec)

    with pytest.raises(PttUnreachable):
        session.start()

    assert session.state == "idle"
    assert session.last_error == "stt_unreachable"
    assert rec.flushes == []
    assert "started" not in rec.events  # never emit started on a failed connect


def test_start_success_listens_and_emits_started(monkeypatch):
    rec = _Recorder()
    _patch_connect(monkeypatch, ws=_FakeWS())
    session = _fast_session(rec)

    session.start()
    try:
        assert session.state == "listening"
        assert session.session_id  # non-empty id minted
        assert rec.events[0] == "started"
        assert session.buffered_chars == 0
    finally:
        session.stop()
        _wait_state(session, "idle")


# ──────────────────────────────────────────────────────────────────────────
# Buffering, cap, anti-loop
# ──────────────────────────────────────────────────────────────────────────


def test_buffer_accumulates_and_reports_char_count(monkeypatch):
    rec = _Recorder()
    ws = _FakeWS()
    _patch_connect(monkeypatch, ws=ws)
    session = _fast_session(rec)
    session.start()
    try:
        ws.feed_text("hola mundo")
        end = time.time() + 2.0
        while time.time() < end and session.buffered_chars == 0:
            session.keepalive()
            time.sleep(0.01)
        assert session.buffered_chars == len("hola mundo")
    finally:
        session.stop()
        _wait_state(session, "idle")


def test_buffer_respects_max_chars_cap(monkeypatch):
    rec = _Recorder()
    ws = _FakeWS()
    _patch_connect(monkeypatch, ws=ws)
    session = _fast_session(rec, max_chars=20)
    session.start()
    try:
        for _ in range(40):
            ws.feed_text("palabra")
        end = time.time() + 2.0
        while time.time() < end:
            session.keepalive()
            time.sleep(0.01)
        # Soft cap: once the buffer reaches max_chars no further segment is
        # appended, so it never grows past one extra segment length.
        assert session.buffered_chars <= 20 + len("palabra") + 1
    finally:
        session.stop()
        _wait_state(session, "idle")


def test_buffer_full_emits_once_per_cycle_not_per_dropped_segment(monkeypatch):
    rec = _Recorder()
    ws = _FakeWS()
    _patch_connect(monkeypatch, ws=ws)
    session = _fast_session(rec, max_chars=10)
    session.start()
    try:
        for _ in range(25):
            ws.feed_text("palabra")
        end = time.time() + 2.0
        while time.time() < end:
            session.keepalive()
            time.sleep(0.01)
        assert rec.events.count("buffer_full") == 1
    finally:
        session.stop()
        _wait_state(session, "idle")


def test_buffer_full_flag_resets_on_new_session(monkeypatch):
    rec = _Recorder()
    for _ in range(2):
        ws = _FakeWS()
        _patch_connect(monkeypatch, ws=ws)
        session = _fast_session(rec, max_chars=10)
        session.start()
        for _ in range(5):
            ws.feed_text("palabra")
        end = time.time() + 2.0
        while time.time() < end:
            session.keepalive()
            time.sleep(0.01)
        session.stop()
        _wait_state(session, "idle")
    assert rec.events.count("buffer_full") == 2


def test_buffer_full_event_is_single_string_arg(monkeypatch):
    rec = _Recorder()
    ws = _FakeWS()
    _patch_connect(monkeypatch, ws=ws)
    session = _fast_session(rec, max_chars=10)
    session.start()
    try:
        for _ in range(5):
            ws.feed_text("palabra")
        end = time.time() + 2.0
        while time.time() < end:
            session.keepalive()
            time.sleep(0.01)
        assert "buffer_full" in rec.events
        idx = rec.events.index("buffer_full")
        assert isinstance(rec.events[idx], str)
    finally:
        session.stop()
        _wait_state(session, "idle")


def test_antiloop_regex_collapses_repeats_before_dispatch(monkeypatch):
    rec = _Recorder()
    ws = _FakeWS()
    _patch_connect(monkeypatch, ws=ws)
    session = _fast_session(rec)
    session.start()
    ws.feed_text("hola hola hola mundo")
    time.sleep(0.1)
    session.stop()
    _wait_state(session, "idle")

    assert rec.flushes == ["hola mundo"]


# ──────────────────────────────────────────────────────────────────────────
# stop -> grace -> flush -> dispatch
# ──────────────────────────────────────────────────────────────────────────


def test_stop_grace_flush_dispatches_exactly_once(monkeypatch):
    rec = _Recorder()
    ws = _FakeWS()
    _patch_connect(monkeypatch, ws=ws)
    session = _fast_session(rec)
    session.start()
    ws.feed_text("esto es una prueba")
    time.sleep(0.1)
    session.stop()
    _wait_state(session, "idle")

    assert rec.flushes == ["esto es una prueba"]
    assert rec.events == ["started", "stopped", "flushed"]
    assert rec.closes == [None]


def test_short_buffer_no_dispatch_emits_empty(monkeypatch):
    rec = _Recorder()
    ws = _FakeWS()
    _patch_connect(monkeypatch, ws=ws)
    session = _fast_session(rec)
    session.start()
    ws.feed_text("hola")  # one word — under the 2-word floor
    time.sleep(0.1)
    session.stop()
    _wait_state(session, "idle")

    assert rec.flushes == []  # no LLM call for a sub-2-word dictation
    assert "empty" in rec.events
    assert "flushed" not in rec.events


def test_empty_session_no_transcript_emits_empty(monkeypatch):
    rec = _Recorder()
    ws = _FakeWS()
    _patch_connect(monkeypatch, ws=ws)
    session = _fast_session(rec)
    session.start()
    session.stop()  # nothing ever heard
    _wait_state(session, "idle")

    assert rec.flushes == []
    assert rec.events == ["started", "stopped", "empty"]


def test_idempotent_stop_double_guillotine_is_noop(monkeypatch):
    rec = _Recorder()
    ws = _FakeWS()
    _patch_connect(monkeypatch, ws=ws)
    session = _fast_session(rec)
    session.start()
    ws.feed_text("una segunda prueba")
    time.sleep(0.1)
    session.stop()
    session.stop()  # racing retry / double-guillotine
    _wait_state(session, "idle")

    assert rec.flushes == ["una segunda prueba"]
    assert rec.events.count("stopped") == 1
    assert rec.events.count("flushed") == 1


# ──────────────────────────────────────────────────────────────────────────
# Watchdog — the HTTP guillotine (keepalive starvation)
# ──────────────────────────────────────────────────────────────────────────


def test_watchdog_autostop_delivers_buffer(monkeypatch):
    rec = _Recorder()
    ws = _FakeWS()
    _patch_connect(monkeypatch, ws=ws)
    session = _fast_session(rec)
    session.start()
    ws.feed_text("la biblia entera dictada")
    # Deliberately NO keepalive: client crash / dropped pointer-up.
    _wait_state(session, "idle")

    assert rec.flushes == ["la biblia entera dictada"]  # DELIVERED, not discarded
    assert "auto_stopped" in rec.events
    assert rec.events[-1] == "flushed"


def test_keepalive_stream_prevents_autostop(monkeypatch):
    rec = _Recorder()
    ws = _FakeWS()
    _patch_connect(monkeypatch, ws=ws)
    session = _fast_session(rec)
    session.start()
    try:
        ws.feed_text("mantengo la tecla apretada")
        # Keep beating for well over keepalive_timeout (0.3s).
        end = time.time() + 0.9
        while time.time() < end:
            session.keepalive()
            time.sleep(0.03)
        assert session.state == "listening"
        assert "auto_stopped" not in rec.events
    finally:
        session.stop()
        _wait_state(session, "idle")


# ──────────────────────────────────────────────────────────────────────────
# Grace-window extension + WS drop
# ──────────────────────────────────────────────────────────────────────────


def test_grace_window_segment_extends_deadline(monkeypatch):
    rec = _Recorder()
    ws = _FakeWS()
    _patch_connect(monkeypatch, ws=ws)
    session = _fast_session(rec, grace=0.4)
    session.start()
    ws.feed_text("primera parte")
    time.sleep(0.1)
    session.stop()  # grace begins
    time.sleep(0.15)  # still inside grace
    ws.feed_text("segunda parte")  # arrives during grace -> extends + included
    _wait_state(session, "idle")

    assert rec.flushes == ["primera parte segunda parte"]


def test_ws_drop_midlistening_flushes_and_marks_stt_lost(monkeypatch):
    rec = _Recorder()
    ws = _FakeWS()
    _patch_connect(monkeypatch, ws=ws)
    session = _fast_session(rec)
    session.start()
    ws.feed_text("lo que alcance a escuchar")
    time.sleep(0.1)
    ws.closed = True  # WhisperLive vanishes mid-listening
    _wait_state(session, "idle")

    assert rec.flushes == ["lo que alcance a escuchar"]  # deliver what was heard
    assert session.last_error == "stt_lost"
    assert "error" in rec.events
    assert rec.closes == ["stt_lost"]


# ──────────────────────────────────────────────────────────────────────────
# PRIVACY — transcript must not reach any log sink
# ──────────────────────────────────────────────────────────────────────────


def test_transcript_never_appears_in_any_log_record(monkeypatch, caplog):
    sentinel = "secret banana phrase alpha bravo charlie"
    rec = _Recorder()
    ws = _FakeWS()
    _patch_connect(monkeypatch, ws=ws)
    session = _fast_session(rec)

    with caplog.at_level(logging.DEBUG):
        session.start()
        ws.feed_text(sentinel)
        time.sleep(0.1)
        session.stop()
        _wait_state(session, "idle")

    # The dispatch path DID receive it (that is the one allowed destination).
    assert rec.flushes == [sentinel]
    # But no log record — at any level — may contain it.
    for record in caplog.records:
        assert sentinel not in record.getMessage()
    assert sentinel not in caplog.text
    # Events are metadata-only literals; never the transcript.
    assert all(isinstance(a, str) and sentinel not in a for a in rec.events)
    assert isinstance(session.buffered_chars, int)
