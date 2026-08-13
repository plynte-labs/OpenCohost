"""Tests for the RF3 stream chat-live CONTROL-PLANE (Workstream 2).

Scope: GET/POST/PUT /api/stream/chat-live* only — Tier-C direct aggregator
calls from the sync-def threadpool (NOT the command_queue). The chat->engine
feeder (Kira reacting to chat) is explicitly DEFERRED — not covered here.

No real network: FakeAggregator stands in for
opencohost.smart_aggregator.aggregator.Aggregator; its connect() never opens
a socket. R8: the GET response carries connection state + limits only —
never viewer message text.
"""

import threading

import pytest
from fastapi.testclient import TestClient

from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


# ──────────────────────────────────────────────────────────────────────────
# Fakes
# ──────────────────────────────────────────────────────────────────────────


class _FakeActivity:
    def __init__(self, threshold=5.0, cooldown=10.0):
        self.threshold_per_second = threshold
        self.cooldown_seconds = cooldown


class _FakeSource:
    def __init__(self, source_id, platform):
        self._source_id = source_id
        self.platform = platform
        self._connected = True

    def is_connected(self):
        return self._connected

    def disconnect(self):
        self._connected = False


class FakeAggregator:
    """Stands in for the real Aggregator — no yaml config, no network."""

    def __init__(self):
        self._source = None
        self.activity = _FakeActivity()
        self._spam_max_messages = 10
        self._filter_policy_name = "balanced"
        self.connect_calls = []
        self.disconnect_calls = 0
        # optional gate a test can hold closed to simulate an in-flight connect
        self.block_connect = None  # threading.Event
        # optional exception a test injects to exercise connect error mapping
        self.raise_on_connect = None  # Exception instance

    def connect(self, source_id, platform="youtube"):
        self.connect_calls.append((source_id, platform))
        if self.block_connect is not None:
            self.block_connect.wait(timeout=5)
        if self.raise_on_connect is not None:
            raise self.raise_on_connect
        self._source = _FakeSource(source_id, platform)

    def disconnect(self):
        self.disconnect_calls += 1
        if self._source is not None:
            self._source.disconnect()
        self._source = None

    def set_activity_limits(self, threshold_per_second=None, cooldown_seconds=None, reset=False):
        if threshold_per_second is not None:
            self.activity.threshold_per_second = threshold_per_second
        if cooldown_seconds is not None:
            self.activity.cooldown_seconds = cooldown_seconds

    def set_spam_limits(self, max_messages_per_user=None, user_window_seconds=None):
        if max_messages_per_user is not None:
            self._spam_max_messages = max_messages_per_user

    def set_filter_policy(self, preset_name):
        if preset_name not in ("balanced", "twitch_relaxed", "strict"):
            raise ValueError(f"Unknown filter preset: {preset_name}")
        self._filter_policy_name = preset_name

    def get_filter_policy(self):
        return self._filter_policy_name


def _host_with_aggregator(aggregator):
    def factory():
        host = FakeHost()
        host.aggregator = aggregator
        host.aggregator_lock = threading.Lock()
        return host

    return factory


def _app(host_factory):
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=host_factory, cors_origins=_DEFAULT_TEST_ORIGINS)


# ──────────────────────────────────────────────────────────────────────────
# GET /api/stream/chat-live
# ──────────────────────────────────────────────────────────────────────────


def test_get_stream_state_disconnected_shape():
    agg = FakeAggregator()
    app = _app(_host_with_aggregator(agg))
    with TestClient(app) as client:
        resp = client.get("/api/stream/chat-live")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {
            "connected",
            "platform",
            "source_id",
            "threshold_per_second",
            "cooldown_seconds",
            "max_messages_per_user",
            "filter_policy",
            "input_contract",
            "stream_over_agenda",
            "stream_ttl_seconds",
            "effective_stream_ttl_seconds",
        }
        assert body["connected"] is False
        assert body["platform"] is None
        assert body["source_id"] is None
        assert body["threshold_per_second"] == 5.0
        assert body["cooldown_seconds"] == 10.0
        assert body["max_messages_per_user"] == 10
        assert body["filter_policy"] == "balanced"
        assert body["input_contract"] is False  # module flag ships OFF


