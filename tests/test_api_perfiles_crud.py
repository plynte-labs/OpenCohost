"""Tests for the Perfiles CRUD endpoints (WU5): /api/perfiles/{name}
(GET/PUT/DELETE) and /api/perfiles (POST).

R8/D5: the profile detail response is FIELD-FILTERED — exactly
{name, prompt, use_system}, never the stored `id`. Mutations run under a
dedicated `_profiles_lock`; rename preserves the stable on-disk `id` (R12).
DELETE has ONLY the last-profile guard (resolution 2905 — deleting the
active profile is allowed, matching CTK parity, not an improvement).

Isolation: `opencohost.core.profiles.PROFILES_FILE` is monkeypatched to a
tmp json file — no real user profiles are touched. `cargar_perfiles` /
`guardar_perfiles` read that module global at call time, so patching it
redirects both the API handlers and the on-disk assertions.
"""

import json

import pytest
from fastapi.testclient import TestClient

from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


def _write_profiles(path, profiles):
    path.write_text(json.dumps(profiles), encoding="utf-8")


def _read_profiles(path):
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


@pytest.fixture
def profiles_file(tmp_path, monkeypatch):
    """Seed a >=2-profile tmp json and redirect PROFILES_FILE to it."""
    import opencohost.core.profiles as profiles_mod

    path = tmp_path / "perfiles.json"
    _write_profiles(
        path,
        {
            "default": {"id": "id-default", "prompt": "Default prompt", "use_system": False},
            "streamer": {"id": "id-streamer", "prompt": "Streamer prompt", "use_system": True},
        },
    )
    monkeypatch.setattr(profiles_mod, "PROFILES_FILE", str(path))
    return path


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)


# ──────────────────────────────────────────────────────────────────────────
# GET /api/perfiles/{name} (R8/D5 field filter)
# ──────────────────────────────────────────────────────────────────────────


def test_get_perfil_returns_only_prompt_and_use_system(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/perfiles/default")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"name", "prompt", "use_system"}
        assert "id" not in body  # R8/D5: the stored id must never leak
        assert body == {"name": "default", "prompt": "Default prompt", "use_system": False}


def test_get_perfil_404_when_missing(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/perfiles/nonexistent")
        assert resp.status_code == 404


# ──────────────────────────────────────────────────────────────────────────
# POST /api/perfiles (create)
# ──────────────────────────────────────────────────────────────────────────


def test_create_perfil_then_visible_in_list_and_get(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/perfiles", json={"name": "newbie", "prompt": "Hi there", "use_system": True}
        )
        assert resp.status_code == 200
        assert resp.json() == {"name": "newbie", "prompt": "Hi there", "use_system": True}

        list_resp = client.get("/api/perfiles")
        assert "newbie" in list_resp.json()["profiles"]

        get_resp = client.get("/api/perfiles/newbie")
        assert get_resp.json() == {"name": "newbie", "prompt": "Hi there", "use_system": True}


def test_create_perfil_duplicate_name_409(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.post("/api/perfiles", json={"name": "default", "prompt": "x"})
        assert resp.status_code == 409


def test_create_perfil_name_too_long_422(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.post("/api/perfiles", json={"name": "a" * 101, "prompt": "x"})
        assert resp.status_code == 422


def test_create_perfil_prompt_too_long_422(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.post("/api/perfiles", json={"name": "okname", "prompt": "a" * 20001})
        assert resp.status_code == 422


# ──────────────────────────────────────────────────────────────────────────
# PUT /api/perfiles/{name} (update / rename)
# ──────────────────────────────────────────────────────────────────────────


def test_update_perfil_edits_prompt_in_place(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/perfiles/default", json={"prompt": "Edited prompt"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "default"
        assert resp.json()["prompt"] == "Edited prompt"

        data = _read_profiles(profiles_file)
        assert data["default"]["prompt"] == "Edited prompt"
        assert data["default"]["id"] == "id-default"  # id preserved on in-place edit


def test_update_perfil_rename_preserves_id_on_disk(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/perfiles/default", json={"new_name": "renamed"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "renamed"

        data = _read_profiles(profiles_file)
        assert "default" not in data
        assert "renamed" in data
        assert data["renamed"]["id"] == "id-default"  # R12: stable id across rename


def test_update_perfil_rename_onto_existing_name_409(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/perfiles/default", json={"new_name": "streamer"})
        assert resp.status_code == 409


def test_update_perfil_404_when_missing(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/perfiles/ghost", json={"prompt": "x"})
        assert resp.status_code == 404


# ──────────────────────────────────────────────────────────────────────────
# DELETE /api/perfiles/{name} (last-profile guard only)
# ──────────────────────────────────────────────────────────────────────────


def test_delete_perfil_removes_it(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.delete("/api/perfiles/streamer")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}

        list_resp = client.get("/api/perfiles")
        assert "streamer" not in list_resp.json()["profiles"]


def test_delete_last_perfil_409(profiles_file):
    _write_profiles(
        profiles_file,
        {"default": {"id": "id-default", "prompt": "Default prompt", "use_system": False}},
    )
    app = _app()
    with TestClient(app) as client:
        resp = client.delete("/api/perfiles/default")
        assert resp.status_code == 409
        assert resp.json()["detail"] == "cannot delete the last profile"

        data = _read_profiles(profiles_file)
        assert "default" in data  # still present after the refused delete


def test_delete_active_profile_is_allowed(profiles_file):
    # FakeHost's motor._current_profile_name is "default"; deleting it is
    # ALLOWED (resolution 2905 — no active-profile guard, CTK parity).
    app = _app()
    with TestClient(app) as client:
        assert app.state.host.motor._current_profile_name == "default"
        resp = client.delete("/api/perfiles/default")
        assert resp.status_code == 200


# ──────────────────────────────────────────────────────────────────────────
# CORS (D6): allow_methods must include PUT and DELETE
# ──────────────────────────────────────────────────────────────────────────


def test_cors_allows_put_and_delete():
    app = _app()
    with TestClient(app) as client:
        for method in ("PUT", "DELETE"):
            resp = client.options(
                "/api/perfiles/default",
                headers={
                    "Origin": "http://localhost:5173",
                    "Access-Control-Request-Method": method,
                },
            )
            assert resp.status_code == 200, f"{method} preflight rejected"
            assert method in resp.headers.get("access-control-allow-methods", ""), method
