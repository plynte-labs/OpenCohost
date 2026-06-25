"""Tests for the English locale bundle (T2) — the first real 'swap'.

The whole point of the i18n-core architecture: adding a language is adding a
*data bundle*, not engine code. These tests prove the on-disk ``en`` bundle
loads through the registry and flows all the way to the accessor the engines
read (``active.edge_voice``), turning Kira's light TTS English — with zero
engine changes since T1.

The bundle declares ``fallback: []`` (no cross-locale chain), honoring the
degrade rule: a failed locale drops the WHOLE active locale to es at startup,
never limps slot-by-slot into an English-text + Spanish-voice Frankenstein.
"""
from __future__ import annotations

import pytest

from opencohost.i18n import active
from opencohost.i18n.contract import resolve
from opencohost.i18n.registry import (
    build_chain,
    discover_bundles,
    official_locales_dir,
    validate_bundle,
)
from opencohost.i18n.startup import resolve_active_bundle

EN_EXPECTED_VOICE = "en-US-AriaNeural"
LEGACY_ES_VOICE = "es-MX-DaliaNeural"


@pytest.fixture
def official():
    """The real on-disk official registry (es + en)."""
    return discover_bundles(official_locales_dir(), "official")


@pytest.fixture(autouse=True)
def _reset_active():
    active.reset_active_bundle()
    yield
    active.reset_active_bundle()


def test_en_bundle_is_discoverable(official):
    # Adding a language = dropping a bundle dir. No engine code needed.
    assert "en" in official


def test_en_bundle_resolves_english_edge_voice(official):
    bundle = build_chain("en", official)
    voice = resolve(bundle, "tts.edge_voice")
    assert voice == EN_EXPECTED_VOICE
    assert voice != LEGACY_ES_VOICE


def test_en_bundle_qwen_language_is_english(official):
    bundle = build_chain("en", official)
    assert resolve(bundle, "tts.qwen_language") == "English"


def test_en_bundle_has_no_cross_locale_fallback(official):
    # Degrade rule: never limp slot-by-slot across locales.
    bundle = build_chain("en", official)
    assert bundle.fallback is None


def test_en_bundle_validates_structurally(official):
    # Non-strict: structural only. Strict (guardrails required) lands in T5.
    bundle = build_chain("en", official)
    assert validate_bundle(bundle).ok


def test_resolve_active_en_swaps_voice(official):
    # The full resilient resolver picks en and yields the English voice.
    bundle = resolve_active_bundle(locale="en", registry=official)
    assert bundle.code == "en"
    assert resolve(bundle, "tts.edge_voice") == EN_EXPECTED_VOICE


def test_set_active_en_makes_accessor_speak_english(official):
    # End-to-end: the accessor the engines call now returns the English voice.
    active.set_active_bundle(resolve_active_bundle(locale="en", registry=official))
    assert active.edge_voice() == EN_EXPECTED_VOICE
