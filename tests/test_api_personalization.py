"""Tests for the Personalization CRUD endpoints (Phase 2 of
kira_personalization_onboarding_20260705): GET/PUT/DELETE
/api/personalization.

File-based only — no dispatcher/engine queue coupling (design §4), mirrors
the perfiles CRUD block (tests/test_api_perfiles_crud.py). The response is
FIELD-FILTERED — exactly {enabled, nickname, occupation, interests,
custom_instructions, updated_at} — built via explicit field picks, never
`**data`, so a future persisted field (or `version`) can never leak through.

Isolation: the `_isolate_personalization_file` autouse fixture in
tests/conftest.py already redirects
`opencohost.core.personalization.PERSONALIZATION_FILE` to a per-test tmp
path — no real user data is touched here.
"""

import json

import pytest
from fastapi.testclient import TestClient

from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]

_EXPECTED_KEYS = {
    "enabled",
    "nickname",
    "occupation",
    "interests",
    "custom_instructions",
    "updated_at",
}


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)


# ──────────────────────────────────────────────────────────────────────────
# GET /api/personalization
# ──────────────────────────────────────────────────────────────────────────


def test_get_personalization_returns_defaults_when_file_absent():
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/personalization")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == _EXPECTED_KEYS
        assert body == {
            "enabled": True,
            "nickname": "",
            "occupation": "",
            "interests": "",
            "custom_instructions": "",
            "updated_at": None,
        }


def test_get_personalization_normalizes_non_string_updated_at(tmp_path):
    """A hand-edited store file with a non-string `updated_at` (valid JSON,
    wrong type) must still return 200 with `updated_at` coerced to str, not
    500 a ValidationError (design §9: corrupt/hand-edited files fail open)."""
    file_path = tmp_path / "personalization.json"
    file_path.write_text(
        json.dumps({"enabled": True, "nickname": "Fran", "updated_at": 12345}),
        encoding="utf-8",
    )
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/personalization")
        assert resp.status_code == 200
        assert resp.json()["updated_at"] == "12345"


# ──────────────────────────────────────────────────────────────────────────
# PUT /api/personalization (partial update)
# ──────────────────────────────────────────────────────────────────────────


def test_put_personalization_partial_update_omitted_fields_unchanged():
    app = _app()
    with TestClient(app) as client:
        resp = client.put(
            "/api/personalization",
            json={"nickname": "Fran", "occupation": "streamer"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["nickname"] == "Fran"
        assert body["occupation"] == "streamer"
        assert body["interests"] == ""
        assert body["custom_instructions"] == ""
        assert body["enabled"] is True
        assert body["updated_at"] is not None

        # Second PUT only touches `interests` — prior fields must survive.
        resp2 = client.put("/api/personalization", json={"interests": "chess"})
        assert resp2.status_code == 200
        body2 = resp2.json()
        assert body2["nickname"] == "Fran"
        assert body2["occupation"] == "streamer"
        assert body2["interests"] == "chess"


def test_put_personalization_enabled_toggle():
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/personalization", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False


# ──────────────────────────────────────────────────────────────────────────
# PUT /api/personalization — 422 field caps
# ──────────────────────────────────────────────────────────────────────────


def test_put_personalization_422_nickname_too_long():
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/personalization", json={"nickname": "a" * 61})
        assert resp.status_code == 422


def test_put_personalization_422_occupation_too_long():
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/personalization", json={"occupation": "a" * 121})
        assert resp.status_code == 422


def test_put_personalization_422_interests_too_long():
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/personalization", json={"interests": "a" * 241})
        assert resp.status_code == 422


def test_put_personalization_422_custom_instructions_too_long():
    app = _app()
    with TestClient(app) as client:
        resp = client.put(
            "/api/personalization", json={"custom_instructions": "a" * 401}
        )
        assert resp.status_code == 422


# ──────────────────────────────────────────────────────────────────────────
# PUT /api/personalization — 503 on save failure
# ──────────────────────────────────────────────────────────────────────────


def test_put_personalization_503_on_save_failure(monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "save_personalization", lambda data: False)
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/personalization", json={"nickname": "Fran"})
        assert resp.status_code == 503


# ──────────────────────────────────────────────────────────────────────────
# DELETE /api/personalization
# ──────────────────────────────────────────────────────────────────────────


def test_delete_personalization_clears_and_returns_ok():
    app = _app()
    with TestClient(app) as client:
        client.put("/api/personalization", json={"nickname": "Fran"})
        resp = client.delete("/api/personalization")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        get_resp = client.get("/api/personalization")
        assert get_resp.json()["nickname"] == ""


def test_delete_personalization_503_on_save_failure(monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setattr(main_mod, "clear_personalization", lambda: False)
    app = _app()
    with TestClient(app) as client:
        resp = client.delete("/api/personalization")
        assert resp.status_code == 503