def test_get_stream_state_connected_reflects_source():
    agg = FakeAggregator()
    agg._source = _FakeSource("abc123", "youtube")
    app = _app(_host_with_aggregator(agg))
    with TestClient(app) as client:
        resp = client.get("/api/stream/chat-live")
        body = resp.json()
        assert body["connected"] is True
        assert body["platform"] == "youtube"
        assert body["source_id"] == "abc123"


def test_get_stream_state_503_when_aggregator_none():
    app = _app(_host_with_aggregator(None))
    with TestClient(app) as client:
        resp = client.get("/api/stream/chat-live")
        assert resp.status_code == 503
        assert resp.json()["detail"] == "stream_unavailable"


def test_get_stream_state_never_contains_message_text():
    """R8: state + limits ONLY — never any viewer message text field."""
    agg = FakeAggregator()
    agg._source = _FakeSource("abc123", "youtube")
    app = _app(_host_with_aggregator(agg))
    with TestClient(app) as client:
        resp = client.get("/api/stream/chat-live")
        body = resp.json()
        for forbidden in ("message", "messages", "text", "user", "chat_log"):
            assert forbidden not in body


# ──────────────────────────────────────────────────────────────────────────
# POST /api/stream/chat-live/connect + /disconnect
# ──────────────────────────────────────────────────────────────────────────


def test_connect_parses_valid_url_and_calls_aggregator_connect():
    agg = FakeAggregator()
    app = _app(_host_with_aggregator(agg))
    with TestClient(app) as client:
        resp = client.post(
            "/api/stream/chat-live/connect",
            json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        )
        assert resp.status_code == 200
        assert agg.connect_calls == [("dQw4w9WgXcQ", "youtube")]
        body = resp.json()
        assert body["connected"] is True
        assert body["platform"] == "youtube"


def test_connect_invalid_url_422_and_does_not_call_connect():
    agg = FakeAggregator()
    app = _app(_host_with_aggregator(agg))
    with TestClient(app) as client:
        resp = client.post("/api/stream/chat-live/connect", json={"url": "not a valid url"})
        assert resp.status_code == 422
        assert agg.connect_calls == []


def test_connect_503_when_aggregator_none():
    app = _app(_host_with_aggregator(None))
    with TestClient(app) as client:
        resp = client.post(
            "/api/stream/chat-live/connect",
            json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        )
        assert resp.status_code == 503


def test_connect_single_flight_second_concurrent_call_409_busy():
    agg = FakeAggregator()
    agg.block_connect = threading.Event()
    app = _app(_host_with_aggregator(agg))
    with TestClient(app) as client:
        results = {}

        def _first_call():
            results["first"] = client.post(
                "/api/stream/chat-live/connect",
                json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
            )

        t = threading.Thread(target=_first_call)
        t.start()
        try:
            # give the first request time to acquire the lock and block inside connect()
            for _ in range(200):
                if agg.connect_calls:
                    break
                threading.Event().wait(0.01)
            second = client.post(
                "/api/stream/chat-live/connect",
                json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
            )
            assert second.status_code == 409
            assert second.json()["detail"] == "busy"
        finally:
            agg.block_connect.set()
            t.join(timeout=5)
        assert results["first"].status_code == 200


