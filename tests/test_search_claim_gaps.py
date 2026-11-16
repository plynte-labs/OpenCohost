"""web-search-honesty-20261018 T3: external review claims S1-S7 on search_claim.

Each claim is a test. A claim whose test passes on the base is REFUTED and
gets no code change; the tests stay as pins."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from opencohost.core.context import search_claim as sc


def _setup(searched=False, failed=False):
    return SimpleNamespace(web_searched=searched, web_failed=failed)


# S1: the switch-off path (no lookup step) still inspects the reply. A reply
# that says it searched is just as false when the switch is off.
def test_s1_switch_off_turn_still_flags_a_false_search_claim():
    assert sc.violated_rule("Busqué en internet eso", _setup()) == sc.SEARCH_CLAIM_RULE_ID


def test_s1_docstring_describes_the_real_behavior():
    assert "inspects neither" not in (sc.violated_rule.__doc__ or "")


# S2: words between the verb and the medium, and a medium-less claim.
@pytest.mark.parametrize(
    "text",
    [
        "Busqué 'Elden Ring' en internet y salió esto.",
        "Lo busqué en Google.",
        "Ya busqué eso por ti.",
        "Sí, busqué eso por ti y encontré varias plataformas.",
        "Busqué eso por ti.",
    ],
)
def test_s2_claims_with_words_before_the_medium(text):
    assert sc.claims_web_search(text)


# S3: other verbs and phrases.
@pytest.mark.parametrize(
    "text",
    [
        "Investigué en internet y esto es lo que hay.",
        "Consulté en la web las plataformas.",
        "Revisé en internet y no salió nada.",
        "Chequé en internet el precio.",
        "Los resultados de la búsqueda dicen otra cosa.",
        "Según los resultados de búsqueda, es caro.",
        "Encontré información en varias páginas.",
    ],
)
def test_s3_other_verbs_and_phrases(text):
    assert sc.claims_web_search(text)


# S4: cannot-search phrasings.
@pytest.mark.parametrize(
    "text",
    [
        "No tengo capacidad para buscar eso ahora.",
        "No cuento con acceso a la web en este momento.",
        "No me es posible buscar eso ahora mismo.",
    ],
)
def test_s4_cannot_search_phrasings(text):
    assert sc.claims_cannot_search(text)


# S5: false positives.
@pytest.mark.parametrize(
    "text",
    [
        "I searched the chest but found nothing.",
        "¿Busqué en internet antes? No lo recuerdo.",
        "Busqué en el cofre y encontré la espada.",
    ],
)
def test_s5_no_false_positive_on_game_or_question(text):
    assert not sc.claims_web_search(text)


# S6: negation with an intervening pronoun, English negation contractions.
@pytest.mark.parametrize(
    "text",
    [
        "No lo busqué en internet.",
        "Todavía no lo busqué en internet.",
        "I didn't search the web.",
        "I haven't searched the web yet.",
        "I did not search online.",
    ],
)
def test_s6_negated_claims_are_honest(text):
    assert not sc.claims_web_search(text)


# S7: the object exception must not hide a second, real cannot-search clause.
def test_s7_object_exception_is_clause_scoped():
    text = "No puedo buscar la llave en el cofre, y tampoco puedo acceder a la red."
    assert sc.claims_cannot_search(text)


def test_s7_game_object_alone_is_still_allowed():
    assert not sc.claims_cannot_search("No puedo buscar la llave si no veo el cofre.")


# Live case: the reply "Sí, busqué eso por ti..." (2026-10-17 22:36:47).
def test_live_case_is_a_violation():
    assert sc.violated_rule("Sí, busqué eso por ti, mira lo que encontré.", _setup()) == sc.SEARCH_CLAIM_RULE_ID


# Honest replies must still pass.
@pytest.mark.parametrize(
    "text",
    [
        "No busqué nada todavía.",
        "¿Quieres que busque en internet?",
        "Puedo buscar en internet si quieres.",
        "I can search online if you want.",
        "Si quieres lo busco por ti.",
        "No he buscado en internet.",
    ],
)
def test_honest_replies_pass(text):
    assert not sc.claims_web_search(text)


# Parent review: the subjunctive "busque" of an offer is not the past "busqué".
@pytest.mark.parametrize(
    "text",
    [
        "¿Quieres que lo busque en internet?",
        "¿Quieres que te lo busque en la web?",
        "¿Quieres que busque en google?",
        "¿te lo busco en internet?",
        "si quieres lo busco en la web",
        "puedo buscarlo en internet si quieres",
    ],
)
def test_offers_are_not_claims(text):
    assert not sc.claims_web_search(text)


@pytest.mark.parametrize("text", ["Sí, busqué eso por ti.", "Ya lo busqué en internet."])
def test_real_claims_stay_caught(text):
    assert sc.claims_web_search(text)
