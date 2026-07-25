"""Tests for GET/PUT /api/llm/provider (multi_provider_llm_20260723 Phase 1).

Design 'Provider Config Surface' (per-provider persisted profiles amendment):
global operator-posture fields (`active_provider`, `fallback_mode`,
`pregen_enabled`) live outside per-provider `profiles` (`base_url`, `model`,
`preset`). Keys never live in this response -- they persist separately via
`OAuthStore` (`config/llm_keys.json`), one key per profile id. Absent config
resolves to the local-only default (byte-identical pre-track behavior).
"""

import json
import logging

import pytest
from fastapi.testclient import TestClient

from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


def _raise_oserror(*args, **kwargs):
    raise OSError("simulated write failure")


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


@pytest.fixture(autouse=True)
def _isolated_llm_provider_files(tmp_path, monkeypatch):
    """No real llm_provider.json / llm_keys.json is ever touched by these tests."""
    import opencohost.api.main as main_mod
    import opencohost.config.llm_provider as llm_provider_mod

    monkeypatch.setattr(
        llm_provider_mod, "LLM_PROVIDER_CONFIG_FILE", str(tmp_path / "llm_provider.json")
    )
    monkeypatch.setattr(main_mod, "LLM_KEYS_FILE", str(tmp_path / "llm_keys.json"))


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)


def _configure(client, profile_id, base_url=None, model=None, api_key=None):
    body = {"profile_id": profile_id}
    if base_url is not None:
        body["base_url"] = base_url
    if model is not None:
        body["model"] = model
    if api_key is not None:
        body["api_key"] = api_key
    return client.put("/api/llm/provider", json=body)


# ──────────────────────────────────────────────────────────────────────────
# GET — absent config, response shape, no key leakage
# ──────────────────────────────────────────────────────────────────────────


def test_get_absent_config_defaults_local():
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/llm/provider")
        assert resp.status_code == 200
        assert resp.json() == {
            "active_provider": "local",
            "fallback_mode": "auto",
            "pregen_enabled": False,
            "profiles": {},
        }


def test_get_never_leaks_api_key_field():
    app = _app()
    with TestClient(app) as client:
        _configure(client, "openai", base_url="https://api.openai.com/v1", model="gpt-4o-mini", api_key="sk-secret")
        resp = client.get("/api/llm/provider")
        assert '"api_key":' not in resp.text
        assert "sk-secret" not in resp.text
        assert resp.json()["profiles"]["openai"]["api_key_set"] is True


# ──────────────────────────────────────────────────────────────────────────
# PUT — round-trip, no-overwrite guarantee, selector swap, key clear
# ──────────────────────────────────────────────────────────────────────────


