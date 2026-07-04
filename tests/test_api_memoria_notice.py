"""Tests for GET/POST /api/memoria/notice (WU1, F1 disclosure-banner state).

Mirrors test_api_memoria_mutations.py's fixture shape (FakeHost from
tests.test_api_phase1, `_reset_host_active` autouse fixture, `_app()` helper
with `_DEFAULT_TEST_ORIGINS`). The backing functions
load_memorias_notice_dismissed / save_memorias_notice_dismissed read the module
global MEMORIAS_NOTICE_FILE at CALL TIME (via the `config_file=None` default),
so the fixture monkeypatches `opencohost.config.settings.MEMORIAS_NOTICE_FILE`
directly — NOT an import-time binding in another module.

The `notice_file` fixture deliberately does NOT pre-create the file: the first
GET must exercise the fail-open path (absent file -> {"dismissed": false}, the
banner shows by default rather than silently hiding an undismissed disclosure).
"""

import json

import pytest
from fastapi.testclient import TestClient

from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


@pytest.fixture
def notice_file(tmp_path, monkeypatch):
    """Point MEMORIAS_NOTICE_FILE at a tmp path WITHOUT creating it (fail-open)."""
    import opencohost.config.settings as settings_mod

    path = tmp_path / "config" / "memorias_notice.json"
    monkeypatch.setattr(settings_mod, "MEMORIAS_NOTICE_FILE", str(path))
    return path


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)


def test_get_notice_no_file_fails_open_to_not_dismissed(notice_file):
    assert not notice_file.exists()

    with TestClient(_app()) as client:
        resp = client.get("/api/memoria/notice")
        assert resp.status_code == 200
        assert resp.json() == {"dismissed": False}


def test_post_notice_true_persists_to_disk_and_echoes(notice_file):
    with TestClient(_app()) as client:
        resp = client.post("/api/memoria/notice", json={"dismissed": True})
        assert resp.status_code == 200
        assert resp.json() == {"dismissed": True}

    # The response is not just echoing the body — the flag is on disk.
    assert notice_file.exists()
    assert json.loads(notice_file.read_text(encoding="utf-8")) == {"dismissed": True}


def test_get_after_post_round_trips_from_disk(notice_file):
    with TestClient(_app()) as client:
        client.post("/api/memoria/notice", json={"dismissed": True})
        # A fresh GET re-reads disk — proves POST did not merely echo the request.
        resp = client.get("/api/memoria/notice")
        assert resp.status_code == 200
        assert resp.json() == {"dismissed": True}


def test_post_false_after_true_flips_persisted_value_back(notice_file):
    with TestClient(_app()) as client:
        client.post("/api/memoria/notice", json={"dismissed": True})

        resp = client.post("/api/memoria/notice", json={"dismissed": False})
        assert resp.status_code == 200
        assert resp.json() == {"dismissed": False}

    assert json.loads(notice_file.read_text(encoding="utf-8")) == {"dismissed": False}


def test_post_missing_dismissed_field_422(notice_file):
    with TestClient(_app()) as client:
        resp = client.post("/api/memoria/notice", json={})
        assert resp.status_code == 422


def test_post_non_bool_dismissed_422(notice_file):
    with TestClient(_app()) as client:
        # Pydantic v2 lax-coerces "yes"/1/"0" to bool, so a JSON array is used to
        # force rejection (phase-1 'Learned' precedent).
        resp = client.post("/api/memoria/notice", json={"dismissed": [1, 2]})
        assert resp.status_code == 422
