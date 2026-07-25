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


# ---------------------------------------------------------------------------
# Phase 4 (multi_provider_llm_20260723): llm.provider_fallback_notice present
# in BOTH official locales, and genuinely translated (not a copy-paste).
# ---------------------------------------------------------------------------


def test_provider_fallback_notice_present_in_both_locales(official):
    en_bundle = build_chain("en", official)
    es_bundle = build_chain("es", official)
    en_notice = resolve(en_bundle, "llm.provider_fallback_notice")
    es_notice = resolve(es_bundle, "llm.provider_fallback_notice")
    assert en_notice
    assert es_notice
    assert en_notice != es_notice


def test_set_active_en_makes_provider_fallback_notice_accessor_english(official):
    active.set_active_bundle(resolve_active_bundle(locale="en", registry=official))
    assert active.provider_fallback_notice() == resolve(
        build_chain("en", official), "llm.provider_fallback_notice"
    )


# ---------------------------------------------------------------------------
# grounding_authority_temporal_humility: llm.grounding_rules and
# llm.editorial_card_instruction must ship in BOTH official locales, authored
# per-locale (never a copy-paste of the other language).
#
# These assert PRESENCE and STRUCTURE of the injected text, not model
# behavior. Whether Kira actually stops saying "that doesn't exist" is a
# runtime property only the owner's live validation can confirm.
# ---------------------------------------------------------------------------

GROUNDING_SLOTS = ("llm.grounding_rules", "llm.editorial_card_instruction")


@pytest.mark.parametrize("slot", GROUNDING_SLOTS)
def test_grounding_slots_present_in_both_locales(official, slot):
    es_text = resolve(build_chain("es", official), slot)
    en_text = resolve(build_chain("en", official), slot)
    assert es_text and isinstance(es_text, str)
    assert en_text and isinstance(en_text, str)
    assert es_text != en_text, f"{slot} must be authored per locale, not copied"


@pytest.mark.parametrize("slot", GROUNDING_SLOTS)
def test_grounding_slots_en_carries_no_spanish_diacritics(official, slot):
    # Same discriminator the connector-pool parity tests use: an English slot
    # that still carries es diacritics is an un-translated copy.
    en_text = resolve(build_chain("en", official), slot)
    assert not any(ch in en_text for ch in "¿¡óáéúíñ"), f"{slot} not translated: {en_text}"


def test_grounding_rules_name_no_block_tag(official):
    # These rules ship on EVERY turn, but <editorial_context> only exists on a
    # turn where a card matched. Naming an absent block invites the model to
    # reference one it cannot see — so the card-specific authority framing lives
    # inside the block (editorial_card_instruction) instead.
    for code in ("es", "en"):
        rules = resolve(build_chain(code, official), "llm.grounding_rules")
        for tag in ("<editorial_context>", "<memoria_de_fondo", "<background_memory", "<memorias_guardadas"):
            assert tag not in rules, f"{code} grounding_rules must not name {tag}"


def test_editorial_card_instruction_carries_the_authority_framing(official):
    # Incident 1's fix: authority + no-invention sit WITH the card data.
    es = resolve(build_chain("es", official), "llm.editorial_card_instruction")
    assert "MANDAN" in es
    assert "fechas" in es and "versiones" in es
    en = resolve(build_chain("en", official), "llm.editorial_card_instruction")
    assert "OUTRANK" in en
    assert "dates" in en and "versions" in en


def test_grounding_rules_carry_temporal_humility_in_both_locales(official):
    # Incident 2: Kira asserted that shipped models "do not exist". The rule
    # must state the cutoff posture AND explicitly ban the denial phrasing.
    es_rules = resolve(build_chain("es", official), "llm.grounding_rules")
    assert "corte" in es_rules
    assert "no existe" in es_rules
    en_rules = resolve(build_chain("en", official), "llm.grounding_rules")
    assert "cutoff" in en_rules
    assert "doesn't exist" in en_rules


def test_grounding_rules_preserve_pushback_and_ban_disclaimer_spam(official):
    # The two properties the humility must NOT destroy, pinned as text:
    # (1) she still calls false things false, (2) she does not hedge every line.
    es_rules = resolve(build_chain("es", official), "llm.grounding_rules")
    assert "lo falso sigue siendo falso" in es_rules
    assert "no en cada frase" in es_rules
    en_rules = resolve(build_chain("en", official), "llm.grounding_rules")
    assert "false is still false" in en_rules
    assert "not in every sentence" in en_rules
