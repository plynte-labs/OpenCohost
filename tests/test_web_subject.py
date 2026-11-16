"""Subject resolution and query compaction (web-query-followup-and-retry)."""
from __future__ import annotations

import pytest

from opencohost.core.web import trigger

# Characterization of short queries on base 9928119: after command stripping
# they have 8 words or fewer and must stay byte-identical (design D2).
_GOLDEN = [
    ('busca en internet qu\xe9 es un roguelike', 'search', 'qu\xe9 es un roguelike'),
    ('Kira, busca en la web cu\xe1ndo sale Silksong', 'search', 'cu\xe1ndo sale Silksong'),
    ('b\xfascalo en internet por favor', 'search', ''),
    ('busca noticias de nvidia', 'search', 'noticias de nvidia'),
    ('cuanto recaudo anthropic esta semana en su IPO', 'no_search', 'cuanto recaudo anthropic esta semana en su IPO'),
    ('busca qui\xe9n gan\xf3 el mundial', 'search', 'qui\xe9n gan\xf3 el mundial'),
    ('googlea el clima en Madrid', 'search', 'el clima en Madrid'),
    ('search the web for rust async', 'search', 'rust async'),
    ('look it up online', 'search', ''),
    ('google that', 'search', ''),
    ('qu\xe9 hay de nuevo en Elden Ring', 'search', 'qu\xe9 hay de nuevo en Elden Ring'),
    ('\xfaltimas noticias sobre OpenAI', 'search', '\xfaltimas noticias sobre OpenAI'),
    ('busca la llave', 'no_search', 'busca la llave'),
    ('busca el cofre nuevo', 'no_search', 'busca el cofre nuevo'),
    ('busca si Hollow Knight tiene dlc', 'search', 'si Hollow Knight tiene dlc'),
    ('busca las mejores tendencias de 2025', 'search', 'las mejores tendencias de 2025'),
    ('investiga c\xf3mo funciona un transformer', 'search', 'c\xf3mo funciona un transformer'),
    ('Oye Kira busca en google el precio del bitcoin', 'search', 'el precio del bitcoin'),
    ('b\xfasqueda web de modelos de lenguaje', 'search', 'modelos de lenguaje'),
    ('qu\xe9 dice la web sobre el lag', 'search', 'el lag'),
    ('buscar recomendaciones de juegos indie', 'no_search', 'buscar recomendaciones de juegos indie'),
    ('busca en reddit juegos como celeste', 'search', 'juegos como celeste'),
    ('hola kira c\xf3mo est\xe1s', 'no_search', 'hola kira c\xf3mo est\xe1s'),
    ('cu\xe1ndo sale Silksong', 'no_search', 'cu\xe1ndo sale Silksong'),
    ('mira en internet el horario de la tienda', 'search', 'el horario de la tienda'),
    ('pero busca en internet tutorial de blender', 'search', 'tutorial de blender'),
    ('busca modelos de inteligencia artificial', 'search', 'modelos de inteligencia artificial'),
    ('busca informaci\xf3n de la nueva consola', 'search', 'informaci\xf3n de la nueva consola'),
    ("what's new with Python", 'search', "what's new with Python"),
    ('latest news about SpaceX', 'search', 'latest news about SpaceX'),
    ('ok busca en internet qu\xe9 tiempo hace hoy', 'search', 'qu\xe9 tiempo hace hoy'),
    ('y busca en la red pel\xedculas de 2026', 'search', 'pel\xedculas de 2026'),
    ('busca en internet receta de pizza', 'search', 'receta de pizza'),
    ('Kira busca en internet cu\xe1ntos a\xf1os tiene Messi', 'search', 'cu\xe1ntos a\xf1os tiene Messi'),
    ('puedes buscar en la web el mejor monitor', 'search', 'el mejor monitor'),
    ('busca en internet c\xf3mo arreglar el error 404', 'search', 'c\xf3mo arreglar el error 404'),
    ('dime las noticias de hoy de M\xe9xico', 'search', 'dime las noticias de hoy de M\xe9xico'),
]


@pytest.mark.parametrize("text, rule, query", _GOLDEN)
def test_short_queries_are_byte_identical_to_the_base(text, rule, query):
    assert trigger.web_rule(text) == rule
    assert trigger.search_query(text) == query


# --- T2: query compaction (D2) ---

_LONG = (
    "Quiero que busques un modelo de inteligencia artificial en la web. "
    "Que sea lo suficientemente capaz. Como para realizar documentos. Complejos."
)


