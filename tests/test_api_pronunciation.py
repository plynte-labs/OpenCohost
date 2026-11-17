"""GET/PUT /api/tts/pronunciation (tts-pronunciation-dictionary T2).

The pronunciation file is redirected to a per-test tmp path by the autouse
``_isolate_pronunciation_dictionary`` fixture in tests/conftest.py.
"""

import json

import pytest
from fastapi.testclient import TestClient

from opencohost.api import auth
from opencohost.config import settings
from opencohost.core.speech import pronunciation as pr
from opencohost.core.speech.tts_sanitizer import _sanitize_tts_text_for_playback
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


def test_get_returns_empty_entries_and_limits():
    with TestClient(_app()) as client:
        resp = client.get("/api/tts/pronunciation")
    assert resp.status_code == 200
    assert resp.json() == {
        "entries": [],
        "limits": {"max_entries": 200, "match_max": 64, "replace_max": 128},
    }


def test_put_happy_path_roundtrip_and_live_apply():
    body = {"entries": [{"match": "OBS", "replace": "o be ese"}]}
    with TestClient(_app()) as client:
        resp = client.put("/api/tts/pronunciation", json=body)
        assert resp.status_code == 200
        assert resp.json()["entries"] == body["entries"]
        assert resp.json()["limits"]["max_entries"] == 200
        assert client.get("/api/tts/pronunciation").json()["entries"] == body["entries"]
        # Rebuilt on save: no restart needed.
        assert _sanitize_tts_text_for_playback("abre OBS") == "abre o be ese"
        # Reset to empty is a PUT of [].
        assert client.put("/api/tts/pronunciation", json={"entries": []}).status_code == 200
        assert _sanitize_tts_text_for_playback("abre OBS") == "abre OBS"


def test_put_persists_to_disk_and_startup_loads_it():
    with TestClient(_app()) as client:
        client.put(
            "/api/tts/pronunciation",
            json={"entries": [{"match": "GG", "replace": "bien jugado"}]},
        )
    pr.reset_pronunciation()
    with TestClient(_app()) as client:  # lifespan calls load_pronunciation()
        assert client.get("/api/tts/pronunciation").json()["entries"] == [
            {"match": "GG", "replace": "bien jugado"}
        ]


def test_put_422_field_level_errors():
    body = {
        "entries": [
            {"match": "OBS", "replace": "a"},
            {"match": "obs", "replace": "b"},
            {"match": "x", "replace": "bad. text"},
        ]
    }
    with TestClient(_app()) as client:
        resp = client.put("/api/tts/pronunciation", json=body)
    assert resp.status_code == 422
    data = resp.json()
    assert data["detail"] == "invalid_pronunciation"
    assert {"index": 1, "field": "match", "code": "duplicate"} in data["errors"]
    assert {"index": 2, "field": "replace", "code": "forbidden_char"} in data["errors"]
    assert pr.get_entries() == []  # nothing installed


def test_put_422_missing_entries_key():
    with TestClient(_app()) as client:
        resp = client.put("/api/tts/pronunciation", json={"nope": 1})
    assert resp.status_code == 422
    assert resp.json()["errors"][0]["code"] == "not_list"


def test_put_503_on_write_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(pr.os, "replace", boom)
    with TestClient(_app()) as client:
        resp = client.put(
            "/api/tts/pronunciation",
            json={"entries": [{"match": "OBS", "replace": "o be ese"}]},
        )
    assert resp.status_code == 503
    assert resp.json() == {"detail": "pronunciation_write_failed"}
    assert pr.get_entries() == []


def test_put_does_not_log_entry_text(caplog):
    caplog.set_level("DEBUG")
    with TestClient(_app()) as client:
        client.put(
            "/api/tts/pronunciation",
            json={"entries": [{"match": "SECRETWORD", "replace": "hiddenvalue"}]},
        )
    assert "SECRETWORD" not in caplog.text and "hiddenvalue" not in caplog.text


class TestOperatorProtection:
    """Same generic rule-2 middleware as PUT /api/personalization."""

    @pytest.fixture(autouse=True)
    def _flag_on(self, monkeypatch):
        monkeypatch.setattr(settings, "API_AUTH_ENFORCED", True)

    def test_missing_token_is_401(self):
        with TestClient(_app()) as client:
            assert client.put("/api/tts/pronunciation", json={"entries": []}).status_code == 401

    def test_agent_token_is_403(self):
        auth.ensure_tokens()
        tokens = auth.load_tokens()
        with TestClient(_app()) as client:
            resp = client.put(
                "/api/tts/pronunciation",
                json={"entries": []},
                headers={"Authorization": f"Bearer {tokens['agent']}"},
            )
        assert resp.status_code == 403

    def test_operator_token_is_200_and_get_stays_open(self):
        auth.ensure_tokens()
        tokens = auth.load_tokens()
        with TestClient(_app()) as client:
            resp = client.put(
                "/api/tts/pronunciation",
                json={"entries": []},
                headers={"Authorization": f"Bearer {tokens['operator']}"},
            )
            assert resp.status_code == 200
            assert client.get("/api/tts/pronunciation").status_code == 200
