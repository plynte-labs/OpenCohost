"""Tests for POST /api/llm/provider/probe (WU2, cloud_rearm_20260801).

Thin synchronous surface over MotorVocalIA.trigger_cloud_probe_now() (WU1),
same tier as PUT /api/llm/provider (main.py's put_llm_provider): direct
motor call, no dispatcher, no config-file write. Fixtures mirror
tests/test_llm_provider_config.py; FakeMotor/FakeHost come from
tests/test_api_phase1.py.
"""

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


@pytest.fixture(autouse=True)
def _isolated_llm_provider_files(tmp_path, monkeypatch):
    """No real llm_provider.json / llm_keys.json is ever touched by these tests."""
    import opencohost.api.main as main_mod
    import opencohost.config.llm_provider as llm_provider_mod

    monkeypatch.setattr(
        llm_provider_mod, "LLM_PROVIDER_CONFIG_FILE", str(tmp_path / "llm_provider.json")
    )
    monkeypatch.setattr(main_mod, "LLM_KEYS_FILE", str(tmp_path / "llm_keys.json"))
    return tmp_path


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)


def test_returns_motor_result():
    app = _app()
    with TestClient(app) as client:
        client.app.state.host.motor.trigger_cloud_probe_now.return_value = {
            "armed": True,
            "reason": None,
        }
        resp = client.post("/api/llm/provider/probe")
        assert resp.status_code == 200
        assert resp.json() == {"armed": True, "reason": None}


def test_200_on_not_in_fallback():
    app = _app()
    with TestClient(app) as client:
        client.app.state.host.motor.trigger_cloud_probe_now.return_value = {
            "armed": False,
            "reason": "not_in_fallback",
        }
        resp = client.post("/api/llm/provider/probe")
        assert resp.status_code == 200
        assert resp.json() == {"armed": False, "reason": "not_in_fallback"}


def test_503_when_method_missing():
    app = _app()
    with TestClient(app) as client:
        # Simulate a motor build that predates trigger_cloud_probe_now.
        del client.app.state.host.motor.trigger_cloud_probe_now
        resp = client.post("/api/llm/provider/probe")
        assert resp.status_code == 503
        assert resp.json() == {"detail": "motor_unavailable"}


def test_never_writes_config_file(tmp_path):
    app = _app()
    with TestClient(app) as client:
        resp = client.post("/api/llm/provider/probe")
        assert resp.status_code == 200
    assert not (tmp_path / "llm_provider.json").exists()
    assert not (tmp_path / "llm_keys.json").exists()
