"""Endpoint, lifespan, EngineHost, and CORS tests for the Kira FastAPI API
layer (Phase 1, PR2).

PR1's Dispatcher/pydantic unit tests live in test_api_dispatch.py and
test_api_models.py — this file covers everything that needs FastAPI's
TestClient plus the EngineHost seam (design v2.1, tests 1-2, 3, 9-18).

No live Ollama/audio required: EngineHost construction is stubbed via
`create_app(host_factory=...)` for all endpoint/lifespan tests, and
`opencohost.api.engine_host.MotorVocalIA`/`HealthMonitor` are monkeypatched
for the EngineHost unit tests below.
"""

import inspect
import os
import re
import subprocess
import sys
import threading
from queue import Queue
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from opencohost.core.health_monitor import MonitorState

COMMAND_ID_RE = re.compile(r"^cmd_[0-9a-f]{32}$")
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ──────────────────────────────────────────────────────────────────────────
# EngineHost + log_sink units (design tests 9-unit, 10-lock, 14, 18)
# ──────────────────────────────────────────────────────────────────────────


def test_log_sink_drain_never_logs_and_returns_instantly(caplog):
    import opencohost.api.engine_host as engine_host_mod

    sink = engine_host_mod._Drain()
    with caplog.at_level(0):
        sink.put("\U0001f9e0 [Kira]: this is secret generated dialogue")
    assert len(caplog.records) == 0


def test_log_sink_drain_leaks_nothing_to_any_channel(monkeypatch, capsys):
    """PRIVACY: _Drain.put must not leak dialogue via stdout/stderr or disk —
    `logging` is only one of several channels a regression could use."""
    import builtins

    import opencohost.api.engine_host as engine_host_mod

    def _forbidden_open(*args, **kwargs):
        raise AssertionError("_Drain.put must never open a file")

    def _forbidden_print(*args, **kwargs):
        raise AssertionError("_Drain.put must never print")

    monkeypatch.setattr(builtins, "open", _forbidden_open)
    monkeypatch.setattr(builtins, "print", _forbidden_print)

    sink = engine_host_mod._Drain()
    sink.put("\U0001f9e0 [Kira]: this is secret generated dialogue")

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_engine_host_stop_sends_sentinel_and_tears_down(tmp_path, monkeypatch):
    import opencohost.api.engine_host as engine_host_mod

    fake_ollama = MagicMock()
    monkeypatch.setattr(engine_host_mod, "ollama", fake_ollama)

    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    host._acquire_lock()

    fake_queue = Queue()
    fake_motor = MagicMock()
    fake_motor.command_queue = fake_queue
    fake_motor.current_model = "qwen3:8b"
    fake_monitor = MagicMock()
    host.motor = fake_motor
    host.monitor = fake_monitor

    host.stop()

    assert fake_queue.get_nowait() is None
    fake_monitor.stop.assert_called_once()
    fake_ollama.generate.assert_called_once_with(model="qwen3:8b", prompt="", keep_alive=0)
    assert host._lock_fd is None


def test_engine_host_stop_swallows_exceptions(tmp_path, monkeypatch):
    import opencohost.api.engine_host as engine_host_mod

    fake_ollama = MagicMock()
    fake_ollama.generate.side_effect = RuntimeError("ollama unreachable")
    monkeypatch.setattr(engine_host_mod, "ollama", fake_ollama)

    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    host._acquire_lock()

    fake_motor = MagicMock()
    fake_motor.command_queue.put = MagicMock(side_effect=RuntimeError("queue broken"))
    fake_motor.current_model = "qwen3:8b"
    fake_monitor = MagicMock()
    fake_monitor.stop.side_effect = RuntimeError("monitor broken")
    host.motor = fake_motor
    host.monitor = fake_monitor

    host.stop()  # must not raise despite every step failing


