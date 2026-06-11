"""Tests for profile seeding behavior in core/profiles.py.

Covers:
- missing PROFILES_FILE + defaults present  -> seeded with 6 profiles, written to disk
- defaults missing                           -> SYSTEM_PROMPT fallback (single profile)
- corrupt defaults (invalid JSON)            -> fallback, no raise
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXPECTED_PROFILE_NAMES = {
    "Akira",
    "Akira (Learn)",
    "Comunidad",
    "Calmado",
    "Técnico",
    "Show",
}

VALID_DEFAULTS = {
    "Akira": {"prompt": "Eres Kira, una co-host virtual.", "use_system": True},
    "Akira (Learn)": {"prompt": "Eres Kira, la co-host senior.", "use_system": False},
    "Comunidad": {"prompt": "Eres Kira, co-host del stream.", "use_system": True},
    "Calmado": {"prompt": "Eres Kira, calmada.", "use_system": True},
    "Técnico": {"prompt": "Eres Kira, técnica.", "use_system": True},
    "Show": {"prompt": "Eres Kira, show.", "use_system": True},
}


def _call_cargar(profiles_path: str, defaults_path: str) -> dict:
    """Call cargar_perfiles() with patched paths. Always uses fresh patch context."""
    import core.profiles as prof_mod
    with (
        patch.object(prof_mod, "PROFILES_FILE", profiles_path),
        patch.object(prof_mod, "DEFAULT_PROFILES_FILE", defaults_path),
    ):
        return prof_mod.cargar_perfiles()


# ---------------------------------------------------------------------------
# Scenario 1 — PROFILES_FILE missing, defaults present → seeded with 6 profiles
# ---------------------------------------------------------------------------

def test_seed_from_defaults_when_profiles_file_missing(tmp_path):
    """When PROFILES_FILE does not exist and default_profiles.json is present,
    cargar_perfiles() must return all 6 curated profiles and write them to
    PROFILES_FILE so subsequent calls load from disk."""
    profiles_file = str(tmp_path / "perfiles.json")
    defaults_file = tmp_path / "default_profiles.json"
    defaults_file.write_text(json.dumps(VALID_DEFAULTS), encoding="utf-8")

    result = _call_cargar(profiles_file, str(defaults_file))

    assert set(result.keys()) == EXPECTED_PROFILE_NAMES
    # Must also write the file so next boot loads from disk
    assert os.path.exists(profiles_file), "PROFILES_FILE must be written after seeding"
    on_disk = json.loads(Path(profiles_file).read_text(encoding="utf-8"))
    assert set(on_disk.keys()) == EXPECTED_PROFILE_NAMES


def test_seed_does_not_overwrite_existing_profiles_file(tmp_path):
    """When PROFILES_FILE already exists, cargar_perfiles() must NOT replace it
    with defaults — existing user profiles take priority."""
    existing = {"MyProfile": {"prompt": "custom", "use_system": False}}
    profiles_file = tmp_path / "perfiles.json"
    profiles_file.write_text(json.dumps(existing), encoding="utf-8")
    defaults_file = tmp_path / "default_profiles.json"
    defaults_file.write_text(json.dumps(VALID_DEFAULTS), encoding="utf-8")

    result = _call_cargar(str(profiles_file), str(defaults_file))

    assert list(result.keys()) == ["MyProfile"]


# ---------------------------------------------------------------------------
# Scenario 2 — defaults file missing → SYSTEM_PROMPT fallback
# ---------------------------------------------------------------------------

def test_fallback_to_system_prompt_when_defaults_missing(tmp_path):
    """When PROFILES_FILE does not exist AND default_profiles.json is absent,
    cargar_perfiles() must return the bare SYSTEM_PROMPT fallback dict."""
    profiles_file = str(tmp_path / "perfiles.json")
    missing_defaults = str(tmp_path / "nonexistent_defaults.json")

    result = _call_cargar(profiles_file, missing_defaults)

    assert "Kira (Default)" in result
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Scenario 3 — corrupt defaults (invalid JSON) → fallback, no raise
# ---------------------------------------------------------------------------

def test_fallback_when_defaults_corrupt(tmp_path):
    """When default_profiles.json exists but is invalid JSON, cargar_perfiles()
    must NOT raise; it must silently fall back to SYSTEM_PROMPT."""
    profiles_file = str(tmp_path / "perfiles.json")
    defaults_file = tmp_path / "default_profiles.json"
    defaults_file.write_text("{this is: not valid json", encoding="utf-8")

    result = _call_cargar(profiles_file, str(defaults_file))

    assert "Kira (Default)" in result
    assert len(result) == 1


# ---------------------------------------------------------------------------
# Scenario 4 — defaults empty dict → fallback (defensive)
# ---------------------------------------------------------------------------

def test_fallback_when_defaults_empty_dict(tmp_path):
    """When default_profiles.json parses as an empty dict, treat as
    missing/corrupt and fall back to SYSTEM_PROMPT."""
    profiles_file = str(tmp_path / "perfiles.json")
    defaults_file = tmp_path / "default_profiles.json"
    defaults_file.write_text("{}", encoding="utf-8")

    result = _call_cargar(profiles_file, str(defaults_file))

    assert "Kira (Default)" in result


# ---------------------------------------------------------------------------
# Scenario 5 — seeded profiles must not contain personal attribution strings
# ---------------------------------------------------------------------------

def test_seeded_profiles_have_no_attribution(tmp_path):
    """None of the seeded profiles should contain personal attribution text."""
    real_defaults = Path(__file__).resolve().parent.parent / "config" / "default_profiles.json"
    if not real_defaults.exists():
        pytest.skip("config/default_profiles.json not yet created")

    profiles_file = str(tmp_path / "perfiles.json")
    result = _call_cargar(profiles_file, str(real_defaults))

    for name, profile in result.items():
        prompt = profile.get("prompt", "")
        assert "creada por " not in prompt.lower(), (
            f"Profile '{name}' still contains personal attribution"
        )


# ---------------------------------------------------------------------------
# Scenario 6 — real default_profiles.json has exactly the 6 expected profiles
# ---------------------------------------------------------------------------

def test_real_defaults_file_has_expected_profiles(tmp_path):
    """config/default_profiles.json must contain exactly the 6 curated profiles."""
    real_defaults = Path(__file__).resolve().parent.parent / "config" / "default_profiles.json"
    if not real_defaults.exists():
        pytest.skip("config/default_profiles.json not yet created")

    data = json.loads(real_defaults.read_text(encoding="utf-8"))
    assert set(data.keys()) == EXPECTED_PROFILE_NAMES
