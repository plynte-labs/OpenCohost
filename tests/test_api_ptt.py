"""Contract, privacy, and auth tests for the /api/ptt/* routes + PttController
(liveaudio_ptt_tauri_20260710, WU2).

The WhisperLive STT server is ALWAYS mocked. HTTP-shape tests inject a fake
controller; the integration/privacy test drives a REAL PttController + REAL
PttSession over a monkeypatched ``websockets.connect``.

PRIVACY (hard rule 2): the strongest property is that the transcript NEVER
crosses HTTP. The integration test feeds a sentinel phrase through a full
mocked session and asserts it appears ONLY in the dispatcher queue (the one
allowed destination — the R8 process_context path) and is absent from every
/api/ptt/* response body AND from every /api/events entry.
"""

import json
import os
import time

import pytest
from fastapi.testclient import TestClient

from opencohost.config import settings
from opencohost.api.ptt_session import PttController, PttUnreachable, SessionActive
import opencohost.api.ptt_session as ptt_session_mod
from tests.test_api_phase1 import FakeHost
from tests.test_ptt_session import _FakeConnect, _FakeWS, _patch_connect

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)


# ──────────────────────────────────────────────────────────────────────────
# Fake controller — HTTP shape only
# ──────────────────────────────────────────────────────────────────────────


class _FakeController:
    def __init__(self):
        self.start_result = "ptt_deadbeef"
        self.start_exc = None
        self.keepalive_result = {
            "session_id": "ptt_deadbeef",
            "state": "listening",
            "buffered_chars": 7,
        }
        self.stop_result = {"state": "flushing"}
        self.state_result = {
            "state": "idle",
            "session_id": None,
            "buffered_chars": 0,
            "last_error": None,
            "stt_ws_url": "ws://127.0.0.1:8765",
        }
        self.calls = []

    def start(self):
        self.calls.append("start")
        if self.start_exc is not None:
            raise self.start_exc
        return self.start_result

    def keepalive(self, session_id):
        self.calls.append(("keepalive", session_id))
        return self.keepalive_result

    def stop(self, session_id=None):
        self.calls.append(("stop", session_id))
        return self.stop_result

    def state(self):
        self.calls.append("state")
        return self.state_result


def _client_with_fake(fake):
    app = _app()
    client = TestClient(app)
    client.__enter__()
    app.state.ptt_controller = fake
    return app, client


# ──────────────────────────────────────────────────────────────────────────
# POST /api/ptt/start
# ──────────────────────────────────────────────────────────────────────────


def test_start_200_listening():
    fake = _FakeController()
    app, client = _client_with_fake(fake)
    try:
        resp = client.post("/api/ptt/start", json={})
        assert resp.status_code == 200
        assert resp.json() == {"session_id": "ptt_deadbeef", "state": "listening"}
    finally:
        client.__exit__(None, None, None)


def test_start_409_session_active():
    fake = _FakeController()
    fake.start_exc = SessionActive()
    app, client = _client_with_fake(fake)
    try:
        resp = client.post("/api/ptt/start", json={})
        assert resp.status_code == 409
        assert resp.json() == {"detail": "session_active"}
    finally:
        client.__exit__(None, None, None)


def test_start_503_stt_unreachable():
    fake = _FakeController()
    fake.start_exc = PttUnreachable()
    app, client = _client_with_fake(fake)
    try:
        resp = client.post("/api/ptt/start", json={})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "stt_unreachable"}
    finally:
        client.__exit__(None, None, None)


# ──────────────────────────────────────────────────────────────────────────
# POST /api/ptt/keepalive
# ──────────────────────────────────────────────────────────────────────────


def test_keepalive_200_state_rides_response():
    fake = _FakeController()
    app, client = _client_with_fake(fake)
    try:
        resp = client.post("/api/ptt/keepalive", json={"session_id": "ptt_deadbeef"})
        assert resp.status_code == 200
        assert resp.json() == {
            "session_id": "ptt_deadbeef",
            "state": "listening",
            "buffered_chars": 7,
        }
        assert fake.calls[-1] == ("keepalive", "ptt_deadbeef")
    finally:
        client.__exit__(None, None, None)


