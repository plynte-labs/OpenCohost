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


def test_edge_voice_for_default_es_matches_legacy():
    # Real default locale (es) must equal the pre-i18n hard-coded value exactly.
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


def test_set_then_reset_re_resolves_to_default():
    active.set_active_bundle(_bundle("en", "en-US-AriaNeural"))
    assert active.get_active_bundle().code == "en"
    active.reset_active_bundle()
    assert active.get_active_bundle().code == "es"
