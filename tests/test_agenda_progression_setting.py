"""Agenda progression deck variants: default ON, persisted toggle, env override.

Owner decision 2026-11-16:
progression is ON by default ('auto'), the operator can turn it off from the
Agenda panel (persisted in agenda_settings.json, applied without restart), and
OPENCOHOST_AGENDA_PROGRESSION, when set, still overrides the stored choice.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from opencohost.config import settings
from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController
from tests.test_api_phase1 import FakeHost

_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    cfg = tmp_path / "agenda_settings.json"
    monkeypatch.setattr(settings, "AGENDA_SETTINGS_FILE", str(cfg))
    monkeypatch.delenv("OPENCOHOST_AGENDA_PROGRESSION", raising=False)
    # Restore the module globals after each test (the setter mutates them).
    monkeypatch.setattr(settings, "AGENDA_PROGRESSION_VARIANT", settings.AGENDA_PROGRESSION_VARIANT)
    monkeypatch.setattr(settings, "AGENDA_PROGRESSION_ENABLED", settings.AGENDA_PROGRESSION_ENABLED)
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield cfg
    main_mod._host_active = False


# -- settings layer ---------------------------------------------------------


def test_load_defaults_to_enabled_when_file_absent():
    assert settings.load_agenda_progression_enabled() is True


def test_load_defaults_to_enabled_on_corrupt_file(_isolate):
    _isolate.write_text("{not json", encoding="utf-8")
    assert settings.load_agenda_progression_enabled() is True


def test_load_non_bool_value_fails_open_to_enabled(_isolate):
    _isolate.write_text(json.dumps({"progression_enabled": "no"}), encoding="utf-8")
    assert settings.load_agenda_progression_enabled() is True


def test_save_then_load_roundtrip_and_preserves_keys(_isolate):
    _isolate.write_text(json.dumps({"other": 1}), encoding="utf-8")
    assert settings.save_agenda_progression_enabled(False) is True
    assert settings.load_agenda_progression_enabled() is False
    data = json.loads(_isolate.read_text(encoding="utf-8"))
    assert data["other"] == 1
    assert data["progression_enabled"] is False
    assert settings.save_agenda_progression_enabled(True) is True
    assert settings.load_agenda_progression_enabled() is True


def test_resolve_default_is_auto_when_env_unset_and_nothing_stored():
    assert settings._resolve_agenda_progression_variant() == "auto"


def test_resolve_follows_stored_off():
    settings.save_agenda_progression_enabled(False)
    assert settings._resolve_agenda_progression_variant() == ""


@pytest.mark.parametrize("env,expected", [("0", ""), ("off", ""), ("v3", "v3"), ("1", "auto"), ("bogus", "")])
def test_env_overrides_stored_choice(monkeypatch, env, expected):
    settings.save_agenda_progression_enabled(env not in ("0", "off", "bogus"))
    monkeypatch.setenv("OPENCOHOST_AGENDA_PROGRESSION", env)
    assert settings.agenda_progression_env_override() is True
    assert settings._resolve_agenda_progression_variant() == expected


def test_env_override_flag_false_when_unset_or_blank(monkeypatch):
    assert settings.agenda_progression_env_override() is False
    monkeypatch.setenv("OPENCOHOST_AGENDA_PROGRESSION", "  ")
    assert settings.agenda_progression_env_override() is False


def test_explicit_raw_resolution_is_unchanged():
    assert settings._resolve_agenda_progression_variant("") == ""
    assert settings._resolve_agenda_progression_variant("v2") == "v2"


def test_apply_updates_runtime_globals_without_restart():
    settings.apply_agenda_progression_enabled(False)
    assert settings.get_agenda_progression_variant() == ""
    assert KiraAgendaController().active_progression_variant() == ""
    settings.apply_agenda_progression_enabled(True)
    assert settings.get_agenda_progression_variant() == "auto"
    assert KiraAgendaController().active_progression_variant() == "auto"


def test_apply_does_not_override_env(monkeypatch):
    monkeypatch.setenv("OPENCOHOST_AGENDA_PROGRESSION", "v2")
    settings.apply_agenda_progression_enabled(False)
    assert settings.get_agenda_progression_variant() == "v2"


# -- API --------------------------------------------------------------------


def _client():
    import opencohost.api.main as main_mod

    def factory():
        host = FakeHost()
        host.agenda = KiraAgendaController()
        return host

    return TestClient(main_mod.create_app(host_factory=factory, cors_origins=_ORIGINS))


def test_get_progression_default_on():
    with _client() as client:
        body = client.get("/api/agenda/progression").json()
    assert body == {"enabled": True, "variant": "auto", "env_override": False}


def test_put_progression_off_persists_and_applies(_isolate):
    with _client() as client:
        resp = client.put("/api/agenda/progression", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json() == {"enabled": False, "variant": "", "env_override": False}
        assert client.get("/api/agenda/progression").json()["enabled"] is False
    assert settings.load_agenda_progression_enabled() is False
    assert settings.get_agenda_progression_variant() == ""


def test_put_progression_rejects_non_bool():
    with _client() as client:
        assert client.put("/api/agenda/progression", json={"enabled": "maybe"}).status_code == 422
        assert client.put("/api/agenda/progression", json={}).status_code == 422


def test_put_progression_with_env_override_reports_it(monkeypatch):
    monkeypatch.setenv("OPENCOHOST_AGENDA_PROGRESSION", "v3")
    with _client() as client:
        body = client.put("/api/agenda/progression", json={"enabled": False}).json()
    assert body == {"enabled": False, "variant": "v3", "env_override": True}
    assert settings.load_agenda_progression_enabled() is False


def test_put_progression_503_when_write_fails(monkeypatch):
    monkeypatch.setattr(settings, "save_agenda_progression_enabled", lambda enabled, config_file=None: False)
    with _client() as client:
        resp = client.put("/api/agenda/progression", json={"enabled": False})
    assert resp.status_code == 503
    assert resp.json()["detail"] == "agenda_settings_write_failed"