def test_keepalive_409_session_not_active():
    fake = _FakeController()
    fake.keepalive_result = None  # controller says: no such active session
    app, client = _client_with_fake(fake)
    try:
        resp = client.post("/api/ptt/keepalive", json={"session_id": "stale"})
        assert resp.status_code == 409
        assert resp.json() == {"state": "idle", "detail": "session_not_active"}
    finally:
        client.__exit__(None, None, None)


# ──────────────────────────────────────────────────────────────────────────
# POST /api/ptt/stop — always 200, idempotent
# ──────────────────────────────────────────────────────────────────────────


def test_stop_200_flushing_when_cutting_active():
    fake = _FakeController()
    app, client = _client_with_fake(fake)
    try:
        resp = client.post("/api/ptt/stop", json={"session_id": "ptt_deadbeef"})
        assert resp.status_code == 200
        assert resp.json() == {"state": "flushing"}
    finally:
        client.__exit__(None, None, None)


def test_stop_200_idle_for_unknown_session():
    fake = _FakeController()
    fake.stop_result = {"state": "idle"}
    app, client = _client_with_fake(fake)
    try:
        resp = client.post("/api/ptt/stop", json={"session_id": "who-dis"})
        assert resp.status_code == 200
        assert resp.json() == {"state": "idle"}
    finally:
        client.__exit__(None, None, None)


def test_stop_200_with_no_body():
    fake = _FakeController()
    app, client = _client_with_fake(fake)
    try:
        resp = client.post("/api/ptt/stop")
        assert resp.status_code == 200
        assert fake.calls[-1] == ("stop", None)
    finally:
        client.__exit__(None, None, None)


# ──────────────────────────────────────────────────────────────────────────
# GET /api/ptt/state
# ──────────────────────────────────────────────────────────────────────────


def test_get_state_shape_counts_never_text():
    fake = _FakeController()
    fake.state_result = {
        "state": "flushing",
        "session_id": "ptt_deadbeef",
        "buffered_chars": 42,
        "last_error": None,
        "stt_ws_url": "ws://127.0.0.1:8765",
    }
    app, client = _client_with_fake(fake)
    try:
        resp = client.get("/api/ptt/state")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"state", "session_id", "buffered_chars", "last_error", "stt_ws_url"}
        assert isinstance(body["buffered_chars"], int)
        assert body["state"] == "flushing"
        # A URL/port is NOT transcript text — the privacy rule bans text, not
        # the address of the viewer socket (same info the OBS overlay uses).
        assert body["stt_ws_url"] == "ws://127.0.0.1:8765"
    finally:
        client.__exit__(None, None, None)


def test_get_state_exposes_stt_ws_url_from_real_controller():
    """The viewer WS URL rides /api/ptt/state (idle included) so the webview
    can open its own recv-only connection to LiveAudio's broadcast socket —
    the exact OBS-browser-source pattern. It is the URL the controller itself
    was configured with, and it is present BEFORE any session exists (the
    client needs it at hold start, not after)."""
    app = _app()
    client = TestClient(app)
    client.__enter__()
    try:
        controller = PttController(
            "ws://test/whisperlive",
            app.state.dispatcher,
            app.state.host.event_log,
        )
        app.state.ptt_controller = controller
        body = client.get("/api/ptt/state").json()
        assert body["state"] == "idle"
        assert body["stt_ws_url"] == "ws://test/whisperlive"
    finally:
        client.__exit__(None, None, None)


# ──────────────────────────────────────────────────────────────────────────
# Auth: POST operator-token tier + NOT rate-limited; GET open
# ──────────────────────────────────────────────────────────────────────────


def test_post_start_requires_operator_token_when_enforced(monkeypatch):
    monkeypatch.setattr(settings, "API_AUTH_ENFORCED", True)
    fake = _FakeController()
    app, client = _client_with_fake(fake)
    try:
        resp = client.post("/api/ptt/start", json={})
        assert resp.status_code == 401  # missing bearer token
        assert "start" not in fake.calls  # never reached the controller
    finally:
        client.__exit__(None, None, None)


def test_get_state_open_when_enforced(monkeypatch):
    monkeypatch.setattr(settings, "API_AUTH_ENFORCED", True)
    fake = _FakeController()
    app, client = _client_with_fake(fake)
    try:
        resp = client.get("/api/ptt/state")
        assert resp.status_code == 200  # GET stays open (rule 3)
    finally:
        client.__exit__(None, None, None)


