"""T9 runtime-fixes-20261110: the owner-changeable VRM models folder.

Resolution order (one function): saved folder, env, default. The router resolves per
request, so a change applies with no engine restart. The bundled Kira v3 stays listed
and servable whatever the folder is. Nothing in these tests touches a real model.
"""
import json
import logging

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opencohost.api.auth import auth_middleware
from opencohost.api.routers import avatar_vrm
from opencohost.avatar import avatar_config as avatar_config_mod
from opencohost.avatar import vrm_folder
from opencohost.config import settings

BUNDLED = b"glTF-bundled"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated avatar.yaml, bundled model, env folder and tokens."""
    cfg = tmp_path / "config" / "avatar.yaml"
    monkeypatch.setattr(avatar_config_mod, "AVATAR_CONFIG_FILE", cfg)
    bundled = tmp_path / "bundled" / "Opencohost Kira v3.vrm"
    bundled.parent.mkdir()
    bundled.write_bytes(BUNDLED)
    monkeypatch.setattr(settings, "PHONE_VRM_PATH", bundled)
    default_dir = tmp_path / "default"
    default_dir.mkdir()
    monkeypatch.setattr(avatar_vrm, "VRM_MODEL_DIR", default_dir)
    monkeypatch.delenv("OPENCOHOST_VRM_MODEL_DIR", raising=False)
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"operator": "op-token", "agent": "ag-token"}))
    monkeypatch.setattr(settings, "API_TOKENS_FILE", str(tokens))
    monkeypatch.setattr(settings, "API_AUTH_ENFORCED", True)
    return tmp_path


@pytest.fixture
def client(env):
    app = FastAPI()
    app.middleware("http")(auth_middleware)
    app.include_router(avatar_vrm.router)
    with TestClient(app) as c:
        c.headers["Authorization"] = "Bearer op-token"
        yield c


def _folder(tmp_path, name, *models):
    d = tmp_path / name
    d.mkdir()
    for m in models:
        (d / m).write_bytes(b"glTF-" + m.encode())
    return d


# ---- resolution order ------------------------------------------------------

def test_saved_folder_wins_over_env_and_default(env, monkeypatch):
    saved = _folder(env, "saved")
    envdir = _folder(env, "envdir")
    monkeypatch.setenv("OPENCOHOST_VRM_MODEL_DIR", str(envdir))
    cfg = avatar_config_mod.load_avatar_config()
    cfg.vrm_model_dir = str(saved)
    avatar_config_mod.save_avatar_config(cfg)
    path, source = vrm_folder.resolve_vrm_model_dir(env / "default")
    assert (path, source) == (saved, "saved")


def test_vanished_saved_folder_falls_back_to_env(env, monkeypatch):
    envdir = _folder(env, "envdir")
    monkeypatch.setenv("OPENCOHOST_VRM_MODEL_DIR", str(envdir))
    cfg = avatar_config_mod.load_avatar_config()
    cfg.vrm_model_dir = str(env / "gone")
    avatar_config_mod.save_avatar_config(cfg)
    assert vrm_folder.resolve_vrm_model_dir(env / "default") == (env / "default", "env")


def test_saved_pointing_at_a_file_is_ignored(env):
    f = env / "afile"
    f.write_bytes(b"x")
    cfg = avatar_config_mod.load_avatar_config()
    cfg.vrm_model_dir = str(f)
    avatar_config_mod.save_avatar_config(cfg)
    assert vrm_folder.resolve_vrm_model_dir(env / "default")[1] == "default"


def test_default_when_nothing_saved_and_no_env(env):
    assert vrm_folder.resolve_vrm_model_dir(env / "default") == (env / "default", "default")


def test_env_source_when_env_set(env, monkeypatch):
    monkeypatch.setenv("OPENCOHOST_VRM_MODEL_DIR", str(env / "default"))
    assert vrm_folder.resolve_vrm_model_dir(env / "default")[1] == "env"


# ---- validation ------------------------------------------------------------

@pytest.mark.parametrize("raw,code", [
    ("", "path_empty"),
    ("   ", "path_empty"),
    ("relative\\models", "path_not_absolute"),
    ("models", "path_not_absolute"),
    ("\\\\server\\share\\models", "path_network_or_device"),
    ("//server/share/models", "path_network_or_device"),
    ("\\\\?\\C:\\models", "path_network_or_device"),
    ("\\\\.\\PhysicalDrive0", "path_network_or_device"),
    ("C:\\models\x00evil", "path_invalid"),
])
def test_validate_rejects_with_stable_code(raw, code):
    with pytest.raises(vrm_folder.VrmFolderError) as exc:
        vrm_folder.validate_folder(raw)
    assert exc.value.code == code


def test_validate_missing_and_not_a_directory(env):
    with pytest.raises(vrm_folder.VrmFolderError) as exc:
        vrm_folder.validate_folder(str(env / "nope"))
    assert exc.value.code == "path_not_found"
    f = env / "file.vrm"
    f.write_bytes(b"x")
    with pytest.raises(vrm_folder.VrmFolderError) as exc:
        vrm_folder.validate_folder(str(f))
    assert exc.value.code == "path_not_directory"


def test_validate_unreadable(env, monkeypatch):
    d = _folder(env, "locked")

    def boom(_p):
        raise PermissionError

    monkeypatch.setattr(vrm_folder.os, "scandir", boom)
    with pytest.raises(vrm_folder.VrmFolderError) as exc:
        vrm_folder.validate_folder(str(d))
    assert exc.value.code == "path_unreadable"


def test_validate_mapped_network_drive_rejected(env, monkeypatch):
    d = _folder(env, "mapped")
    monkeypatch.setattr(vrm_folder, "_is_remote_drive", lambda p: True)
    with pytest.raises(vrm_folder.VrmFolderError) as exc:
        vrm_folder.validate_folder(str(d))
    assert exc.value.code == "path_network_or_device"


def test_validate_accepts_a_real_folder(env):
    d = _folder(env, "ok")
    assert vrm_folder.validate_folder(str(d)) == d.resolve()


# ---- endpoints -------------------------------------------------------------

def test_get_folder_default(client, env):
    body = client.get("/api/avatar/vrm/folder").json()
    assert body["path"] == str(env / "default")
    assert body["source"] == "default"
    assert body["model_count"] == 0


def test_put_sets_saves_and_lists_without_restart(client, env):
    new = _folder(env, "mine", "a.vrm", "b.vrm", "notes.txt")
    before = [m["filename"] for m in client.get("/api/avatar/vrm/list").json()]
    assert before == ["Opencohost Kira v3.vrm"]
    r = client.put("/api/avatar/vrm/folder", json={"path": str(new)})
    assert r.status_code == 200
    assert r.json()["source"] == "saved" and r.json()["model_count"] == 2
    # The very next request on the same app instance sees the new folder.
    names = [m["filename"] for m in client.get("/api/avatar/vrm/list").json()]
    assert names == ["Opencohost Kira v3.vrm", "a.vrm", "b.vrm"]
    assert client.get("/api/avatar/vrm/model", params={"name": "a.vrm"}).content == b"glTF-a.vrm"
    # Persisted: a fresh load of the config sees it.
    assert avatar_config_mod.load_avatar_config().vrm_model_dir == str(new.resolve())


def test_put_preserves_other_avatar_settings(client, env):
    cfg = avatar_config_mod.load_avatar_config()
    cfg.mode = "static"
    avatar_config_mod.save_avatar_config(cfg)
    new = _folder(env, "mine")
    assert client.put("/api/avatar/vrm/folder", json={"path": str(new)}).status_code == 200
    after = avatar_config_mod.load_avatar_config()
    assert after.mode == "static" and after.vrm_model_dir == str(new.resolve())


def test_avatar_image_upload_keeps_saved_folder(env, monkeypatch):
    monkeypatch.setattr(avatar_config_mod, "USER_AVATAR_DIR", env / "avatar_user")
    cfg = avatar_config_mod.load_avatar_config()
    cfg.vrm_model_dir = str(_folder(env, "keep"))
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 16
    kept = avatar_config_mod.store_uploaded_image(cfg, "idle", "x.png", png)
    assert kept.vrm_model_dir == cfg.vrm_model_dir


@pytest.mark.parametrize("path,code", [
    ("", "path_empty"),
    ("relative", "path_not_absolute"),
    ("\\\\server\\share", "path_network_or_device"),
])
def test_put_rejects_with_422_and_code_and_saves_nothing(client, path, code):
    r = client.put("/api/avatar/vrm/folder", json={"path": path})
    assert r.status_code == 422 and r.json()["detail"] == code
    assert avatar_config_mod.load_avatar_config().vrm_model_dir == ""


def test_put_missing_folder_is_422_not_found(client, env):
    r = client.put("/api/avatar/vrm/folder", json={"path": str(env / "nope")})
    assert r.status_code == 422 and r.json()["detail"] == "path_not_found"


def test_delete_resets_to_default(client, env):
    new = _folder(env, "mine", "a.vrm")
    client.put("/api/avatar/vrm/folder", json={"path": str(new)})
    r = client.delete("/api/avatar/vrm/folder")
    assert r.status_code == 200 and r.json()["source"] == "default"
    assert [m["filename"] for m in client.get("/api/avatar/vrm/list").json()] == ["Opencohost Kira v3.vrm"]
    assert avatar_config_mod.load_avatar_config().vrm_model_dir == ""


def test_write_routes_need_the_operator_token(client, env):
    new = _folder(env, "mine")
    for token in (None, "ag-token", "bogus"):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        c = TestClient(client.app)
        assert c.put("/api/avatar/vrm/folder", json={"path": str(new)}, headers=headers).status_code in (401, 403)
        assert c.delete("/api/avatar/vrm/folder", headers=headers).status_code in (401, 403)
    assert avatar_config_mod.load_avatar_config().vrm_model_dir == ""


def test_route_never_writes_into_the_chosen_folder(client, env):
    new = _folder(env, "mine", "a.vrm")
    client.put("/api/avatar/vrm/folder", json={"path": str(new)})
    client.get("/api/avatar/vrm/list")
    client.delete("/api/avatar/vrm/folder")
    assert sorted(p.name for p in new.iterdir()) == ["a.vrm"]


def test_logs_carry_enum_and_count_never_the_path(client, env, caplog):
    new = _folder(env, "secret-user-name", "a.vrm")
    with caplog.at_level(logging.DEBUG):
        client.put("/api/avatar/vrm/folder", json={"path": str(new)})
        client.put("/api/avatar/vrm/folder", json={"path": str(env / "secret-user-missing")})
        client.delete("/api/avatar/vrm/folder")
    text = caplog.text
    assert "[VRM_FOLDER]" in text and "outcome=set" in text and "models=1" in text
    assert "secret-user" not in text


# ---- bundled model ---------------------------------------------------------

def test_bundled_listed_first_marked_and_deduplicated(client, env):
    new = _folder(env, "mine", "Opencohost Kira v3.vrm", "z.vrm")
    client.put("/api/avatar/vrm/folder", json={"path": str(new)})
    items = client.get("/api/avatar/vrm/list").json()
    assert [m["filename"] for m in items] == ["Opencohost Kira v3.vrm", "z.vrm"]
    assert [m["bundled"] for m in items] == [True, False]
    assert {"filename", "size_mb", "modified_at"} <= set(items[0])
    # The bundled copy wins over the same-named file in the folder.
    got = client.get("/api/avatar/vrm/model", params={"name": "Opencohost Kira v3.vrm"})
    assert got.content == BUNDLED


def test_bundled_is_served_when_folder_has_no_models(client):
    got = client.get("/api/avatar/vrm/model", params={"name": "Opencohost Kira v3.vrm"})
    assert got.status_code == 200 and got.content == BUNDLED


def test_missing_bundled_file_is_omitted(client, env, monkeypatch):
    monkeypatch.setattr(settings, "PHONE_VRM_PATH", env / "bundled" / "gone.vrm")
    assert client.get("/api/avatar/vrm/list").json() == []
    assert client.get("/api/avatar/vrm/model", params={"name": "gone.vrm"}).status_code == 404


def test_bundled_route_is_not_a_traversal_hole(client, env):
    (env / "secret.vrm").write_bytes(b"private")
    for name in ("../secret.vrm", "..\\secret.vrm", "bundled/Opencohost Kira v3.vrm",
                 "C:Opencohost Kira v3.vrm", "Opencohost Kira v3.vrm:evil"):
        assert client.get("/api/avatar/vrm/model", params={"name": name}).status_code == 400
    # Another name never resolves to the bundled directory.
    assert client.get("/api/avatar/vrm/model", params={"name": "secret.vrm"}).status_code == 404


def test_phone_model_route_is_unchanged(env):
    # The gateway reads settings.PHONE_VRM_PATH directly; the folder choice is not involved.
    cfg = avatar_config_mod.load_avatar_config()
    cfg.vrm_model_dir = str(_folder(env, "other", "x.vrm"))
    avatar_config_mod.save_avatar_config(cfg)
    assert settings.PHONE_VRM_PATH.read_bytes() == BUNDLED
