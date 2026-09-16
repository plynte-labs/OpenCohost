"""VRM serving is confined to the model root; audio is consumer-owned."""
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opencohost.api.routers import avatar_vrm
from opencohost.core.speech.vrm_audio import VrmAudioSnapshot


@pytest.fixture
def client(tmp_path, monkeypatch):
    import json
    from opencohost.config import settings
    tokens = tmp_path / "tokens.json"
    tokens.write_text(json.dumps({"operator": "test-operator", "agent": "test-agent"}))
    monkeypatch.setattr(settings, "API_TOKENS_FILE", str(tokens))
    monkeypatch.setattr(avatar_vrm, "VRM_MODEL_DIR", tmp_path)
    app = FastAPI()
    app.state.host = SimpleNamespace(motor=SimpleNamespace(_speaking=True))
    app.include_router(avatar_vrm.router)
    with TestClient(app) as client:
        yield client


def test_models_list_and_cache(client, tmp_path):
    (tmp_path / "model.vrm").write_bytes(b"glTF")
    (tmp_path / "secret.txt").write_text("private")
    assert [m["filename"] for m in client.get("/api/avatar/vrm/list").json()] == ["model.vrm"]
    response = client.get("/api/avatar/vrm/model", params={"name": "model.vrm"})
    assert response.content == b"glTF"
    assert response.headers["content-type"] == "model/gltf-binary"
    assert client.get("/api/avatar/vrm/model", params={"name": "model.vrm"}, headers={"If-None-Match": response.headers["etag"]}).status_code == 304


@pytest.mark.parametrize("name", ["../secret.vrm", "..\\secret.vrm", "C:secret.vrm", "/secret.vrm", "file.vrm:evil", "secret.txt", ""])
def test_rejects_unsafe_names(client, name):
    assert client.get("/api/avatar/vrm/model", params={"name": name}).status_code == 400


def test_symlink_escape(client, tmp_path):
    target = tmp_path.parent / "outside.vrm"
    target.write_bytes(b"private")
    try:
        (tmp_path / "link.vrm").symlink_to(target)
    except OSError:
        pytest.skip("symlink privilege unavailable")
    assert client.get("/api/avatar/vrm/list").json() == []
    assert client.get("/api/avatar/vrm/model", params={"name": "link.vrm"}).status_code == 404


def test_audio_identity_and_stop(client):
    motor = client.app.state.host.motor
    assert client.get("/api/avatar/vrm/audio/state").json()["active"] is False
    motor._vrm_audio_snapshot = VrmAudioSnapshot(1, b"wave", "audio/wav", 1.0, True)
    client.headers["Authorization"] = "Bearer test-operator"
    state = client.get("/api/avatar/vrm/audio/state").json()
    assert set(state) == {"sequence", "active", "elapsed_ms"}
    assert state["sequence"] == 1
    assert client.get("/api/avatar/vrm/audio/last?sequence=0").status_code == 409
    response = client.get("/api/avatar/vrm/audio/last?sequence=1")
    assert response.content == b"wave"
    assert response.headers["cache-control"] == "no-store"
    motor._speaking = False
    assert client.get("/api/avatar/vrm/audio/state").json()["active"] is False
    assert client.get("/api/avatar/vrm/audio/last?sequence=1").status_code == 404


@pytest.mark.parametrize("token", [None, "test-agent", "invalid"])
def test_audio_requires_operator(client, token):
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    assert client.get("/api/avatar/vrm/audio/last", headers=headers).status_code == 401


def test_audio_auth_failure_is_closed(client, tmp_path):
    (tmp_path / "tokens.json").write_text("invalid")
    response = client.get("/api/avatar/vrm/audio/last", headers={"Authorization": "Bearer test-operator"})
    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