def test_engine_host_start_partial_failure_calls_stop_and_reraises(tmp_path, monkeypatch):
    import opencohost.api.engine_host as engine_host_mod

    fake_queue = Queue()
    fake_motor = MagicMock()
    fake_motor.command_queue = fake_queue
    fake_motor.current_model = None

    fake_monitor = MagicMock()
    fake_monitor.start.side_effect = RuntimeError("monitor boot failed")

    monkeypatch.setattr(engine_host_mod, "MotorVocalIA", lambda *a, **kw: fake_motor)
    monkeypatch.setattr(engine_host_mod, "HealthMonitor", lambda: fake_monitor)
    monkeypatch.setattr(engine_host_mod, "ollama", MagicMock())

    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    with pytest.raises(RuntimeError, match="monitor boot failed"):
        host.start()

    assert fake_queue.get_nowait() is None  # engine thread was told to stop
    fake_monitor.stop.assert_called_once()
    assert host._lock_fd is None  # lock released, no leaked hold


def test_engine_host_start_second_holder_raises(tmp_path, monkeypatch):
    import opencohost.api.engine_host as engine_host_mod

    lock_path = str(tmp_path / "held.lock")

    fake_motor = MagicMock()
    fake_motor.command_queue = Queue()
    fake_motor.current_model = None
    fake_monitor = MagicMock()

    monkeypatch.setattr(engine_host_mod, "MotorVocalIA", lambda *a, **kw: fake_motor)
    monkeypatch.setattr(engine_host_mod, "HealthMonitor", lambda: fake_monitor)
    monkeypatch.setattr(engine_host_mod, "ollama", MagicMock())

    first = engine_host_mod.EngineHost(lock_path=lock_path)
    first.start()
    try:
        second = engine_host_mod.EngineHost(lock_path=lock_path)
        with pytest.raises(RuntimeError):
            second.start()
    finally:
        first.stop()


