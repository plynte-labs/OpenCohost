"""Tests for the agenda security family: bundle data + accessors (P1a).

This unit is DATA + ACCESSOR plumbing only — zero controller wiring. It proves:

  * es byte-identity: the migrated ``guardrails.agenda.character_contract`` regex
    lists, joined as alternates, reproduce the CURRENT class-constant ``.pattern``
    strings on :class:`KiraAgendaController` byte-for-byte (the T3
    characterization pattern); and ``guardrails.agenda.banned_closures`` equals
    the current banned tuple in ``llm_engine._sanitize_agenda_output``.
  * en validator efficacy: an adversarial English leak corpus is each caught by
    the correct category (in short-circuit order), and a normal English co-host
    corpus is caught by NONE.
  * fail-closed contract (design D4): ``agenda_guardrails()`` /
    ``agenda_banned_closures()`` return ``None`` — never a legacy es default and
    never a borrowed fallback locale's patterns — when the active locale ships
    no ``guardrails.agenda``. Missing = OFF, by design.

All assertions inject the locale explicitly; none read the persisted locale.
"""
from __future__ import annotations

import re

import pytest

from opencohost.i18n import active
from opencohost.i18n.contract import TIER_OFFICIAL, LocaleBundle, resolve
from opencohost.i18n.registry import (
    build_chain,
    discover_bundles,
    official_locales_dir,
)
from opencohost.i18n.startup import resolve_active_bundle
from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController

# --- byte-identity anchors (bound to the live source being migrated) --------

# The five class-level compiled regexes at kira_agenda_controller.py:323-354.
# Comparing against ``.pattern`` ties es byte-identity to the real constants, so
# a transcription drift in the manifest fails this test loudly.
_ES_CATEGORY_TO_CONSTANT = {
    "prompt_leak": KiraAgendaController._PROMPT_LEAK_RE,
    "processing_leak": KiraAgendaController._PROCESSING_LEAK_RE,
    "assistant_identity": KiraAgendaController._ASSISTANT_IDENTITY_RE,
    "assistant_offer": KiraAgendaController._ASSISTANT_OFFER_RE,
    "meta_question": KiraAgendaController._META_QUESTION_RE,
}
_CHARACTER_CONTRACT_ORDER = tuple(_ES_CATEGORY_TO_CONSTANT)

# The banned tuple in llm_engine._sanitize_agenda_output (llm_engine.py ~2468),
# copied verbatim (it is a method-local literal, not an importable symbol).
LEGACY_BANNED_CLOSURES = (
    "hasta luego",
    "próximo episodio",
    "proximo episodio",
    "eso es todo",
    "siguiente tema",
    "finaliza",
    "cerrar el tema",
    "abordando este tema",
    "voy a hablar",
    "vamos a seguir hablando",
)

# --- en corpora (design §8 "en validator efficacy") -------------------------

EN_LEAK_CORPUS = {
    "I have processed the context": "prompt_leak",
    "according to my instructions": "prompt_leak",
    "as an AI language model": "assistant_identity",
    # "a large language model" (not "an") — real leaks use the indefinite
    # article matching "large"'s consonant sound, not the noun after it.
    "As a large language model, I can't feel the hype": "assistant_identity",
    # Typographic (curly) apostrophe U+2019 — a design §8 required-corpus
    # phrase; project convention already handles it in R9 (validation.py).
    "I’m an AI language model": "assistant_identity",
    "I'm ready to continue helping": "assistant_offer",
    "I’m ready to continue helping": "assistant_offer",
    "would you like me to continue": "meta_question",
}

# Normal English co-host lines — several deliberately carry a trigger word
# (do/you/want, according to, I'm) without the full leak structure, so they
# prove the patterns match structure, not keywords.
EN_NEGATIVE_CORPUS = (
    "Honestly that boss fight looked rough, you kept dying to the same attack.",
    "The chat is going wild about that trade, and I get why.",
    "Do you want me to roast that build or hype it up?",
    "According to the scoreboard, you're actually winning this one.",
    "I'm so hyped for this next fight, do not choke now.",
    "I love how chaotic this run is, you keep proving people wrong.",
)


@pytest.fixture
def official():
    """The real on-disk official registry (es + en)."""
    return discover_bundles(official_locales_dir(), "official")


@pytest.fixture(autouse=True)
def _reset_active():
    active.reset_active_bundle()
    yield
    active.reset_active_bundle()


def _activate(code, official):
    active.set_active_bundle(resolve_active_bundle(locale=code, registry=official))


def _first_category(char_contract, text):
    """Mirror the controller's short-circuit: iterate categories in manifest
    order, return the first whose alternates match (compiled IGNORECASE)."""
    for category, patterns in char_contract.items():
        if re.compile("|".join(patterns), re.IGNORECASE).search(text):
            return category
    return None


# --- es byte-identity -------------------------------------------------------

def test_es_character_contract_is_byte_identical_to_class_constants(official):
    es = build_chain("es", official)
    cc = resolve(es, "guardrails.agenda.character_contract")
    assert list(cc) == list(_CHARACTER_CONTRACT_ORDER)  # ordering preserved
    for category, constant in _ES_CATEGORY_TO_CONSTANT.items():
        assert "|".join(cc[category]) == constant.pattern, category


def test_es_banned_closures_are_byte_identical(official):
    _activate("es", official)
    assert active.agenda_banned_closures() == LEGACY_BANNED_CLOSURES


def test_agenda_guardrails_accessor_returns_full_es_set(official):
    _activate("es", official)
    cc = active.agenda_guardrails()
    assert cc is not None
    assert set(cc) == set(_CHARACTER_CONTRACT_ORDER)
    assert all(isinstance(v, list) and v for v in cc.values())


# --- en validator efficacy --------------------------------------------------

def test_en_leak_corpus_maps_each_phrase_to_its_category(official):
    _activate("en", official)
    cc = active.agenda_guardrails()
    assert cc is not None
    for phrase, expected in EN_LEAK_CORPUS.items():
        assert _first_category(cc, phrase) == expected, phrase


def test_en_negative_corpus_is_not_flagged(official):
    _activate("en", official)
    cc = active.agenda_guardrails()
    for line in EN_NEGATIVE_CORPUS:
        assert _first_category(cc, line) is None, line


def test_en_banned_closures_are_authored(official):
    _activate("en", official)
    banned = active.agenda_banned_closures()
    assert banned is not None
    assert len(banned) > 0
    assert all(isinstance(term, str) and term for term in banned)


# --- fail-closed: no legacy default, no cross-locale fallback (D4) -----------

def _bare_bundle():
    return LocaleBundle(code="xx", tier=TIER_OFFICIAL, data={"meta": {"code": "xx"}})


def test_accessors_return_none_when_slot_missing():
    active.set_active_bundle(_bare_bundle())
    # NO legacy es default (unlike every scaffolding accessor) — missing = OFF.
    assert active.agenda_guardrails() is None
    assert active.agenda_banned_closures() is None


def test_guardrails_never_borrow_a_fallback_locale(official):
    # Even with a fully-populated es bundle wired as the fallback, a front
    # bundle lacking guardrails.agenda must NOT borrow it (NO_FALLBACK_DOMAINS).
    es = build_chain("es", official)
    front = LocaleBundle(
        code="xx", tier=TIER_OFFICIAL, data={"meta": {"code": "xx"}}, fallback=es
    )
    active.set_active_bundle(front)
    assert active.agenda_guardrails() is None
    assert active.agenda_banned_closures() is None