def test_keepalive_not_rate_limited():
    """1 Hz keepalives (and idempotent stops) must never hit the RateLimiter —
    it counts only /api/agent/ mutations. Fire well past any threshold."""
    fake = _FakeController()
    app, client = _client_with_fake(fake)
    try:
        for _ in range(40):
            resp = client.post("/api/ptt/stop", json={})
            assert resp.status_code == 200  # never 429
    finally:
        client.__exit__(None, None, None)


# ──────────────────────────────────────────────────────────────────────────
# Integration + PRIVACY — real controller + real session over mocked WS
# ──────────────────────────────────────────────────────────────────────────


def _install_real_controller(app, ws, monkeypatch):
    # Real websockets.connect() never returns the same connection twice, so
    # a probe dialed against a DIFFERENT uri (POST /api/ptt/test) must not
    # share — and must not be able to close — the live session's socket.
    # Only the session's own configured uri reuses the shared `ws` so other
    # tests can keep pushing transcript frames into it via ws.feed_text(...).
    session_uri = "ws://test/whisperlive"

    def _connect(uri, **kwargs):
        return _FakeConnect(ws=ws if uri == session_uri else _FakeWS())

    monkeypatch.setattr(ptt_session_mod.websockets, "connect", _connect)
    controller = PttController(
        session_uri,
        app.state.dispatcher,
        app.state.host.event_log,
        grace=0.15,
        keepalive_timeout=0.3,
        watchdog_tick=0.03,
        ws_open_timeout=2.0,
    )
    app.state.ptt_controller = controller
    return controller


def _drain_dispatch_queue(app):
    q = app.state.host.motor.command_queue
    items = []
    while not q.empty():
        items.append(q.get_nowait())
    return items


def test_full_session_dispatches_turn_and_never_leaks_transcript(monkeypatch):
    sentinel = "secret banana phrase whiskey tango"
    ws = _FakeWS()
    app = _app()
    client = TestClient(app)
    client.__enter__()
    try:
        _install_real_controller(app, ws, monkeypatch)

        # Collect every /api/ptt/* response body to scan for leaks.
        bodies = []

        start = client.post("/api/ptt/start", json={})
        assert start.status_code == 200
        bodies.append(start.text)
        sid = start.json()["session_id"]
        assert start.json()["state"] == "listening"

        ws.feed_text(sentinel)

        # Hold: keepalive a couple of times (state rides the response).
        for _ in range(2):
            ka = client.post("/api/ptt/keepalive", json={"session_id": sid})
            assert ka.status_code == 200
            bodies.append(ka.text)
            time.sleep(0.02)

        stop = client.post("/api/ptt/stop", json={"session_id": sid})
        assert stop.status_code == 200
        assert stop.json() == {"state": "flushing"}
        bodies.append(stop.text)

        # Poll state until the background watcher flushes + frees the slot.
        end = time.time() + 3.0
        while time.time() < end:
            st = client.get("/api/ptt/state")
            bodies.append(st.text)
            if st.json()["state"] == "idle":
                break
            time.sleep(0.03)
        assert st.json()["state"] == "idle"

        # 1) The dictation WAS dispatched exactly once, as ONE process_context
        #    turn, wrapped by ptt_wrapper — the one allowed destination.
        dispatched = _drain_dispatch_queue(app)
        assert len(dispatched) == 1
        # A1 (memoria_quality_20260717): the F10 path now puts a 3-tuple
        # (command, payload, history_text). Tolerant unpack mirrors run().
        command, payload, *rest = dispatched[0]
        history_text = rest[0] if rest else None
        assert command == "process_context"
        assert payload == f"El streamer acaba de decir (PTT): {sentinel}"
        # The honest history_text rides alongside the prompt wrapper.
        assert history_text == f"El streamer dijo (PTT): {sentinel}"

        # 2) PRIVACY: the transcript never crossed HTTP — absent from EVERY
        #    /api/ptt/* response body.
        for body in bodies:
            assert sentinel not in body

        # 3) Events landed on the host event log: source "ptt", detail ALWAYS
        #    None, only whitelisted lifecycle literals, never the transcript.
        events = client.get("/api/events").json()["events"]
        ptt_events = [e for e in events if e["source"] == "ptt"]
        actions = [e["action"] for e in ptt_events]
        assert actions == ["started", "stopped", "flushed"]
        for e in ptt_events:
            assert e["detail"] is None
            assert sentinel not in json.dumps(e)
    finally:
        client.__exit__(None, None, None)


