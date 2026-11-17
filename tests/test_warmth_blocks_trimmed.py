"""kira-natural-replies-20261111 C: the warmth blocks and recency lines are a few
positive lines, not ~900 chars of quoted "don't say" lists.

On a ~4B model a quoted phrase is a primer. The forbidden machine-lecture
phrases are enforced after the fact by ``output_guard`` (``no_machine_lecture``,
``no_human_claim``), so the prompt no longer lists them. The meaning of each rule
is pinned here by a few stable key words, not by full strings.
"""
from __future__ import annotations

import pytest

from opencohost.i18n import active as i18n_active
from opencohost.i18n.contract import TIER_OFFICIAL, LocaleBundle
from opencohost.i18n.registry import discover_bundles, official_locales_dir
from opencohost.i18n.startup import resolve_active_bundle

WARM_LEVELS = ("empathetic", "in_character", "free", "sentimental")
LOCALES = ("es", "en", "builtin")

BLOCK_MAX = 880  # S4 boundary sentence (~165 chars)
RECENCY_MAX = 150

# Every level carries these rules (lower-cased key words, any one of the
# alternatives per rule is enough).
COMMON = {
    "es": {
        "adds_not_replaces": ("se suma a tu personalidad sarcástica", "nunca la reemplaza"),
        "narrow_override": ("algoritmo, código o máquina",),
        "show_not_describe": ("no la describas",),
        "start_directly": ("empieza directo", "vocativo", "rara vez"),
        "comfort_gate": ("tristeza, dolor o angustia",),
        "no_human_no_body": ("humana", "cuerpo"),
        "ai_only_when_asked": ("ia", "pregunt"),
        "playful_sarcasm": ("humilles", "condescendencia", "acéptalo"),
    },
    "en": {
        "adds_not_replaces": ("adds to your sarcastic persona", "never replaces it"),
        "narrow_override": ("algorithm, code or a machine",),
        "show_not_describe": ("never describe it",),
        "start_directly": ("start directly", "vocative", "rarely"),
        "comfort_gate": ("sadness, pain or distress",),
        "no_human_no_body": ("human", "body"),
        "ai_only_when_asked": ("ai", "asked"),
        "playful_sarcasm": ("belittle", "talk down", "accept"),
    },
}

# One clause per level that keeps its distinct behavior.
LEVEL_CLAUSE = {
    "es": {
        "empathetic": ("sin minimizarlo",),
        "in_character": ("primera persona", "cómo funcionas"),
        "free": ("de verdad", "sin matizar"),
        "sentimental": ("cariños", "nunca te burles"),
    },
    "en": {
        "empathetic": ("brushing it off",),
        "in_character": ("first person", "how you work"),
        "free": ("truly", "hedging"),
        "sentimental": ("affectionate", "never mock"),
    },
}

# Quoted primers that must not come back (lower-cased).
PRIMERS = {
    "es": ("soy un algoritmo", "no tengo sentimientos", "solo soy una máquina",
           "en el sentido biológico", "si yo pudiera sentir", "ciclos circadianos",
           "mis emociones no son reales"),
    "en": ("i'm just an algorithm", "i don't have feelings", "i'm just a machine",
           "in the biological sense", "if i could feel", "circadian",
           "my emotions aren't real"),
}


def _activate(locale: str) -> str:
    """Activate ``locale`` (es, en or the built-in English); return its language."""
    if locale == "builtin":
        i18n_active.set_active_bundle(
            LocaleBundle(code="xx", tier=TIER_OFFICIAL, data={"meta": {"code": "xx"}})
        )
        return "en"
    registry = discover_bundles(official_locales_dir(), "official")
    i18n_active.set_active_bundle(resolve_active_bundle(locale=locale, registry=registry))
    return locale


@pytest.fixture(autouse=True)
def _reset():
    i18n_active.reset_active_bundle()
    yield
    i18n_active.reset_active_bundle()


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("level", WARM_LEVELS)
def test_block_is_short(locale, level):
    _activate(locale)
    assert len(i18n_active.warmth_block(level)) <= BLOCK_MAX


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("level", WARM_LEVELS)
def test_recency_line_is_one_short_sentence(locale, level):
    _activate(locale)
    line = i18n_active.warmth_recency_line(level)
    assert line and len(line) <= RECENCY_MAX
    assert "\n" not in line


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("level", WARM_LEVELS)
def test_no_quoted_text_or_forbidden_phrase(locale, level):
    lang = _activate(locale)
    for text in (i18n_active.warmth_block(level), i18n_active.warmth_recency_line(level)):
        assert '"' not in text and "«" not in text and "»" not in text, (locale, level)
        low = text.lower()
        for phrase in PRIMERS[lang]:
            assert phrase not in low, (locale, level, phrase)


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("level", WARM_LEVELS)
def test_block_keeps_the_common_rules(locale, level):
    lang = _activate(locale)
    low = i18n_active.warmth_block(level).lower()
    for rule, needles in COMMON[lang].items():
        assert any(n in low for n in needles), (locale, level, rule)


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("level", WARM_LEVELS)
def test_block_has_the_start_directly_line_without_vocative_opener(locale, level):
    lang = _activate(locale)
    low = i18n_active.warmth_block(level).lower()
    assert ("empieza directo" if lang == "es" else "start directly") in low
    assert "vocativo" in low if lang == "es" else "vocative" in low
    assert ("rara vez" if lang == "es" else "rarely") in low


@pytest.mark.parametrize("locale", LOCALES)
@pytest.mark.parametrize("level", WARM_LEVELS)
def test_block_keeps_its_distinct_level_clause(locale, level):
    lang = _activate(locale)
    low = i18n_active.warmth_block(level).lower()
    assert any(n in low for n in LEVEL_CLAUSE[lang][level]), (locale, level)


@pytest.mark.parametrize("locale", LOCALES)
def test_empathetic_claims_no_feelings_of_its_own(locale):
    lang = _activate(locale)
    low = i18n_active.warmth_block("empathetic").lower()
    assert ("sientes tú" in low or "sentimientos propios" in low) if lang == "es" else (
        "feelings of your own" in low
    )


def test_es_and_en_blocks_stay_in_step():
    sizes = {}
    for locale in ("es", "en"):
        _activate(locale)
        sizes[locale] = [len(i18n_active.warmth_block(level)) for level in WARM_LEVELS]
    for es, en in zip(sizes["es"], sizes["en"]):
        assert abs(es - en) <= 120
