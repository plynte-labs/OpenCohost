"""voice-catalog-selection-20261118 T2: /api/tts/piper/* and /api/tts/edge/voices routes."""
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opencohost.api.routers import i18n_tts
from opencohost.config import settings
from opencohost.core.speech import edge_voices, piper_catalog


def _voice(folder, name, config=None):
    (folder / name).write_bytes(b"x")
    if config is not None:
        (folder / (name + ".json")).write_text(json.dumps(config))


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "PIPER_VOICE_DIR_FILE", str(tmp_path / "cfg" / "piper_voice_dir.json"))
    default_dir = tmp_path / "default"
    default_dir.mkdir()
    monkeypatch.setattr(piper_catalog, "_default_dir", lambda: default_dir)
    monkeypatch.delenv("OPENCOHOST_PIPER_VOICE_DIR", raising=False)
    monkeypatch.setattr(settings, "PIPER_VOICES", {})
    cat = edge_voices.EdgeVoiceCatalog(
        fetch=lambda: [{"ShortName": "es-MX-DaliaNeural", "Locale": "es-MX", "Gender": "Female", "FriendlyName": "Dalia"}],
        fallback=lambda lang: ["es-MX-DaliaNeural"],
    )
    monkeypatch.setattr(edge_voices, "DEFAULT_CATALOG", cat)
    app = FastAPI()
    app.include_router(i18n_tts.router)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def voices_dir(tmp_path):
    d = tmp_path / "voices"
    d.mkdir()
    _voice(d, "a.onnx", {"language": {"code": "es_ES"}, "num_speakers": 2, "speaker_id_map": {"M": 0, "F": 1}, "audio": {"sample_rate": 22050}})
    _voice(d, "b.onnx", {"language": {"code": "en-us"}})
    _voice(d, "c.onnx")
    return d


def test_folder_defaults_then_put_get_delete(client, voices_dir, tmp_path):
    first = client.get("/api/tts/piper/folder").json()
    assert first["source"] == "default" and first["voice_count"] == 0
    put = client.put("/api/tts/piper/folder", json={"path": str(voices_dir)})
    assert put.status_code == 200
    body = put.json()
    assert body == {"path": str(voices_dir.resolve()), "source": "saved", "exists": True, "voice_count": 2}
    assert client.get("/api/tts/piper/folder").json() == body
    reset = client.delete("/api/tts/piper/folder").json()
    assert reset["source"] == "default"


@pytest.mark.parametrize(
    "path, code",
    [("", "path_empty"), ("relative", "path_not_absolute"), ("\\\\srv\\share", "path_network_or_device"), ("MISSING", "path_not_found")],
)
def test_put_folder_refusals_use_vrm_codes(client, tmp_path, path, code):
    if path == "MISSING":
        path = str(tmp_path / "definitely" / "not" / "here")
    resp = client.put("/api/tts/piper/folder", json={"path": path})
    assert resp.status_code == 422 and resp.json() == {"detail": code}


def test_put_folder_write_failure_is_503(client, voices_dir, monkeypatch):
    def boom(_p):
        raise OSError

    monkeypatch.setattr(piper_catalog, "save_voice_dir", boom)
    resp = client.put("/api/tts/piper/folder", json={"path": str(voices_dir)})
    assert resp.status_code == 503 and resp.json() == {"detail": "config_write_failed"}


def test_piper_voices_list_and_lang_filter(client, voices_dir):
    client.put("/api/tts/piper/folder", json={"path": str(voices_dir)})
    items = {v["id"]: v for v in client.get("/api/tts/piper/voices").json()}
    assert set(items) == {"a.onnx", "b.onnx", "c.onnx"}
    assert items["a.onnx"] == {
        "id": "a.onnx",
        "label": "a",
        "lang": "es",
        "num_speakers": 2,
        "speakers": [{"name": "M", "id": 0}, {"name": "F", "id": 1}],
        "sample_rate": 22050,
        "bundled": False,
        "status": "ok",
        "reason": None,
    }
    assert items["c.onnx"]["status"] == "incomplete" and items["c.onnx"]["reason"] == "missing_config"
    es = {v["id"] for v in client.get("/api/tts/piper/voices", params={"lang": "es"}).json()}
    assert es == {"a.onnx", "c.onnx"}


def test_piper_voices_rescans_every_request(client, voices_dir):
    client.put("/api/tts/piper/folder", json={"path": str(voices_dir)})
    assert len(client.get("/api/tts/piper/voices").json()) == 3
    _voice(voices_dir, "d.onnx", {"language": {"code": "es_ES"}})
    assert len(client.get("/api/tts/piper/voices").json()) == 4


def test_edge_voices_endpoint_live_then_cache(client):
    first = client.get("/api/tts/edge/voices", params={"lang": "es"}).json()
    assert first == {
        "voices": [{"id": "es-MX-DaliaNeural", "label": "Dalia", "locale": "es-MX", "gender": "Female"}],
        "source": "live",
    }
    assert client.get("/api/tts/edge/voices").json()["source"] == "cache"


def test_edge_voices_endpoint_fallback_when_offline(client, monkeypatch):
    def boom():
        raise OSError

    monkeypatch.setattr(
        edge_voices,
        "DEFAULT_CATALOG",
        edge_voices.EdgeVoiceCatalog(fetch=boom, fallback=lambda lang: ["es-MX-DaliaNeural"]),
    )
    body = client.get("/api/tts/edge/voices", params={"lang": "es"}).json()
    assert body["source"] == "fallback"
    assert [v["id"] for v in body["voices"]] == ["es-MX-DaliaNeural"]