def test_buffer_full_event_reaches_api_events_once_with_no_detail(monkeypatch):
    """PTT buffer cap cue (memoria_rag_followups_20260716 Batch C, candidate 5):
    a hold that overflows the char cap emits exactly one ``buffer_full`` event
    on the shared /api/events log, detail ALWAYS None (privacy — the cap DROP
    behavior itself is unchanged, this only adds the signal)."""
    sentinel = "secret cap overflow sentinel"
    ws = _FakeWS()
    app = _app()
    client = TestClient(app)
    client.__enter__()
    try:
        _patch_connect(monkeypatch, ws=ws)
        controller = PttController(
            "ws://test/whisperlive",
            app.state.dispatcher,
            app.state.host.event_log,
            grace=0.15,
            keepalive_timeout=0.3,
            watchdog_tick=0.03,
            ws_open_timeout=2.0,
            max_chars=10,
        )
        app.state.ptt_controller = controller

        start = client.post("/api/ptt/start", json={})
        assert start.status_code == 200
        sid = start.json()["session_id"]

        ws.feed_text(sentinel)
        for _ in range(20):
            ws.feed_text("palabra")

        end = time.time() + 2.0
        while time.time() < end:
            client.post("/api/ptt/keepalive", json={"session_id": sid})
            time.sleep(0.02)

        stop = client.post("/api/ptt/stop", json={"session_id": sid})
        assert stop.status_code == 200

        end = time.time() + 3.0
        st = None
        while time.time() < end:
            st = client.get("/api/ptt/state")
            if st.json()["state"] == "idle":
                break
            time.sleep(0.03)
        assert st is not None and st.json()["state"] == "idle"

        resp = client.get("/api/events")
        body = resp.json()
        buffer_full_events = [
            e for e in body["events"] if e["source"] == "ptt" and e["action"] == "buffer_full"
        ]
        assert len(buffer_full_events) == 1
        assert buffer_full_events[0]["detail"] is None
        assert sentinel not in json.dumps(body)
    finally:
        client.__exit__(None, None, None)


def test_session_close_invokes_audio_suspect_hook(monkeypatch):
    """Recovery wiring (2026-07-15 PTT voice-death fix): PttController must
    call the on_audio_suspect hook DIRECTLY on session close (stopped here;
    auto_stopped/error share the same PttSession._notify_close -> here path),
    so MotorVocalIA re-inits its mixer before the caller's NEXT turn, not
    only after a queued command drains."""
    ws = _FakeWS()
    app = _app()
    client = TestClient(app)
    client.__enter__()
    try:
        _patch_connect(monkeypatch, ws=ws)
        calls = []
        controller = PttController(
            "ws://test/whisperlive",
            app.state.dispatcher,
            app.state.host.event_log,
            on_audio_suspect=lambda: calls.append(1),
            grace=0.15,
            keepalive_timeout=0.3,
            watchdog_tick=0.03,
            ws_open_timeout=2.0,
        )
        app.state.ptt_controller = controller

        start = client.post("/api/ptt/start", json={})
        assert start.status_code == 200
        sid = start.json()["session_id"]
        assert calls == []  # never fired on start, only on close

        stop = client.post("/api/ptt/stop", json={"session_id": sid})
        assert stop.status_code == 200

        end = time.time() + 3.0
        st = None
        while time.time() < end:
            st = client.get("/api/ptt/state")
            if st.json()["state"] == "idle":
                break
            time.sleep(0.03)
        assert st is not None and st.json()["state"] == "idle"

        assert calls == [1]
    finally:
        client.__exit__(None, None, None)


