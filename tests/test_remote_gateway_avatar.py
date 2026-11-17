"""T1 of remote-vrm-avatar: avatar bundle, VRM model and idle clips on the gateway.

Never touches the real 20 MB model: settings are pointed at tmp dirs.
"""

import pytest
from fastapi.testclient import TestClient

from tests.test_remote_gateway_phone_page import EXPECTED_HEADERS
from tests.test_remote_gateway_turns import Rig

MODEL_BYTES = b"glTF" + b"\x00" * 64
CLIPS = [
    "VRMA_02", "idle", "VRMA_06", "005_smartphone", "004_hello_1",
    "006_drinkwater", "001_motion_pose", "VRMA_03", "VRMA_04",
]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from opencohost.api.routers import avatar_vrm
    from opencohost.config import settings

    models, anims = tmp_path / "models", tmp_path / "anims"
    models.mkdir()
    anims.mkdir()
    (models / "a.vrm").write_bytes(b"glTF-other-model")
    (models / "notes.txt").write_text("x")
    phone = tmp_path / "Kira v3.vrm"
    phone.write_bytes(MODEL_BYTES)
    for clip in CLIPS + ["evil"]:
        (anims / f"{clip}.vrma").write_bytes(b"clip-" + clip.encode())
    monkeypatch.setattr(avatar_vrm, "VRM_MODEL_DIR", models)
    monkeypatch.setattr(settings, "VRMA_DIR", anims)
    monkeypatch.setattr(settings, "PHONE_VRM_PATH", phone)
    return Rig()


def test_avatar_js_is_open_with_page_headers(rig):
    r = rig.client.get("/avatar.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/javascript")
    for name, value in EXPECTED_HEADERS.items():
        assert r.headers[name] == value
    assert "access-control-allow-origin" not in r.headers


def test_csp_img_src_allows_blob_only_extra(rig):
    csp = rig.client.get("/avatar.js").headers["content-security-policy"]
    assert "img-src 'self' data: blob:" in csp
    assert "default-src 'none'" in csp and "connect-src 'self'" in csp


def test_avatar_js_404_when_static_file_missing(tmp_path, monkeypatch):
    import opencohost.gateway.app as app_mod

    (tmp_path / "phone.html").write_bytes(b"<html></html>")
    (tmp_path / "phone.js").write_bytes(b"//")
    monkeypatch.setattr(app_mod, "_STATIC_DIR", tmp_path)
    r = Rig().client.get("/avatar.js")
    assert r.status_code == 404


def test_model_requires_bearer(rig):
    assert rig.client.get("/avatar/model").status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert rig.client.get("/avatar/model", headers=bad).status_code == 401


def test_model_serves_phone_vrm_path_not_model_dir(rig):
    _, headers = rig.pair()
    r = rig.client.get("/avatar/model", headers=headers)
    assert r.status_code == 200
    assert r.headers["content-type"] == "model/gltf-binary"
    assert r.headers["cache-control"] == "private, max-age=0, must-revalidate"
    assert r.headers["etag"]
    assert r.content == MODEL_BYTES
    assert "Kira" not in str(dict(r.headers))
    assert "access-control-allow-origin" not in r.headers


def test_model_revalidates_with_304(rig):
    _, headers = rig.pair()
    etag = rig.client.get("/avatar/model", headers=headers).headers["etag"]
    r = rig.client.get("/avatar/model", headers={**headers, "If-None-Match": etag})
    assert r.status_code == 304
    assert r.content == b""


def test_model_ignores_query_parameters(rig):
    _, headers = rig.pair()
    r = rig.client.get("/avatar/model", params={"name": "a.vrm", "path": "../x"}, headers=headers)
    assert r.content == MODEL_BYTES


def test_model_404_when_phone_file_missing_even_if_dir_has_models(rig, tmp_path, monkeypatch):
    from opencohost.config import settings

    monkeypatch.setattr(settings, "PHONE_VRM_PATH", tmp_path / "missing.vrm")
    _, headers = rig.pair()
    assert rig.client.get("/avatar/model", headers=headers).status_code == 404


def test_model_404_when_phone_path_is_not_vrm(rig, tmp_path, monkeypatch):
    from opencohost.config import settings

    txt = tmp_path / "model.txt"
    txt.write_bytes(MODEL_BYTES)
    monkeypatch.setattr(settings, "PHONE_VRM_PATH", txt)
    _, headers = rig.pair()
    assert rig.client.get("/avatar/model", headers=headers).status_code == 404


def test_model_404_when_phone_path_is_a_directory(rig, tmp_path, monkeypatch):
    from opencohost.config import settings

    d = tmp_path / "dir.vrm"
    d.mkdir()
    monkeypatch.setattr(settings, "PHONE_VRM_PATH", d)
    _, headers = rig.pair()
    assert rig.client.get("/avatar/model", headers=headers).status_code == 404


def test_default_phone_vrm_path_is_kira_v3_in_resources():
    from opencohost.config import settings

    assert settings.PHONE_VRM_PATH.name == "Opencohost Kira v3.vrm"
    assert settings.PHONE_VRM_PATH.parent.parts[-3:] == ("src-tauri", "resources", "vrm")


@pytest.mark.parametrize("clip", CLIPS)
def test_allowlisted_clips_are_served_open(rig, clip):
    r = rig.client.get(f"/animations/{clip}.vrma")
    assert r.status_code == 200
    assert r.content == b"clip-" + clip.encode()
    assert r.headers["content-type"] == "application/octet-stream"
    assert r.headers["cache-control"] == "private, max-age=86400"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "access-control-allow-origin" not in r.headers


def test_clip_allowlist_missing_file_is_404(rig):
    import opencohost.config.settings as settings

    (settings.VRMA_DIR / "idle.vrma").unlink()
    assert rig.client.get("/animations/idle.vrma").status_code == 404


@pytest.mark.parametrize(
    "path",
    [
        "/animations/evil.vrma",
        "/animations/idle.vrm",
        "/animations/idle",
        "/animations/..%2Fidle.vrma",
        "/animations/%2e%2e/idle.vrma",
        "/animations/idle.vrma/x",
        "/avatar/anything",
        "/avatar/model/../x",
        "/avatar/model/extra",
    ],
)
def test_other_avatar_paths_are_404(rig, path):
    _, headers = rig.pair()
    assert rig.client.get(path, headers=headers).status_code == 404
