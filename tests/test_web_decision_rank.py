"""web-fetch-laya-20261009 T2: the ``web`` decision, its explicit-request
rule and Laya ranking of links and chunks (fail-open).

Laya is a fake in-process backend; nothing touches the network or a worker.
"""
from __future__ import annotations

import pytest

from opencohost.config import decisions as dc
from opencohost.core.decision import QUESTIONS, SOURCES, VALUES, DecisionBus
from opencohost.core.decision.consumers import apply_web_decision
from opencohost.core.web import rank, trigger
from opencohost.core.web.search import SearchResult

SECRET = "zanahoriaconfidencial"


# --- decision schema -----------------------------------------------------------


def test_web_question_is_a_two_option_choice_for_owner_voice_sources():
    q = QUESTIONS["web"]
    assert q["type"] == "choice"
    assert tuple(q["criteria"]) == ("search", "no_search")
    assert VALUES["web"] == ("search", "no_search")
    assert SOURCES["web"] == frozenset({"direct", "ptt"})


# --- explicit-request rule -----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "busca en internet las noticias de Elden Ring",
        "Kira, búscalo en google",
        "búscame en la web cuándo sale el parche",
        "buscar en internet el precio de la 5090",
        "¿Qué hay de nuevo sobre Hollow Knight Silksong?",
        "dame las últimas noticias de Valorant",
        "noticias sobre la final de la champions",
        "googlea quién ganó el mundial de clubes",
        "search the web for the new Zelda trailer",
        "can you look up online when the patch drops",
        "look it up online please",
        "any news about Silksong?",
        "what's new with Minecraft",
        "search online for speedrun records",
    ],
)
def test_rule_matches_explicit_requests(text):
    assert trigger.web_rule(text) == "search"


@pytest.mark.parametrize(
    "text",
    [
        "hola Kira, ¿cómo estás?",
        "qué opinas de este jefe",
        "mira la pantalla",
        "busca la llave en el cofre",
        "I searched everywhere for that sword",
        "",
        None,
    ],
)
def test_rule_ignores_everything_else(text):
    assert trigger.web_rule(text) == "no_search"


@pytest.mark.parametrize(
    "text,query",
    [
        ("busca en internet las noticias de Elden Ring", "las noticias de Elden Ring"),
        ("Kira, búscame en google cuándo sale el parche", "cuándo sale el parche"),
        ("search the web for the new Zelda trailer.", "the new Zelda trailer"),
        ("¿Qué hay de nuevo sobre Silksong?", "Qué hay de nuevo sobre Silksong"),
        ("busca en internet", ""),
    ],
)
def test_search_query_strips_the_command_and_keeps_the_topic(text, query):
    assert trigger.search_query(text) == query


@pytest.mark.parametrize(
    "text",
    [
        "Busca que noticias hay actualmente en LATAM",
        "busca que dice el ultimo post de X de franguh",
        "busca web que youtuber subió el video del speedrun",
        "Busca en internet cuando sale silksong",
        "Kira, búscame quién ganó el mundial de clubes",
        "Oye Kira, buscá cuándo sale el parche",
        "investiga qué pasó con el servidor de Valorant",
        "investiga sobre el nuevo DLC de Elden Ring",
        "Ok. Busca en reddit qué opinan del jefe final",
        "busca en la web",
        "busca noticias de la final de la champions",
        "¿Qué noticias hay hoy?",
        "cuál es el último post de franguh",
        "qué dice el último tweet de Elon",
    ],
)
def test_rule_matches_bare_imperatives_and_fresh_info_asks(text):
    assert trigger.web_rule(text) == "search"


@pytest.mark.parametrize(
    "text",
    [
        "Cuando sale silksong?",
        "¿Cuándo sale Silksong?",
        "estoy buscando la espada de fuego",
        "busca la forma de pasar este jefe",
        "¿buscas algo en el inventario?",
        "busco pareja para el raid",
        "creo que busca pelea ese jefe",
        "no busques más, ya lo encontré",
    ],
)
def test_rule_keeps_non_requests_out(text):
    assert trigger.web_rule(text) == "no_search"


