"""Tests for the avatar config Tier-C endpoints (WS3 slice 2):
/api/avatar/config (GET/PUT), /api/avatar/upload (POST) and /api/avatar/image (GET).

Uploads are bytes (JSON+base64), copied server-side into the user avatar dir —
no multipart handling anywhere. Isolation: AVATAR_CONFIG_FILE/USER_AVATAR_DIR/
_BUNDLED_AVATAR_DIR are patched to tmp dirs so no real user config is touched.
"""

import base64

import pytest
from fastapi.testclient import TestClient

from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_JPG = b"\xff\xd8\xff\xe0" + b"\x00" * 32


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
    monkeypatch.setattr(avatar_config_mod, "USER_AVATAR_DIR", tmp_path / "user")
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
    """PUT stays paths-only (JSON); bytes upload lives at POST /api/avatar/upload."""
    app = _app()
    with TestClient(app) as client:
        resp = client.put(
            "/api/avatar/config",
            files={"file": ("idle.png", b"fake-bytes", "image/png")},
        )
        assert resp.status_code == 422


def test_get_image_falls_back_to_bundled_default():
    """Fresh install (empty user config): bundled `assets/avatar/kira` still serves."""
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/avatar/image", params={"state": "idle"})
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert len(resp.content) > 1000


# ──────────────────────────────────────────────────────────────────────────
# POST /api/avatar/upload (bytes in, user-data copy out)
# ──────────────────────────────────────────────────────────────────────────


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def test_upload_copies_bytes_into_user_dir(tmp_path):
    import opencohost.avatar.avatar_config as avatar_config_mod

    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/avatar/upload",
            json={"state": "idle", "filename": "Kira.png", "content_b64": _b64(_PNG)},
        )
        assert resp.status_code == 200
        body = resp.json()
        stored = body["state_images"]["idle"]
        # Canonical user-dir location, canonical name — not the source path.
        assert stored == str(avatar_config_mod.USER_AVATAR_DIR / "idle.png")
        assert (tmp_path / "user" / "idle.png").read_bytes() == _PNG
        # Survives the test tmp dir intact (nothing references a source file).
        get_resp = client.get("/api/avatar/config")
        assert get_resp.json()["state_images"]["idle"] == stored


def test_upload_replaces_previous_same_state():
    app = _app()
    with TestClient(app) as client:
        client.post(
            "/api/avatar/upload",
            json={"state": "angry", "filename": "a.png", "content_b64": _b64(_PNG)},
        )
        resp = client.post(
            "/api/avatar/upload",
            json={"state": "angry", "filename": "b.jpg", "content_b64": _b64(_JPG)},
        )
        assert resp.status_code == 200
        assert resp.json()["state_images"]["angry"].endswith("angry.jpg")


def test_upload_unknown_state_422_leaves_config_untouched():
    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/avatar/upload",
            json={"state": "bogus", "filename": "x.png", "content_b64": _b64(_PNG)},
        )
        assert resp.status_code == 422
        assert client.get("/api/avatar/config").json()["state_images"] == {}


def test_upload_bad_extension_422():
    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/avatar/upload",
            json={"state": "idle", "filename": "x.exe", "content_b64": _b64(_PNG)},
        )
        assert resp.status_code == 422


def test_upload_content_mismatch_422():
    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/avatar/upload",
            json={"state": "idle", "filename": "x.png", "content_b64": _b64(b"not-an-image")},
        )
        assert resp.status_code == 422


def test_upload_invalid_base64_422():
    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/avatar/upload",
            json={"state": "idle", "filename": "x.png", "content_b64": "!!!"},
        )
        assert resp.status_code == 422


def test_upload_oversize_413(monkeypatch):
    import opencohost.avatar.avatar_config as avatar_config_mod

    monkeypatch.setattr(avatar_config_mod, "MAX_UPLOAD_BYTES", 16)
    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/avatar/upload",
            json={"state": "idle", "filename": "x.png", "content_b64": _b64(_PNG)},
        )
        assert resp.status_code == 413


