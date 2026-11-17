"""GET/PUT /api/setup/state (setup-assistant T1).

The status file is redirected per test; Ollama and the key store are faked at
the ``llm_readiness`` seams so nothing touches the network or the keyring.
"""

import json

import pytest
from fastapi.testclient import TestClient

from opencohost.config import settings, setup_state
from opencohost.core.engine import llm_readiness
from tests.test_api_phase1 import FakeHost

_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    monkeypatch.setattr(settings, "SETUP_STATE_FILE", str(tmp_path / "setup_state.json"))
    monkeypatch.setattr(settings, "SETUP_CHECKPOINT_FILE", str(tmp_path / "setup_checkpoint.json"))
    monkeypatch.setattr(llm_readiness, "_ping_ollama", lambda *a, **k: (False, None, []))
    monkeypatch.setattr(llm_readiness, "_check_cloud_key_configured", lambda *a, **k: False)
    monkeypatch.setattr(setup_state, "detect_legacy_install", lambda: False)
    yield
    main_mod._host_active = False


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_ORIGINS)


def _cloud(client_app):
    client_app.state.host.motor._provider_config = {
        "active_provider": "nim",
        "profiles": {"nim": {"base_url": "https://x/v1", "model": "m"}},
    }


def test_get_defaults_to_pending_with_no_engine():
    with TestClient(_app()) as client:
        resp = client.get("/api/setup/state")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "pending",
        "engine_ready": False,
        "engine": None,
        "show_on_start": True,
        "checkpoint": None,
        "checkpoint_reset": None,
        "legacy_install": False,
    }


def test_local_models_make_engine_ready(monkeypatch):
    monkeypatch.setattr(llm_readiness, "_ping_ollama", lambda *a, **k: (True, "x", ["qwen3:1.7b"]))
    with TestClient(_app()) as client:
        body = client.get("/api/setup/state").json()
    assert body["engine_ready"] is True and body["engine"] == "local"
    assert body["status"] == "pending" and body["show_on_start"] is True


def test_active_cloud_profile_with_key_is_ready(monkeypatch):
    monkeypatch.setattr(llm_readiness, "_check_cloud_key_configured", lambda pid="cloud": pid == "nim")
    app = _app()
    with TestClient(app) as client:
        _cloud(app)
        body = client.get("/api/setup/state").json()
    assert body["engine_ready"] is True and body["engine"] == "cloud"


def test_active_cloud_profile_without_key_and_no_models_is_not_ready():
    app = _app()
    with TestClient(app) as client:
        _cloud(app)
        body = client.get("/api/setup/state").json()
    assert body["engine_ready"] is False and body["engine"] is None


def test_ollama_probe_failure_never_raises(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")

    monkeypatch.setattr(llm_readiness, "_ping_ollama", boom)
    with TestClient(_app()) as client:
        resp = client.get("/api/setup/state")
    assert resp.status_code == 200 and resp.json()["engine_ready"] is False


def test_put_persists_status_and_get_reads_it_back(monkeypatch):
    monkeypatch.setattr(llm_readiness, "_ping_ollama", lambda *a, **k: (True, "x", ["m"]))
    with TestClient(_app()) as client:
        resp = client.put("/api/setup/state", json={"status": "skipped"})
        assert resp.status_code == 200
        assert resp.json() == {
            "status": "skipped",
            "engine_ready": True,
            "engine": "local",
            "show_on_start": False,
            "checkpoint": None,
            "checkpoint_reset": None,
            "legacy_install": False,
        }
        assert client.get("/api/setup/state").json()["status"] == "skipped"
    with open(settings.SETUP_STATE_FILE, encoding="utf-8") as f:
        assert json.load(f) == {"status": "skipped"}


def test_completed_without_engine_shows_on_start():
    with TestClient(_app()) as client:
        body = client.put("/api/setup/state", json={"status": "completed"}).json()
    assert body["status"] == "completed" and body["show_on_start"] is True


def test_completed_with_engine_does_not_show(monkeypatch):
    monkeypatch.setattr(llm_readiness, "_ping_ollama", lambda *a, **k: (True, "x", ["m"]))
    with TestClient(_app()) as client:
        body = client.put("/api/setup/state", json={"status": "completed"}).json()
    assert body["show_on_start"] is False


@pytest.mark.parametrize("payload", [{"status": "bogus"}, {}, {"status": None}, {"status": 3}])
def test_put_invalid_status_is_422(payload):
    with TestClient(_app()) as client:
        assert client.put("/api/setup/state", json=payload).status_code == 422


def test_corrupt_or_wrong_typed_file_falls_back_to_pending():
    for raw in ("{not json", json.dumps(["x"]), json.dumps({"status": "weird"})):
        with open(settings.SETUP_STATE_FILE, "w", encoding="utf-8") as f:
            f.write(raw)
        with TestClient(_app()) as client:
            assert client.get("/api/setup/state").json()["status"] == "pending"


def test_write_failure_is_503(monkeypatch):
    from opencohost.config import setup_state

    def fail(*a, **k):
        raise OSError("disk")

    monkeypatch.setattr(setup_state, "atomic_write_text", fail)
    with TestClient(_app()) as client:
        resp = client.put("/api/setup/state", json={"status": "skipped"})
    assert resp.status_code == 503
    assert resp.json()["detail"] == "setup_state_write_failed"


def _settle(app, *, warming=False, check_done=True):
    host = app.state.host
    host.ollama_warming = warming
    host.motor.startup_check_done = check_done


def test_completed_setup_does_not_open_wizard_while_ollama_warming():
    app = _app()
    with TestClient(app) as client:
        _settle(app, warming=True)
        body = client.put("/api/setup/state", json={"status": "completed"}).json()
    assert body["engine_ready"] is False and body["show_on_start"] is False


def test_completed_setup_does_not_open_wizard_before_first_engine_check():
    app = _app()
    with TestClient(app) as client:
        _settle(app, check_done=False)
        body = client.put("/api/setup/state", json={"status": "completed"}).json()
    assert body["show_on_start"] is False


def test_completed_setup_opens_wizard_once_settled_without_engine():
    app = _app()
    with TestClient(app) as client:
        _settle(app)
        body = client.put("/api/setup/state", json={"status": "completed"}).json()
    assert body["show_on_start"] is True


def test_pending_setup_still_opens_wizard_while_settling():
    app = _app()
    with TestClient(app) as client:
        _settle(app, warming=True, check_done=False)
        body = client.get("/api/setup/state").json()
    assert body["status"] == "pending" and body["show_on_start"] is True