def test_put_creates_profile_with_key_and_get_round_trips():
    app = _app()
    with TestClient(app) as client:
        resp = _configure(
            client, "openai", base_url="https://api.openai.com/v1", model="gpt-4o-mini", api_key="sk-secret-openai"
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["profiles"]["openai"] == {
            "base_url": "https://api.openai.com/v1",
            "model": "gpt-4o-mini",
            "preset": None,
            "api_key_set": True,
        }
        assert '"api_key":' not in resp.text
        assert "sk-secret-openai" not in resp.text


def test_put_two_profiles_no_overwrite_guarantee():
    app = _app()
    with TestClient(app) as client:
        _configure(client, "openai", base_url="https://a", model="m1", api_key="key-a")
        _configure(client, "nvidia_nim", base_url="https://b", model="m2", api_key="key-b")

        body = client.get("/api/llm/provider").json()
        assert body["profiles"]["openai"]["base_url"] == "https://a"
        assert body["profiles"]["openai"]["api_key_set"] is True
        assert body["profiles"]["nvidia_nim"]["base_url"] == "https://b"
        assert body["profiles"]["nvidia_nim"]["api_key_set"] is True


def test_active_provider_swap_only_selector_rewrites_nothing():
    app = _app()
    with TestClient(app) as client:
        _configure(client, "openai", base_url="https://a", model="m1", api_key="key-a")
        _configure(client, "nvidia_nim", base_url="https://b", model="m2", api_key="key-b")
        before = client.get("/api/llm/provider").json()["profiles"]

        resp1 = client.put("/api/llm/provider", json={"active_provider": "nvidia_nim"})
        assert resp1.status_code == 200
        assert resp1.json()["active_provider"] == "nvidia_nim"

        resp2 = client.put("/api/llm/provider", json={"active_provider": "openai"})
        assert resp2.status_code == 200
        assert resp2.json()["active_provider"] == "openai"

        after = client.get("/api/llm/provider").json()["profiles"]
        assert after == before


def test_clearing_one_key_leaves_the_other():
    app = _app()
    with TestClient(app) as client:
        _configure(client, "openai", base_url="https://a", model="m1", api_key="key-a")
        _configure(client, "nvidia_nim", base_url="https://b", model="m2", api_key="key-b")

        resp = client.put("/api/llm/provider", json={"profile_id": "openai", "api_key": ""})
        assert resp.status_code == 200

        body = client.get("/api/llm/provider").json()
        assert body["profiles"]["openai"]["api_key_set"] is False
        assert body["profiles"]["nvidia_nim"]["api_key_set"] is True


# ──────────────────────────────────────────────────────────────────────────
# PUT validation ladder (422s in design order)
# ──────────────────────────────────────────────────────────────────────────


def test_put_scoped_field_without_profile_id_422():
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/llm/provider", json={"base_url": "https://x"})
        assert resp.status_code == 422
        assert resp.json() == {"detail": "profile_id required"}


@pytest.mark.parametrize("bad_id", ["local", "Has-Upper", "has space", "has.dot", "openai\n"])
def test_put_invalid_profile_id_422(bad_id):
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/llm/provider", json={"profile_id": bad_id, "base_url": "https://x"})
        assert resp.status_code == 422
        assert resp.json() == {"detail": "invalid profile_id"}


def test_put_unknown_preset_422():
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/llm/provider", json={"profile_id": "openai", "preset": "not_a_preset"})
        assert resp.status_code == 422
        assert resp.json() == {"detail": "unknown preset"}


def test_put_unknown_provider_422():
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/llm/provider", json={"active_provider": "openai"})
        assert resp.status_code == 422
        assert resp.json() == {"detail": "unknown provider"}


def test_put_activation_completeness_missing_base_url_422():
    app = _app()
    with TestClient(app) as client:
        _configure(client, "openai", model="m1")
        resp = client.put("/api/llm/provider", json={"active_provider": "openai"})
        assert resp.status_code == 422
        assert resp.json() == {"detail": "base_url required for active cloud profile"}


def test_put_activation_completeness_missing_model_422():
    app = _app()
    with TestClient(app) as client:
        _configure(client, "openai", base_url="https://api.openai.com/v1")
        resp = client.put("/api/llm/provider", json={"active_provider": "openai"})
        assert resp.status_code == 422
        assert resp.json() == {"detail": "model required for active cloud profile"}


def test_put_draft_save_of_incomplete_inactive_profile_succeeds():
    app = _app()
    with TestClient(app) as client:
        resp = _configure(client, "openai", model="m1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["active_provider"] == "local"
        assert body["profiles"]["openai"]["base_url"] == ""


# ──────────────────────────────────────────────────────────────────────────
# Presets + pregen gate flag
# ──────────────────────────────────────────────────────────────────────────


def test_put_preset_prefills_unset_fields_manual_override_persists():
    app = _app()
    with TestClient(app) as client:
        resp = client.put(
            "/api/llm/provider",
            json={
                "profile_id": "nvidia_nim",
                "preset": "nvidia_nim",
                "base_url": "https://custom.example/v1",
            },
        )
        assert resp.status_code == 200
        profile = resp.json()["profiles"]["nvidia_nim"]
        assert profile["preset"] == "nvidia_nim"
        assert profile["base_url"] == "https://custom.example/v1"
        assert profile["model"]  # prefilled from the preset's first model


def test_pregen_enabled_defaults_false_and_enable_via_put():
    app = _app()
    with TestClient(app) as client:
        assert client.get("/api/llm/provider").json()["pregen_enabled"] is False

        resp = client.put("/api/llm/provider", json={"pregen_enabled": True})
        assert resp.status_code == 200
        assert resp.json()["pregen_enabled"] is True
        assert client.get("/api/llm/provider").json()["pregen_enabled"] is True


# ──────────────────────────────────────────────────────────────────────────
# Write-failure paths -> 503, never 500 (mirrors test_api_write_failures.py)
# ──────────────────────────────────────────────────────────────────────────


def test_key_store_write_failure_returns_503(monkeypatch):
    import opencohost.stream_admin.oauth_store as oauth_store_mod

    monkeypatch.setattr(oauth_store_mod.OAuthStore, "save", _raise_oserror)
    app = _app()
    with TestClient(app) as client:
        resp = _configure(client, "openai", base_url="https://a", model="m1", api_key="key-a")
        assert resp.status_code == 503
        assert resp.json() == {"detail": "key_store_write_failed"}


def test_config_write_failure_returns_503(monkeypatch):
    import opencohost.config.storage as storage_mod

    monkeypatch.setattr(storage_mod.os, "replace", _raise_oserror)
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/llm/provider", json={"pregen_enabled": True})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "provider_config_write_failed"}


def test_put_config_write_failure_with_api_key_leaves_key_store_untouched(monkeypatch):
    """F1: config write is attempted FIRST. If it fails, the key store must
    never be touched -- no orphan secret committed while the 503 claims
    total failure."""
    import opencohost.api.main as main_mod
    import opencohost.config.storage as storage_mod
    from opencohost.stream_admin.oauth_store import OAuthStore

    monkeypatch.setattr(storage_mod.os, "replace", _raise_oserror)
    app = _app()
    with TestClient(app) as client:
        resp = _configure(client, "openai", base_url="https://a", model="m1", api_key="key-a")
        assert resp.status_code == 503
        assert resp.json() == {"detail": "provider_config_write_failed"}

    key_store = OAuthStore(main_mod.LLM_KEYS_FILE)
    assert key_store.has_token("openai") is False


def test_put_key_store_write_failure_leaves_config_persisted_as_keyless_draft(monkeypatch):
    """F1: once config write succeeds, a subsequent key-write failure must
    leave the profile persisted as a visible, keyless draft (api_key_set
    False) so a retry converges -- never an invisible orphan."""
    import opencohost.stream_admin.oauth_store as oauth_store_mod

    monkeypatch.setattr(oauth_store_mod.OAuthStore, "save", _raise_oserror)
    app = _app()
    with TestClient(app) as client:
        resp = _configure(client, "openai", base_url="https://a", model="m1", api_key="key-a")
        assert resp.status_code == 503
        assert resp.json() == {"detail": "key_store_write_failed"}

        get_resp = client.get("/api/llm/provider")
        assert get_resp.status_code == 200
        profile = get_resp.json()["profiles"]["openai"]
        assert profile["base_url"] == "https://a"
        assert profile["model"] == "m1"
        assert profile["api_key_set"] is False


# ──────────────────────────────────────────────────────────────────────────
# Diagnosability: a 503 write failure must log the traceback (the original bug
# had ZERO traceback anywhere -- the handler swallowed OSError silently), and
# the log must never carry the key value.
# ──────────────────────────────────────────────────────────────────────────


def _write_records(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR and r.exc_info]


def test_key_store_write_failure_logs_traceback_without_key(monkeypatch, caplog):
    import opencohost.stream_admin.oauth_store as oauth_store_mod

    monkeypatch.setattr(oauth_store_mod.OAuthStore, "save", _raise_oserror)
    app = _app()
    with caplog.at_level(logging.ERROR, logger="opencohost.api.main"):
        with TestClient(app) as client:
            resp = _configure(
                client, "openai", base_url="https://a", model="m1", api_key="super-secret-key"
            )
    assert resp.status_code == 503
    recs = _write_records(caplog)
    assert recs, "a key-store 503 must log an ERROR record with a traceback"
    assert "super-secret-key" not in caplog.text  # redaction: key never logged


def test_provider_config_write_failure_logs_traceback(monkeypatch, caplog):
    import opencohost.config.storage as storage_mod

    monkeypatch.setattr(storage_mod.os, "replace", _raise_oserror)
    app = _app()
    with caplog.at_level(logging.ERROR, logger="opencohost.api.main"):
        with TestClient(app) as client:
            resp = client.put("/api/llm/provider", json={"pregen_enabled": True})
    assert resp.status_code == 503
    assert _write_records(caplog), "a config 503 must log an ERROR record with a traceback"


# ──────────────────────────────────────────────────────────────────────────
# F3 — orphan invisible key: api_key-only PUT for a brand-new profile id
# ──────────────────────────────────────────────────────────────────────────


def test_put_api_key_only_for_fresh_profile_id_creates_visible_draft():
    app = _app()
    with TestClient(app) as client:
        resp = client.put(
            "/api/llm/provider", json={"profile_id": "openai", "api_key": "sk-secret"}
        )
        assert resp.status_code == 200

        body = client.get("/api/llm/provider").json()
        assert body["profiles"]["openai"] == {
            "base_url": "",
            "model": "",
            "preset": None,
            "api_key_set": True,
        }


# ──────────────────────────────────────────────────────────────────────────
# F6 — activation requires a STORED key (spec.md 'Cloud selected')
# ──────────────────────────────────────────────────────────────────────────


def test_put_activation_completeness_missing_api_key_422():
    app = _app()
    with TestClient(app) as client:
        _configure(client, "openai", base_url="https://a", model="m1")
        resp = client.put("/api/llm/provider", json={"active_provider": "openai"})
        assert resp.status_code == 422
        assert resp.json() == {"detail": "api_key required for active cloud profile"}


def test_put_clearing_active_profile_key_422():
    app = _app()
    with TestClient(app) as client:
        _configure(client, "openai", base_url="https://a", model="m1", api_key="key-a")
        activate_resp = client.put("/api/llm/provider", json={"active_provider": "openai"})
        assert activate_resp.status_code == 200

        resp = client.put("/api/llm/provider", json={"profile_id": "openai", "api_key": ""})
        assert resp.status_code == 422
        assert resp.json() == {"detail": "api_key required for active cloud profile"}

        # never persisted the clear -- key must still be there
        body = client.get("/api/llm/provider").json()
        assert body["profiles"]["openai"]["api_key_set"] is True


def test_put_activation_with_key_in_same_put_succeeds():
    app = _app()
    with TestClient(app) as client:
        resp = client.put(
            "/api/llm/provider",
            json={
                "profile_id": "openai",
                "base_url": "https://a",
                "model": "m1",
                "api_key": "key-a",
                "active_provider": "openai",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["active_provider"] == "openai"
        assert resp.json()["profiles"]["openai"]["api_key_set"] is True


# ──────────────────────────────────────────────────────────────────────────
# F7 — corrupted (non-dict) profile entries dropped on load, never a 500
# ──────────────────────────────────────────────────────────────────────────


def test_get_survives_corrupted_non_dict_profile_entry():
    import opencohost.config.llm_provider as llm_provider_mod

    app = _app()
    with TestClient(app) as client:
        with open(llm_provider_mod.LLM_PROVIDER_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"active_provider": "local", "profiles": {"openai": "not-a-dict"}}, f)

        resp = client.get("/api/llm/provider")
        assert resp.status_code == 200
        assert resp.json()["profiles"] == {}


# ──────────────────────────────────────────────────────────────────────────
# F8 — pin: the on-disk config file bytes never contain the raw api_key
# ──────────────────────────────────────────────────────────────────────────


def test_provider_config_file_never_contains_api_key_bytes():
    import opencohost.config.llm_provider as llm_provider_mod

    app = _app()
    with TestClient(app) as client:
        resp = _configure(
            client,
            "openai",
            base_url="https://api.openai.com/v1",
            model="gpt-4o-mini",
            api_key="sk-secret-openai",
        )
        assert resp.status_code == 200

    with open(llm_provider_mod.LLM_PROVIDER_CONFIG_FILE, "r", encoding="utf-8") as f:
        on_disk = f.read()
    assert "sk-secret-openai" not in on_disk


# ──────────────────────────────────────────────────────────────────────────
# delete_profile — remove a profile (config + key store), owner-ordered
# ──────────────────────────────────────────────────────────────────────────


def test_delete_inactive_profile_removes_from_config_and_key_store():
    app = _app()
    with TestClient(app) as client:
        _configure(client, "nvidia_nim", base_url="https://a", model="m1", api_key="key-a")

        resp = client.put("/api/llm/provider", json={"delete_profile": "nvidia_nim"})
        assert resp.status_code == 200
        assert "nvidia_nim" not in resp.json()["profiles"]

        get_body = client.get("/api/llm/provider").json()
        assert "nvidia_nim" not in get_body["profiles"]

    import opencohost.api.main as main_mod
    from opencohost.stream_admin.oauth_store import OAuthStore

    key_store = OAuthStore(main_mod.LLM_KEYS_FILE)
    assert key_store.has_token("nvidia_nim") is False


def test_delete_active_profile_422():
    app = _app()
    with TestClient(app) as client:
        _configure(client, "nvidia_nim", base_url="https://a", model="m1", api_key="key-a")
        client.put("/api/llm/provider", json={"active_provider": "nvidia_nim"})

        resp = client.put("/api/llm/provider", json={"delete_profile": "nvidia_nim"})
        assert resp.status_code == 422
        assert resp.json() == {"detail": "cannot delete active profile"}

        body = client.get("/api/llm/provider").json()
        assert "nvidia_nim" in body["profiles"]


def test_switch_then_delete_in_one_put_succeeds():
    """Ergonomic path: switch away + delete the now-inactive profile in ONE PUT."""
    app = _app()
    with TestClient(app) as client:
        _configure(client, "nvidia_nim", base_url="https://a", model="m1", api_key="key-a")
        client.put("/api/llm/provider", json={"active_provider": "nvidia_nim"})

        resp = client.put(
            "/api/llm/provider",
            json={"active_provider": "local", "delete_profile": "nvidia_nim"},
        )
        assert resp.status_code == 200
        assert resp.json()["active_provider"] == "local"
        assert "nvidia_nim" not in resp.json()["profiles"]


def test_delete_profile_with_edit_field_422():
    app = _app()
    with TestClient(app) as client:
        _configure(client, "nvidia_nim", base_url="https://a", model="m1")

        resp = client.put(
            "/api/llm/provider", json={"delete_profile": "nvidia_nim", "model": "new-model"}
        )
        assert resp.status_code == 422
        assert resp.json() == {"detail": "delete_profile cannot be combined with profile edits"}


def test_delete_unknown_profile_422():
    app = _app()
    with TestClient(app) as client:
        resp = client.put("/api/llm/provider", json={"delete_profile": "does_not_exist"})
        assert resp.status_code == 422
        assert resp.json() == {"detail": "unknown profile"}


def test_delete_profile_key_store_failure_returns_503_config_unchanged(monkeypatch):
    """Key delete is attempted BEFORE the profile is removed from config -- a
    503 here must leave the profile (and its key) exactly as they were."""
    import opencohost.stream_admin.oauth_store as oauth_store_mod

    app = _app()
    with TestClient(app) as client:
        _configure(client, "nvidia_nim", base_url="https://a", model="m1", api_key="key-a")

        monkeypatch.setattr(oauth_store_mod.OAuthStore, "delete", _raise_oserror)
        resp = client.put("/api/llm/provider", json={"delete_profile": "nvidia_nim"})
        assert resp.status_code == 503
        assert resp.json() == {"detail": "key_store_write_failed"}

        body = client.get("/api/llm/provider").json()
        assert body["profiles"]["nvidia_nim"]["base_url"] == "https://a"
        assert body["profiles"]["nvidia_nim"]["api_key_set"] is True