@pytest.mark.parametrize(
    "text,query",
    [
        ("Busca que noticias hay actualmente en LATAM", "que noticias hay actualmente en LATAM"),
        ("busca que dice el ultimo post de X de franguh", "que dice el ultimo post de X de franguh"),
        ("busca web que youtuber subió el video", "que youtuber subió el video"),
        ("Busca en internet cuando sale silksong", "cuando sale silksong"),
        ("Kira, búscame quién ganó el mundial", "quién ganó el mundial"),
        ("busca en internet qué es un roguelike", "qué es un roguelike"),
        ("busca que noticias hay en latam", "que noticias hay en latam"),
        ("investiga sobre el nuevo DLC", "el nuevo DLC"),
    ],
)
def test_search_query_strips_a_leading_imperative(text, query):
    assert trigger.search_query(text) == query


@pytest.mark.parametrize(
    "text",
    [
        "busca en la web",
        "Kira, búscalo en internet por favor",
        "busca en google eso",
        "search the web for it",
        "googlealo",
        "Sí, búscalo en internet",
    ],
)
def test_search_query_is_empty_when_only_trigger_words_are_left(text):
    assert trigger.web_rule(text) == "search"
    assert trigger.search_query(text) == ""


def test_search_query_caps_and_cleans():
    q = trigger.search_query("busca en internet " + "a\x00b " * 200)
    assert len(q) <= trigger.QUERY_MAX_CHARS
    assert "\x00" not in q


# --- consumer -------------------------------------------------------------------


class FakeBackend:
    def __init__(self, answers=None, *, ready=True, response=None):
        self.answers = answers or {}
        self.ready = ready
        self.response = response
        self.calls = []

    def available(self):
        return self.ready

    def ask(self, state, questions, timeout_s):
        self.calls.append({"state": state, "questions": dict(questions), "timeout_s": timeout_s})
        if callable(self.response):
            return self.response(state, questions)
        if self.response is not None:
            return self.response
        return {"ok": True, "answers": {q: self.answers[q] for q in questions if q in self.answers}}


def _turn(choice, mode="on", conf=0.97):
    dc.set_decision("web", mode=mode)
    backend = FakeBackend({"web": {"choice": choice, "answer_confidence": conf}})
    return DecisionBus(backend).decide("x", "direct", ids=("web",))


def test_no_turn_means_the_rule():
    assert apply_web_decision(None, "search") == "search"
    assert apply_web_decision(None, "no_search") == "no_search"


def test_laya_on_may_add_a_search_the_rule_missed():
    assert apply_web_decision(_turn("search"), "no_search") == "search"


def test_laya_never_cancels_an_explicit_request():
    turn = _turn("no_search")
    assert apply_web_decision(turn, "search") == "search"
    assert turn.resolve("web", "search").status == "skipped"


def test_shadow_and_low_confidence_keep_the_rule():
    assert apply_web_decision(_turn("search", mode="shadow"), "no_search") == "no_search"
    assert apply_web_decision(_turn("search", conf=0.5), "no_search") == "no_search"


def test_unasked_web_decision_keeps_the_rule():
    dc.set_decision("memory", mode="on")
    turn = DecisionBus(FakeBackend({"memory": {"choice": "skip", "answer_confidence": 0.99}})).decide(
        "x", "direct", ids=("memory",)
    )
    assert apply_web_decision(turn, "no_search") == "no_search"


# --- ranking ----------------------------------------------------------------------


def _results(n):
    return [SearchResult(f"T{i}", f"https://s{i}.example/p", f"snippet {i}") for i in range(n)]


def _scored(scores):
    """A backend answering ``useful`` with probability ``scores[i]`` for the
    i-th call, read from the item marker in the state."""

    def respond(state, questions):
        (qid,) = questions
        i = int(state.split("#", 1)[1].split()[0]) if "#" in state else 0
        p = scores[i]
        choice = "useful" if p >= 0.5 else "not_useful"
        return {"ok": True, "answers": {qid: {"choice": choice, "probabilities": {"useful": p, "not_useful": 1 - p}}}}

    return FakeBackend(response=respond)


def test_link_ranking_keeps_the_top_two_above_the_floor():
    results = [SearchResult(f"#{i} title", f"https://s{i}.example/", "s") for i in range(5)]
    backend = _scored([0.2, 0.9, 0.05, 0.7, 0.95])
    out = rank.rank_links(backend, "pregunta", results, deadline=None)
    assert [r.url for r in out.kept] == ["https://s4.example/", "https://s1.example/"]
    assert out.scored == 5
    assert len(backend.calls) == 5
    q = backend.calls[0]["questions"]["web_link"]
    assert q["type"] == "choice" and tuple(q["criteria"]) == ("useful", "not_useful")
    assert "pregunta" in q["instructions"]