def test_concurrent_start_rejected_409(monkeypatch):
    ws = _FakeWS()
    app = _app()
    client = TestClient(app)
    client.__enter__()
    try:
        _install_real_controller(app, ws, monkeypatch)
        first = client.post("/api/ptt/start", json={})
        assert first.status_code == 200
        try:
            second = client.post("/api/ptt/start", json={})
            assert second.status_code == 409
            assert second.json() == {"detail": "session_active"}
        finally:
            client.post("/api/ptt/stop", json={})
            end = time.time() + 3.0
            while time.time() < end and client.get("/api/ptt/state").json()["state"] != "idle":
                time.sleep(0.03)
    finally:
        client.__exit__(None, None, None)


# ──────────────────────────────────────────────────────────────────────────
# PUT /api/ptt/config — repoint LiveAudio without restarting the backend
# (liveaudio_ws_uri_config_20260724)
# ──────────────────────────────────────────────────────────────────────────


def _real_controller(app, uri="ws://old-host:8765"):
    controller = PttController(uri, app.state.dispatcher, app.state.host.event_log)
    app.state.ptt_controller = controller
    return controller


def test_put_ptt_config_persists_and_returns_the_effective_config():
    app = _app()
    client = TestClient(app)
    client.__enter__()
    try:
        resp = client.put("/api/ptt/config", json={"stt_ws_uri": "ws://10.0.0.5:8765"})
        assert resp.status_code == 200
        assert resp.json() == {"stt_ws_uri": "ws://10.0.0.5:8765"}
        # Persisted, not just held in memory.
        assert settings.load_ptt_ws_uri() == "ws://10.0.0.5:8765"
    finally:
        client.__exit__(None, None, None)


def test_put_ptt_config_accepts_wss_and_a_remote_host():
    """Dual-PC setups are legitimate: LiveAudio may run on the capture machine,
    exactly like the OBS config accepting any host. No loopback restriction."""
    app = _app()
    client = TestClient(app)
    client.__enter__()
    try:
        resp = client.put("/api/ptt/config", json={"stt_ws_uri": "wss://capture-pc.lan:8765"})
        assert resp.status_code == 200
        assert resp.json()["stt_ws_uri"] == "wss://capture-pc.lan:8765"
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize(
    "bad_uri",
    ["http://127.0.0.1:8765", "https://evil.example", "127.0.0.1:8765", "ws://", ""],
)
def test_put_ptt_config_rejects_a_non_ws_uri_with_422(bad_uri):
    app = _app()
    client = TestClient(app)
    client.__enter__()
    try:
        resp = client.put("/api/ptt/config", json={"stt_ws_uri": bad_uri})
        assert resp.status_code == 422
        assert resp.json()["detail"] == "stt_ws_uri must start with ws:// or wss://"
        # Nothing persisted on the rejection path.
        assert settings.load_ptt_ws_uri() == settings.WS_URI
    finally:
        client.__exit__(None, None, None)


def test_put_ptt_config_with_no_field_returns_current_and_writes_nothing():
    app = _app()
    client = TestClient(app)
    client.__enter__()
    try:
        resp = client.put("/api/ptt/config", json={})
        assert resp.status_code == 200
        assert resp.json() == {"stt_ws_uri": settings.WS_URI}
        assert not os.path.exists(settings.PTT_CONFIG_FILE)
    finally:
        client.__exit__(None, None, None)


def test_put_ptt_config_live_applies_to_the_controller():
    """The whole point: the NEXT hold uses the new socket, with no restart.
    GET /api/ptt/state is the read surface (no dedicated GET was added)."""
    app = _app()
    client = TestClient(app)
    client.__enter__()
    try:
        controller = _real_controller(app)
        assert client.get("/api/ptt/state").json()["stt_ws_url"] == "ws://old-host:8765"

        resp = client.put("/api/ptt/config", json={"stt_ws_uri": "ws://10.0.0.5:8765"})
        assert resp.status_code == 200

        assert controller.state()["stt_ws_url"] == "ws://10.0.0.5:8765"
        assert client.get("/api/ptt/state").json()["stt_ws_url"] == "ws://10.0.0.5:8765"
    finally:
        client.__exit__(None, None, None)


