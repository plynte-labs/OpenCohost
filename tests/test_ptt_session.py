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
from unittest.mock import MagicMock

import pytest
import websockets

import opencohost.api.ptt_session as ptt_session_mod
from opencohost.api.ptt_session import PttController, PttSession, PttUnreachable


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
# PttController._dispatch — honest history_text (memoria_quality_20260717 A1)
# ──────────────────────────────────────────────────────────────────────────


class _RecordingDispatcher:
    def __init__(self):
        self.calls = []

    def dispatch(self, command, payload, key=None, history_text=None, source=None, submitted_at=None):
        self.calls.append(
            {
                "command": command,
                "payload": payload,
                "key": key,
                "history_text": history_text,
                "source": source,
                "submitted_at": submitted_at,
            }
        )


def test_dispatch_declares_ptt_source():
    # B2 (turn provenance): the engine hardcoded source="direct" for every
    # dispatched turn, so an F10 PTT turn logged/telemetered as "direct" and the
    # real origin was lost before it ever reached the engine. The controller is
    # the only place that KNOWS this turn came through PTT, so it declares it.
    disp = _RecordingDispatcher()
    controller = PttController("ws://test/whisperlive", disp, MagicMock())

    controller._dispatch("hola mundo esto es una prueba")

    assert disp.calls[0]["source"] == "ptt"


def test_dispatch_sends_ptt_wrapper_prompt_and_ptt_history_wrapper_history_text():
    # A1: the F10 flush must send the motor BOTH the ptt_wrapper prompt AND an
    # honest history_text via ptt_history_wrapper, so the historial records
    # "El streamer dijo (PTT): ..." instead of burying the real words under the
    # "acaba de decir (PTT)" prompt boilerplate. The F10 path uses no
    # idempotency key (key stays None).
    disp = _RecordingDispatcher()
    controller = PttController("ws://test/whisperlive", disp, MagicMock())

    controller._dispatch("hola mundo esto es una prueba")

    assert len(disp.calls) == 1
    call = disp.calls[0]
    assert call["command"] == "process_context"
    assert call["payload"] == "El streamer acaba de decir (PTT): hola mundo esto es una prueba"
    assert call["history_text"] == "El streamer dijo (PTT): hola mundo esto es una prueba"
    assert call["key"] is None


def test_dispatch_stamps_submitted_at_monotonic():
    # Unit 4.1 (runtime_findings_batch_20260731, F5): PTT shares the same
    # dispatch() seam as /api/chat/turn "for free" — no separate stamp path.
    disp = _RecordingDispatcher()
    controller = PttController("ws://test/whisperlive", disp, MagicMock())

    before = time.monotonic()
    controller._dispatch("hola mundo esto es una prueba")
    after = time.monotonic()

    submitted_at = disp.calls[0]["submitted_at"]
    assert isinstance(submitted_at, float)
    assert before <= submitted_at <= after


# ──────────────────────────────────────────────────────────────────────────
# PttController.set_ws_uri — live LiveAudio reconnect without a restart
# (liveaudio_ws_uri_config_20260724)
# ──────────────────────────────────────────────────────────────────────────


class _FakeSession:
    """Minimal PttSession stand-in that captures the ws_uri it was BUILT with
    and never opens a socket. Mirrors the real read surface (state /
    buffered_chars / last_error) as plain attributes."""

    def __init__(self, ws_uri, on_flush=None, on_event=None, on_close=None, **kwargs):
        self.ws_uri = ws_uri
        self._on_close = on_close
        self.session_id = f"ptt_fake_{id(self):x}"
        self.state = "idle"
        self.buffered_chars = 0
        self.last_error = None

    def start(self):
        self.state = "listening"

    def keepalive(self):
        pass

    def stop(self):
        self.state = "idle"
        if self._on_close is not None:
            self._on_close(None)


def _controller_with_fake_sessions(uri):
    created = []

    def factory(ws_uri, **kwargs):
        session = _FakeSession(ws_uri, **kwargs)
        created.append(session)
        return session

    controller = PttController(
        uri, _RecordingDispatcher(), MagicMock(), session_factory=factory
    )
    return controller, created


def test_set_ws_uri_applies_to_the_next_session_only():
    """The operator can repoint OpenCohost at a freshly-launched LiveAudio
    without restarting the backend: set_ws_uri swaps the controller's URI and
    the NEXT hold builds its session against it. An in-flight session captured
    its own ws_uri at construction (ptt_session.py __init__) and must be
    completely unaffected — no mid-hold socket swap, ever."""
    controller, created = _controller_with_fake_sessions("ws://old-host:8765")

    controller.start()
    assert created[0].ws_uri == "ws://old-host:8765"

    controller.set_ws_uri("ws://new-host:9999")

    # In-flight session keeps the URI it was built with.
    assert created[0].ws_uri == "ws://old-host:8765"

    controller.stop()  # fake session frees the slot through on_close
    controller.start()

    assert len(created) == 2
    assert created[1].ws_uri == "ws://new-host:9999"


def test_set_ws_uri_is_reflected_in_state_immediately():
    """GET /api/ptt/state is the ONLY read surface for the configured URL (no
    dedicated GET endpoint exists), so the swap must be visible there as soon
    as it is applied — including while idle."""
    controller, _created = _controller_with_fake_sessions("ws://old-host:8765")

    assert controller.state()["stt_ws_url"] == "ws://old-host:8765"
    controller.set_ws_uri("ws://new-host:9999")
    assert controller.state()["stt_ws_url"] == "ws://new-host:9999"


def test_set_ws_uri_during_active_session_reports_new_uri_but_keeps_session():
    """A swap DURING a hold is legal: state() reports the new configured URL
    while the live session keeps streaming from the old one until it ends."""
    controller, created = _controller_with_fake_sessions("ws://old-host:8765")

    session_id = controller.start()
    controller.set_ws_uri("ws://new-host:9999")

    state = controller.state()
    assert state["session_id"] == session_id  # session untouched
    assert state["state"] == "listening"
    assert state["stt_ws_url"] == "ws://new-host:9999"
    assert created[0].ws_uri == "ws://old-host:8765"


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
