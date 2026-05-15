"""Tests for guarded Co-host profile persistence helpers."""

from core.cohost_profiles import normalize_cohost_profile, sanitize_profile_name


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