def test_put_ptt_config_live_apply_failure_never_fails_the_write():
    """Best-effort live-apply, same contract as _apply_avatar_runtime: a
    controller that cannot be repointed (minimal test double / absent state)
    must not turn a successful save into a 500."""
    fake = _FakeController()  # deliberately has no set_ws_uri
    app, client = _client_with_fake(fake)
    try:
        resp = client.put("/api/ptt/config", json={"stt_ws_uri": "ws://10.0.0.5:8765"})
        assert resp.status_code == 200
        assert resp.json() == {"stt_ws_uri": "ws://10.0.0.5:8765"}
        assert settings.load_ptt_ws_uri() == "ws://10.0.0.5:8765"
    finally:
        client.__exit__(None, None, None)


def test_put_ptt_config_returns_503_when_the_write_fails(monkeypatch):
    """A failed save must never be reported as success, and must never
    live-apply a URL that is not on disk (the restart would silently revert)."""
    import opencohost.api.main as main_mod

    app = _app()
    client = TestClient(app)
    client.__enter__()
    try:
        controller = _real_controller(app)

        def _boom(uri, config_file=None):
            raise OSError("disk on fire")

        monkeypatch.setattr(main_mod, "save_ptt_ws_uri", _boom)

        resp = client.put("/api/ptt/config", json={"stt_ws_uri": "ws://10.0.0.5:8765"})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "config_write_failed"}
        # Never live-applied.
        assert controller.state()["stt_ws_url"] == "ws://old-host:8765"
    finally:
        client.__exit__(None, None, None)


def test_put_ptt_config_requires_operator_token_when_enforced(monkeypatch):
    monkeypatch.setattr(settings, "API_AUTH_ENFORCED", True)
    app = _app()
    client = TestClient(app)
    client.__enter__()
    try:
        resp = client.put("/api/ptt/config", json={"stt_ws_uri": "ws://10.0.0.5:8765"})
        assert resp.status_code == 401
        assert not os.path.exists(settings.PTT_CONFIG_FILE)  # never reached the write
    finally:
        client.__exit__(None, None, None)


def test_persisted_uri_seeds_the_controller_at_boot():
    """Persistence is pointless if boot ignores it: a fresh app must build its
    PttController from the saved URL, not the WS_URI module constant."""
    settings.save_ptt_ws_uri("ws://10.0.0.5:8765")
    app = _app()
    client = TestClient(app)
    client.__enter__()
    try:
        assert client.get("/api/ptt/state").json()["stt_ws_url"] == "ws://10.0.0.5:8765"
    finally:
        client.__exit__(None, None, None)


# ──────────────────────────────────────────────────────────────────────────
# POST /api/ptt/test — probe LiveAudio before going live
# ──────────────────────────────────────────────────────────────────────────


def _record_connect(monkeypatch, ws=None, fail=False):
    """Patch websockets.connect and capture every URI it was called with."""
    seen = []

    def _connect(uri, **kwargs):
        seen.append(uri)
        return _FakeConnect(ws=ws, fail=fail)

    monkeypatch.setattr(ptt_session_mod.websockets, "connect", _connect)
    return seen


def test_post_ptt_test_ok_true_on_a_successful_probe(monkeypatch):
    seen = _record_connect(monkeypatch, ws=_FakeWS())
    app = _app()
    client = TestClient(app)
    client.__enter__()
    try:
        resp = client.post("/api/ptt/test", json={"stt_ws_uri": "ws://10.0.0.5:8765"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "detail": "connected"}
        assert seen == ["ws://10.0.0.5:8765"]
    finally:
        client.__exit__(None, None, None)


def test_post_ptt_test_failure_is_a_result_not_an_http_error(monkeypatch):
    _record_connect(monkeypatch, fail=True)
    app = _app()
    client = TestClient(app)
    client.__enter__()
    try:
        resp = client.post("/api/ptt/test", json={"stt_ws_uri": "ws://10.0.0.5:8765"})
        assert resp.status_code == 200  # a failed probe is a RESULT, never a 5xx
        assert resp.json() == {"ok": False, "detail": "unreachable"}
    finally:
        client.__exit__(None, None, None)


def test_post_ptt_test_rejects_a_non_ws_scheme_without_probing(monkeypatch):
    seen = _record_connect(monkeypatch, ws=_FakeWS())
    app = _app()
    client = TestClient(app)
    client.__enter__()
    try:
        resp = client.post("/api/ptt/test", json={"stt_ws_uri": "http://evil.example"})
        assert resp.status_code == 200
        assert resp.json() == {"ok": False, "detail": "invalid_scheme"}
        assert seen == []  # never opened anything
    finally:
        client.__exit__(None, None, None)


def test_post_ptt_test_falls_back_to_the_configured_uri(monkeypatch):
    seen = _record_connect(monkeypatch, ws=_FakeWS())
    app = _app()
    client = TestClient(app)
    client.__enter__()
    try:
        _real_controller(app, uri="ws://configured-host:8765")
        resp = client.post("/api/ptt/test", json={})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "detail": "connected"}
        assert seen == ["ws://configured-host:8765"]
    finally:
        client.__exit__(None, None, None)


