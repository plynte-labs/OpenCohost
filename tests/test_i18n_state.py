"""Tests for i18n-core locale state + CLI (next-boot model).

T0c of the english_compatibility_i18n track. The CLI writes the desired locale;
it takes effect on the NEXT startup. No runtime switch (owner decision).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from opencohost.i18n import state
from opencohost.i18n.cli import main as cli_main

# Capture before conftest._isolate_i18n_locale_file patches state._default_locale_file
# so the contract test exercises the real default path builder.
_REAL_DEFAULT_LOCALE_FILE = state._default_locale_file


@pytest.fixture(autouse=True)
def _isolate_profiles_file(tmp_path, monkeypatch):
    """set_locale() now additively seeds locale profiles as a side effect
    (kira_english_default_locale reachability fix, Judgment Day Finding 1,
    judge-confirmed 2026-08-14) -- redirect PROFILES_FILE so these tests
    never touch the real on-disk perfiles.json (USER_DATA_DIR resolves to
    the repo root in dev mode, so an unpatched PROFILES_FILE IS the
    developer's real file)."""
    import opencohost.config.settings as settings_mod
    import opencohost.core.profiles.profiles as prof_mod

    monkeypatch.setattr(prof_mod, "PROFILES_FILE", str(tmp_path / "perfiles.json"))
    monkeypatch.setattr(settings_mod, "LAST_PROFILE_FILE", str(tmp_path / "last_profile.json"))
    monkeypatch.setattr(settings_mod, "PIPER_VOICE_FILE", str(tmp_path / "piper_voice.json"))


# --- state -----------------------------------------------------------------

def test_get_locale_defaults_to_es_when_no_file(tmp_path):
    assert state.get_locale(tmp_path / "locale.json") == "es"


def test_set_then_get_round_trip(tmp_path):
    f = tmp_path / "config" / "locale.json"
    state.set_locale("en", f)
    assert state.get_locale(f) == "en"


def test_set_locale_records_timestamp(tmp_path):
    import json

    f = tmp_path / "locale.json"
    state.set_locale("en", f)
    data = json.loads(f.read_text(encoding="utf-8"))
    assert data["locale"] == "en"
    assert data.get("set_at")


def test_corrupt_file_falls_back_to_default(tmp_path):
    f = tmp_path / "locale.json"
    f.write_text("{not valid json", encoding="utf-8")
    assert state.get_locale(f) == "es"


def test_atomic_write_leaves_no_tmp(tmp_path):
    f = tmp_path / "locale.json"
    state.set_locale("en", f)
    assert not (tmp_path / "locale.json.tmp").exists()


def test_set_locale_seeds_missing_locale_profiles_additively(tmp_path, monkeypatch):
    """Judgment Day Finding 1 (CRITICAL, 2026-08-14): the ONLY locale switch
    is set_locale() (via PUT /api/i18n), and it needs the backend already
    running -- i.e. already past first-run profile seeding. Without this
    fix, the six English personas ship but are permanently unreachable.
    End-to-end proof through the real integration point, with a controlled
    defaults file so this test doesn't depend on default_profiles.json's
    exact contents."""
    import opencohost.core.profiles.profiles as prof_mod

    # Simulate the real flow: first boot already seeded the Spanish set.
    prof_mod.guardar_perfiles({"Akira": {"prompt": "es default", "use_system": True}})

    defaults_file = tmp_path / "default_profiles.json"
    defaults_file.write_text(json.dumps({
        "Akira": {"prompt": "es default", "use_system": True, "locale": "es"},
        "Kira": {"prompt": "en default", "use_system": True, "locale": "en"},
    }), encoding="utf-8")
    monkeypatch.setattr(prof_mod, "DEFAULT_PROFILES_FILE", str(defaults_file))

    locale_file = tmp_path / "locale.json"
    state.set_locale("en", locale_file)

    on_disk = json.loads(Path(prof_mod.PROFILES_FILE).read_text(encoding="utf-8"))
    assert "Kira" in on_disk  # the English default is now reachable
    assert on_disk["Kira"]["prompt"] == "en default"
    assert on_disk["Akira"]["prompt"] == "es default"  # untouched


def test_set_locale_never_raises_when_seeding_fails(tmp_path, monkeypatch):
    """Seeding is a convenience, not a gate: if it blows up for any reason,
    the locale write itself must still succeed."""
    import opencohost.core.profiles.profiles as prof_mod

    def _boom(locale_code):
        raise RuntimeError("simulated seeding failure")

    monkeypatch.setattr(prof_mod, "seed_locale_profiles", _boom)

    locale_file = tmp_path / "locale.json"
    state.set_locale("en", locale_file)  # must not raise

    assert state.get_locale(locale_file) == "en"


@pytest.mark.parametrize("code", ["es", "en"])
def test_set_locale_liveaudio_contract_path_and_schema(tmp_path, monkeypatch, code):
    """Cross-process contract: set_locale writes <data root>/config/locale.json
    with a top-level string "locale" key consumed by `liveaudio-cli serve`."""
    import opencohost.config.storage as storage_mod

    assert _REAL_DEFAULT_LOCALE_FILE.__module__ == state.__name__

    monkeypatch.setenv("OPENCOHOST_DATA_ROOT", str(tmp_path))
    assert storage_mod.get_user_data_dir() == tmp_path.resolve()

    monkeypatch.setattr(storage_mod, "USER_DATA_DIR", tmp_path)
    monkeypatch.setattr(state, "_default_locale_file", _REAL_DEFAULT_LOCALE_FILE)

    state.set_locale(code)

    expected_path = tmp_path / "config" / "locale.json"
    assert expected_path.is_file()

    payload = json.loads(expected_path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    assert "locale" in payload
    assert isinstance(payload["locale"], str)
    assert payload["locale"] == code
    assert "set_at" in payload
    assert isinstance(payload["set_at"], str)
    datetime.fromisoformat(payload["set_at"])
    assert state.get_locale() == code


# --- CLI -------------------------------------------------------------------

def test_cli_set_locale_writes_and_returns_zero(tmp_path, capsys):
    f = tmp_path / "locale.json"
    rc = cli_main(["--set-locale", "en"], locale_file=f, codes=["es", "en"])
    assert rc == 0
    assert state.get_locale(f) == "en"
    assert "next start" in capsys.readouterr().out


def test_cli_unknown_locale_rejected_and_not_written(tmp_path, capsys):
    f = tmp_path / "locale.json"
    rc = cli_main(["--set-locale", "xx"], locale_file=f, codes=["es", "en"])
    assert rc == 2
    assert not f.exists()  # nothing persisted on rejection
    assert "unknown locale" in capsys.readouterr().out


def test_cli_show_locale(tmp_path, capsys):
    f = tmp_path / "locale.json"
    state.set_locale("en", f)
    rc = cli_main(["--show-locale"], locale_file=f, codes=["es", "en"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "en"


def test_cli_list_codes(tmp_path, capsys):
    rc = cli_main(["--list"], locale_file=tmp_path / "locale.json", codes=["es", "en"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "es" in out and "en" in out
