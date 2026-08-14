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

# English counterparts shipped alongside the es set in the real
# default_profiles.json (kira_english_default_locale_20260814). Distinct dict
# keys (no collision with EXPECTED_PROFILE_NAMES above) since both locales
# live in the same flat JSON object.
EXPECTED_PROFILE_NAMES_EN = {
    "Kira",
    "Kira (Learn)",
    "Community",
    "Calm",
    "Technical",
    "Showtime",
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
    import opencohost.core.profiles.profiles as prof_mod
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
    real_defaults = Path(__file__).resolve().parent.parent / "opencohost" / "config" / "default_profiles.json"
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
# Scenario 6 — real default_profiles.json has exactly the 6+6 expected profiles
# ---------------------------------------------------------------------------

def test_real_defaults_file_has_expected_profiles(tmp_path):
    """config/default_profiles.json must contain exactly the 6 curated es
    profiles plus their 6 en counterparts (kira_english_default_locale_20260814),
    each correctly tagged, and nothing else."""
    real_defaults = Path(__file__).resolve().parent.parent / "opencohost" / "config" / "default_profiles.json"
    if not real_defaults.exists():
        pytest.skip("config/default_profiles.json not yet created")

    data = json.loads(real_defaults.read_text(encoding="utf-8"))
    assert set(data.keys()) == EXPECTED_PROFILE_NAMES | EXPECTED_PROFILE_NAMES_EN
    es_names = {name for name, p in data.items() if p.get("locale") == "es"}
    en_names = {name for name, p in data.items() if p.get("locale") == "en"}
    assert es_names == EXPECTED_PROFILE_NAMES
    assert en_names == EXPECTED_PROFILE_NAMES_EN


# ---------------------------------------------------------------------------
# Scenario 7 — first-run seeding picks the default set matching active locale
# ---------------------------------------------------------------------------


def test_seed_from_real_defaults_picks_es_set_for_es_locale(tmp_path):
    """A first run under the (default) es locale must seed the 6 Spanish
    personas from the real default_profiles.json, not the English ones."""
    real_defaults = Path(__file__).resolve().parent.parent / "opencohost" / "config" / "default_profiles.json"
    if not real_defaults.exists():
        pytest.skip("config/default_profiles.json not yet created")

    from opencohost.i18n import state as i18n_state

    profiles_file = str(tmp_path / "perfiles.json")
    with patch("opencohost.i18n.state.get_locale", return_value="es"):
        result = _call_cargar(profiles_file, str(real_defaults))

    assert set(result.keys()) == EXPECTED_PROFILE_NAMES
    assert all(p.get("locale") == "es" for p in result.values())


def test_seed_from_real_defaults_picks_en_set_for_en_locale(tmp_path):
    """A first run under the en locale must seed the 6 English personas from
    the real default_profiles.json, not the Spanish ones — this is the fix
    for Kira keeping her Spanish persona under an English locale."""
    real_defaults = Path(__file__).resolve().parent.parent / "opencohost" / "config" / "default_profiles.json"
    if not real_defaults.exists():
        pytest.skip("config/default_profiles.json not yet created")

    with patch("opencohost.i18n.state.get_locale", return_value="en"):
        profiles_file = str(tmp_path / "perfiles.json")
        result = _call_cargar(profiles_file, str(real_defaults))

    assert set(result.keys()) == EXPECTED_PROFILE_NAMES_EN
    assert all(p.get("locale") == "en" for p in result.values())
    on_disk = json.loads(Path(profiles_file).read_text(encoding="utf-8"))
    assert set(on_disk.keys()) == EXPECTED_PROFILE_NAMES_EN


def test_select_locale_profiles_missing_locale_field_defaults_to_es():
    """A profile with no "locale" key predates the bilingual default set and
    must be treated as es — kept under es, and under en it falls back to the
    full unfiltered set (the "never seed empty" guard, since nothing matches
    "en" here) rather than silently vanishing."""
    from opencohost.core.profiles.profiles import _select_locale_profiles

    defaults = {"Legacy": {"prompt": "x", "use_system": True}}
    assert _select_locale_profiles(defaults, "es") == defaults
    assert _select_locale_profiles(defaults, "en") == defaults


def test_select_locale_profiles_never_seeds_empty():
    """If filtering would leave nothing (e.g. every profile tagged for a
    locale that isn't active and none untagged), fall back to the full set
    rather than seeding an empty profiles file."""
    from opencohost.core.profiles.profiles import _select_locale_profiles

    defaults = {"OnlyEn": {"prompt": "x", "locale": "en"}}
    assert _select_locale_profiles(defaults, "es") == defaults


# ---------------------------------------------------------------------------
# Scenario 8 — Judgment Day Finding 1 (CRITICAL, 2026-08-14): seed_locale_profiles
# ---------------------------------------------------------------------------
#
# Root cause: cargar_perfiles() only seeds ONCE, at first boot, filtered by
# whatever locale was active at that single moment. The only locale switch
# (PUT /api/i18n) is next-boot only and needs the backend already running --
# i.e. already past first-run seeding. So a machine that boots es and later
# switches to en never got the English personas onto disk. The fix:
# seed_locale_profiles() is called additively whenever a locale is persisted
# (see test_i18n_state.py for the end-to-end proof through set_locale()).


def test_seed_locale_profiles_additive_and_never_overwrites(tmp_path):
    """A profile already on disk under the same name always wins; a missing
    default for the target locale gets added; another locale's profiles
    already on disk are untouched."""
    import opencohost.core.profiles.profiles as prof_mod

    profiles_file = tmp_path / "perfiles.json"
    existing = {
        "Kira": {"prompt": "MY CUSTOM ENGLISH PERSONA", "use_system": True, "id": "keep-me"},
        "Akira": {"prompt": "spanish persona kept as-is", "use_system": True},
    }
    profiles_file.write_text(json.dumps(existing), encoding="utf-8")

    defaults_file = tmp_path / "default_profiles.json"
    all_defaults = {
        "Kira": {"prompt": "shipped en default -- must NOT overwrite", "use_system": True, "locale": "en"},
        "Kira (Learn)": {"prompt": "shipped en default, missing locally", "use_system": False, "locale": "en"},
        "Akira": {"prompt": "shipped es default", "use_system": True, "locale": "es"},
    }
    defaults_file.write_text(json.dumps(all_defaults), encoding="utf-8")

    with (
        patch.object(prof_mod, "PROFILES_FILE", str(profiles_file)),
        patch.object(prof_mod, "DEFAULT_PROFILES_FILE", str(defaults_file)),
    ):
        prof_mod.seed_locale_profiles("en")

    on_disk = json.loads(profiles_file.read_text(encoding="utf-8"))
    # Existing "Kira" survives byte-for-byte -- an existing profile always wins.
    assert on_disk["Kira"]["prompt"] == "MY CUSTOM ENGLISH PERSONA"
    assert on_disk["Kira"]["id"] == "keep-me"
    # The missing en default got added.
    assert "Kira (Learn)" in on_disk
    assert on_disk["Kira (Learn)"]["prompt"] == "shipped en default, missing locally"
    # The other locale's profile already on disk is untouched.
    assert on_disk["Akira"]["prompt"] == "spanish persona kept as-is"


def test_seed_locale_profiles_noop_when_defaults_missing(tmp_path):
    """Best-effort: no defaults file to seed from must not raise and must
    not touch the existing profiles file."""
    import opencohost.core.profiles.profiles as prof_mod

    profiles_file = tmp_path / "perfiles.json"
    profiles_file.write_text(json.dumps({"Existing": {"prompt": "x", "use_system": True}}), encoding="utf-8")

    with (
        patch.object(prof_mod, "PROFILES_FILE", str(profiles_file)),
        patch.object(prof_mod, "DEFAULT_PROFILES_FILE", str(tmp_path / "missing.json")),
    ):
        prof_mod.seed_locale_profiles("en")  # must not raise

    on_disk = json.loads(profiles_file.read_text(encoding="utf-8"))
    assert on_disk == {"Existing": {"prompt": "x", "use_system": True}}


# ---------------------------------------------------------------------------
# Scenario 9 — Judgment Day Finding 6 (2026-08-14): corrupt PROFILES_FILE
# must be quarantined, never silently overwritten
# ---------------------------------------------------------------------------


def test_corrupt_profiles_file_quarantined_not_overwritten(tmp_path):
    """A PROFILES_FILE that exists but fails to parse (crash-truncated JSON,
    or a transient Windows PermissionError) must be quarantined to
    *.corrupt, never destroyed by the reseed branch -- mirrors the
    established pattern in core/profiles/cohost_profiles.py."""
    profiles_file = tmp_path / "perfiles.json"
    original_bytes = b'{"MyProfile": {"prompt": "very important custom persona"'  # truncated, invalid JSON
    profiles_file.write_bytes(original_bytes)
    defaults_file = tmp_path / "default_profiles.json"
    defaults_file.write_text(json.dumps(VALID_DEFAULTS), encoding="utf-8")

    result = _call_cargar(str(profiles_file), str(defaults_file))

    # The corrupt original survives, byte-for-byte, under *.corrupt.
    corrupt_path = tmp_path / "perfiles.json.corrupt"
    assert corrupt_path.exists()
    assert corrupt_path.read_bytes() == original_bytes
    # A fresh, valid seeded set replaces it at the ORIGINAL path -- the
    # normal first-run path, now that PROFILES_FILE genuinely doesn't exist.
    assert set(result.keys()) == EXPECTED_PROFILE_NAMES
    on_disk = json.loads(profiles_file.read_text(encoding="utf-8"))
    assert set(on_disk.keys()) == EXPECTED_PROFILE_NAMES