def test_link_ranking_below_floor_keeps_nothing():
    results = [SearchResult(f"#{i} t", f"https://s{i}.example/", "s") for i in range(3)]
    out = rank.rank_links(_scored([0.01, 0.02, 0.03]), "q", results, deadline=None)
    assert out.kept == []


def test_unavailable_laya_keeps_the_first_in_order():
    results = _results(5)
    for backend in (None, FakeBackend(ready=False)):
        out = rank.rank_links(backend, "q", results, deadline=None)
        assert out.kept == results[:2] and out.scored == 0
    chunks = [("a.example", f"c{i}") for i in range(5)]
    out = rank.rank_chunks(None, "q", chunks, deadline=None)
    assert out.kept == chunks[:3]


def test_timeout_stops_asking_and_fills_in_order():
    results = [SearchResult(f"#{i} t", f"https://s{i}.example/", "s") for i in range(4)]
    calls = []

    def respond(state, questions):
        calls.append(state)
        if len(calls) == 2:
            return None  # the worker timed out: do not keep queueing work on it
        (qid,) = questions
        return {"ok": True, "answers": {qid: {"choice": "not_useful", "probabilities": {"useful": 0.1}}}}

    out = rank.rank_links(FakeBackend(response=respond), "q", results, deadline=None)
    assert len(calls) == 2
    # s0 scored below the floor; s1..s3 unscored -> fail-open order
    assert [r.url for r in out.kept] == ["https://s1.example/", "https://s2.example/"]


def test_deadline_bounds_the_ranking():
    results = _results(4)
    clock = [0.0]
    backend = FakeBackend(response=lambda s, q: clock.__setitem__(0, clock[0] + 1.0) or {
        "ok": True, "answers": {next(iter(q)): {"choice": "useful", "probabilities": {"useful": 0.9}}}
    })
    out = rank.rank_links(backend, "q", results, deadline=1.5, clock=lambda: clock[0])
    assert len(backend.calls) == 2
    assert all(c["timeout_s"] <= 1.5 for c in backend.calls)
    assert len(out.kept) == 2


def test_errors_and_garbage_answers_are_unscored():
    results = _results(3)
    for response in ({"ok": False, "error": "x"}, {"ok": True, "answers": {}}, "junk"):
        out = rank.rank_links(FakeBackend(response=response), "q", results, deadline=None)
        assert out.kept == results[:2]


def test_probability_from_confidence_when_no_probabilities():
    def respond(state, questions):
        (qid,) = questions
        return {"ok": True, "answers": {qid: {"choice": "not_useful", "answer_confidence": 0.8}}}

    out = rank.rank_chunks(FakeBackend(response=respond), "q", [("a", "x")], deadline=None)
    assert out.kept == []  # p(useful) = 0.2 < floor


def test_chunk_ranking_keeps_top_three_in_score_order():
    chunks = [("d.example", f"#{i} text") for i in range(5)]
    out = rank.rank_chunks(_scored([0.4, 0.95, 0.1, 0.8, 0.6]), "q", chunks, deadline=None)
    assert [c[1] for c in out.kept] == ["#1 text", "#3 text", "#4 text"]


def test_states_carry_web_text_and_never_the_raw_wrapper():
    backend = _scored([0.9])
    rank.rank_chunks(backend, f"{SECRET} pregunta", [("d.example", "#0 the passage")], deadline=None)
    call = backend.calls[0]
    assert call["state"] == "#0 the passage"
    assert SECRET in call["questions"]["web_chunk"]["instructions"]


def test_question_text_is_capped_and_cleaned():
    backend = _scored([0.9])
    rank.rank_chunks(backend, "q\x00" + "x" * 1000, [("d", "#0 p")], deadline=None)
    ins = backend.calls[0]["questions"]["web_chunk"]["instructions"]
    assert "\x00" not in ins
    assert len(ins) < 600


# --- lexical prefilter (web-lookup-polish-20261010 T6) --------------------------------


