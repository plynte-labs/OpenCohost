"""Tests for GET/PUT /api/i18n (kira_bilingual_e2e_20260705, P7 config surface).

Design §7.1: GET reuses load_registry() (official + community discovery,
zero new state) for `available`, active.get_active_bundle() for
`active_locale`, i18n.state.get_locale() for `persisted_locale`, and
check_coherence(bundle) (bundle-level only, no profile/piper args) for
`warnings`. PUT normalizes the body via i18n.tags.match, 422s on no matching
bundle, and persists via i18n.state.set_locale() (D6: next-boot only, no
hot-swap of the running process's active bundle).

Isolation: the active bundle cache (opencohost.i18n.active) and the
persisted-locale file (opencohost.i18n.state, via
opencohost.config.storage.USER_DATA_DIR) are both reset/redirected per test —
this is the first work unit to exercise state.set_locale()'s DEFAULT (no
explicit path) resolution through a real API call, so it is the first place
that hazard can leak into a developer's real config/ directory.
"""

import pytest
from fastapi.testclient import TestClient

from opencohost.i18n import active as i18n_active
from opencohost.i18n.startup import resolve_active_bundle
from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


@pytest.fixture(autouse=True)
def _isolate_locale_state(tmp_path, monkeypatch):
    """Redirect USER_DATA_DIR so state.set_locale()'s default path never
    touches a developer's real config/locale.json (same hazard class as
    conftest.py's _isolate_api_tokens_file / _isolate_piper_voice_file)."""
    from opencohost.config import storage as storage_mod

    monkeypatch.setattr(storage_mod, "USER_DATA_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _reset_active_bundle():
    i18n_active.reset_active_bundle()
    yield
    i18n_active.reset_active_bundle()


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)


def _pin_active(locale: str) -> None:
    i18n_active.set_active_bundle(resolve_active_bundle(locale=locale))


# ──────────────────────────────────────────────────────────────────────────
# GET /api/i18n shape
# ──────────────────────────────────────────────────────────────────────────


def test_get_i18n_shape():
    _pin_active("es")
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/i18n")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {
            "active_locale",
            "persisted_locale",
            "pending_restart",
            "available",
            "warnings",
        }
        assert body["active_locale"] == "es"


def test_get_i18n_available_lists_both_official_bundles():
    _pin_active("es")
    app = _app()
    with TestClient(app) as client:
        body = client.get("/api/i18n").json()
        codes = {entry["code"] for entry in body["available"]}
        assert codes == {"es", "en"}
        for entry in body["available"]:
            assert set(entry.keys()) == {"code", "display", "tier", "status"}
            assert entry["tier"] == "official"


def test_get_i18n_es_bundle_reports_complete_status():
    _pin_active("es")
    app = _app()
    with TestClient(app) as client:
        body = client.get("/api/i18n").json()
        es_entry = next(e for e in body["available"] if e["code"] == "es")
        assert es_entry["status"] == "complete"
        assert es_entry["display"] == "Español"


def test_get_i18n_persisted_locale_defaults_to_es_when_unset():
    _pin_active("es")
    app = _app()
    with TestClient(app) as client:
        body = client.get("/api/i18n").json()
        assert body["persisted_locale"] == "es"
        assert body["pending_restart"] is False


def test_get_i18n_pending_restart_true_when_persisted_diverges_from_active():
    # Active process loaded 'es', but a prior PUT persisted 'en' — pending.
    _pin_active("es")
    from opencohost.i18n import state as i18n_state_mod

    i18n_state_mod.set_locale("en")
    app = _app()
    with TestClient(app) as client:
        body = client.get("/api/i18n").json()
        assert body["persisted_locale"] == "en"
        assert body["active_locale"] == "es"
        assert body["pending_restart"] is True


def test_get_i18n_warnings_is_bundle_level_only():
    # No profile/piper context is threaded through — a coherent es bundle
    # produces no warnings from this endpoint.
    _pin_active("es")
    app = _app()
    with TestClient(app) as client:
        body = client.get("/api/i18n").json()
        assert body["warnings"] == []


# ──────────────────────────────────────────────────────────────────────────
# PUT /api/i18n happy path + normalization + 422
# ──────────────────────────────────────────────────────────────────────────


def test_put_i18n_happy_path_persists_and_reports_pending_restart():
    _pin_active("es")
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/i18n", json={"locale": "en"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["persisted_locale"] == "en"
        assert body["active_locale"] == "es"  # this process never hot-swaps
        assert body["pending_restart"] is True


def test_put_i18n_normalizes_bcp47_tag():
    _pin_active("es")
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/i18n", json={"locale": "EN_us"})
        assert resp.status_code == 200
        assert resp.json()["persisted_locale"] == "en"


def test_put_i18n_unknown_locale_returns_422_and_does_not_persist():
    _pin_active("es")
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/i18n", json={"locale": "xx"})
        assert resp.status_code == 422
        # Nothing was written — a follow-up GET still reports the default.
        follow_up = client.get("/api/i18n").json()
        assert follow_up["persisted_locale"] == "es"


def test_put_i18n_same_as_active_reports_pending_restart_false():
    _pin_active("es")
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/i18n", json={"locale": "es"})
        assert resp.status_code == 200
        assert resp.json()["pending_restart"] is False
