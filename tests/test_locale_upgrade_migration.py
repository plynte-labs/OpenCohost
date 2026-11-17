"""Tests for 0.3.0 -> 0.5.0 locale preservation, voice inference, and T4 *_en.json migration."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import opencohost.api.main as main_mod
from opencohost.config import settings
from opencohost.core.profiles import cohost_profiles as cohost_profiles_mod
from opencohost.core.profiles import personalization as personalization_mod
from opencohost.core.profiles import profiles as profiles_mod
from opencohost.core.speech import pronunciation as pronunciation_mod
from opencohost.i18n import active as i18n_active
from opencohost.i18n import state as i18n_state
from opencohost.i18n.startup import resolve_active_bundle
from tests.test_api_phase1 import FakeHost

_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_runtime_state():
    main_mod._host_active = False
    i18n_active.reset_active_bundle()
    personalization_mod._cache_state.clear()
    pronunciation_mod.reset_pronunciation()
    yield
    main_mod._host_active = False
    i18n_active.reset_active_bundle()
    personalization_mod._cache_state.clear()
    pronunciation_mod.reset_pronunciation()


@pytest.fixture
def upgrade_env(tmp_path, monkeypatch):
    """Isolate every config/profile/migration store inside a fresh tmp_path."""
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)

    locale_file = config_dir / "locale.json"
    piper_voice_file = config_dir / "piper_voice.json"
    personalization_file = config_dir / "personalization.json"
    pronunciation_file = config_dir / "pronunciation.json"
    cohost_profiles_file = tmp_path / "cohost_profiles.json"
    profiles_file = tmp_path / "perfiles.json"
    marker_file = config_dir / ".en_locale_migrated.json"

    monkeypatch.setattr(i18n_state, "_default_locale_file", lambda: locale_file)
    monkeypatch.setattr(settings, "PIPER_VOICE_FILE", str(piper_voice_file))
    monkeypatch.setattr(personalization_mod, "PERSONALIZATION_FILE", str(personalization_file))
    monkeypatch.setattr(pronunciation_mod, "PRONUNCIATION_FILE", str(pronunciation_file))
    monkeypatch.setattr(cohost_profiles_mod, "COHOST_PROFILES_FILE", str(cohost_profiles_file))
    monkeypatch.setattr(profiles_mod, "PROFILES_FILE", str(profiles_file))

    import opencohost.config.locale_upgrade as locale_upgrade_mod

    monkeypatch.setattr(locale_upgrade_mod, "EN_LOCALE_MIGRATED_FILE", str(marker_file))

    return SimpleNamespace(
        root=tmp_path,
        config_dir=config_dir,
        locale_file=locale_file,
        piper_voice_file=piper_voice_file,
        personalization_file=personalization_file,
        personalization_en_file=config_dir / "personalization_en.json",
        pronunciation_file=pronunciation_file,
        pronunciation_en_file=config_dir / "pronunciation_en.json",
        cohost_profiles_file=cohost_profiles_file,
        cohost_profiles_en_file=tmp_path / "cohost_profiles_en.json",
        profiles_file=profiles_file,
        marker_file=marker_file,
        locale_upgrade_mod=locale_upgrade_mod,
    )


def _seed_unsuffixed_stores(env) -> dict[str, bytes]:
    personalization_raw = json.dumps(
        {
            "version": 1,
            "enabled": True,
            "nickname": "StreamerZero",
            "occupation": "Streamer",
            "interests": "Speedruns",
            "custom_instructions": "Be witty and concise",
            "updated_at": "2026-01-10T12:00:00+00:00",
        },
        indent=2,
    )
    pronunciation_raw = json.dumps(
        {
            "version": 1,
            "entries": [{"match": "OBS", "replace": "O B S"}],
        },
        indent=2,
    )
    cohost_raw = json.dumps(
        {
            "CustomLegacy": {
                "style": "Legacy cohost style",
                "default_priority": "alta",
                "default_response_length": "corta",
            }
        },
        indent=2,
    )
    env.personalization_file.write_text(personalization_raw, encoding="utf-8")
    env.pronunciation_file.write_text(pronunciation_raw, encoding="utf-8")
    env.cohost_profiles_file.write_text(cohost_raw, encoding="utf-8")
    return {
        "personalization": env.personalization_file.read_bytes(),
        "pronunciation": env.pronunciation_file.read_bytes(),
        "cohost": env.cohost_profiles_file.read_bytes(),
    }


def test_existing_en_locale_stays_en_over_neutral_voice_and_migrates_en_files(upgrade_env):
    locale_raw = json.dumps({"locale": "en", "set_at": "2026-01-01T00:00:00"})
    voice_raw = json.dumps({"piper_voice": "neutral"})
    upgrade_env.locale_file.write_text(locale_raw, encoding="utf-8")
    upgrade_env.piper_voice_file.write_text(voice_raw, encoding="utf-8")
    before = _seed_unsuffixed_stores(upgrade_env)

    upgrade_env.locale_upgrade_mod.ensure_upgrade_locale_and_en_files()

    assert i18n_state.get_locale() == "en"
    assert upgrade_env.locale_file.read_text(encoding="utf-8") == locale_raw
    assert upgrade_env.piper_voice_file.read_text(encoding="utf-8") == voice_raw

    assert upgrade_env.personalization_en_file.read_bytes() == before["personalization"]
    assert upgrade_env.pronunciation_en_file.read_bytes() == before["pronunciation"]
    assert upgrade_env.cohost_profiles_en_file.read_bytes() == before["cohost"]

    assert upgrade_env.personalization_file.read_bytes() == before["personalization"]
    assert upgrade_env.pronunciation_file.read_bytes() == before["pronunciation"]
    assert upgrade_env.cohost_profiles_file.read_bytes() == before["cohost"]
    assert upgrade_env.marker_file.exists()


def test_existing_es_locale_stays_es_even_when_piper_voice_is_english(upgrade_env):
    locale_raw = json.dumps({"locale": "es", "set_at": "2026-01-01T00:00:00"})
    voice_raw = json.dumps({"piper_voice": "english"})
    upgrade_env.locale_file.write_text(locale_raw, encoding="utf-8")
    upgrade_env.piper_voice_file.write_text(voice_raw, encoding="utf-8")
    _seed_unsuffixed_stores(upgrade_env)

    upgrade_env.locale_upgrade_mod.ensure_upgrade_locale_and_en_files()

    assert i18n_state.get_locale() == "es"
    assert upgrade_env.locale_file.read_text(encoding="utf-8") == locale_raw
    assert upgrade_env.piper_voice_file.read_text(encoding="utf-8") == voice_raw
    assert not upgrade_env.personalization_en_file.exists()
    assert not upgrade_env.pronunciation_en_file.exists()
    assert not upgrade_env.cohost_profiles_en_file.exists()
    assert not upgrade_env.marker_file.exists()


@pytest.mark.parametrize("locale_content", [None, "", "   ", json.dumps({"locale": ""})])
def test_missing_or_empty_locale_infers_en_from_english_voice(upgrade_env, locale_content):
    if locale_content is not None:
        upgrade_env.locale_file.write_text(locale_content, encoding="utf-8")
    voice_raw = json.dumps({"piper_voice": "english", "kept": True})
    upgrade_env.piper_voice_file.write_text(voice_raw, encoding="utf-8")
    upgrade_env.profiles_file.write_text(
        json.dumps({"Akira": {"prompt": "Eres Kira.", "use_system": True, "locale": "es"}}),
        encoding="utf-8",
    )
    before = _seed_unsuffixed_stores(upgrade_env)

    # Simulate an already-cached es bundle before upgrade inference runs.
    i18n_active.set_active_bundle(resolve_active_bundle(locale="es"))

    upgrade_env.locale_upgrade_mod.ensure_upgrade_locale_and_en_files()

    assert i18n_state.get_locale() == "en"
    persisted = json.loads(upgrade_env.locale_file.read_text(encoding="utf-8"))
    assert persisted["locale"] == "en"
    assert upgrade_env.piper_voice_file.read_text(encoding="utf-8") == voice_raw

    # Active bundle cache must be reset so this boot immediately resolves "en".
    assert i18n_active.get_active_bundle().code == "en"

    # English default profiles must be additively seeded without touching existing ones.
    seeded_profiles = json.loads(upgrade_env.profiles_file.read_text(encoding="utf-8"))
    assert "Akira" in seeded_profiles
    assert "Kira" in seeded_profiles

    assert upgrade_env.personalization_en_file.read_bytes() == before["personalization"]
    assert upgrade_env.pronunciation_en_file.read_bytes() == before["pronunciation"]
    assert upgrade_env.cohost_profiles_en_file.read_bytes() == before["cohost"]
    assert upgrade_env.marker_file.exists()


def test_existing_en_subdivided_files_are_never_overwritten(upgrade_env):
    upgrade_env.locale_file.write_text(json.dumps({"locale": "en"}), encoding="utf-8")
    before = _seed_unsuffixed_stores(upgrade_env)

    existing_pers_en = json.dumps({"version": 1, "enabled": True, "nickname": "AlreadySubdivided"})
    existing_cohost_en = json.dumps({"AlreadyEN": {"style": "Keep me", "default_priority": "normal", "default_response_length": "normal"}})
    upgrade_env.personalization_en_file.write_text(existing_pers_en, encoding="utf-8")
    upgrade_env.cohost_profiles_en_file.write_text(existing_cohost_en, encoding="utf-8")

    upgrade_env.locale_upgrade_mod.ensure_upgrade_locale_and_en_files()

    # Pre-existing *_en.json files are never overwritten.
    assert upgrade_env.personalization_en_file.read_text(encoding="utf-8") == existing_pers_en
    assert upgrade_env.cohost_profiles_en_file.read_text(encoding="utf-8") == existing_cohost_en
    # Missing *_en.json counterpart is still copied.
    assert upgrade_env.pronunciation_en_file.read_bytes() == before["pronunciation"]
    assert upgrade_env.marker_file.exists()


@pytest.mark.parametrize(
    "voice_payload",
    [
        None,
        json.dumps({"piper_voice": "neutral"}),
        json.dumps({"piper_voice": "argentina"}),
        "{corrupt json",
    ],
)
def test_missing_locale_with_neutral_or_missing_voice_stays_es_without_en_files(upgrade_env, voice_payload):
    if voice_payload is not None:
        upgrade_env.piper_voice_file.write_text(voice_payload, encoding="utf-8")
    _seed_unsuffixed_stores(upgrade_env)

    upgrade_env.locale_upgrade_mod.ensure_upgrade_locale_and_en_files()

    assert i18n_state.get_locale() == "es"
    assert i18n_active.get_active_bundle().code == "es"
    assert not upgrade_env.personalization_en_file.exists()
    assert not upgrade_env.pronunciation_en_file.exists()
    assert not upgrade_env.cohost_profiles_en_file.exists()
    assert not upgrade_env.marker_file.exists()
    if voice_payload is not None:
        assert upgrade_env.piper_voice_file.read_text(encoding="utf-8") == voice_payload


def test_one_time_marker_prevents_recopy_after_en_file_deleted(upgrade_env):
    upgrade_env.locale_file.write_text(json.dumps({"locale": "en"}), encoding="utf-8")
    before = _seed_unsuffixed_stores(upgrade_env)

    upgrade_env.locale_upgrade_mod.ensure_upgrade_locale_and_en_files()
    assert upgrade_env.personalization_en_file.exists()
    assert upgrade_env.pronunciation_en_file.exists()
    assert upgrade_env.cohost_profiles_en_file.exists()
    assert upgrade_env.marker_file.exists()

    # User intentionally deletes the *_en.json files later.
    upgrade_env.personalization_en_file.unlink()
    upgrade_env.pronunciation_en_file.unlink()
    upgrade_env.cohost_profiles_en_file.unlink()

    # Subsequent startup must NOT re-copy from the unsuffixed files.
    upgrade_env.locale_upgrade_mod.ensure_upgrade_locale_and_en_files()
    assert not upgrade_env.personalization_en_file.exists()
    assert not upgrade_env.pronunciation_en_file.exists()
    assert not upgrade_env.cohost_profiles_en_file.exists()
    assert upgrade_env.personalization_file.read_bytes() == before["personalization"]
    assert upgrade_env.pronunciation_file.read_bytes() == before["pronunciation"]
    assert upgrade_env.cohost_profiles_file.read_bytes() == before["cohost"]


def test_lifespan_e2e_infers_en_from_piper_voice_and_serves_migrated_en_stores(upgrade_env):
    voice_raw = json.dumps({"piper_voice": "english"})
    upgrade_env.piper_voice_file.write_text(voice_raw, encoding="utf-8")
    before = _seed_unsuffixed_stores(upgrade_env)

    # Pre-seed stale active bundle to verify lifespan inference resets it for first boot.
    i18n_active.set_active_bundle(resolve_active_bundle(locale="es"))

    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_ORIGINS)
    with TestClient(app) as client:
        i18n_resp = client.get("/api/i18n")
        assert i18n_resp.status_code == 200
        i18n_body = i18n_resp.json()
        assert i18n_body["active_locale"] == "en"
        assert i18n_body["persisted_locale"] == "en"
        assert i18n_body["pending_restart"] is False

        pers_resp = client.get("/api/personalization")
        assert pers_resp.status_code == 200
        assert pers_resp.json()["nickname"] == "StreamerZero"
        assert pers_resp.json()["custom_instructions"] == "Be witty and concise"

        pron_resp = client.get("/api/tts/pronunciation")
        assert pron_resp.status_code == 200
        assert pron_resp.json()["entries"] == [{"match": "OBS", "replace": "O B S"}]

        cohost_resp = client.get("/api/agenda/cohost-profiles")
        assert cohost_resp.status_code == 200
        names = {item["name"]: item for item in cohost_resp.json()["profiles"]}
        assert "CustomLegacy" in names
        assert names["CustomLegacy"]["style"] == "Legacy cohost style"

    assert upgrade_env.piper_voice_file.read_text(encoding="utf-8") == voice_raw
    assert upgrade_env.personalization_file.read_bytes() == before["personalization"]
    assert upgrade_env.pronunciation_file.read_bytes() == before["pronunciation"]
    assert upgrade_env.cohost_profiles_file.read_bytes() == before["cohost"]