def test_post_ptt_test_with_no_body_at_all(monkeypatch):
    seen = _record_connect(monkeypatch, ws=_FakeWS())
    app = _app()
    client = TestClient(app)
    client.__enter__()
    try:
        _real_controller(app, uri="ws://configured-host:8765")
        resp = client.post("/api/ptt/test")
        assert resp.status_code == 200
        assert seen == ["ws://configured-host:8765"]
    finally:
        client.__exit__(None, None, None)


def test_post_ptt_test_never_touches_the_live_session_slot(monkeypatch):
    """The probe is a bare connect/close: it must not build a PttSession, must
    not take the single slot, must not dispatch, and must not log an event —
    so it stays safe to run mid-hold."""
    ws = _FakeWS()
    app = _app()
    client = TestClient(app)
    client.__enter__()
    try:
        controller = _install_real_controller(app, ws, monkeypatch)
        start = client.post("/api/ptt/start", json={})
        assert start.status_code == 200
        sid = start.json()["session_id"]

        resp = client.post("/api/ptt/test", json={"stt_ws_uri": "ws://some-other-host:9999"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # The live hold is untouched: same session, still listening.
        state = client.get("/api/ptt/state").json()
        assert state["session_id"] == sid
        assert state["state"] == "listening"
        # No probe event polluted the lifecycle log.
        actions = [e["action"] for e in client.get("/api/events").json()["events"]
                   if e["source"] == "ptt"]
        assert actions == ["started"]
        assert _drain_dispatch_queue(app) == []
    finally:
        client.post("/api/ptt/stop", json={})
        end = time.time() + 3.0
        while time.time() < end and client.get("/api/ptt/state").json()["state"] != "idle":
            time.sleep(0.03)
        client.__exit__(None, None, None)


def test_post_ptt_test_requires_operator_token_when_enforced(monkeypatch):
    monkeypatch.setattr(settings, "API_AUTH_ENFORCED", True)
    seen = _record_connect(monkeypatch, ws=_FakeWS())
    app = _app()
    client = TestClient(app)
    client.__enter__()
    try:
        resp = client.post("/api/ptt/test", json={"stt_ws_uri": "ws://10.0.0.5:8765"})
        assert resp.status_code == 401
        assert seen == []  # never reached the probe
    finally:
        client.__exit__(None, None, None)


def test_probe_is_bounded_and_never_blocks_on_a_hung_server(monkeypatch):
    """A WhisperLive that accepts the TCP connection and then stalls must not
    pin the request thread — same backstop contract as
    _test_obs_connection_bounded (and the same non-joining shutdown)."""
    import opencohost.api.main as main_mod

    def _hang(uri, open_timeout=None):
        time.sleep(30)

    monkeypatch.setattr(main_mod, "probe_stt_ws", _hang)
    started = time.time()
    ok, detail = main_mod._test_stt_connection_bounded("ws://hung-host:8765", timeout=0.2)
    assert (ok, detail) == (False, "timeout")
    assert time.time() - started < 5.0  # returned on the bound, did not join


# ──────────────────────────────────────────────────────────────────────────
# Persistence — config/ptt_settings.json is SHARED with the legacy CTK
# PTTManager hotkey store (liveaudio_ws_uri_config_20260724)
# ──────────────────────────────────────────────────────────────────────────


def test_load_ptt_ws_uri_defaults_when_file_absent(tmp_path):
    assert settings.load_ptt_ws_uri(str(tmp_path / "absent.json")) == settings.WS_URI


def test_load_ptt_ws_uri_falls_back_on_corrupt_file(tmp_path):
    path = tmp_path / "ptt_settings.json"
    path.write_text("{ this is not json", encoding="utf-8")
    assert settings.load_ptt_ws_uri(str(path)) == settings.WS_URI


def test_load_ptt_ws_uri_ignores_a_persisted_non_ws_scheme(tmp_path):
    """A hand-edited or hostile file must not repoint the recv-only bridge at
    an arbitrary scheme — the read side enforces the SAME guard as the PUT, so
    the validation cannot be bypassed by writing the file directly."""
    path = tmp_path / "ptt_settings.json"
    path.write_text(json.dumps({"stt_ws_uri": "http://evil.example"}), encoding="utf-8")
    assert settings.load_ptt_ws_uri(str(path)) == settings.WS_URI


def test_save_ptt_ws_uri_round_trips(tmp_path):
    path = str(tmp_path / "ptt_settings.json")
    settings.save_ptt_ws_uri("ws://10.0.0.5:8765", path)
    assert settings.load_ptt_ws_uri(path) == "ws://10.0.0.5:8765"


def test_save_ptt_ws_uri_merges_and_never_clobbers_the_ctk_hotkey(tmp_path):
    path = tmp_path / "ptt_settings.json"
    path.write_text(json.dumps({"hotkey": "F8"}), encoding="utf-8")

    settings.save_ptt_ws_uri("ws://10.0.0.5:8765", str(path))

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["hotkey"] == "F8"  # legacy CTK key survived
    assert data["stt_ws_uri"] == "ws://10.0.0.5:8765"


def test_save_ptt_ws_uri_survives_a_corrupt_existing_file(tmp_path):
    """A merge that cannot read the old file must still persist the new key
    rather than raising — the operator's reconnect is the point of the
    feature, and the only thing lost is an already-unreadable hotkey."""
    path = tmp_path / "ptt_settings.json"
    path.write_text("{ garbage", encoding="utf-8")

    settings.save_ptt_ws_uri("ws://10.0.0.5:8765", str(path))

    assert settings.load_ptt_ws_uri(str(path)) == "ws://10.0.0.5:8765"


def test_ctk_ptt_manager_still_reads_its_hotkey_after_an_api_write(tmp_path):
    """The legacy CTK consumer (opencohost/ui/ptt_manager.py) must keep
    working untouched across an API config write — it is the OTHER owner of
    this file."""
    from opencohost.ui.ptt_manager import PTTManager

    path = str(tmp_path / "ptt_settings.json")
    PTTManager(config_file=path).save_config("F8")

    settings.save_ptt_ws_uri("ws://10.0.0.5:8765", path)

    assert PTTManager(config_file=path).get_hotkey() == "F8"
    assert settings.load_ptt_ws_uri(path) == "ws://10.0.0.5:8765"


def test_ctk_hotkey_write_preserves_the_api_stt_ws_uri(tmp_path):
    """The reverse direction of the same shared-file hazard: remapping the
    hotkey in the legacy CTK UI must not silently reset the operator's
    LiveAudio URL back to the default."""
    from opencohost.ui.ptt_manager import PTTManager

    path = str(tmp_path / "ptt_settings.json")
    settings.save_ptt_ws_uri("ws://10.0.0.5:8765", path)

    PTTManager(config_file=path).save_config("F8")

    assert settings.load_ptt_ws_uri(path) == "ws://10.0.0.5:8765"
    assert PTTManager(config_file=path).get_hotkey() == "F8"


def test_stt_unreachable_returns_503_and_frees_slot(monkeypatch):
    app = _app()
    client = TestClient(app)
    client.__enter__()
    try:
        _patch_connect(monkeypatch, fail=True)
        app.state.ptt_controller = PttController(
            "ws://test/whisperlive",
            app.state.dispatcher,
            app.state.host.event_log,
            grace=0.15,
            keepalive_timeout=0.3,
            watchdog_tick=0.03,
            ws_open_timeout=0.2,
        )
        resp = client.post("/api/ptt/start", json={})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "stt_unreachable"}

        # Slot freed: state reports idle + the honest last_error, never fake
        # listening. A retry can start cleanly.
        st = client.get("/api/ptt/state").json()
        assert st["state"] == "idle"
        assert st["last_error"] == "stt_unreachable"
    finally:
        client.__exit__(None, None, None)
