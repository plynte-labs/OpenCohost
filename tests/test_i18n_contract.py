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
