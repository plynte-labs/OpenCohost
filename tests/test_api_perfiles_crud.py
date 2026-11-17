"""Tests for the Perfiles CRUD endpoints (WU5): /api/perfiles/{name}
(GET/PUT/DELETE) and /api/perfiles (POST).

R8/D5: the profile detail response is FIELD-FILTERED — exactly
{name, prompt, use_system}, never the stored `id`. Mutations run under a
dedicated `_profiles_lock`; rename preserves the stable on-disk `id` (R12).
DELETE has ONLY the last-profile guard (resolution 2905 — deleting the
active profile is allowed, matching CTK parity, not an improvement).

Isolation: `opencohost.core.profiles.profiles.PROFILES_FILE` is monkeypatched to a
tmp json file — no real user profiles are touched. `cargar_perfiles` /
`guardar_perfiles` read that module global at call time, so patching it
redirects both the API handlers and the on-disk assertions. The
`_isolate_last_profile_and_model_files` autouse fixture in tests/conftest.py
separately redirects `settings.LAST_PROFILE_FILE` — /api/perfiles/switch
calls `save_last_profile` on success, and without that fixture it would
write the developer's real `config/last_profile.json`.
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


@pytest.fixture(autouse=True)
def _reset_active_bundle():
    from opencohost.i18n import active as i18n_active

    i18n_active.reset_active_bundle()
    yield
    i18n_active.reset_active_bundle()


@pytest.fixture
def profiles_file(tmp_path, monkeypatch):
    """Seed a >=2-profile tmp json and redirect PROFILES_FILE to it."""
    import opencohost.core.profiles.profiles as profiles_mod

    path = tmp_path / "perfiles.json"
    _write_profiles(
        path,
        {
            "default": {"id": "id-default", "prompt": "Default prompt", "use_system": False},
            "streamer": {
                "id": "id-streamer",
                "prompt": "Streamer prompt",
                "use_system": True,
                "locale": "en",
            },
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


def test_get_perfil_returns_name_id_prompt_and_use_system(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/perfiles/default")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {
            "name",
            "id",
            "prompt",
            "use_system",
            "locale",
            "extended_responses",
            "max_turns",
            "max_turns_cap",
            "warmth",
            "owner_gender",
        }
        assert body == {
            "name": "default",
            "id": "id-default",
            "prompt": "Default prompt",
            "use_system": False,
            "locale": None,
            "extended_responses": False,
            "max_turns": 3,
            "max_turns_cap": 10,
            "warmth": "cold",
            "owner_gender": "",
        }


def test_get_perfil_returns_declared_locale(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/perfiles/streamer")
        assert resp.status_code == 200
        assert resp.json()["locale"] == "en"


def test_get_perfil_404_when_missing(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/perfiles/nonexistent")
        assert resp.status_code == 404


def test_list_perfiles_without_locale_returns_all(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/perfiles")
        assert resp.status_code == 200
        profiles = resp.json()["profiles"]
        assert "default" in profiles
        assert "streamer" in profiles


def test_list_perfiles_with_locale_en_filters_correctly(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/perfiles?locale=en")
        assert resp.status_code == 200
        profiles = resp.json()["profiles"]
        assert "streamer" in profiles
        assert "default" not in profiles


def test_list_perfiles_with_locale_es_includes_unspecified(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/perfiles?locale=es")
        assert resp.status_code == 200
        profiles = resp.json()["profiles"]
        assert "default" in profiles
        assert "streamer" not in profiles


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
        created = resp.json()
        assert created["name"] == "newbie"
        assert created["prompt"] == "Hi there"
        assert created["use_system"] is True
        assert isinstance(created["id"], str) and created["id"]  # server-minted uuid

        list_resp = client.get("/api/perfiles")
        assert "newbie" in list_resp.json()["profiles"]

        get_resp = client.get("/api/perfiles/newbie")
        assert get_resp.json() == created  # GET matches what POST returned, id included


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


def test_create_perfil_with_locale_persists_and_returns_it(profiles_file):
    # D7 (kira_bilingual_e2e): real HTTP round-trip, not just model
    # validation — the on-disk write must not strip the new key.
    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/perfiles",
            json={"name": "gringo", "prompt": "Hi there", "locale": "en"},
        )
        assert resp.status_code == 200
        assert resp.json()["locale"] == "en"

        data = _read_profiles(profiles_file)
        assert data["gringo"]["locale"] == "en"

        get_resp = client.get("/api/perfiles/gringo")
        assert get_resp.json()["locale"] == "en"


def test_create_perfil_without_locale_defaults_to_active_en_and_is_listed(profiles_file):
    from opencohost.i18n import active as i18n_active
    from opencohost.i18n.startup import resolve_active_bundle

    i18n_active.set_active_bundle(resolve_active_bundle(locale="en"))
    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/perfiles",
            json={"name": "Tsundere", "prompt": "English anime prompt", "use_system": True},
        )
        assert resp.status_code == 200
        assert resp.json()["locale"] == "en"

        data = _read_profiles(profiles_file)
        assert data["Tsundere"]["locale"] == "en"

        list_en = client.get("/api/perfiles?locale=en")
        assert list_en.status_code == 200
        assert "Tsundere" in list_en.json()["profiles"]


def test_create_perfil_with_explicit_null_locale_defaults_to_active_locale(profiles_file):
    from opencohost.i18n import active as i18n_active
    from opencohost.i18n.startup import resolve_active_bundle

    i18n_active.set_active_bundle(resolve_active_bundle(locale="en"))
    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/perfiles",
            json={"name": "Gril", "prompt": "English anime prompt", "locale": None},
        )
        assert resp.status_code == 200
        assert resp.json()["locale"] == "en"

        data = _read_profiles(profiles_file)
        assert data["Gril"]["locale"] == "en"
        assert "Gril" in client.get("/api/perfiles?locale=en").json()["profiles"]


def test_create_perfil_without_locale_defaults_to_active_es(profiles_file):
    from opencohost.i18n import active as i18n_active
    from opencohost.i18n.startup import resolve_active_bundle

    i18n_active.set_active_bundle(resolve_active_bundle(locale="es"))
    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/perfiles",
            json={"name": "nueva_es", "prompt": "Hola"},
        )
        assert resp.status_code == 200
        assert resp.json()["locale"] == "es"
        assert _read_profiles(profiles_file)["nueva_es"]["locale"] == "es"
        assert "nueva_es" in client.get("/api/perfiles?locale=es").json()["profiles"]
        assert "nueva_es" not in client.get("/api/perfiles?locale=en").json()["profiles"]


def test_create_and_list_perfiles_normalize_primary_tags_preserving_es_legacy_fallback(profiles_file):
    profiles = _read_profiles(profiles_file)
    profiles["regional_stored"] = {
        "id": "id-regional",
        "prompt": "British prompt",
        "use_system": False,
        "locale": "en-GB",
    }
    _write_profiles(profiles_file, profiles)

    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/perfiles",
            json={"name": "us_created", "prompt": "Hi", "locale": "en-US"},
        )
        assert resp.status_code == 200
        assert resp.json()["locale"] == "en"
        assert _read_profiles(profiles_file)["us_created"]["locale"] == "en"

        en_profiles = client.get("/api/perfiles?locale=en").json()["profiles"]
        en_us_profiles = client.get("/api/perfiles?locale=en_US").json()["profiles"]
        es_profiles = client.get("/api/perfiles?locale=es").json()["profiles"]

        assert "us_created" in en_profiles
        assert "regional_stored" in en_profiles
        assert "regional_stored" in en_us_profiles
        assert "default" not in en_profiles
        assert "default" in es_profiles
        assert "regional_stored" not in es_profiles


def test_create_perfil_with_extended_responses_persists_and_returns_it(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/perfiles",
            json={"name": "deep_learner", "prompt": "Teach deeply", "extended_responses": True},
        )
        assert resp.status_code == 200
        assert resp.json()["extended_responses"] is True

        data = _read_profiles(profiles_file)
        assert data["deep_learner"]["extended_responses"] is True

        get_resp = client.get("/api/perfiles/deep_learner")
        assert get_resp.json()["extended_responses"] is True


def test_create_perfil_with_max_turns_persists_and_returns_it(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/perfiles",
            json={"name": "long_memory", "prompt": "Remember more", "max_turns": 5},
        )
        assert resp.status_code == 200
        assert resp.json()["max_turns"] == 5

        data = _read_profiles(profiles_file)
        assert data["long_memory"]["max_turns"] == 5

        get_resp = client.get("/api/perfiles/long_memory")
        assert get_resp.json()["max_turns"] == 5


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


def test_update_perfil_sets_locale_on_a_profile_without_one(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/perfiles/default", json={"locale": "en"})
        assert resp.status_code == 200
        assert resp.json()["locale"] == "en"

        data = _read_profiles(profiles_file)
        assert data["default"]["locale"] == "en"
        assert data["default"]["prompt"] == "Default prompt"  # untouched


def test_update_perfil_omitted_locale_leaves_existing_value_unchanged(profiles_file):
    # "streamer" is seeded with locale="en" (see profiles_file fixture).
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/perfiles/streamer", json={"prompt": "New streamer prompt"})
        assert resp.status_code == 200
        assert resp.json()["locale"] == "en"

        data = _read_profiles(profiles_file)
        assert data["streamer"]["locale"] == "en"
        assert data["streamer"]["prompt"] == "New streamer prompt"


def test_update_perfil_sets_extended_responses(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/perfiles/default", json={"extended_responses": True})
        assert resp.status_code == 200
        assert resp.json()["extended_responses"] is True

        data = _read_profiles(profiles_file)
        assert data["default"]["extended_responses"] is True

        # Toggle back to False
        resp_off = client.put("/api/perfiles/default", json={"extended_responses": False})
        assert resp_off.status_code == 200
        assert resp_off.json()["extended_responses"] is False
        assert _read_profiles(profiles_file)["default"]["extended_responses"] is False


def test_update_perfil_omitted_extended_responses_leaves_existing_value_unchanged(profiles_file):
    app = _app()
    with TestClient(app) as client:
        # First set it to True
        client.put("/api/perfiles/default", json={"extended_responses": True})
        # Update only prompt
        resp = client.put("/api/perfiles/default", json={"prompt": "Only prompt changed"})
        assert resp.status_code == 200
        assert resp.json()["extended_responses"] is True
        assert _read_profiles(profiles_file)["default"]["extended_responses"] is True


def test_update_perfil_sets_max_turns(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/perfiles/default", json={"max_turns": 5})
        assert resp.status_code == 200
        assert resp.json()["max_turns"] == 5

        data = _read_profiles(profiles_file)
        assert data["default"]["max_turns"] == 5

        # Update to another value
        resp_2 = client.put("/api/perfiles/default", json={"max_turns": 8})
        assert resp_2.status_code == 200
        assert resp_2.json()["max_turns"] == 8
        assert _read_profiles(profiles_file)["default"]["max_turns"] == 8


def test_update_perfil_omitted_max_turns_leaves_existing_value_unchanged(profiles_file):
    app = _app()
    with TestClient(app) as client:
        # First set it to 5
        client.put("/api/perfiles/default", json={"max_turns": 5})
        # Update only prompt
        resp = client.put("/api/perfiles/default", json={"prompt": "Only prompt changed"})
        assert resp.status_code == 200
        assert resp.json()["max_turns"] == 5
        assert _read_profiles(profiles_file)["default"]["max_turns"] == 5


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
# POST /api/perfiles/switch — locale pass-through (D7)
#
# switch_profile's dispatch payload is `dict(perfiles[name])` (main.py) — a
# real HTTP round-trip proves the wire model / json.dump on-disk write don't
# silently drop the new key before it reaches the command queue that feeds
# `set_profile` (which already forwards `payload.get("locale")` to the T4
# coherence gate — zero engine changes needed, see llm_engine.py:519).
# ──────────────────────────────────────────────────────────────────────────


def test_switch_profile_enqueues_payload_with_locale(profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.post("/api/perfiles/switch", json={"name": "streamer"})
        assert resp.status_code == 200

        queued = app.state.host.motor.command_queue.get_nowait()
        command, payload = queued
        assert command == "set_profile"
        assert payload["locale"] == "en"


def test_switch_profile_enqueues_payload_with_max_turns(profiles_file):
    app = _app()
    with TestClient(app) as client:
        # Create a profile with explicit max_turns
        client.post(
            "/api/perfiles",
            json={"name": "custom_window", "prompt": "Window test", "max_turns": 8},
        )
        resp = client.post("/api/perfiles/switch", json={"name": "custom_window"})
        assert resp.status_code == 200

        queued = app.state.host.motor.command_queue.get_nowait()
        command, payload = queued
        assert command == "set_profile"
        assert payload["max_turns"] == 8


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


# ──────────────────────────────────────────────────────────────────────────
# max_turns_cap (vram-aware-context-budget T4): the live engine cap the
# ProfileEditor uses as its input max; persisted max_turns is range-checked.
# ──────────────────────────────────────────────────────────────────────────


def _app_with_cap(cap):
    import opencohost.api.main as main_mod

    def _factory():
        host = FakeHost()
        host.motor._max_turns_cap = cap
        return host

    return main_mod.create_app(host_factory=_factory, cors_origins=_DEFAULT_TEST_ORIGINS)


def test_get_perfil_reports_engine_max_turns_cap(profiles_file):
    with TestClient(_app_with_cap(40)) as client:
        assert client.get("/api/perfiles/default").json()["max_turns_cap"] == 40


def test_max_turns_cap_falls_back_to_10_when_engine_has_none(profiles_file):
    with TestClient(_app_with_cap(None)) as client:
        assert client.get("/api/perfiles/default").json()["max_turns_cap"] == 10


def test_update_and_create_report_max_turns_cap(profiles_file):
    with TestClient(_app_with_cap(25)) as client:
        put = client.put("/api/perfiles/default", json={"max_turns": 20})
        assert put.json()["max_turns_cap"] == 25
        post = client.post("/api/perfiles", json={"name": "n", "prompt": "p", "max_turns": 20})
        assert post.json()["max_turns_cap"] == 25


def test_persisted_max_turns_is_range_checked(profiles_file):
    with TestClient(_app()) as client:
        # Stored value keeps the profile's intent up to the absolute ceiling (40);
        # the engine clamps it further to the live, ctx-derived cap.
        resp = client.put("/api/perfiles/default", json={"max_turns": 500})
        assert resp.json()["max_turns"] == 40
        assert _read_profiles(profiles_file)["default"]["max_turns"] == 40

        resp = client.put("/api/perfiles/default", json={"max_turns": 0})
        assert resp.json()["max_turns"] == 1

        post = client.post("/api/perfiles", json={"name": "big", "prompt": "p", "max_turns": 99})
        assert post.json()["max_turns"] == 40
        assert _read_profiles(profiles_file)["big"]["max_turns"] == 40