@pytest.mark.parametrize(
    "text, expected",
    [
        (_LONG, "modelo de inteligencia artificial capaz de realizar documentos complejos"),
        ("Puedes buscar opciones que sean más recientes este año.", "recientes 2026"),
        (
            "Puedes buscar que modelos de inteligencia artificial. Salieron este año en la web.",
            "que modelos de inteligencia artificial salieron 2026",
        ),
        ("busca en internet qué es un roguelike", "qué es un roguelike"),
        ("cuanto recaudo anthropic esta semana en su IPO", "cuanto recaudo anthropic esta semana en su IPO"),
        ("busca noticias recientes de nvidia", "noticias recientes de nvidia 2026"),
    ],
)
def test_search_query_compaction_table(text, expected):
    assert trigger.search_query(text, year=2026) == expected


def test_long_dictation_is_capped_and_keeps_recency():
    text = (
        "busca en internet " + " ".join(f"palabra{i}" for i in range(28)) + " recientes este año"
    )
    query = trigger.search_query(text, year=2026)
    assert len(query.split()) <= 14
    assert len(query) <= 200
    assert "recientes" in query and "2026" in query


@pytest.mark.parametrize(
    "split, joined",
    [
        (_LONG, _LONG.replace(". ", " ").rstrip(".")),
        (
            "Puedes buscar que modelos de inteligencia artificial. Salieron este año en la web.",
            "Puedes buscar que modelos de inteligencia artificial salieron este año en la web",
        ),
    ],
)
def test_stt_split_and_unsplit_give_the_same_query(split, joined):
    assert trigger.search_query(split, year=2026) == trigger.search_query(joined, year=2026)


@pytest.mark.parametrize(
    "text",
    [
        _LONG,
        "Puedes buscar opciones que sean más recientes este año.",
        "puedes buscar en internet quiero que sea lo suficientemente bueno para jugar con amigos ahora",
    ],
)
def test_compacted_queries_carry_no_scaffolding(text):
    words = set(trigger.search_query(text, year=2026).lower().split())
    assert not words & {"web", "internet", "quiero", "puedes", "suficientemente"}


# --- T3: subject ledger and resolver (D1) ---

from opencohost.core.web import subject as subj  # noqa: E402

_BASE = "modelo de inteligencia artificial capaz de realizar documentos complejos"


def _turn(ts, query, *, search=False, refine=False):
    return subj.LedgerTurn(ts, query, search, refine)


def _resolve(turns, *, now=None, ttl=600.0):
    now = (turns[-1].ts + 5.0) if now is None and turns else (now or 0.0)
    return subj.resolve_followup_subject(turns, now=now, ttl=ttl, year=2026)


def test_resolver_merges_a_refinement_chain_into_its_base():
    turns = [_turn(0.0, _BASE, search=True), _turn(30.0, "recientes 2026", search=True, refine=True)]
    assert _resolve(turns) == f"{_BASE} recientes 2026"


def test_resolver_falls_back_to_the_latest_owner_question():
    assert _resolve([_turn(0.0, "cuando sale silksong")]) == "cuando sale silksong"


def test_resolver_with_no_turns_is_empty():
    assert _resolve([], now=10.0) == ""


def test_resolver_ignores_turns_past_the_ttl():
    assert _resolve([_turn(0.0, "modelos de ia", search=True)], now=601.0) == ""


def test_resolver_does_not_resurrect_a_search_behind_a_chat_turn():
    turns = [_turn(0.0, "nvidia drivers", search=True), _turn(20.0, "hola que tal")]
    assert _resolve(turns) == ""


def test_resolver_needs_a_base_behind_a_refinement():
    assert _resolve([_turn(0.0, "recientes 2026", search=True, refine=True)]) == ""


def test_resolver_adds_the_year_to_a_cue_without_one():
    turns = [_turn(0.0, "silksong dlc novedades", search=True), _turn(9.0, "recientes", search=True, refine=True)]
    assert _resolve(turns) == "silksong dlc novedades recientes 2026"


def test_resolver_does_not_repeat_cues_already_in_the_base():
    turns = [
        _turn(0.0, "modelos de ia recientes 2026", search=True),
        _turn(9.0, "recientes 2026", search=True, refine=True),
    ]
    assert _resolve(turns) == "modelos de ia recientes 2026"


@pytest.mark.parametrize(
    "query, expected",
    [
        ("recientes 2026", True),
        ("recientes", True),
        ("opciones recientes", True),
        ("noticias de nvidia", False),
        ("nvidia drivers recientes", False),
        ("silksong", False),
    ],
)
def test_is_refinement(query, expected):
    assert subj.is_refinement(query) is expected


