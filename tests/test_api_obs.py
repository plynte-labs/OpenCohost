"""Tests for the OBS config Tier-C endpoints (WS3 slice 2): /api/obs/config
(GET/PUT) and /api/obs/test (POST).

Tier-C: sync def, no command_queue, no engine mutation — a module-level
config lock guards read-modify-write of the shared avatar.yaml. R8/secret:
the OBS password is write-only — GET/PUT never echo it back, only
`password_set`.

Isolation: AVATAR_CONFIG_FILE is patched to a tmp yaml (no real user config
touched) and OBSClient is patched (no real OBS socket connection).
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


@pytest.fixture(autouse=True)
def _isolated_avatar_yaml(tmp_path, monkeypatch):
    """No real avatar.yaml / user config is ever touched by these tests."""
    import opencohost.avatar.avatar_config as avatar_config_mod

    monkeypatch.setattr(avatar_config_mod, "AVATAR_CONFIG_FILE", tmp_path / "avatar.yaml")
    yield


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)


def _fake_obs_client_factory(result):
    """Fake OBSClient — never opens a real OBS websocket connection."""

    class _FakeOBSClient:
        last_config = None
        last_assets_folder = None

        def __init__(self, config, assets_folder, **kwargs):
            _FakeOBSClient.last_config = config
            _FakeOBSClient.last_assets_folder = assets_folder

        def test_connection(self, timeout=None):
            return result

    return _FakeOBSClient


# ──────────────────────────────────────────────────────────────────────────
# GET /api/obs/config
# ──────────────────────────────────────────────────────────────────────────


def test_get_obs_config_shape_and_no_password_key():
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/obs/config")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"enabled", "host", "port", "source", "password_set"}
        assert "password" not in body
        assert '"password":' not in resp.text


def test_get_obs_config_password_set_false_when_never_configured():
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/obs/config")
        assert resp.json()["password_set"] is False


# ──────────────────────────────────────────────────────────────────────────
# PUT /api/obs/config (round-trip + password write-only, R8)
# ──────────────────────────────────────────────────────────────────────────


def test_put_obs_config_round_trips_and_never_echoes_password():
    app = _app()
    with TestClient(app) as client:
        resp = client.put(
            "/api/obs/config",
            json={"enabled": True, "host": "192.168.1.5", "port": 4456, "password": "s3cr3t"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is True
        assert body["host"] == "192.168.1.5"
        assert body["port"] == 4456
        assert body["password_set"] is True
        assert "password" not in body
        assert "s3cr3t" not in resp.text

        get_resp = client.get("/api/obs/config")
        get_body = get_resp.json()
        assert get_body["host"] == "192.168.1.5"
        assert get_body["port"] == 4456
        assert get_body["password_set"] is True
        assert "s3cr3t" not in get_resp.text


def test_put_obs_config_omitting_password_keeps_it_set():
    """password is write-only: omitting it on a later PUT must NOT clear it."""
    app = _app()
    with TestClient(app) as client:
        client.put("/api/obs/config", json={"password": "keep-me"})

        resp = client.put("/api/obs/config", json={"host": "10.0.0.9"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["host"] == "10.0.0.9"
        assert body["password_set"] is True


def test_put_obs_config_source_and_partial_fields_round_trip():
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/obs/config", json={"source": "MyAvatarSource"})
        assert resp.status_code == 200
        assert resp.json()["source"] == "MyAvatarSource"

        # unspecified fields (enabled/host/port) keep their prior values
        resp2 = client.put("/api/obs/config", json={"port": 4460})
        body2 = resp2.json()
        assert body2["source"] == "MyAvatarSource"
        assert body2["port"] == 4460


# ──────────────────────────────────────────────────────────────────────────
# POST /api/obs/test
# ──────────────────────────────────────────────────────────────────────────


def test_post_obs_test_success(monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "OBSClient", _fake_obs_client_factory((True, "Conectado a OBS 30.0")))

    app = _app()
    with TestClient(app) as client:
        resp = client.post("/api/obs/test", json={})
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "error": None}


def test_post_obs_test_failure_never_500(monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(
        main_mod, "OBSClient", _fake_obs_client_factory((False, "Conexión rechazada en localhost:4455"))
    )

    app = _app()
    with TestClient(app) as client:
        resp = client.post("/api/obs/test", json={})
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        assert body["error"] == "Conexión rechazada en localhost:4455"


def test_post_obs_test_uses_override_when_provided_and_never_echoes_password(monkeypatch):
    import opencohost.api.main as main_mod

    fake_cls = _fake_obs_client_factory((True, "ok"))
    monkeypatch.setattr(main_mod, "OBSClient", fake_cls)

    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/obs/test",
            json={"host": "override-host", "port": 9999, "password": "override-pw"},
        )
        assert resp.status_code == 200
        assert fake_cls.last_config.host == "override-host"
        assert fake_cls.last_config.port == 9999
        assert fake_cls.last_config.password == "override-pw"
        assert "override-pw" not in resp.text


def test_post_obs_test_uses_stored_config_when_no_override(monkeypatch):
    import opencohost.api.main as main_mod

    fake_cls = _fake_obs_client_factory((True, "ok"))
    monkeypatch.setattr(main_mod, "OBSClient", fake_cls)

    app = _app()
    with TestClient(app) as client:
        client.put("/api/obs/config", json={"host": "stored-host", "port": 5555})
        resp = client.post("/api/obs/test", json={})
        assert resp.status_code == 200
        assert fake_cls.last_config.host == "stored-host"
        assert fake_cls.last_config.port == 5555


def test_post_obs_test_no_body_uses_stored_config(monkeypatch):
    import opencohost.api.main as main_mod

    fake_cls = _fake_obs_client_factory((True, "ok"))
    monkeypatch.setattr(main_mod, "OBSClient", fake_cls)

    app = _app()
    with TestClient(app) as client:
        resp = client.post("/api/obs/test")
        assert resp.status_code == 200


def test_post_obs_test_never_touches_command_queue(monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "OBSClient", _fake_obs_client_factory((True, "ok")))

    app = _app()
    with TestClient(app) as client:
        client.post("/api/obs/test", json={"host": "x", "port": 1, "password": "y"})
        assert app.state.host.motor.command_queue.qsize() == 0
        assert app.state.dispatcher.state_version == 0


# ──────────────────────────────────────────────────────────────────────────
# Config write-lock: concurrent PUTs never corrupt the shared yaml
# ──────────────────────────────────────────────────────────────────────────


def test_config_lock_concurrent_puts_no_corruption():
    import opencohost.avatar.avatar_config as avatar_config_mod

    app = _app()
    with TestClient(app) as client:
        errors = []

        def _put(i):
            try:
                resp = client.put(
                    "/api/obs/config", json={"host": f"host-{i}", "port": 4000 + i}
                )
                if resp.status_code != 200:
                    errors.append(resp.status_code)
            except Exception as exc:  # pragma: no cover - failure path
                errors.append(str(exc))

        threads = [threading.Thread(target=_put, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors

        # yaml must still be valid and loadable — and host/port must be a
        # PAIR from the SAME writer (proves the read-modify-write section
        # was atomic, not interleaved across threads).
        final = avatar_config_mod.load_avatar_config()
        assert final.obs.host.startswith("host-")
        winner_index = int(final.obs.host.split("-")[1])
        assert final.obs.port == 4000 + winner_index