def test_prefilter_keeps_the_best_overlap_in_original_order():
    texts = [
        "Cookies y aviso legal del sitio",
        "Silksong tiene fecha: sale el 4 de septiembre",
        "Menu principal, iniciar sesion",
        "Hollow Knight Silksong en Switch",
        "Otros juegos de plataformas",
        "La FECHA de salida de Silksong, confirmada",
    ]
    kept = rank.lexical_prefilter("cuándo sale silksong fecha", list(range(6)), texts, keep=3)
    assert kept == [1, 3, 5]


def test_prefilter_ties_and_no_overlap_keep_the_first_in_order():
    assert rank.lexical_prefilter("zzz", list("abcde"), ["x"] * 5, keep=3) == ["a", "b", "c"]
    assert rank.lexical_prefilter("", list("abcde"), ["x"] * 5, keep=2) == ["a", "b"]


def test_prefilter_is_a_no_op_under_the_cap():
    items = ["a", "b"]
    assert rank.lexical_prefilter("q", items, ["x", "y"], keep=3) == items


def test_prefilter_ignores_accents_case_and_stopwords():
    texts = ["de la que el en", "Pokémon Leyendas"]
    assert rank.lexical_prefilter("de la que pokemon", [0, 1], texts, keep=1) == [1]


# --- trigger vocabulary & query cleanup (web-trigger-vocab-20261010 T1 & T2) ---


@pytest.mark.parametrize(
    "text",
    [
        # Findings table turns from live log
        "Puedes buscar que hizo openai con los problemas del milenio",
        "buscar en la web que hizo openai con los problemas del milenio",
        "puedes buscar en la web que dicen los matematicos",
        "buscar que es bueno programar en 2026",
        "que dice la gente en la web sobre la programacion",
        "que dice la web acerca de franguh, puedes buscarlo",
        "buscar en la web en las versiones mas recientes de minecraft que capa recomiendan",
        "pero que dice la web sobre minar diamantes en minecraft que capa recomiendan",
        "busca en la web en pc cual es la plataforma recomendada",
        "busqueda web de quien es tibo o el dios de los resets de openai",
        "busca en la web que modelos tiene anthropic",
        "busqueda en la web de que empreza o modelo de IA hackeo huggin face",
        "puedes buscar en la web que empresa de IA hackeo a huggin face",
        # Polite leads
        "podrías buscar qué hora es en Tokio",
        "me puedes buscar información sobre Python 3.14",
        "ayúdame a buscar en la web sobre astrofísica",
        "quiero que busques en internet el horario del partido",
        "puedes tú buscarlo en google",
        "Kira, podrías investigar qué pasó con el cohete",
        "ayudame a investigar qué es la teoría de cuerdas",
        # Infinitives & enclitics
        "buscar sobre el telescopio James Webb",
        "investigar qué es la teoría de cuerdas",
        "buscarlo en internet",
        "investigarlo en la web",
        "buscar noticias de última hora",
        # Noun forms
        "búsqueda web de novedades de Linux",
        "busqueda en internet sobre el clima de hoy",
        "búsqueda en google de tutoriales de rust",
        "búsqueda en la web de cómo arreglar este error",
        # Web as source
        "qué dice la web sobre este problema",
        "qué dicen en internet acerca del lanzamiento",
        "qué opina reddit sobre el nuevo anime",
        "qué se dice en la red sobre la actualización",
        "qué comentan en la web sobre el tema",
        # Trailing ask with topic/medium
        "la fecha del parche, puedes buscarlo",
        "sobre el nuevo procesador, puedes buscarlo en la web",
    ],
)
def test_rule_matches_vocab_expansion_findings_and_patterns(text):
    assert trigger.web_rule(text) == "search"


@pytest.mark.parametrize(
    "text",
    [
        "busca la llave del cofre",
        "puedes buscar la llave",
        "que dice el NPC",
        "la web de mi proyecto no carga",
        "busco trabajo",
        "estoy buscando",
        "¿cuándo sale Silksong?",
        "que dice mi github de franguh",
        "ayúdame a limpiar el cuarto",
        "quiero que vengas aquí",
        "podrías pasarme la sal",
        "puedes buscarlo",
    ],
)
def test_rule_keeps_negative_cases_out(text):
    assert trigger.web_rule(text) == "no_search"