def test_upload_predcode_guard_413_without_decoding(monkeypatch):
    """Bodies larger than the base64 ceiling are refused BEFORE b64decode."""
    import opencohost.avatar.avatar_config as avatar_config_mod

    monkeypatch.setattr(avatar_config_mod, "MAX_B64_CHARS", 8)
    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/avatar/upload",
            json={"state": "idle", "filename": "x.png", "content_b64": "A" * 9},
        )
        assert resp.status_code == 413


def test_upload_cross_format_reupload_removes_orphan(tmp_path):
    app = _app()
    with TestClient(app) as client:
        client.post(
            "/api/avatar/upload",
            json={"state": "angry", "filename": "a.png", "content_b64": _b64(_PNG)},
        )
        assert (tmp_path / "user" / "angry.png").exists()
        client.post(
            "/api/avatar/upload",
            json={"state": "angry", "filename": "b.jpg", "content_b64": _b64(_JPG)},
        )
        assert (tmp_path / "user" / "angry.jpg").exists()
        assert not (tmp_path / "user" / "angry.png").exists()


def test_get_image_mapped_directory_404s(tmp_path, monkeypatch):
    """A mapped directory (never a file) resolves to our 404, not a 500."""
    import opencohost.avatar.avatar_config as avatar_config_mod

    subdir = tmp_path / "somedir"
    subdir.mkdir()
    empty = tmp_path / "empty"
    empty.mkdir()
    # Isolate the bundled fallback too, or the repo's own idle.png answers.
    monkeypatch.setattr(avatar_config_mod, "_BUNDLED_AVATAR_DIR", empty)
    (tmp_path / "avatar.yaml").write_text(
        "avatar:\n  enabled: true\n  mode: image_states\n"
        f"  assets_folder: '{empty}'\n"
        f"  state_images:\n    idle: '{subdir}'\n",
        encoding="utf-8",
    )
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/avatar/image", params={"state": "idle"})
        assert resp.status_code == 404


# ──────────────────────────────────────────────────────────────────────────
# GET /api/avatar/image (serve resolved bytes)
# ──────────────────────────────────────────────────────────────────────────


def test_get_image_serves_uploaded_bytes_with_etag():
    app = _app()
    with TestClient(app) as client:
        client.post(
            "/api/avatar/upload",
            json={"state": "idle", "filename": "Kira.png", "content_b64": _b64(_PNG)},
        )
        resp = client.get("/api/avatar/image", params={"state": "idle"})
        assert resp.status_code == 200
        assert resp.content == _PNG
        assert resp.headers["content-type"] == "image/png"
        etag = resp.headers["etag"]
        assert etag
        reval = client.get(
            "/api/avatar/image", params={"state": "idle"}, headers={"if-none-match": etag}
        )
        assert reval.status_code == 304


def test_get_image_unknown_state_422():
    app = _app()
    with TestClient(app) as client:
        assert client.get("/api/avatar/image", params={"state": "bogus"}).status_code == 422


def test_get_image_missing_everywhere_404(tmp_path, monkeypatch):
    """With an empty user dir, empty assets folder and empty bundled dir,
    an unmapped non-idle state 404s instead of serving Kira defaults."""
    import opencohost.avatar.avatar_config as avatar_config_mod

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(avatar_config_mod, "_BUNDLED_AVATAR_DIR", empty)
    (tmp_path / "avatar.yaml").write_text(
        "avatar:\n  enabled: true\n  mode: image_states\n"
        f"  assets_folder: '{empty}'\n  state_images: {{}}\n",
        encoding="utf-8",
    )
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/avatar/image", params={"state": "idle"})
        assert resp.status_code == 404
        # Feature-specific body: an unregistered route also 404s but answers
        # {"detail": "Not Found"} — this pins OUR 404 (judge finding).
        assert resp.json() == {"detail": "no image for state 'idle'"}