def test_engine_host_start_wires_drain_as_log_queue_and_noop_ui_callback(tmp_path, monkeypatch):
    """PRIVACY wiring: start() must construct MotorVocalIA with a _Drain
    instance as the log_queue (not a real queue.Queue or logging-backed
    sink) and the module's _noop_event as ui_callback."""
    import opencohost.api.engine_host as engine_host_mod

    fake_motor = MagicMock()
    fake_motor.command_queue = Queue()
    fake_motor.current_model = None
    fake_monitor = MagicMock()

    captured = {}

    def _spy_motor_ctor(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return fake_motor

    monkeypatch.setattr(engine_host_mod, "MotorVocalIA", _spy_motor_ctor)
    monkeypatch.setattr(engine_host_mod, "HealthMonitor", lambda: fake_monitor)
    monkeypatch.setattr(engine_host_mod, "ollama", MagicMock())

    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    try:
        host.start()

        assert len(captured["args"]) >= 1
        assert isinstance(captured["args"][0], engine_host_mod._Drain)
        assert captured["kwargs"].get("ui_callback") is engine_host_mod._noop_event
    finally:
        host.stop()


# ──────────────────────────────────────────────────────────────────────────
# Fakes for main.py endpoint/lifespan tests
# ──────────────────────────────────────────────────────────────────────────


class FakeMotor:
    def __init__(self):
        self.is_ready = True
        self.current_model = "qwen3:8b"
        self._is_speaking = False
        self._is_processing = False
        self._current_profile_name = "default"
        self.command_queue = Queue()
        # B3 (Tier-B reads): mirrors MotorVocalIA.active_llm_tier / .motor_tts
        # and the memory_inspector_snapshot() provenance-gated read.
        self.active_llm_tier = "balanced"
        self.motor_tts = "ligero"
        self._memory_snapshot = {
            "entries": [],
            "source_breakdown": {},
            "digest": {"line_count": 0, "total_chars": 0, "max_chars": 4000},
        }

    @property
    def is_speaking(self):
        return self._is_speaking

    @property
    def is_processing(self):
        return self._is_processing

    def is_alive(self):
        return True

    def memory_inspector_snapshot(self):
        return self._memory_snapshot


class _DeadMotor(FakeMotor):
    """Motor whose worker thread is not running — for /api/health."""

    def is_alive(self):
        return False


class _RaisingMotor:
    """Motor whose current_model access raises — for the sanitized-500 test."""

    is_ready = True
    is_speaking = False
    is_processing = False
    _current_profile_name = "default"

    def __init__(self):
        self.command_queue = Queue()

    @property
    def current_model(self):
        raise RuntimeError("boom - must never leak to an HTTP client")


def _canned_monitor_state():
    return MonitorState(
        vram_status="ok",
        rtf_status="ok",
        ollama_status="ok",
        qwen_status="ok",
        overall_status="ok",
        ollama_lifecycle="running",
        qwen_lifecycle="running",
        free_vram_mb=1024.0,
        rtf_rolling_avg=0.5,
        last_updated=123.0,
    )


class FakeMonitor:
    def __init__(self):
        self.state = _canned_monitor_state()
        self.stop_calls = 0

    def stop(self):
        self.stop_calls += 1


class FakeHost:
    def __init__(self):
        self.motor = FakeMotor()
        self.monitor = FakeMonitor()
        self.stop_calls = 0
        # RF3 stream control-plane (Workstream 2): FakeHost defaults to no
        # aggregator (mirrors a resilient-construction-failed EngineHost) —
        # tests that need a live one set `host.aggregator = FakeAggregator()`
        # after construction (host_factory takes no args).
        self.aggregator = None
        self.aggregator_lock = threading.Lock()
        # WS3 slice 3: headless agenda controller — defaults to None (mirrors
        # a resilient-construction-failed EngineHost); tests that need a live
        # one set `host.agenda = KiraAgendaController()` after construction.
        self.agenda = None
        self.agenda_lock = threading.Lock()

    def start(self):
        pass

    def stop(self):
        self.stop_calls += 1


class DeadEngineHost:
    """Host whose motor thread has died — for /api/health engine_alive=False."""

    def __init__(self):
        self.motor = _DeadMotor()
        self.monitor = FakeMonitor()

    def start(self):
        pass

    def stop(self):
        pass


class RaisingStatusHost:
    """Host whose motor raises on status read — for the sanitized-500 test."""

    def __init__(self):
        self.motor = _RaisingMotor()
        self.monitor = FakeMonitor()

    def start(self):
        pass

    def stop(self):
        pass


_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


def _perfiles_fixture():
    return {
        "streamer_mode": {"prompt": "modo streamer", "use_system": True},
        "chill_mode": {"prompt": "modo relax", "use_system": False},
    }


# ──────────────────────────────────────────────────────────────────────────
# Isolation + import-time purity (design tests 1, 2)
# ──────────────────────────────────────────────────────────────────────────


def test_import_isolation_no_ui_modules_loaded():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import opencohost.api.main; import sys; "
            "assert 'customtkinter' not in sys.modules; "
            "assert not any(m == 'opencohost.ui' or m.startswith('opencohost.ui.') for m in sys.modules); "
            "print('OK')",
        ],
        cwd=ROOT_DIR,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout


def test_import_time_purity_no_engine_construction(monkeypatch):
    import opencohost.api.engine_host as engine_host_mod

    class RaisingMotor:
        def __init__(self, *a, **kw):
            raise AssertionError("MotorVocalIA must not be constructed at import time")

    monkeypatch.setattr(engine_host_mod, "MotorVocalIA", RaisingMotor)

    import importlib

    import opencohost.api.main as main_mod

    importlib.reload(main_mod)  # must not raise: engine is built only in lifespan


# ──────────────────────────────────────────────────────────────────────────
# Lifespan + single-host guards (design tests 10-env, 10-double, 10-seq, 14)
# ──────────────────────────────────────────────────────────────────────────


def test_lifespan_rejects_multi_worker_env(monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setenv("WEB_CONCURRENCY", "2")
    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)
    with pytest.raises(RuntimeError):
        with TestClient(app):
            pass


def test_lifespan_rejects_concurrent_second_host_same_process(monkeypatch):
    import opencohost.api.main as main_mod

    app1 = main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)
    app2 = main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)
    with TestClient(app1):
        with pytest.raises(RuntimeError):
            with TestClient(app2):
                pass


def test_lifespan_sequential_starts_both_succeed():
    import opencohost.api.main as main_mod

    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)
    with TestClient(app):
        pass
    with TestClient(app):
        pass


def test_lifespan_teardown_calls_host_stop():
    import opencohost.api.main as main_mod

    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)
    with TestClient(app):
        host = app.state.host
    assert host.stop_calls == 1


