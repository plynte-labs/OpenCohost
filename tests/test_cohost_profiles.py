"""Tests for guarded Co-host profile persistence helpers."""

import pytest

from opencohost.core.profiles.cohost_profiles import (
    DEFAULT_COHOST_PROFILES,
    DEFAULT_COHOST_PROFILES_EN,
    _cohost_profiles_path,
    _default_cohost_profiles,
    load_cohost_profiles,
    normalize_cohost_profile,
    sanitize_profile_name,
    save_cohost_profiles,
)
from opencohost.i18n import active
from opencohost.i18n.contract import TIER_OFFICIAL, LocaleBundle
from opencohost.i18n.registry import build_chain, discover_bundles, official_locales_dir
from opencohost.i18n.startup import resolve_active_bundle


@pytest.fixture
def official():
    return discover_bundles(official_locales_dir(), "official")


@pytest.fixture(autouse=True)
def _reset_active():
    active.reset_active_bundle()
    yield
    active.reset_active_bundle()


def _activate(code, official):
    active.set_active_bundle(resolve_active_bundle(locale=code, registry=official))


# ---------------------------------------------------------------------------
# kira_english_default_locale_20260814 — locale-aware default preset selection
# ---------------------------------------------------------------------------


def test_default_cohost_profiles_es_is_unchanged(official):
    _activate("es", official)
    assert _default_cohost_profiles() is DEFAULT_COHOST_PROFILES


def test_default_cohost_profiles_en_returns_english_set(official):
    _activate("en", official)
    assert _default_cohost_profiles() is DEFAULT_COHOST_PROFILES_EN
    assert set(_default_cohost_profiles().keys()) == {"Natural", "Spicy with nuance", "Entertaining teacher"}


def test_default_cohost_profiles_falls_back_to_es_on_bundle_failure():
    # A bundle with no usable `.code` resolution still degrades to es, never
    # raises and never silently returns the en set.
    active.set_active_bundle(LocaleBundle(code="es", tier=TIER_OFFICIAL, data={"meta": {"code": "es"}}))
    assert _default_cohost_profiles() is DEFAULT_COHOST_PROFILES


def test_normalize_cohost_profile_empty_style_falls_back_to_active_locale_default(official):
    _activate("en", official)
    profile = normalize_cohost_profile({"style": ""})
    assert profile["style"] == DEFAULT_COHOST_PROFILES_EN["Natural"]["style"]


def test_normalize_cohost_profile_caps_style_and_preserves_defaults():
    profile = normalize_cohost_profile({
        "style": "x" * 700,
        "default_priority": "alta",
        "default_response_length": "expandida",
    })

    assert len(profile["style"]) == 600
    assert profile["default_priority"] == "alta"
    assert profile["default_response_length"] == "expandida"


def test_sanitize_profile_name_is_compact_and_capped():
    name = sanitize_profile_name("  Perfil   demasiado   largo " + "x" * 80)

    assert "  " not in name
    assert len(name) <= 40


def test_cohost_profiles_path_none_follows_active_locale(official, tmp_path, monkeypatch):
    import opencohost.core.profiles.cohost_profiles as cp_mod

    base = tmp_path / "cohost_profiles.json"
    monkeypatch.setattr(cp_mod, "COHOST_PROFILES_FILE", str(base))

    _activate("es", official)
    assert _cohost_profiles_path() == base

    _activate("en", official)
    assert _cohost_profiles_path() == tmp_path / "cohost_profiles_en.json"
    assert _cohost_profiles_path("es-MX") == base
    assert _cohost_profiles_path("en-US") == tmp_path / "cohost_profiles_en.json"


def test_load_and_save_cohost_profiles_none_follow_active_locale(official, tmp_path, monkeypatch):
    import opencohost.core.profiles.cohost_profiles as cp_mod

    base = tmp_path / "cohost_profiles.json"
    en_path = tmp_path / "cohost_profiles_en.json"
    monkeypatch.setattr(cp_mod, "COHOST_PROFILES_FILE", str(base))

    _activate("en", official)
    defaults = load_cohost_profiles()
    assert set(defaults.keys()) == {"Natural", "Spicy with nuance", "Entertaining teacher"}

    defaults["Dry EN"] = {"style": "Dry wit", "default_priority": "alta", "default_response_length": "corta"}
    assert save_cohost_profiles(defaults) is True
    assert en_path.exists()
    assert not base.exists()

    reloaded = load_cohost_profiles()
    assert reloaded["Dry EN"]["style"] == "Dry wit"
    # Explicit es stays isolated even when active runtime locale is en
    assert "Dry EN" not in load_cohost_profiles("es")


def test_normalize_cohost_profile_whitespace_and_bcp47_locale(official):
    _activate("en", official)
    assert normalize_cohost_profile({"style": ""}, locale="   ")["style"] == DEFAULT_COHOST_PROFILES_EN["Natural"]["style"]

    _activate("es", official)
    assert normalize_cohost_profile({"style": ""}, locale="en-US")["style"] == DEFAULT_COHOST_PROFILES_EN["Natural"]["style"]
