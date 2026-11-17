"""GET/POST /api/memoria/settings — per-layer memory controls (T4,
memory-semantic-recall).

GET returns the effective runtime flags (the settings module attributes the
engine reads each turn) plus the read-only MEMORIAS_ENABLED master switch.
POST is a partial update: omitted layers stay unchanged, an all-omitted body
is a 422 no-op (mirrors /api/memoria/flags). The merged state is persisted
atomically to MEMORY_LAYERS_FILE first and applied to the runtime only after
the write succeeded; a failed write is a 503, never a silent runtime-only
change that would be lost on restart.
"""

import json

import pytest
from fastapi.testclient import TestClient

from opencohost.config import settings
from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


@pytest.fixture
def layers_file(tmp_path, monkeypatch):
    path = tmp_path / "config" / "memory_layers.json"
    monkeypatch.setattr(settings, "MEMORY_LAYERS_FILE", str(path))
    monkeypatch.setattr(settings, "MEMORY_FACTS_ENABLED", True)
    monkeypatch.setattr(settings, "MEMORY_EPISODES_ENABLED", True)
    monkeypatch.setattr(settings, "MEMORY_SEMANTIC_ENABLED", True)
    return path


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)


def test_get_returns_effective_flags_and_master_switch(layers_file, monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "MEMORIAS_ENABLED", True)
    monkeypatch.setattr(settings, "MEMORY_SEMANTIC_ENABLED", False)

    with TestClient(_app()) as client:
        resp = client.get("/api/memoria/settings")

    assert resp.status_code == 200
    assert resp.json() == {
        "memorias_enabled": True,
        "facts": True,
        "episodes": True,
        "semantic": False,
    }


def test_post_partial_update_persists_applies_and_echoes(layers_file):
    with TestClient(_app()) as client:
        resp = client.post("/api/memoria/settings", json={"episodes": False})
        assert resp.status_code == 200
        body = resp.json()
        assert body["facts"] is True and body["episodes"] is False and body["semantic"] is True

        # Runtime applied: the engine reads these attributes each turn.
        assert settings.MEMORY_EPISODES_ENABLED is False
        assert settings.MEMORY_FACTS_ENABLED is True

        # A fresh GET reflects the change.
        assert client.get("/api/memoria/settings").json()["episodes"] is False

    assert json.loads(layers_file.read_text(encoding="utf-8")) == {
        "facts": True, "episodes": False, "semantic": True,
    }


def test_post_updates_merge_over_previous_state(layers_file):
    with TestClient(_app()) as client:
        client.post("/api/memoria/settings", json={"facts": False})
        resp = client.post("/api/memoria/settings", json={"semantic": False})

    assert resp.json()["facts"] is False and resp.json()["semantic"] is False
    assert json.loads(layers_file.read_text(encoding="utf-8")) == {
        "facts": False, "episodes": True, "semantic": False,
    }


def test_post_empty_body_is_a_422_no_op(layers_file):
    with TestClient(_app()) as client:
        resp = client.post("/api/memoria/settings", json={})
    assert resp.status_code == 422
    assert not layers_file.exists()


def test_post_non_bool_is_422(layers_file):
    with TestClient(_app()) as client:
        resp = client.post("/api/memoria/settings", json={"facts": [1, 2]})
    assert resp.status_code == 422
    assert settings.MEMORY_FACTS_ENABLED is True


def test_post_write_failure_is_503_and_runtime_unchanged(tmp_path, monkeypatch, layers_file):
    blocker = tmp_path / "blocked"
    blocker.mkdir()
    monkeypatch.setattr(settings, "MEMORY_LAYERS_FILE", str(blocker))

    with TestClient(_app()) as client:
        resp = client.post("/api/memoria/settings", json={"facts": False})

    assert resp.status_code == 503
    assert resp.json() == {"detail": "memory_settings_write_failed"}
    assert settings.MEMORY_FACTS_ENABLED is True