# ──────────────────────────────────────────────────────────────────────────
# GET /api/health
# ──────────────────────────────────────────────────────────────────────────


def test_health_shape_and_engine_alive_true():
    import opencohost.api.main as main_mod

    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)
    with TestClient(app) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"status", "engine_alive"}
        assert body["status"] == "ok"
        assert body["engine_alive"] is True


def test_health_engine_alive_false_when_worker_dead():
    import opencohost.api.main as main_mod

    app = main_mod.create_app(host_factory=DeadEngineHost, cors_origins=_DEFAULT_TEST_ORIGINS)
    with TestClient(app) as client:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["engine_alive"] is False


def test_health_does_not_touch_command_queue():
    import opencohost.api.main as main_mod

    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)
    with TestClient(app) as client:
        client.get("/api/health")
        assert app.state.host.motor.command_queue.qsize() == 0
        assert app.state.dispatcher.state_version == 0


# ──────────────────────────────────────────────────────────────────────────
# GET /api/status (design tests 3, 11, 12, 15)
# ──────────────────────────────────────────────────────────────────────────


def test_status_shape_and_read_purity():
    import opencohost.api.main as main_mod

    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)
    with TestClient(app) as client:
        body = None
        for _ in range(3):
            resp = client.get("/api/status")
            assert resp.status_code == 200
            body = resp.json()
            assert set(body.keys()) == {
                "is_ready",
                "is_speaking",
                "is_processing",
                "current_model",
                "active_profile",
                "health",
                "state_version",
            }
        assert body["state_version"] == 0
        assert app.state.host.motor.command_queue.qsize() == 0
        assert app.state.dispatcher.state_version == 0


def test_status_active_profile_reflects_applied_state():
    import opencohost.api.main as main_mod

    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)
    with TestClient(app) as client:
        app.state.host.motor._current_profile_name = "streamer_mode"
        before_version = app.state.dispatcher.state_version
        resp = client.get("/api/status")
        assert resp.json()["active_profile"] == "streamer_mode"
        assert app.state.dispatcher.state_version == before_version


def test_active_profile_source_scan_pin():
    import opencohost.api.main as main_mod

    source = inspect.getsource(main_mod)
    assert "_current_profile_name" in source


def test_status_endpoint_returns_sanitized_500_on_handler_error():
    import opencohost.api.main as main_mod

    app = main_mod.create_app(host_factory=RaisingStatusHost, cors_origins=_DEFAULT_TEST_ORIGINS)
    assert app.debug is False
    with TestClient(app, raise_server_exceptions=False) as client:
        resp = client.get("/api/status")
        assert resp.status_code == 500
        assert "boom" not in resp.text
        assert "Traceback" not in resp.text
        assert __file__ not in resp.text


# ──────────────────────────────────────────────────────────────────────────
# POST /api/perfiles/switch (design tests 4, 5, 6, 7, 8, 16)
# ──────────────────────────────────────────────────────────────────────────


def test_switch_profile_accepts_and_enqueues_exact_payload(monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "cargar_perfiles", _perfiles_fixture)
    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)
    with TestClient(app) as client:
        resp = client.post("/api/perfiles/switch", json={"name": "streamer_mode"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] is True
        assert COMMAND_ID_RE.match(body["command_id"])
        assert body["status"] == "queued"

        queued = app.state.host.motor.command_queue.get_nowait()
        assert queued == (
            "set_profile",
            {"prompt": "modo streamer", "use_system": True, "_profile_name": "streamer_mode"},
        )
        assert app.state.dispatcher.state_version == 1


def test_switch_profile_idempotency_replay_same_key_same_payload(monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "cargar_perfiles", _perfiles_fixture)
    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)
    with TestClient(app) as client:
        headers = {"Idempotency-Key": "req-1"}
        first = client.post("/api/perfiles/switch", json={"name": "streamer_mode"}, headers=headers)
        second = client.post("/api/perfiles/switch", json={"name": "streamer_mode"}, headers=headers)

        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["command_id"] == second.json()["command_id"]
        assert app.state.host.motor.command_queue.qsize() == 1
        assert app.state.dispatcher.state_version == 1