@pytest.mark.parametrize(
    "text,expected",
    [
        # Findings table turns with expected queries
        (
            "Puedes buscar que hizo openai con los problemas del milenio",
            "que hizo openai con los problemas del milenio",
        ),
        (
            "buscar que es bueno programar en 2026",
            "que es bueno programar en 2026",
        ),
        (
            "que dice la gente en la web sobre la programacion",
            "la programacion",
        ),
        (
            "que dice la web acerca de franguh, puedes buscarlo",
            "acerca de franguh",
        ),
        (
            "pero que dice la web sobre minar diamantes en minecraft que capa recomiendan",
            "minar diamantes en minecraft que capa recomiendan",
        ),
        (
            "busqueda web de quien es tibo o el dios de los resets de openai",
            "quien es tibo o el dios de los resets de openai",
        ),
        (
            "busqueda en la web de que empreza o modelo de IA hackeo huggin face",
            "que empreza o modelo de IA hackeo huggin face",
        ),
        (
            "puedes buscar en la web que empresa de IA hackeo a huggin face",
            "que empresa de IA hackeo a huggin face",
        ),
        # Polite leads & command stripping
        (
            "puedes buscar en la web que dicen los matematicos",
            "que dicen los matematicos",
        ),
        (
            "oye puedes buscar en internet la receta de paella",
            "la receta de paella",
        ),
        (
            "me puedes buscar cuándo abren las tiendas",
            "cuándo abren las tiendas",
        ),
        (
            "ayúdame a buscar noticias de IA",
            "noticias de IA",
        ),
        (
            "buscar sobre el telescopio James Webb",
            "el telescopio James Webb",
        ),
        # Trailing ask stripping
        (
            "qué opinan de Silksong, puedes buscarlo",
            "qué opinan de Silksong",
        ),
        (
            "la historia de Doom, podrías buscarlo en internet",
            "la historia de Doom",
        ),
        # Leading filler stripping
        (
            "y qué dice la web sobre el nuevo juego",
            "el nuevo juego",
        ),
    ],
)
def test_search_query_cleanup_findings_and_patterns(text, expected):
    assert trigger.search_query(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "puedes buscarlo en internet",
        "búsqueda web",
        "qué dice la web",
        "puedes buscar en la web por favor",
    ],
)
def test_search_query_empty_on_bare_triggers_and_leads(text):
    assert trigger.web_rule(text) == "search"
    assert trigger.search_query(text) == ""



# --- web-query-followup-and-retry T1: recency cue and bare search commands ---


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Puedes buscar opciones que sean más recientes este año.", "search"),
        ("busca modelos más recientes", "search"),
        ("buscar noticias esta semana", "search"),
        ("busca la llave", "no_search"),
        ("busca el cofre nuevo", "no_search"),
        ("busca la llave de hoy", "no_search"),
        ("búscalo", "no_search"),
        ("haz la búsqueda", "no_search"),
        ("Ok, realiza la búsqueda en la web.", "search"),
    ],
)
def test_recency_cue_and_bare_commands_in_the_rule(text, expected):
    assert trigger.web_rule(text) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("búscalo", True),
        ("sí, búscalo.", True),
        ("dale, búscalo ahora", True),
        ("busca eso", True),
        ("haz la búsqueda", True),
        ("realiza una búsqueda", True),
        ("Ok, realiza la búsqueda en la web.", True),
        ("búscalo en internet por favor", True),
        ("busca la llave", False),
        ("haz una búsqueda del tesoro en el mapa", False),
        ("hazlo", False),
        ("puedes buscar opciones recientes", False),
    ],
)
def test_is_bare_search_command(text, expected):
    assert trigger.is_bare_search_command(text) is expected


@pytest.mark.parametrize(
    "text",
    ["Ok, realiza la búsqueda en la web.", "haz una búsqueda en internet", "sí, búscalo"],
)
def test_bare_commands_leave_an_empty_query(text):
    assert trigger.search_query(text) == ""


# --- O2: near-miss counters for a later zone decision (counters only) ------------------


@pytest.fixture
def near_miss_counters():
    from opencohost.core.decision import consumers

    consumers.reset_web_near_miss()
    yield consumers
    consumers.reset_web_near_miss()


