"""Tests for the process-wide active-bundle accessor (T1).

Behavior-preserving guarantee: edge_voice() returns the EXACT legacy Spanish
voice for the es locale, so swapping the hard-coded literal in the engines for
this accessor is a no-op today. The swap mechanism and the hard fallback (i18n
failure must never change pre-track behavior) are also pinned.
"""
from __future__ import annotations

import pytest

from opencohost.i18n import active
from opencohost.i18n.contract import TIER_OFFICIAL, LocaleBundle
from opencohost.i18n.startup import resolve_active_bundle

LEGACY_ES_VOICE = "es-MX-DaliaNeural"


@pytest.fixture(autouse=True)
def _reset_active():
    active.reset_active_bundle()
    yield
    active.reset_active_bundle()


def _bundle(code, voice):
    return LocaleBundle(
        code=code,
        tier=TIER_OFFICIAL,
        data={"meta": {"code": code}, "tts": {"edge_voice": voice}},
    )


def test_edge_voice_for_es_matches_legacy():
    # es locale must equal the pre-i18n hard-coded value exactly.
    # Inject es explicitly — never depend on the machine's persisted locale.
    active.set_active_bundle(resolve_active_bundle(locale="es"))
    assert active.edge_voice() == LEGACY_ES_VOICE


def test_edge_voice_reflects_injected_locale():
    active.set_active_bundle(_bundle("en", "en-US-AriaNeural"))
    assert active.edge_voice() == "en-US-AriaNeural"


def test_edge_voice_falls_back_to_legacy_when_slot_missing():
    active.set_active_bundle(
        LocaleBundle(code="xx", tier=TIER_OFFICIAL, data={"meta": {"code": "xx"}})
    )
    assert active.edge_voice() == LEGACY_ES_VOICE


def test_get_active_bundle_is_cached():
    assert active.get_active_bundle() is active.get_active_bundle()


def test_set_then_reset_re_resolves():
    # Mechanism test: set caches the bundle; reset clears it so the next access
    # re-resolves. Hermetic — does not assert the persisted locale value.
    sentinel = _bundle("zz", "zz-Voice")
    active.set_active_bundle(sentinel)
    assert active.get_active_bundle() is sentinel
    active.reset_active_bundle()
    assert active.get_active_bundle() is not sentinel


# ---------------------------------------------------------------------------
# qwen_language() — P5 TTS locale coupling
# ---------------------------------------------------------------------------

def test_qwen_language_for_es_matches_legacy():
    active.set_active_bundle(resolve_active_bundle(locale="es"))
    assert active.qwen_language() == "Spanish"


def test_qwen_language_reflects_injected_locale():
    bundle = LocaleBundle(
        code="en", tier=TIER_OFFICIAL,
        data={"meta": {"code": "en"}, "tts": {"qwen_language": "English"}},
    )
    active.set_active_bundle(bundle)
    assert active.qwen_language() == "English"


def test_qwen_language_falls_back_to_legacy_when_slot_missing():
    active.set_active_bundle(
        LocaleBundle(code="xx", tier=TIER_OFFICIAL, data={"meta": {"code": "xx"}})
    )
    assert active.qwen_language() == "Spanish"
