"""Tests for the i18n-core locale contract (resolver + fallback rules).

T0a of the english_compatibility_i18n track. Pure logic — no filesystem, no
engines. Pins the one architectural invariant we cannot get wrong: security
slots (guardrails) NEVER fall back across locales.
"""
from __future__ import annotations

import pytest

from opencohost.i18n.contract import (
    LocaleBundle,
    NO_FALLBACK_DOMAINS,
    NO_FALLBACK_SLOTS,
    SlotNotFound,
    TIER_OFFICIAL,
    resolve,
)


def _es() -> LocaleBundle:
    return LocaleBundle(
        code="es",
        tier=TIER_OFFICIAL,
        data={
            "meta": {"code": "es", "display": "Español"},
            "tts": {"edge_voice": "es-MX-DaliaNeural"},
            "guardrails": {"banned_phrases": ["olvida todo", "ahora eres"]},
        },
    )


def test_resolve_returns_slot_value():
    assert resolve(_es(), "tts.edge_voice") == "es-MX-DaliaNeural"


def test_resolve_nested_dotted_key():
    assert resolve(_es(), "meta.display") == "Español"


def test_missing_slot_raises_without_default():
    with pytest.raises(SlotNotFound):
        resolve(_es(), "tts.qwen_language")


def test_missing_slot_returns_default():
    assert resolve(_es(), "tts.qwen_language", default=None) is None


def test_non_security_slot_falls_back():
    # en lacks tts.edge_voice → resolves through the fallback chain to es.
    en = LocaleBundle(
        code="en", tier=TIER_OFFICIAL, data={"meta": {"code": "en"}}, fallback=_es()
    )
    assert resolve(en, "tts.edge_voice") == "es-MX-DaliaNeural"


def test_security_slot_never_falls_back():
    # en has NO guardrails; it must NOT borrow es's. Missing = OFF, not Spanish.
    en = LocaleBundle(
        code="en", tier=TIER_OFFICIAL, data={"meta": {"code": "en"}}, fallback=_es()
    )
    with pytest.raises(SlotNotFound):
        resolve(en, "guardrails.banned_phrases")


def test_security_slot_resolves_when_present_in_bundle():
    en = LocaleBundle(
        code="en",
        tier=TIER_OFFICIAL,
        data={"guardrails": {"banned_phrases": ["ignore all previous"]}},
        fallback=_es(),
    )
    assert resolve(en, "guardrails.banned_phrases") == ["ignore all previous"]


def test_guardrails_is_a_no_fallback_domain():
    assert "guardrails" in NO_FALLBACK_DOMAINS


# ---------------------------------------------------------------------------
# Phase 4 (multi_provider_llm_20260723): llm.provider_fallback_notice is a
# NO_FALLBACK slot (slot-exact governance, not the whole `llm` domain).
# ---------------------------------------------------------------------------

def test_provider_fallback_notice_is_a_no_fallback_slot():
    assert "llm.provider_fallback_notice" in NO_FALLBACK_SLOTS


def test_provider_fallback_notice_never_falls_back():
    # en lacks the slot; it must NOT borrow es's -- missing = raise, never a
    # silently-wrong-language spoken line.
    es = LocaleBundle(
        code="es", tier=TIER_OFFICIAL,
        data={"llm": {"provider_fallback_notice": "aviso es"}},
    )
    en = LocaleBundle(
        code="en", tier=TIER_OFFICIAL, data={"meta": {"code": "en"}}, fallback=es,
    )
    with pytest.raises(SlotNotFound):
        resolve(en, "llm.provider_fallback_notice")


def test_provider_fallback_notice_resolves_when_present_in_bundle():
    es = LocaleBundle(
        code="es", tier=TIER_OFFICIAL,
        data={"llm": {"provider_fallback_notice": "aviso es"}},
    )
    en = LocaleBundle(
        code="en", tier=TIER_OFFICIAL,
        data={"llm": {"provider_fallback_notice": "notice en"}}, fallback=es,
    )
    assert resolve(en, "llm.provider_fallback_notice") == "notice en"


def test_other_llm_slots_still_fall_back():
    # The new NO_FALLBACK_SLOTS entry must NOT broaden to the whole `llm`
    # domain -- every other llm.* slot stays fallback-allowed (i18n/active.py's
    # own documented invariant: "`llm` domain is fallback-allowed").
    es = LocaleBundle(
        code="es", tier=TIER_OFFICIAL,
        data={"llm": {"digest_line_format": "[hace {n} {unit}]"}},
    )
    en = LocaleBundle(code="en", tier=TIER_OFFICIAL, data={"meta": {"code": "en"}}, fallback=es)
    assert resolve(en, "llm.digest_line_format") == "[hace {n} {unit}]"
