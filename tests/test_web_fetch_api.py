"""web-fetch-laya-20261009 T3: the web lookup master switch, its store and
GET/PUT /api/web-fetch.

``web_fetch.json`` and ``OPENCOHOST_WEB_FETCH`` are isolated per test by the
autouse ``_isolate_web_fetch`` fixture in tests/conftest.py.
"""
from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from opencohost.config import settings
from opencohost.config import web_fetch
from tests.test_api_phase1 import FakeHost

_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_ORIGINS)


# --- settings and store --------------------------------------------------------


def test_file_lives_in_config_dir():
    assert settings.WEB_FETCH_FILE == os.path.join(settings.CONFIG_DIR, "web_fetch.json")


@pytest.mark.parametrize("raw,want", [("", True), ("  ", True), ("0", False), ("false", False), ("off", False), ("no", False), (" NO ", False), ("1", True), ("true", True), ("ON", True)])
def test_env_default(monkeypatch, raw, want):
    monkeypatch.setenv("OPENCOHOST_WEB_FETCH", raw)
    assert settings._resolve_web_fetch_default() is want


def test_env_default_unset_is_on(monkeypatch):
    monkeypatch.delenv("OPENCOHOST_WEB_FETCH", raising=False)
    assert settings._resolve_web_fetch_default() is True


def test_default_is_off():
    assert web_fetch.is_enabled() is False


def test_load_missing_corrupt_and_invalid_fall_back_to_the_default(tmp_path):
    path = web_fetch.WEB_FETCH_FILE
    assert web_fetch.load_web_fetch() is False
    for content in ("{not json", json.dumps({"enabled": "yes"}), json.dumps([1])):
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        assert web_fetch.load_web_fetch() is False


def test_env_default_and_file_wins(monkeypatch):
    monkeypatch.setattr(settings, "WEB_FETCH_DEFAULT", True)
    web_fetch.reset_web_fetch()
    assert web_fetch.is_enabled() is True
    with open(web_fetch.WEB_FETCH_FILE, "w", encoding="utf-8") as f:
        json.dump({"enabled": False}, f)
    assert web_fetch.load_web_fetch() is False


def test_save_is_atomic_and_validated():
    assert web_fetch.save_web_fetch(True) is True
    assert web_fetch.is_enabled() is True
    with open(web_fetch.WEB_FETCH_FILE, encoding="utf-8") as f:
        assert json.load(f) == {"enabled": True}
    for bad in ("true", 1, None):
        with pytest.raises(ValueError):
            web_fetch.save_web_fetch(bad)
    assert web_fetch.is_enabled() is True


def test_failed_write_keeps_memory(monkeypatch):
    monkeypatch.setattr(web_fetch, "WEB_FETCH_FILE", os.path.join(os.devnull, "x", "web_fetch.json"))
    assert web_fetch.save_web_fetch(True) is None
    assert web_fetch.is_enabled() is False


# --- API --------------------------------------------------------------------------


def test_get_default():
    with TestClient(_app()) as client:
        resp = client.get("/api/web-fetch")
    assert resp.status_code == 200
    assert resp.json() == {"enabled": False}


def test_put_roundtrip_persists_and_startup_loads_it():
    with TestClient(_app()) as client:
        resp = client.put("/api/web-fetch", json={"enabled": True})
        assert resp.status_code == 200 and resp.json() == {"enabled": True}
        assert client.get("/api/web-fetch").json() == {"enabled": True}
    assert web_fetch.is_enabled() is True
    web_fetch.reset_web_fetch()
    with TestClient(_app()) as client:  # lifespan calls load_web_fetch()
        assert client.get("/api/web-fetch").json() == {"enabled": True}


@pytest.mark.parametrize("body", [{}, {"enabled": "yes"}, {"enabled": 1}, {"enabled": True, "x": 1}, {"enabled": None}])
def test_put_invalid_is_422_with_stable_code(body):
    with TestClient(_app()) as client:
        resp = client.put("/api/web-fetch", json=body)
    assert resp.status_code == 422
    assert resp.json() == {"detail": "invalid_web_fetch_setting"}
    assert web_fetch.is_enabled() is False


def test_put_write_failure_is_503(monkeypatch):
    with TestClient(_app()) as client:
        monkeypatch.setattr(web_fetch, "WEB_FETCH_FILE", os.path.join(os.devnull, "x", "web_fetch.json"))
        resp = client.put("/api/web-fetch", json={"enabled": True})
    assert resp.status_code == 503
    assert resp.json() == {"detail": "web_fetch_write_failed"}
    assert web_fetch.is_enabled() is False


def test_decisions_api_lists_the_web_decision():
    with TestClient(_app()) as client:
        got = client.get("/api/decisions").json()["decisions"]
        assert got["web"] == {"mode": "on", "threshold": 0.9}
        resp = client.put("/api/decisions", json={"decisions": {"web": {"mode": "shadow"}}})
        assert resp.status_code == 200
        assert resp.json()["decisions"]["web"]["mode"] == "shadow"
