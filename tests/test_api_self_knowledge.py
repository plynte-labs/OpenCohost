"""GET/PUT /api/self-knowledge (self-knowledge K3).

``self_knowledge.json`` is redirected per test by the autouse
``_isolate_self_knowledge_settings`` fixture in tests/conftest.py.
"""
import json

import pytest
from fastapi.testclient import TestClient

from opencohost.api import auth
from opencohost.config import self_knowledge_settings as sks
from opencohost.config import settings
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


def test_get_returns_defaults_and_builtin_identity_defaults():
    with TestClient(_app()) as client:
        resp = client.get("/api/self-knowledge")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True and body["viewers"] is False
    assert (body["creator"], body["repository"], body["description"]) == ("", "", "")
    assert body["defaults"]["creator"] == "plynte-labs"
    assert body["defaults"]["repository"].startswith("https://github.com/")


def test_put_partial_update_roundtrips_and_applies_live():
    with TestClient(_app()) as client:
        resp = client.put("/api/self-knowledge", json={"viewers": True, "creator": "acme"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["viewers"] is True and body["creator"] == "acme" and body["enabled"] is True
        assert client.get("/api/self-knowledge").json() == body
        resp = client.put("/api/self-knowledge", json={"enabled": False})
        assert resp.json()["creator"] == "acme"  # untouched fields persist
    assert sks.load_settings().enabled is False  # the live reader sees it
    with open(sks.SELF_KNOWLEDGE_FILE, encoding="utf-8") as f:
        assert json.load(f)["viewers"] is True


def test_put_empty_string_resets_an_identity_field():
    with TestClient(_app()) as client:
        client.put("/api/self-knowledge", json={"creator": "acme"})
        resp = client.put("/api/self-knowledge", json={"creator": ""})
    assert resp.json()["creator"] == ""


@pytest.mark.parametrize(
    "payload,field,code",
    [
        ({"enabled": "yes"}, "enabled", "invalid"),
        ({"viewers": 1}, "viewers", "invalid"),
        ({"creator": 5}, "creator", "invalid"),
        ({"creator": "x" * (sks.CREATOR_MAX + 1)}, "creator", "too_long"),
        ({"description": "d" * (sks.DESCRIPTION_MAX + 1)}, "description", "too_long"),
        ({"repository": "r" * (sks.REPOSITORY_MAX + 1)}, "repository", "too_long"),
        ({"creator": "a\nb"}, "creator", "invalid"),
        ({"repository": "ftp://example.test/r"}, "repository", "invalid"),
        ({"repository": "https://user:secret@example.test/r"}, "repository", "invalid"),
        ({"bogus": 1}, "bogus", "unknown"),
    ],
)
def test_put_422_on_invalid_fields_and_nothing_is_saved(payload, field, code):
    with TestClient(_app()) as client:
        resp = client.put("/api/self-knowledge", json=payload)
        assert resp.status_code == 422
        body = resp.json()
        assert body["detail"] == "invalid_self_knowledge"
        assert {"field": field, "code": code} in body["errors"]
        assert client.get("/api/self-knowledge").json()["enabled"] is True
    assert not __import__("os").path.exists(sks.SELF_KNOWLEDGE_FILE)


def test_put_accepts_a_plain_https_repository():
    with TestClient(_app()) as client:
        resp = client.put("/api/self-knowledge", json={"repository": "https://github.com/acme/r"})
    assert resp.status_code == 200 and resp.json()["repository"] == "https://github.com/acme/r"


def test_put_503_on_write_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(sks.os, "replace", boom)
    with TestClient(_app()) as client:
        resp = client.put("/api/self-knowledge", json={"viewers": True})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "self_knowledge_write_failed"}
        assert client.get("/api/self-knowledge").json()["viewers"] is False


class TestOperatorProtection:
    @pytest.fixture(autouse=True)
    def _flag_on(self, monkeypatch):
        monkeypatch.setattr(settings, "API_AUTH_ENFORCED", True)

    def test_missing_token_is_401(self):
        with TestClient(_app()) as client:
            assert client.put("/api/self-knowledge", json={"viewers": True}).status_code == 401

    def test_agent_token_is_403(self):
        auth.ensure_tokens()
        tokens = auth.load_tokens()
        with TestClient(_app()) as client:
            resp = client.put(
                "/api/self-knowledge",
                json={"viewers": True},
                headers={"Authorization": f"Bearer {tokens['agent']}"},
            )
        assert resp.status_code == 403

    def test_operator_token_is_200_and_get_stays_open(self):
        auth.ensure_tokens()
        tokens = auth.load_tokens()
        with TestClient(_app()) as client:
            resp = client.put(
                "/api/self-knowledge",
                json={"viewers": True},
                headers={"Authorization": f"Bearer {tokens['operator']}"},
            )
            assert resp.status_code == 200
            assert client.get("/api/self-knowledge").status_code == 200
