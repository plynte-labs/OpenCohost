"""
Packaging Task 4: EXPERIMENTAL_HEAVY_TTS_ENABLED flag.

When the app is frozen (packaged), the flag defaults to False so the
experimental Qwen3-TTS heavy-TTS UI option is hidden. The env var
OPENCOHOST_EXPERIMENTAL_TTS=1 overrides it to True in all environments.
"""
from __future__ import annotations

import os
import sys

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


# ---------------------------------------------------------------------------
# Task 4a — frozen=True → flag defaults False (env var absent)
# ---------------------------------------------------------------------------

def test_frozen_without_env_var_defaults_false(monkeypatch):
    """Simulated packaged build without override env var → flag is False."""
    import importlib
    import opencohost.config.settings as settings_mod

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.delenv("OPENCOHOST_EXPERIMENTAL_TTS", raising=False)

    # Re-evaluate the flag expression as settings.py would compute it
    result = settings_mod._resolve_experimental_heavy_tts()
    assert result is False, (
        "In a frozen build without env override, EXPERIMENTAL_HEAVY_TTS_ENABLED must be False."
    )


# ---------------------------------------------------------------------------
# Task 4b — frozen=True + env var=1 → flag is True
# ---------------------------------------------------------------------------

def test_frozen_with_env_var_override_is_true(monkeypatch):
    """Packaged build with OPENCOHOST_EXPERIMENTAL_TTS=1 → flag is True."""
    import opencohost.config.settings as settings_mod

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setenv("OPENCOHOST_EXPERIMENTAL_TTS", "1")

    result = settings_mod._resolve_experimental_heavy_tts()
    assert result is True, (
        "OPENCOHOST_EXPERIMENTAL_TTS=1 must enable the heavy-TTS flag even in frozen builds."
    )


# ---------------------------------------------------------------------------
# Task 4c — dev mode (not frozen) → flag defaults False (env-opt-in only)
# ---------------------------------------------------------------------------

def test_dev_mode_defaults_false(monkeypatch):
    """Non-frozen (dev) environment without env var → flag is False.

    qwen_tts_extirpation_20260627 WU 1.1: heavy TTS is opt-in via
    OPENCOHOST_EXPERIMENTAL_TTS=1 in every environment, so dev now matches the
    packaged default instead of defaulting the experimental UI on.
    """
    import opencohost.config.settings as settings_mod

    # Ensure sys.frozen is absent
    if hasattr(sys, "frozen"):
        monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delenv("OPENCOHOST_EXPERIMENTAL_TTS", raising=False)

    result = settings_mod._resolve_experimental_heavy_tts()
    assert result is False, (
        "In dev mode without the opt-in env var, EXPERIMENTAL_HEAVY_TTS_ENABLED must be False."
    )


def test_dev_mode_env_opt_in_is_true(monkeypatch):
    """Non-frozen (dev) + OPENCOHOST_EXPERIMENTAL_TTS=1 → flag is True (the only dev opt-in)."""
    import opencohost.config.settings as settings_mod

    if hasattr(sys, "frozen"):
        monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("OPENCOHOST_EXPERIMENTAL_TTS", "1")

    result = settings_mod._resolve_experimental_heavy_tts()
    assert result is True, (
        "OPENCOHOST_EXPERIMENTAL_TTS=1 must enable the flag in dev too."
    )


# ---------------------------------------------------------------------------
# Task 4d — EXPERIMENTAL_HEAVY_TTS_ENABLED constant is accessible
# ---------------------------------------------------------------------------

def test_flag_is_exported_from_settings():
    """EXPERIMENTAL_HEAVY_TTS_ENABLED must be importable from opencohost.config.settings."""
    from opencohost.config.settings import EXPERIMENTAL_HEAVY_TTS_ENABLED

    assert isinstance(EXPERIMENTAL_HEAVY_TTS_ENABLED, bool)