@pytest.mark.parametrize("conf, bucket", [(0.55, "50_70"), (0.75, "70_80"), (0.85, "80_90")])
def test_below_threshold_laya_search_on_a_rule_miss_is_counted_by_bucket(near_miss_counters, caplog, conf, bucket):
    import logging

    caplog.set_level(logging.INFO, logger="OpenCohost")
    assert apply_web_decision(_turn("search", conf=conf), "no_search") == "no_search"
    assert near_miss_counters.web_near_miss_counts() == {bucket: 1}
    (line,) = [r.getMessage() for r in caplog.records if "laya_near_miss" in r.getMessage()]
    assert line == f"[WEB] laya_near_miss bucket={bucket} count=1"


def test_only_a_low_confidence_search_against_a_rule_no_search_is_a_near_miss(near_miss_counters):
    apply_web_decision(_turn("search", conf=0.97), "no_search")  # acted, not a miss
    apply_web_decision(_turn("search", conf=0.85), "search")  # rule already search
    apply_web_decision(_turn("no_search", conf=0.85), "no_search")  # agrees
    apply_web_decision(_turn("search", conf=0.30), "no_search")  # too weak to count
    apply_web_decision(None, "no_search")
    assert near_miss_counters.web_near_miss_counts() == {}


# --- judge round 1 (F6, F11) -----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "busca el lanzador de granadas",
        "busca los bichos que salieron del suelo",
        "busca la misión de esta semana",
        "busca el cofre de este año",
    ],
)
def test_weak_recency_words_alone_do_not_make_a_search(text):
    assert trigger.web_rule(text) == "no_search"


@pytest.mark.parametrize(
    "text",
    [
        "busca juegos lanzados este año",
        "busca noticias que salieron esta semana",
        "busca en internet lo que salieron esta semana",
        "busca modelos que salieron este año",
    ],
)
def test_weak_recency_words_with_an_information_noun_or_medium_search(text):
    assert trigger.web_rule(text) == "search"


def test_web_rule_is_unchanged_by_the_query_only_filler_words():
    assert trigger.web_rule("un roguelike, búscalo") == "search"
    assert trigger.web_rule("y busca en la red películas de 2026") == "search"


@pytest.mark.parametrize(
    "text, expected_query",
    [
        (
            "can you search in google what's the last model from openai in November",
            "what's the last model from openai in November",
        ),
        (
            "search in web what's the last family of models from anthropic",
            "what's the last family of models from anthropic",
        ),
        ("search in browser python 3.13 features", "python 3.13 features"),
        ("search on google who won the oscars", "who won the oscars"),
        ("look out at web what anthropic released", "what anthropic released"),
        ("look on the web for elden ring dlc", "elden ring dlc"),
        ("look in the browser nvidia stock price", "nvidia stock price"),
        ("websearch openai gpt release", "openai gpt release"),
        ("web search anthropic claude models", "anthropic claude models"),
        ("quick look at web rust async changes", "rust async changes"),
        ("quick search on google best microphone", "best microphone"),
        ("could you please look up silksong release date online", "silksong release date"),
        ("would you find out online who won", "who won"),
        ("please search online for tailwind v4 docs", "tailwind v4 docs"),
    ],
)
def test_english_web_triggers_and_clean_queries(text, expected_query):
    assert trigger.web_rule(text) == "search"
    assert trigger.search_query(text) == expected_query


@pytest.mark.parametrize(
    "text",
    [
        "she is on the web",
        "i searched my memory",
        "browser settings",
        "quick look at the code",
        "check if the server is online",
    ],
)
def test_english_non_search_phrases_stay_no_search(text):
    assert trigger.web_rule(text) == "no_search"


@pytest.mark.parametrize(
    "text",
    [
        "google it",
        "google that",
        "look it up",
        "look that up",
        "search it",
        "search that",
        "websearch",
        "web search",
        "search again",
        "look again",
        "check online",
        "can you look it up please",
        "could you google that again",
        "would you check online",
    ],
)
def test_english_bare_followup_commands(text):
    assert trigger.is_bare_search_command(text) is True
    assert trigger.search_query(text) == ""


def test_rank_prefilter_ignores_english_command_stopwords():
    texts = [
        "how to search online in your browser please",
        "OpenAI November release announcement",
        "can you quickly check Google settings",
    ]
    kept = rank.lexical_prefilter(
        "can you search in browser quickly for OpenAI November",
        list(range(3)),
        texts,
        keep=1,
    )
    assert kept == [1]

