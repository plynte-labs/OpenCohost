"""laya-decision-layer-20261009 T4: GET/PUT /api/decisions and worker restart.

``decisions.json`` and the Laya env are isolated per test by the autouse
``_isolate_decisions`` fixture in tests/conftest.py, so the worker is
unconfigured unless a test installs a fake client.
"""
from __future__ import annotations

import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

from opencohost.config import decisions as dc
from opencohost.core.decision import runtime as decision_runtime
from tests.test_api_phase1 import FakeHost

_ORIGINS = ["http://localhost:5173"]
# The default engine is onnx; conftest points it at an empty dir.
UNCONFIGURED = {
    "configured": False, "state": "unavailable", "checkpoint": None,
    "last_error": "onnx_artifacts_missing", "engine": "onnx",
}


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_ORIGINS)


def _expected(**over):
    # installed-app-parity-20261014: on by default except vision and respond.
    out = {d: {"mode": "on", "threshold": 0.9} for d in ("memory", "reply_length", "web", "clock")}
    out.update({d: {"mode": "shadow", "threshold": 0.9} for d in ("vision", "respond")})
    for d, entry in over.items():
        out[d] = {**out[d], **entry}
    return out


def test_get_defaults():
    with TestClient(_app()) as client:
        resp = client.get("/api/decisions")
    assert resp.status_code == 200
    assert resp.json() == {"decisions": _expected(), "worker": UNCONFIGURED, "budget_ms": 600}


def test_put_partial_roundtrip_and_live_apply():
    with TestClient(_app()) as client:
        resp = client.put(
            "/api/decisions",
            json={"decisions": {"vision": {"mode": "on"}, "memory": {"threshold": 0.95}}},
        )
        assert resp.status_code == 200
        want = _expected(vision={"mode": "on"}, memory={"threshold": 0.95})
        assert resp.json() == {"decisions": want, "worker": UNCONFIGURED, "budget_ms": 600}
        assert client.get("/api/decisions").json()["decisions"] == want
    assert dc.get_mode("vision") == "on"


def test_put_persists_and_startup_loads_it():
    with TestClient(_app()) as client:
        client.put("/api/decisions", json={"decisions": {"reply_length": {"mode": "off"}}})
    dc.reset_decisions()
    with TestClient(_app()) as client:  # lifespan calls load_decisions()
        assert client.get("/api/decisions").json()["decisions"]["reply_length"]["mode"] == "off"


@pytest.mark.parametrize(
    "body",
    [
        {"decisions": {"respond": {"mode": "on"}}},
        {"decisions": {"vision": {"mode": "maybe"}}},
        {"decisions": {"vision": {"threshold": 0.2}}},
        {"decisions": {"vision": {"threshold": 1.5}}},
        {"decisions": {"tool": {"mode": "on"}}},
        {"decisions": {"vision": {"mode": "on", "extra": 1}}},
        {"decisions": "on"},
        {"nope": 1},
    ],
)
def test_put_422_invalid(body):
    with TestClient(_app()) as client:
        resp = client.put("/api/decisions", json=body)
    assert resp.status_code == 422
    assert resp.json() == {"detail": "invalid_decision_setting"}
    assert dc.get_decision_settings() == _expected()


def test_put_503_on_write_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("read-only")

    with TestClient(_app()) as client:
        monkeypatch.setattr(dc.os, "replace", boom)
        resp = client.put("/api/decisions", json={"decisions": {"vision": {"mode": "off"}}})
    assert resp.status_code == 503
    assert resp.json() == {"detail": "decision_settings_write_failed"}
    assert dc.get_mode("vision") == "shadow"


def test_restart_unconfigured_returns_status():
    with TestClient(_app()) as client:
        resp = client.post("/api/decisions/worker/restart")
    assert resp.status_code == 200
    assert resp.json() == UNCONFIGURED


def test_lifespan_starts_and_stops_a_configured_worker(monkeypatch, tmp_path):
    from opencohost.config import settings

    fake = os.path.join(os.path.dirname(__file__), "fixtures", "fake_laya_worker.py")
    monkeypatch.setenv("FAKE_LAYA_MODE", "ok")
    monkeypatch.setattr(settings, "LAYA_PYTHON", sys.executable)
    monkeypatch.setattr(settings, "LAYA_ENGINE", "torch")
    monkeypatch.setattr("opencohost.core.decision.laya_client.WORKER_PATH", fake)
    with TestClient(_app()) as client:
        worker = decision_runtime.get_client()
        assert worker is not None and worker.wait_ready(10)
        body = client.get("/api/decisions").json()
        assert body["worker"]["configured"] is True and body["worker"]["state"] == "ready"
        assert body["worker"]["engine"] == "torch"
        proc = worker._proc
        assert client.post("/api/decisions/worker/restart").json()["state"] in ("starting", "ready")
        assert worker.wait_ready(10)
        proc = worker._proc
    assert proc.poll() is not None
    assert decision_runtime.get_bus().backend is None


def test_openapi_exposes_mode_literal():
    with TestClient(_app()) as client:
        schema = client.get("/openapi.json").json()
    comps = schema["components"]["schemas"]
    assert comps["DecisionSetting"]["properties"]["mode"]["enum"] == ["off", "shadow", "on"]
    assert "/api/decisions/worker/restart" in schema["paths"]


def test_api_literal_matches_store_modes():
    import typing

    from opencohost.api.models import DecisionModeLiteral

    assert typing.get_args(DecisionModeLiteral) == dc.MODES