def test_connect_runtime_error_maps_503_chat_source_unavailable():
    """RuntimeError from agg.connect (e.g. pytchat not installed) must be a
    typed 503 chat_source_unavailable, not a raw 500."""
    agg = FakeAggregator()
    agg.raise_on_connect = RuntimeError("pytchat no esta instalado")
    app = _app(_host_with_aggregator(agg))
    with TestClient(app) as client:
        resp = client.post(
            "/api/stream/chat-live/connect",
            json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"] == "chat_source_unavailable"


def test_connect_value_error_maps_422_unsupported_platform():
    """ValueError from agg.connect (unsupported platform) must be a typed 422
    unsupported_platform — distinct from the parse-stage invalid_url."""
    agg = FakeAggregator()
    agg.raise_on_connect = ValueError("Plataforma no soportada: kick")
    app = _app(_host_with_aggregator(agg))
    with TestClient(app) as client:
        resp = client.post(
            "/api/stream/chat-live/connect",
            json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"] == "unsupported_platform"


def test_connect_generic_exception_maps_503_stream_unavailable_no_leak():
    """Any unexpected exception must degrade to 503 stream_unavailable without
    leaking the internal message or a traceback into the response body."""
    agg = FakeAggregator()
    agg.raise_on_connect = OSError("socket boom leak-token-xyz")
    app = _app(_host_with_aggregator(agg))
    with TestClient(app) as client:
        resp = client.post(
            "/api/stream/chat-live/connect",
            json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        )
        assert resp.status_code == 503
        assert resp.json()["detail"] == "stream_unavailable"
        assert "leak-token-xyz" not in resp.text
        assert "Traceback" not in resp.text


def test_connect_releases_lock_after_exception():
    """The single-flight lock must be released even when connect raises, so a
    later connect is not permanently stuck at 409 busy."""
    agg = FakeAggregator()
    agg.raise_on_connect = RuntimeError("boom")
    app = _app(_host_with_aggregator(agg))
    with TestClient(app) as client:
        first = client.post(
            "/api/stream/chat-live/connect",
            json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        )
        assert first.status_code == 503
        # lock released -> a subsequent valid connect succeeds (not 409 busy)
        agg.raise_on_connect = None
        second = client.post(
            "/api/stream/chat-live/connect",
            json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
        )
        assert second.status_code == 200
        assert second.json()["connected"] is True


def test_disconnect_calls_aggregator_disconnect():
    agg = FakeAggregator()
    agg._source = _FakeSource("abc123", "youtube")
    app = _app(_host_with_aggregator(agg))
    with TestClient(app) as client:
        resp = client.post("/api/stream/chat-live/disconnect")
        assert resp.status_code == 200
        assert agg.disconnect_calls == 1
        assert resp.json()["connected"] is False


def test_disconnect_single_flight_409_while_connect_in_flight():
    agg = FakeAggregator()
    agg.block_connect = threading.Event()
    app = _app(_host_with_aggregator(agg))
    with TestClient(app) as client:
        results = {}

        def _first_call():
            results["first"] = client.post(
                "/api/stream/chat-live/connect",
                json={"url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ"},
            )

        t = threading.Thread(target=_first_call)
        t.start()
        try:
            for _ in range(200):
                if agg.connect_calls:
                    break
                threading.Event().wait(0.01)
            # disconnect must not race an in-flight connect on _source -> 409 busy
            resp = client.post("/api/stream/chat-live/disconnect")
            assert resp.status_code == 409
            assert resp.json()["detail"] == "busy"
            assert agg.disconnect_calls == 0
        finally:
            agg.block_connect.set()
            t.join(timeout=5)
        assert results["first"].status_code == 200


def test_disconnect_503_when_aggregator_none():
    app = _app(_host_with_aggregator(None))
    with TestClient(app) as client:
        resp = client.post("/api/stream/chat-live/disconnect")
        assert resp.status_code == 503


# ──────────────────────────────────────────────────────────────────────────
# PUT /api/stream/chat-live/limits
# ──────────────────────────────────────────────────────────────────────────


def test_limits_sets_provided_fields_only():
    agg = FakeAggregator()
    app = _app(_host_with_aggregator(agg))
    with TestClient(app) as client:
        resp = client.put(
            "/api/stream/chat-live/limits",
            json={"threshold_per_second": 12.5, "max_messages_per_user": 3},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["threshold_per_second"] == 12.5
        assert body["cooldown_seconds"] == 10.0  # unchanged
        assert body["max_messages_per_user"] == 3


def test_limits_unknown_filter_preset_422():
    agg = FakeAggregator()
    app = _app(_host_with_aggregator(agg))
    with TestClient(app) as client:
        resp = client.put("/api/stream/chat-live/limits", json={"filter_policy": "does_not_exist"})
        assert resp.status_code == 422
        assert agg.get_filter_policy() == "balanced"  # unchanged


def test_limits_503_when_aggregator_none():
    app = _app(_host_with_aggregator(None))
    with TestClient(app) as client:
        resp = client.put("/api/stream/chat-live/limits", json={"threshold_per_second": 1.0})
        assert resp.status_code == 503


def test_limits_input_contract_toggles_module_flag(monkeypatch):
    """PUT input_contract flips chat_input_contract.USE_INPUT_CONTRACT_PROMPT --
    the same process-global module flag the CTK toggle writes
    (stream_admin_ui.py _toggle_input_contract). It is NOT aggregator state."""
    import opencohost.smart_aggregator.chat_input_contract as ic

    monkeypatch.setattr(ic, "USE_INPUT_CONTRACT_PROMPT", False)
    agg = FakeAggregator()
    app = _app(_host_with_aggregator(agg))
    with TestClient(app) as client:
        resp = client.put("/api/stream/chat-live/limits", json={"input_contract": True})
        assert resp.status_code == 200
        assert ic.USE_INPUT_CONTRACT_PROMPT is True
        assert resp.json()["input_contract"] is True

        resp = client.put("/api/stream/chat-live/limits", json={"input_contract": False})
        assert resp.status_code == 200
        assert ic.USE_INPUT_CONTRACT_PROMPT is False
        assert resp.json()["input_contract"] is False


def test_limits_omitting_input_contract_leaves_flag_and_get_reflects_it(monkeypatch):
    """Omitted field never writes the flag; GET reads the LIVE module value
    (a from-import would freeze the boot value -- the CTK reader precedent is
    module-attribute access, agenda_audio_controller's `import ... as _ic`)."""
    import opencohost.smart_aggregator.chat_input_contract as ic

    monkeypatch.setattr(ic, "USE_INPUT_CONTRACT_PROMPT", True)
    agg = FakeAggregator()
    app = _app(_host_with_aggregator(agg))
    with TestClient(app) as client:
        resp = client.put("/api/stream/chat-live/limits", json={"threshold_per_second": 2.0})
        assert resp.status_code == 200
        assert ic.USE_INPUT_CONTRACT_PROMPT is True  # untouched
        assert resp.json()["input_contract"] is True

        resp = client.get("/api/stream/chat-live")
        assert resp.json()["input_contract"] is True


# ──────────────────────────────────────────────────────────────────────────
# Tier split settings on PUT/GET (tauri_stream_chat_20260812 §3.2 phase 1)
# ──────────────────────────────────────────────────────────────────────────


def test_get_stream_state_tier_setting_defaults(monkeypatch):
    """GET carries the two process-global tier settings plus the DERIVED
    effective TTL -- module state (turn_priority), NOT aggregator state,
    same mechanism as input_contract."""
    from opencohost.core import turn_priority

    monkeypatch.setattr(turn_priority, "STREAM_OVER_AGENDA", True)
    monkeypatch.setattr(turn_priority, "STREAM_TTL_SECONDS", 120.0)
    app = _app(_host_with_aggregator(FakeAggregator()))
    with TestClient(app) as client:
        body = client.get("/api/stream/chat-live").json()
        assert body["stream_over_agenda"] is True
        assert body["stream_ttl_seconds"] == 120.0
        assert body["effective_stream_ttl_seconds"] == 120.0


def test_limits_stream_order_and_ttl_roundtrip(monkeypatch):
    """PUT writes the turn_priority module attributes; the response's
    effective TTL exposes THE TRAP guard: agenda-first floors the window so
    the streamer can SEE their 45s became 300s instead of debugging a mute
    co-host."""
    from opencohost.core import turn_priority

    monkeypatch.setattr(turn_priority, "STREAM_OVER_AGENDA", True)
    monkeypatch.setattr(turn_priority, "STREAM_TTL_SECONDS", 120.0)
    app = _app(_host_with_aggregator(FakeAggregator()))
    with TestClient(app) as client:
        resp = client.put(
            "/api/stream/chat-live/limits",
            json={"stream_over_agenda": False, "stream_ttl_seconds": 45.0},
        )
        assert resp.status_code == 200
        assert turn_priority.STREAM_OVER_AGENDA is False
        assert turn_priority.STREAM_TTL_SECONDS == 45.0
        body = resp.json()
        assert body["stream_over_agenda"] is False
        assert body["stream_ttl_seconds"] == 45.0
        assert (
            body["effective_stream_ttl_seconds"]
            == turn_priority.AGENDA_FIRST_STREAM_TTL_FLOOR_SECONDS
        )

        resp = client.put("/api/stream/chat-live/limits", json={"stream_over_agenda": True})
        assert resp.status_code == 200
        assert resp.json()["effective_stream_ttl_seconds"] == 45.0


def test_limits_stream_ttl_out_of_bounds_is_422(monkeypatch):
    """Trust boundary: a nonsense TTL (0, negative, sub-floor, absurd) is
    refused and the module value stays untouched -- a 0s window would expire
    every stream item instantly, the mute failure from the other side."""
    from opencohost.core import turn_priority

    monkeypatch.setattr(turn_priority, "STREAM_TTL_SECONDS", 120.0)
    app = _app(_host_with_aggregator(FakeAggregator()))
    with TestClient(app) as client:
        for bad in (0, -5, 9.9, 3601):
            resp = client.put(
                "/api/stream/chat-live/limits", json={"stream_ttl_seconds": bad}
            )
            assert resp.status_code == 422, bad
            assert resp.json()["detail"] == "invalid_stream_ttl"
            assert turn_priority.STREAM_TTL_SECONDS == 120.0


def test_limits_omitting_tier_fields_leaves_them(monkeypatch):
    from opencohost.core import turn_priority

    monkeypatch.setattr(turn_priority, "STREAM_OVER_AGENDA", False)
    monkeypatch.setattr(turn_priority, "STREAM_TTL_SECONDS", 77.0)
    app = _app(_host_with_aggregator(FakeAggregator()))
    with TestClient(app) as client:
        resp = client.put("/api/stream/chat-live/limits", json={"threshold_per_second": 2.0})
        assert resp.status_code == 200
        assert turn_priority.STREAM_OVER_AGENDA is False
        assert turn_priority.STREAM_TTL_SECONDS == 77.0


# ──────────────────────────────────────────────────────────────────────────
# EngineHost resilient aggregator construction (design test — resilience)
# ──────────────────────────────────────────────────────────────────────────


def test_engine_host_start_survives_aggregator_construction_failure(tmp_path, monkeypatch):
    """A host whose Aggregator ctor raises still start()s with
    aggregator=None — engine/profiles/commands must not be bricked by a
    stream failure."""
    import opencohost.api.engine_host as engine_host_mod
    from queue import Queue
    from unittest.mock import MagicMock

    fake_motor = MagicMock()
    fake_motor.command_queue = Queue()
    fake_motor.current_model = None
    fake_monitor = MagicMock()

    monkeypatch.setattr(engine_host_mod, "MotorVocalIA", lambda *a, **kw: fake_motor)
    monkeypatch.setattr(engine_host_mod, "HealthMonitor", lambda: fake_monitor)
    monkeypatch.setattr(engine_host_mod, "ollama", MagicMock())
    monkeypatch.setattr(
        engine_host_mod,
        "Aggregator",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("no yaml config")),
    )

    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    try:
        host.start()  # must not raise
        assert host.aggregator is None
        assert host.motor is fake_motor  # engine still came up
    finally:
        host.stop()


def test_engine_host_stop_disconnects_aggregator_before_teardown(tmp_path, monkeypatch):
    import opencohost.api.engine_host as engine_host_mod
    from queue import Queue
    from unittest.mock import MagicMock

    fake_agg = MagicMock()
    monkeypatch.setattr(engine_host_mod, "ollama", MagicMock())

    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    host._acquire_lock()
    fake_motor = MagicMock()
    fake_motor.command_queue = Queue()
    fake_motor.current_model = None
    host.motor = fake_motor
    host.monitor = MagicMock()
    host.aggregator = fake_agg

    host.stop()

    fake_agg.disconnect.assert_called_once()