# --- judge round 1 ---------------------------------------------------------------------
import itertools  # noqa: E402


@pytest.mark.parametrize(
    "text, expected",
    [
        ("busca en internet los juegos más vendidos del año pasado de steam en el mundo", "los juegos más vendidos del año pasado de steam en el mundo"),
        ("busca los últimos juegos del año pasado", "los últimos juegos del año pasado"),
        ("busca películas recientes del año 2010", "películas recientes del año 2010"),
        ("busca películas recientes del año que viene", "películas recientes del año que viene"),
        ("busca noticias recientes de este año pasado", "noticias recientes de este año pasado"),
        ("busca las acciones de Google este año", "las acciones de google 2026"),
        ("busca noticias recientes de Google en internet", "noticias recientes de google 2026"),
        ("busca los lanzamientos recientes de agosto", "los lanzamientos recientes de agosto"),
        ("busca los juegos recientes de dos mil veinte", "los juegos recientes de dos mil veinte"),
    ],
)
def test_year_and_media_rules_keep_real_subjects(text, expected):
    assert trigger.search_query(text, year=2026) == expected


def test_del_ano_pasado_is_not_a_recency_cue():
    assert not subj.has_recency_cue("los juegos más vendidos del año pasado")
    assert subj.has_recency_cue("juegos de este año")


@pytest.mark.parametrize(
    "query, expected",
    [
        ("el clima de hoy", False),
        ("silksong recientes", False),
        ("clima recientes 2026", False),
        ("opciones recientes", True),
        ("noticias recientes", True),
        ("modelos más recientes 2026", True),
        ("recientes 2026", True),
    ],
)
def test_refinement_needs_generic_nouns_only(query, expected):
    assert subj.is_refinement(query) is expected


def test_strict_resolution_ignores_non_search_turns():
    turns = [_turn(0.0, "mi hermana laura se casa en toledo")]
    assert subj.resolve_followup_subject(turns, now=5.0, ttl=600.0, year=2026, strict=True) == ""
    chain = [_turn(0.0, _BASE, search=True)]
    assert subj.resolve_followup_subject(chain, now=5.0, ttl=600.0, year=2026, strict=True) == _BASE


def test_plain_owner_turn_fallback_expires_after_45_seconds():
    plain = [_turn(0.0, "cuando sale silksong")]
    assert subj.resolve_followup_subject(plain, now=40.0, ttl=600.0, year=2026) == "cuando sale silksong"
    assert subj.resolve_followup_subject(plain, now=50.0, ttl=600.0, year=2026) == ""
    searched = [_turn(0.0, _BASE, search=True)]
    assert subj.resolve_followup_subject(searched, now=500.0, ttl=600.0, year=2026) == _BASE


_ALTERNATIVES = (
    ["búscalo", "búscala", "buscarlo", "buscarla", "investígalo", "busca eso", "búscame eso"]
    + [
        f"{verb} {art} búsqueda"
        for verb in ("haz", "hazme", "realiza", "realízame", "ejecuta", "lanza", "hacer", "realizar")
        for art in ("la", "una", "esa", "otra", "de nuevo la")
    ]
)
_TAILS = ("", "ahora", "ya", "de nuevo", "otra vez", "por favor", "en internet", "en la web por favor")


def test_every_bare_alternative_with_every_tail_is_bare():
    bad = [
        f"{a} {t}".strip()
        for a, t in itertools.product(_ALTERNATIVES, _TAILS)
        if not trigger.is_bare_search_command(f"{a} {t}".strip())
    ]
    assert bad == []


def test_query_only_fillers_do_not_leak_into_the_rule_filler_set():
    assert "una" not in trigger._FILLER and "haz" not in trigger._FILLER
    assert "una" in trigger._QUERY_FILLER and "haz" in trigger._QUERY_FILLER


@pytest.mark.parametrize(
    "text, expected",
    [
        ("can you search on google for models released this year", "models released 2026"),
        ("can you search in browser for options that are more recent this year", "recent 2026"),
    ],
)
def test_english_search_query_compaction(text, expected):
    assert trigger.search_query(text, year=2026) == expected


@pytest.mark.parametrize(
    "query, expected",
    [
        ("more recent options this year", True),
        ("latest models", True),
        ("recent 2026", True),
        ("openai latest models", False),
    ],
)
def test_english_is_refinement(query, expected):
    assert subj.is_refinement(query) is expected


def test_english_followup_refinement_chain():
    turns = [
        _turn(0.0, "openai models", search=True),
        _turn(10.0, "more recent 2026", search=True, refine=True),
    ]
    assert _resolve(turns) == "openai models recent 2026"

