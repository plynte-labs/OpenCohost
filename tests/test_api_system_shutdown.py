"""POST /api/system/shutdown -- graceful engine stop for the Tauri shell.

The shell used to TerminateProcess the backend, so EngineHost.stop() (model
unload, vision teardown) never ran. The shell now asks for a graceful stop
first. The endpoint is operator-token gated UNCONDITIONALLY (not behind
API_AUTH_ENFORCED), runs host.stop() bounded in a threadpool, and returns a
small JSON.
"""

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opencohost.config import settings
from tests.test_api_phase1 import FakeHost

_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


def _tokens() -> dict:
    return json.loads(Path(settings.API_TOKENS_FILE).read_text(encoding="utf-8"))


def _client(host_factory=FakeHost):
    import opencohost.api.main as main_mod

    app = main_mod.create_app(host_factory=host_factory, cors_origins=_ORIGINS)
    return TestClient(app)


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def test_shutdown_requires_operator_token_even_when_auth_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "API_AUTH_ENFORCED", False)
    with _client() as client:
        host = client.app.state.host
        base = host.stop_calls  # lifespan startup does not stop
        assert client.post("/api/system/shutdown").status_code == 403
        assert (
            client.post("/api/system/shutdown", headers=_bearer("nope")).status_code == 403
        )
        agent = _tokens()["agent"]
        assert (
            client.post("/api/system/shutdown", headers=_bearer(agent)).status_code == 403
        )
        assert host.stop_calls == base


def test_shutdown_with_operator_token_stops_host_once_and_reports_model(monkeypatch):
    monkeypatch.setattr(settings, "API_AUTH_ENFORCED", False)
    with _client() as client:
        host = client.app.state.host
        host.last_unloaded_model = "qwen3:8b"
        op = _tokens()["operator"]
        resp = client.post("/api/system/shutdown", headers=_bearer(op))
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"ok": True, "unloaded": "qwen3:8b", "timed_out": False}
        assert host.stop_calls == 1


def test_shutdown_is_repeatable_each_call_delegates_to_idempotent_stop(monkeypatch):
    with _client() as client:
        op = _tokens()["operator"]
        for _ in range(2):
            assert (
                client.post("/api/system/shutdown", headers=_bearer(op)).status_code == 200
            )
        assert client.app.state.host.stop_calls == 2


def test_shutdown_is_bounded_when_stop_hangs(monkeypatch):
    import opencohost.api.routers.system as system_mod

    monkeypatch.setattr(system_mod, "SHUTDOWN_BOUND_SECONDS", 0.2)
    release = threading.Event()

    class HangingHost(FakeHost):
        def stop(self):
            self.stop_calls += 1
            release.wait(5.0)

    with _client(HangingHost) as client:
        op = _tokens()["operator"]
        t0 = time.monotonic()
        resp = client.post("/api/system/shutdown", headers=_bearer(op))
        elapsed = time.monotonic() - t0
        release.set()
        assert resp.status_code == 200
        assert resp.json()["timed_out"] is True
        assert elapsed < 2.0


def test_shutdown_survives_stop_raising():
    class BoomHost(FakeHost):
        def stop(self):
            self.stop_calls += 1
            if self.stop_calls == 1:  # the lifespan's own stop must not raise
                raise RuntimeError("boom")

    with _client(BoomHost) as client:
        op = _tokens()["operator"]
        resp = client.post("/api/system/shutdown", headers=_bearer(op))
        assert resp.status_code == 200
        assert resp.json()["ok"] is False


def test_shutdown_logs_one_line_without_chat(caplog):
    import logging

    with _client() as client:
        client.app.state.host.last_unloaded_model = "qwen3:8b"
        op = _tokens()["operator"]
        with caplog.at_level(logging.INFO):
            client.post("/api/system/shutdown", headers=_bearer(op))
    lines = [r.getMessage() for r in caplog.records if "[SHUTDOWN]" in r.getMessage()]
    assert len(lines) == 1
    assert "unloaded=qwen3:8b" in lines[0]


# --- EngineHost.stop() idempotency -----------------------------------------


def _bare_host(model="qwen3:8b"):
    from opencohost.api.engine_host import EngineHost

    class _Motor:
        current_model = model
        command_queue = type("Q", (), {"put": lambda self, x: None})()
        is_processing = False
        _speech_active = False

        def flush_memorias(self):
            pass

    host = EngineHost.__new__(EngineHost)
    host.motor = _Motor()
    host.aggregator = None
    host.monitor = None
    host._release_lock = lambda: None
    return host


def test_engine_host_stop_is_idempotent_and_unloads_once(monkeypatch):
    import opencohost.api.engine_host as eh

    calls = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        def generate(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(eh.ollama, "Client", _Client)
    host = _bare_host()
    host.stop()
    host.stop()
    assert len(calls) == 1
    assert calls[0]["model"] == "qwen3:8b" and calls[0]["keep_alive"] == 0
    assert host.last_unloaded_model == "qwen3:8b"


# --- unload_models opt-out (settings toggle "Unload models when closing") ---


def test_engine_host_stop_skips_unload_when_unload_models_false(monkeypatch):
    import opencohost.api.engine_host as eh

    calls = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        def generate(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(eh.ollama, "Client", _Client)
    host = _bare_host()
    host.stop(unload_models=False)
    assert calls == []
    assert getattr(host, "last_unloaded_model", None) is None


def test_engine_host_stop_default_still_unloads(monkeypatch):
    import opencohost.api.engine_host as eh

    calls = []

    class _Client:
        def __init__(self, *a, **k):
            pass

        def generate(self, **kw):
            calls.append(kw)

    monkeypatch.setattr(eh.ollama, "Client", _Client)
    host = _bare_host()
    host.stop()
    assert len(calls) == 1


class _RecordingHost(FakeHost):
    def __init__(self):
        super().__init__()
        self.stop_kwargs = []

    def stop(self, unload_models=True):
        self.stop_calls += 1
        self.stop_kwargs.append(unload_models)


def test_shutdown_body_unload_models_false_is_forwarded_to_stop():
    with _client(_RecordingHost) as client:
        op = _tokens()["operator"]
        base = len(client.app.state.host.stop_kwargs)
        resp = client.post(
            "/api/system/shutdown", headers=_bearer(op), json={"unload_models": False}
        )
        assert resp.status_code == 200
        assert client.app.state.host.stop_kwargs[base:] == [False]


def test_shutdown_without_body_or_with_true_unloads():
    with _client(_RecordingHost) as client:
        op = _tokens()["operator"]
        base = len(client.app.state.host.stop_kwargs)
        client.post("/api/system/shutdown", headers=_bearer(op))
        client.post(
            "/api/system/shutdown", headers=_bearer(op), json={"unload_models": True}
        )
        client.post("/api/system/shutdown", headers=_bearer(op), content=b"garbage")
        assert client.app.state.host.stop_kwargs[base:] == [True, True, True]
