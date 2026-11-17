"""Tests for the Cohost-profiles endpoints (WU3):
GET/POST /api/agenda/cohost-profiles and POST /api/agenda/cohost-profiles/select.

SELECT IS STATELESS by design (see design decision): select applies the named
profile's `style` to the running controller (RAM) only. It never persists a
"selected" name to disk. GET therefore returns the on-disk profiles with NO
`selected` field anywhere; clients default to "Natural" (CTK parity — app_shell
tracks `_current_cohost_profile` in RAM, defaults "Natural").

Isolation gotcha: `opencohost.core.profiles.cohost_profiles.COHOST_PROFILES_FILE` is
imported INTO that module's namespace at IMPORT time
(`from opencohost.config.settings import COHOST_PROFILES_FILE`). Patch it THERE
— NOT `settings.COHOST_PROFILES_FILE` — because `load_cohost_profiles` /
`save_cohost_profiles` read the module-local binding, so patching settings would
redirect neither the handlers nor the on-disk assertions.
"""

import json

import pytest
from fastapi.testclient import TestClient

from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod
    from opencohost.i18n import active

    active.reset_active_bundle()
    main_mod._host_active = False
    yield
    main_mod._host_active = False
    active.reset_active_bundle()


@pytest.fixture
def cohost_profiles_file(tmp_path, monkeypatch):
    """Redirect the import-time COHOST_PROFILES_FILE binding to a tmp path.

    No file is written up front — many cases assert the no-file default path,
    so tests that need a seeded file write it themselves.
    """
    import opencohost.core.profiles.cohost_profiles as cp_mod

    path = tmp_path / "cohost_profiles.json"
    monkeypatch.setattr(cp_mod, "COHOST_PROFILES_FILE", str(path))
    return path


def _app(host_factory=FakeHost):
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=host_factory, cors_origins=_DEFAULT_TEST_ORIGINS)


def _host_with_agenda(agenda):
    def factory():
        host = FakeHost()
        host.agenda = agenda
        return host

    return factory


def _read_disk(path):
    return json.loads(path.read_text(encoding="utf-8"))


# ──────────────────────────────────────────────────────────────────────────
# GET /api/agenda/cohost-profiles
# ──────────────────────────────────────────────────────────────────────────


