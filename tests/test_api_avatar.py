"""Tests for the avatar config Tier-C endpoints (WS3 slice 2):
/api/avatar/config (GET/PUT).

Image UPLOAD is deferred (owner decision) — these endpoints are paths-only,
no multipart handling. Isolation: AVATAR_CONFIG_FILE is patched to a tmp
yaml so no real user config is ever touched.
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
def _isolated_avatar_yaml(tmp_path, monkeypatch):
    import opencohost.avatar.avatar_config as avatar_config_mod

    monkeypatch.setattr(avatar_config_mod, "AVATAR_CONFIG_FILE", tmp_path / "avatar.yaml")
    yield


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)


# ──────────────────────────────────────────────────────────────────────────
# GET /api/avatar/config
# ──────────────────────────────────────────────────────────────────────────


def test_get_avatar_config_shape():
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/avatar/config")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"enabled", "mode", "assets_folder", "state_images"}
        assert isinstance(body["state_images"], dict)
        assert isinstance(body["assets_folder"], str)


# ──────────────────────────────────────────────────────────────────────────
# PUT /api/avatar/config (round-trip + unknown-state 422)
# ──────────────────────────────────────────────────────────────────────────


def test_put_avatar_config_round_trips(tmp_path):
    app = _app()
    with TestClient(app) as client:
        image_path = str(tmp_path / "idle.png")
        resp = client.put(
            "/api/avatar/config",
            json={"enabled": False, "mode": "video", "state_images": {"idle": image_path}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["enabled"] is False
        assert body["mode"] == "video"
        assert body["state_images"]["idle"] == image_path

        get_resp = client.get("/api/avatar/config")
        get_body = get_resp.json()
        assert get_body["enabled"] is False
        assert get_body["mode"] == "video"
        assert get_body["state_images"]["idle"] == image_path


def test_put_avatar_config_partial_update_keeps_other_states(tmp_path):
    app = _app()
    with TestClient(app) as client:
        idle_path = str(tmp_path / "idle.png")
        angry_path = str(tmp_path / "angry.png")
        client.put("/api/avatar/config", json={"state_images": {"idle": idle_path}})
        resp = client.put("/api/avatar/config", json={"state_images": {"angry": angry_path}})
        assert resp.status_code == 200
        body = resp.json()
        assert body["state_images"]["idle"] == idle_path
        assert body["state_images"]["angry"] == angry_path


def test_put_avatar_config_unspecified_fields_unchanged():
    app = _app()
    with TestClient(app) as client:
        client.put("/api/avatar/config", json={"mode": "video"})
        resp = client.put("/api/avatar/config", json={"enabled": False})
        body = resp.json()
        assert body["enabled"] is False
        assert body["mode"] == "video"  # not reset by the second PUT


def test_put_avatar_config_unknown_state_422():
    app = _app()
    with TestClient(app) as client:
        resp = client.put(
            "/api/avatar/config", json={"state_images": {"bogus_state": "/tmp/x.png"}}
        )
        assert resp.status_code == 422

        # rejected PUT must leave the config untouched
        get_resp = client.get("/api/avatar/config")
        assert "bogus_state" not in get_resp.json()["state_images"]


def test_put_avatar_config_mixed_known_and_unknown_state_rejects_whole_request(tmp_path):
    app = _app()
    with TestClient(app) as client:
        idle_path = str(tmp_path / "idle.png")
        resp = client.put(
            "/api/avatar/config",
            json={"state_images": {"idle": idle_path, "bogus_state": "/tmp/x.png"}},
        )
        assert resp.status_code == 422

        get_resp = client.get("/api/avatar/config")
        assert get_resp.json()["state_images"] == {}


def test_put_avatar_config_no_multipart_upload_surface():
    """Image UPLOAD is deferred (owner decision) — JSON paths only, never
    multipart/form-data."""
    app = _app()
    with TestClient(app) as client:
        resp = client.put(
            "/api/avatar/config",
            files={"file": ("idle.png", b"fake-bytes", "image/png")},
        )
        assert resp.status_code == 422