def test_switch_profile_conflict_same_key_different_payload(monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "cargar_perfiles", _perfiles_fixture)
    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)
    with TestClient(app) as client:
        headers = {"Idempotency-Key": "req-1"}
        first = client.post("/api/perfiles/switch", json={"name": "streamer_mode"}, headers=headers)
        assert first.status_code == 200

        second = client.post("/api/perfiles/switch", json={"name": "chill_mode"}, headers=headers)
        assert second.status_code == 409


def test_switch_profile_queue_full_gate(monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "cargar_perfiles", _perfiles_fixture)
    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)
    with TestClient(app) as client:
        for _ in range(16):
            app.state.host.motor.command_queue.put_nowait(("noop", {}))

        resp = client.post("/api/perfiles/switch", json={"name": "streamer_mode"})
        assert resp.status_code == 429
        body = resp.json()
        assert body["accepted"] is False
        assert body["reason"] == "queue_full"
        assert app.state.dispatcher.state_version == 0


def test_switch_profile_unknown_name_404(monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "cargar_perfiles", _perfiles_fixture)
    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)
    with TestClient(app) as client:
        resp = client.post("/api/perfiles/switch", json={"name": "nonexistent"})
        assert resp.status_code == 404
        assert app.state.host.motor.command_queue.qsize() == 0


def test_switch_profile_non_dict_perfiles_404(monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "cargar_perfiles", lambda: ["not", "a", "dict"])
    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)
    with TestClient(app) as client:
        resp = client.post("/api/perfiles/switch", json={"name": "streamer_mode"})
        assert resp.status_code == 404
        assert app.state.host.motor.command_queue.qsize() == 0


# ──────────────────────────────────────────────────────────────────────────
# GET /api/perfiles
# ──────────────────────────────────────────────────────────────────────────


def test_list_perfiles_returns_names_only_no_persona_leak(monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "cargar_perfiles", _perfiles_fixture)
    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)
    with TestClient(app) as client:
        resp = client.get("/api/perfiles")
        assert resp.status_code == 200
        assert resp.json() == {"profiles": ["streamer_mode", "chill_mode"]}
        # PRIVACY: only names may leave the process — no persona/prompt fields.
        assert "prompt" not in resp.text
        assert "modo streamer" not in resp.text
        assert "modo relax" not in resp.text
        assert "use_system" not in resp.text


def test_list_perfiles_non_dict_returns_empty_list_not_500(monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "cargar_perfiles", lambda: ["not", "a", "dict"])
    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)
    with TestClient(app) as client:
        resp = client.get("/api/perfiles")
        assert resp.status_code == 200
        assert resp.json() == {"profiles": []}


# ──────────────────────────────────────────────────────────────────────────
# CORS (design test 13)
# ──────────────────────────────────────────────────────────────────────────


def test_cors_default_allowlist_includes_tauri_dev_origin():
    import opencohost.api.main as main_mod

    app = main_mod.create_app(host_factory=FakeHost)  # real default origins, not test override
    with TestClient(app) as client:
        resp = client.options(
            "/api/status",
            headers={
                "origin": "http://localhost:1420",
                "access-control-request-method": "GET",
            },
        )
        assert resp.headers.get("access-control-allow-origin") == "http://localhost:1420"


def test_cors_preflight_allowed_origin_echoed_and_evil_origin_rejected():
    import opencohost.api.main as main_mod

    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)
    with TestClient(app) as client:
        allowed = client.options(
            "/api/status",
            headers={
                "origin": "http://localhost:5173",
                "access-control-request-method": "GET",
            },
        )
        allow_origin = allowed.headers.get("access-control-allow-origin")
        assert allow_origin == "http://localhost:5173"
        assert allow_origin != "*"

        evil = client.options(
            "/api/status",
            headers={
                "origin": "http://evil.example",
                "access-control-request-method": "GET",
            },
        )
        assert "access-control-allow-origin" not in evil.headers


def test_cors_never_wildcards_in_default_allowlist():
    import opencohost.api.main as main_mod

    assert "*" not in main_mod._DEFAULT_CORS_ORIGINS
