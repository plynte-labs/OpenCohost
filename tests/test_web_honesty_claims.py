"""runtime-fixes-20261110 T2 (C): honesty of web answers.

Three holes in ``search_claim.violated_rule``:
1. a claim of having found something when the lookup was empty or failed;
2. a denial of being able to search when a lookup ran and returned results;
3. a claim of having read a page when no page was fetched this turn.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from opencohost.core.context import search_claim as sc


def setup(searched=False, failed=False, link_read=False):
    return SimpleNamespace(web_searched=searched, web_failed=failed, link_read=link_read)


NO_LOOKUP = setup()
OK_LOOKUP = setup(searched=True)
EMPTY_LOOKUP = setup(searched=True, failed=True)
LINK_OK = setup(link_read=True)

FOUND_CLAIMS = [
    "Encontré información sobre el parche, sale el viernes.",
    "Busqué y encontré que el juego sale en marzo.",
    "Encontré algo interesante sobre eso.",
    "Según los resultados, el parche ya salió.",
    "Los resultados muestran que sube el precio.",
    "La búsqueda arrojó varias noticias recientes.",
    "I found some information about the patch.",
    "I found that the game launches in March.",
    "According to my search, it is out already.",
    "The results show that the price went up.",
    "My search turned up several articles.",
]

HONEST_AFTER_EMPTY = [
    "No encontré nada útil sobre eso esta vez.",
    "La búsqueda no arrojó resultados, quieres que lo intente otra vez?",
    "No pude encontrar información, lo siento.",
    "I found nothing useful this time.",
    "I did not find anything about it online.",
    "No encontré nada, pero puedo intentarlo de nuevo.",
    "Mi amigo encontró la llave en el juego.",
    "I found the boss fight really fun.",
    "Si quieres, puedo buscarlo otra vez.",
]


@pytest.mark.parametrize("text", FOUND_CLAIMS)
def test_found_claim_on_an_empty_or_failed_lookup_is_blocked(text):
    assert sc.violated_rule(text, EMPTY_LOOKUP) == sc.FALSE_RESULT_RULE_ID


@pytest.mark.parametrize("text", HONEST_AFTER_EMPTY)
def test_honest_talk_after_an_empty_lookup_is_allowed(text):
    assert sc.violated_rule(text, EMPTY_LOOKUP) is None


@pytest.mark.parametrize("text", FOUND_CLAIMS)
def test_found_claims_are_fine_when_the_lookup_gave_results(text):
    assert sc.violated_rule(text, OK_LOOKUP) is None


def test_found_claim_without_lookup_keeps_the_old_rule():
    assert sc.violated_rule("Busqué en internet y el parche sale el viernes.", NO_LOOKUP) == sc.SEARCH_CLAIM_RULE_ID


CANNOT = [
    "No puedo buscar en internet.",
    "No tengo acceso a internet ahora.",
    "I cannot browse the web.",
    "I can't search for that.",
    "No puedo navegar por la red.",
]


@pytest.mark.parametrize("text", CANNOT)
def test_denying_search_after_a_lookup_with_results_is_blocked(text):
    assert sc.violated_rule(text, OK_LOOKUP) == sc.CANNOT_SEARCH_RULE_ID


@pytest.mark.parametrize("text", CANNOT)
def test_denying_search_after_a_failed_lookup_stays_blocked(text):
    assert sc.violated_rule(text, EMPTY_LOOKUP) == sc.CANNOT_SEARCH_RULE_ID


@pytest.mark.parametrize(
    "text",
    [
        "Puedo buscarlo si quieres.",
        "I can look that up if you want.",
        "Dijiste que otro streamer no puede buscar eso, jaja.",
        "No puedo buscar la llave si no me dices dónde está.",
        "Tu amigo says he cannot search anything without wifi.",
    ],
)
def test_ordinary_sentences_do_not_trip_the_denial_rule(text):
    # "Dijiste que ... no puede": third person, and the object exception.
    assert sc.violated_rule(text, OK_LOOKUP) is None


def test_denial_without_any_lookup_is_not_this_rules_business():
    assert sc.violated_rule("No puedo buscar en internet.", NO_LOOKUP) is None


PAGE_CLAIMS = [
    "Ya leí el enlace que me mandaste, dice que sale el viernes.",
    "Abrí la página y ahí explica todo.",
    "Revisé el artículo con calma.",
    "He leído el enlace.",
    "Acabo de abrir tu link.",
    "I read the article you sent.",
    "I just opened the link, and it says it launches Friday.",
    "I have visited the page.",
    "I went through the website you shared.",
]


@pytest.mark.parametrize("text", PAGE_CLAIMS)
@pytest.mark.parametrize("state", [NO_LOOKUP, setup(searched=True, failed=True)])
def test_page_claim_without_a_fetched_page_is_blocked(text, state):
    rule = sc.violated_rule(text, state)
    assert rule in (sc.PAGE_CLAIM_RULE_ID, sc.SEARCH_CLAIM_RULE_ID, sc.FALSE_RESULT_RULE_ID)
    assert sc.claims_read_page(text) is True


@pytest.mark.parametrize("text", PAGE_CLAIMS)
def test_page_claim_is_fine_when_the_page_was_fetched(text):
    assert sc.violated_rule(text, LINK_OK) is None


def test_page_claim_on_a_successful_search_turn_is_fine_since_pages_were_fetched():
    assert sc.violated_rule("Leí la página de la noticia y dice que sale el viernes.", OK_LOOKUP) is None


@pytest.mark.parametrize(
    "text",
    [
        "No pude abrir el enlace esta vez.",
        "No leí el enlace porque no puedo abrirlo desde el teléfono.",
        "Puedo leer el enlace si lo escribes en la app.",
        "I can read the link if you type it in the app.",
        "I did not open the link.",
        "Le dije a mi amigo que leyera el artículo.",
        "Tú leíste el artículo ayer, ¿verdad?",
        "Did you read the article I mean the one you sent?",
        "Ayer vi un video muy gracioso.",
        "He leído tu mensaje, gracias.",
        "I read your message twice.",
    ],
)
def test_ordinary_sentences_do_not_trip_the_page_claim(text):
    assert sc.claims_read_page(text) is False
    assert sc.violated_rule(text, NO_LOOKUP) is None


def test_the_new_rules_have_reasons_logs_and_nudges():
    from opencohost.i18n import active as i18n_active

    for rule in (sc.FALSE_RESULT_RULE_ID, sc.PAGE_CLAIM_RULE_ID, sc.CANNOT_SEARCH_RULE_ID):
        assert rule in sc.reason_for(rule)
        assert i18n_active.guardrail_retry_nudge(rule).strip()
    assert sc.reason_for(sc.FALSE_RESULT_RULE_ID) != sc.reason_for(sc.PAGE_CLAIM_RULE_ID)


def test_log_claim_carries_counts_only(caplog):
    import logging

    caplog.set_level(logging.WARNING, logger="OpenCohost")
    for rule in (sc.SEARCH_CLAIM_RULE_ID, sc.CANNOT_SEARCH_RULE_ID, sc.FALSE_RESULT_RULE_ID, sc.PAGE_CLAIM_RULE_ID):
        sc.log_claim(rule, stage="buffered", source="direct", chars=42, sentence_index=1)
    text = caplog.text
    assert text.count("chars=42") == 4
    assert "[PAGE_CLAIM]" in text and "[SEARCH_RESULT_CLAIM]" in text


def test_every_nudge_passes_the_guards_it_would_be_judged_by():
    from opencohost.config.validation import output_guard
    from opencohost.i18n import active as i18n_active

    for rule in (sc.FALSE_RESULT_RULE_ID, sc.PAGE_CLAIM_RULE_ID, sc.CANNOT_SEARCH_RULE_ID):
        nudge = i18n_active.guardrail_retry_nudge(rule)
        assert sc.claims_read_page(nudge) is False
        assert sc.claims_cannot_search(nudge) is False
        assert sc.claims_web_search(nudge) is False
        assert sc.claims_found_result(nudge) is False


def test_the_assembler_exposes_link_read_and_failure_flags():
    from opencohost.core.context.prompt_assembler import GenerationSetup

    fields = GenerationSetup.__dataclass_fields__
    assert "link_read" in fields and fields["link_read"].default is False
