"""web-search-honesty-20261018 T1/T2: STT sentence boundary and verb variants."""
from __future__ import annotations

import pytest

from opencohost.core.web import trigger


@pytest.mark.parametrize(
    "text,query",
    [
        ("Podrías buscar. Que plataformas de empleo son las más comunes en México.",
         "Que plataformas de empleo son las más comunes en México"),
        ("Puedes buscar. ¿Qué modelo se presentó de Tesla? De auto.",
         "Qué modelo se presentó de Tesla? De auto"),
        ("Puedes buscar, ¿cuál es el mejor teclado?", "cuál es el mejor teclado"),
        ("Kira, busca. Quién ganó el mundial.", "Quién ganó el mundial"),
        ("Puedes buscar. Noticias de tecnología hoy", "Noticias de tecnología hoy"),
    ],
)
def test_imperative_then_boundary_then_topic_is_a_search(text, query):
    assert trigger.web_rule(text) == trigger.SEARCH
    assert trigger.search_query(text) == query


@pytest.mark.parametrize(
    "text",
    [
        "busca la espada en el cofre",
        "busca la llave",
        "busca. La llave está ahí",
        "Puedes buscar.",
        "Podrías buscar. Ahí",
        "busca. Ahí está la llave del cofre",
    ],
)
def test_game_talk_and_bare_verbs_stay_no_search(text):
    assert trigger.web_rule(text) == trigger.NO_SEARCH


@pytest.mark.parametrize(
    "text",
    [
        "búsquela en internet",
        "Púsquela en internet",
        "púscalo en la web",
        "puedes púsquelo en google",
    ],
)
def test_stt_verb_variants_count_as_the_verb(text):
    assert trigger.web_rule(text) == trigger.SEARCH


def test_stt_garble_is_stripped_from_the_query():
    q = trigger.search_query("Búscame cómo cambian las relaciones. Púsquela, púsquela en una web")
    assert "usquela" not in trigger._fold(q)
    assert "relaciones" in q


def test_stt_variant_bare_command_is_bare():
    assert trigger.is_bare_search_command("búsquelo en internet")
    assert trigger.search_query("búsquelo en internet") == ""


@pytest.mark.parametrize(
    "text",
    ["Puedes buscar. Me da igual.", "Puedes buscar. Hay un cofre al fondo.", "Podrías buscar. Eso se ve raro"],
)
def test_modal_plus_plain_sentence_is_not_a_search(text):
    assert trigger.web_rule(text) == trigger.NO_SEARCH


def test_modal_plus_info_noun_or_topic_cue_is_a_search():
    assert trigger.web_rule("Puedes buscar. Noticias de tecnología hoy") == trigger.SEARCH
    assert trigger.web_rule("Puedes buscar. Los mejores modelos de IA") == trigger.SEARCH


def test_trailing_medium_fragments_are_dropped_from_the_query():
    q = trigger.search_query("Cómo cambian las relaciones. Púsquela, púsquela. En Internet. Una web. Buscala.")
    assert q == "Cómo cambian las relaciones"