def test_get_cohost_profiles_no_file_returns_three_defaults(cohost_profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/agenda/cohost-profiles")
        assert resp.status_code == 200
        names = {p["name"] for p in resp.json()["profiles"]}
        assert names == {"Natural", "Picante con matiz", "Docente entretenida"}


def test_get_cohost_profiles_never_exposes_selected_field(cohost_profiles_file):
    # Stateless-select invariant: no `selected` key anywhere in the response.
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/agenda/cohost-profiles")
        assert resp.status_code == 200
        assert "selected" not in resp.text
        assert "selected" not in resp.json()


# ──────────────────────────────────────────────────────────────────────────
# POST /api/agenda/cohost-profiles (save)
# ──────────────────────────────────────────────────────────────────────────


def test_post_cohost_profile_persists_normalized(cohost_profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/agenda/cohost-profiles",
            json={"name": "Mi perfil", "style": "Hablá tranqui", "priority": "alta", "length": "expandida"},
        )
        assert resp.status_code == 200
        data = _read_disk(cohost_profiles_file)
        assert data["Mi perfil"] == {
            "style": "Hablá tranqui",
            "default_priority": "alta",
            "default_response_length": "expandida",
        }


def test_post_cohost_profile_name_truncated_to_40(cohost_profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/agenda/cohost-profiles",
            json={"name": "a" * 50, "style": "algo"},
        )
        assert resp.status_code == 200
        data = _read_disk(cohost_profiles_file)
        assert "a" * 40 in data
        assert "a" * 41 not in data


def test_post_cohost_profile_style_truncated_to_600(cohost_profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/agenda/cohost-profiles",
            json={"name": "Largo", "style": "b" * 700},
        )
        assert resp.status_code == 200
        data = _read_disk(cohost_profiles_file)
        assert len(data["Largo"]["style"]) == 600


def test_post_cohost_profile_empty_name_422_no_mutation(cohost_profiles_file):
    # Seed a known file, then confirm a whitespace-only name is refused AND
    # leaves the file byte-identical.
    cohost_profiles_file.write_text(
        json.dumps({"Seed": {"style": "x", "default_priority": "normal", "default_response_length": "normal"}}),
        encoding="utf-8",
    )
    before = cohost_profiles_file.read_bytes()
    app = _app()
    with TestClient(app) as client:
        resp = client.post("/api/agenda/cohost-profiles", json={"name": "   ", "style": "y"})
        assert resp.status_code == 422
        assert resp.json()["detail"] == "empty profile name"
    assert cohost_profiles_file.read_bytes() == before


# ──────────────────────────────────────────────────────────────────────────
# POST /api/agenda/cohost-profiles/select (stateless — RAM only)
# ──────────────────────────────────────────────────────────────────────────


def test_select_known_applies_style_to_controller(cohost_profiles_file):
    from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController

    controller = KiraAgendaController()
    app = _app(_host_with_agenda(controller))
    with TestClient(app) as client:
        resp = client.post("/api/agenda/cohost-profiles/select", json={"name": "Picante con matiz"})
        assert resp.status_code == 200
        assert resp.json() == {"selected": "Picante con matiz"}
        # The style actually reached the running controller (RAM side effect).
        assert "picante" in controller.profile["style"].lower()


def test_select_unknown_name_404(cohost_profiles_file):
    from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController

    app = _app(_host_with_agenda(KiraAgendaController()))
    with TestClient(app) as client:
        resp = client.post("/api/agenda/cohost-profiles/select", json={"name": "No existe"})
        assert resp.status_code == 404


def test_select_agenda_none_503(cohost_profiles_file):
    app = _app(_host_with_agenda(None))
    with TestClient(app) as client:
        resp = client.post("/api/agenda/cohost-profiles/select", json={"name": "Natural"})
        assert resp.status_code == 503
        assert resp.json()["detail"] == "agenda_unavailable"


def test_select_writes_no_file(cohost_profiles_file):
    # Stateless assertion: no file on disk before select, and select must not
    # create one — selection lives only in the running controller's RAM.
    from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController

    assert not cohost_profiles_file.exists()
    app = _app(_host_with_agenda(KiraAgendaController()))
    with TestClient(app) as client:
        resp = client.post("/api/agenda/cohost-profiles/select", json={"name": "Natural"})
        assert resp.status_code == 200
    assert not cohost_profiles_file.exists()


def test_get_cohost_profiles_en_returns_english_defaults(cohost_profiles_file):
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/agenda/cohost-profiles?locale=en")
        assert resp.status_code == 200
        names = {p["name"] for p in resp.json()["profiles"]}
        assert names == {"Natural", "Spicy with nuance", "Entertaining teacher"}


def test_save_and_select_cohost_profile_en_isolated_from_es(cohost_profiles_file):
    from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController

    controller = KiraAgendaController()
    app = _app(_host_with_agenda(controller))
    with TestClient(app) as client:
        # Save a custom English profile
        save_resp = client.post(
            "/api/agenda/cohost-profiles",
            json={
                "name": "Dry Sarcasm",
                "style": "Dry, sarcastic, but friendly.",
                "locale": "en",
            },
        )
        assert save_resp.status_code == 200
        en_names = {p["name"] for p in save_resp.json()["profiles"]}
        assert "Dry Sarcasm" in en_names

        # Verify it appears in EN query
        get_en = client.get("/api/agenda/cohost-profiles?locale=en")
        assert "Dry Sarcasm" in {p["name"] for p in get_en.json()["profiles"]}

        # Verify it DOES NOT leak into ES query
        get_es = client.get("/api/agenda/cohost-profiles?locale=es")
        assert "Dry Sarcasm" not in {p["name"] for p in get_es.json()["profiles"]}

        # Select the EN profile
        select_resp = client.post(
            "/api/agenda/cohost-profiles/select",
            json={"name": "Dry Sarcasm", "locale": "en"},
        )
        assert select_resp.status_code == 200
        assert controller.profile["style"] == "Dry, sarcastic, but friendly."


def test_post_cohost_profile_preserves_existing_priority_and_length_when_omitted(cohost_profiles_file):
    app = _app()
    with TestClient(app) as client:
        first = client.post(
            "/api/agenda/cohost-profiles",
            json={"name": "Custom", "style": "Initial style", "priority": "alta", "length": "expandida"},
        )
        assert first.status_code == 200

        # Omitting priority and length on update must preserve existing metadata
        second = client.post(
            "/api/agenda/cohost-profiles",
            json={"name": "Custom", "style": "Updated style"},
        )
        assert second.status_code == 200
        data = _read_disk(cohost_profiles_file)
        assert data["Custom"] == {
            "style": "Updated style",
            "default_priority": "alta",
            "default_response_length": "expandida",
        }


def test_post_cohost_profile_blank_style_preserves_existing_style_but_defaults_new_profile(cohost_profiles_file):
    from opencohost.core.profiles.cohost_profiles import DEFAULT_COHOST_PROFILES

    app = _app()
    with TestClient(app) as client:
        # Updating an existing preset with blank optional estilo preserves its style and length
        upd = client.post(
            "/api/agenda/cohost-profiles",
            json={"name": "Picante con matiz", "style": "   "},
        )
        assert upd.status_code == 200
        data = _read_disk(cohost_profiles_file)
        assert data["Picante con matiz"] == DEFAULT_COHOST_PROFILES["Picante con matiz"]

        # Creating a brand new profile with blank style still gets the Natural fallback style
        created = client.post(
            "/api/agenda/cohost-profiles",
            json={"name": "Brand New", "style": ""},
        )
        assert created.status_code == 200
        data = _read_disk(cohost_profiles_file)
        assert data["Brand New"] == {
            "style": DEFAULT_COHOST_PROFILES["Natural"]["style"],
            "default_priority": "normal",
            "default_response_length": "normal",
        }


def test_cohost_profiles_omitted_locale_follows_active_en_bundle(cohost_profiles_file):
    from opencohost.i18n import active
    from opencohost.i18n.registry import discover_bundles, official_locales_dir
    from opencohost.i18n.startup import resolve_active_bundle

    official = discover_bundles(official_locales_dir(), "official")
    active.set_active_bundle(resolve_active_bundle(locale="en", registry=official))
    en_path = cohost_profiles_file.with_name("cohost_profiles_en.json")

    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/agenda/cohost-profiles")
        assert resp.status_code == 200
        assert {p["name"] for p in resp.json()["profiles"]} == {"Natural", "Spicy with nuance", "Entertaining teacher"}

        save_resp = client.post(
            "/api/agenda/cohost-profiles",
            json={"name": "Runtime EN", "style": "Concise English"},
        )
        assert save_resp.status_code == 200
        assert en_path.exists()
        assert not cohost_profiles_file.exists()
        assert "Runtime EN" in _read_disk(en_path)


def test_cohost_profiles_bcp47_locale_normalizes_primary_subtag(cohost_profiles_file):
    en_path = cohost_profiles_file.with_name("cohost_profiles_en.json")
    app = _app()
    with TestClient(app) as client:
        save_resp = client.post(
            "/api/agenda/cohost-profiles",
            json={"name": "Regional EN", "style": "Calm", "locale": "en-US"},
        )
        assert save_resp.status_code == 200
        assert en_path.exists()
        assert not cohost_profiles_file.exists()

        get_resp = client.get("/api/agenda/cohost-profiles?locale=en_GB")
        assert get_resp.status_code == 200
        assert "Regional EN" in {p["name"] for p in get_resp.json()["profiles"]}


def test_select_cohost_profile_matches_sanitized_truncated_name(cohost_profiles_file):
    from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController

    controller = KiraAgendaController()
    long_name = "  " + ("L" * 45) + "  "
    app = _app(_host_with_agenda(controller))
    with TestClient(app) as client:
        save_resp = client.post(
            "/api/agenda/cohost-profiles",
            json={"name": long_name, "style": "Long name style", "locale": "es"},
        )
        assert save_resp.status_code == 200
        select_resp = client.post(
            "/api/agenda/cohost-profiles/select",
            json={"name": long_name, "locale": "es"},
        )
        assert select_resp.status_code == 200
        assert select_resp.json() == {"selected": "L" * 40}
        assert controller.profile["style"] == "Long name style"

