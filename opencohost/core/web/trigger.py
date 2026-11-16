"""The ``web`` decision's rule: an explicit request to look something up
online (web-fetch-laya-20261009).

Pure and deterministic. The rule fires only on explicit phrasing, in Spanish
or English:

  * commands: "busca/búscalo/búscame/buscar en internet|la web|la red|google",
    "investiga/mira/consulta en internet", "googlea", "search the web/online/
    internet/google (for)", "look (it) up online|on the web|on google",
    "google it/that/for";
  * a bare imperative at the start of a sentence (or right after "Kira,"):
    "busca/búscame/buscá/investiga" followed by a medium ("web", "en
    internet", "en reddit"...), "sobre", "que", a question word ("quién",
    "cuándo", "cuál", "cómo"...) or "noticias/información". "busca la llave",
    "busca la forma de", "buscas", "busco" and "estoy buscando" stay out;
  * news: "qué hay de nuevo", "(últimas) noticias de|del|sobre", "qué
    noticias", "último post/tweet/video", "latest/any news on|about",
    "what's new with|in|on|about".

A bare "¿cuándo sale X?" is not a request to search and stays ``no_search``.

``search_query`` builds what is sent to the search engine: the owner's text
with the command phrase (imperative + medium; a following "que" stays)
and a leading "Kira," removed (a news phrase is kept, it is part of the
topic). When nothing but trigger and filler words is left (a bare
"búscalo en internet"), it returns "" and the caller decides the subject
(web-lookup-polish-20261010 T2). Nothing else leaves the machine.
"""
from __future__ import annotations

import re
import unicodedata

from opencohost.core.web import subject as _subject
from opencohost.core.web.extract import clean_text

SEARCH = "search"
NO_SEARCH = "no_search"
QUERY_MAX_CHARS = 200

# STT mishearings of "búscala/búscalo" (live 2026-10-17: "púsquela, púsquela").
_STT_VERBS = r"(?:busquela|busquelo|pusquela|pusquelo|puscala|puscalo)"
_STT_VERB_RE = re.compile(r"\b" + _STT_VERBS + r"\b", re.IGNORECASE)

_MEDIUM = r"(?:internet|la\s+web|la\s+red|web|google|reddit|linea|duckduckgo|browser|the\s+web|the\s+internet|the\s+browser)"
_EN_PREP_MEDIUM = r"(?:in|on|at)\s+(?:the\s+)?(?:web|internet|browser|google|reddit|duckduckgo)"
_EN_MEDIUM = r"(?:online|(?:(?:in|on|at)\s+)?(?:the\s+)?(?:web|internet|browser|google|reddit|duckduckgo))"
_COMMANDS = re.compile(
    r"\b(?:busca|buscar|buscame|buscalo|buscala|buscalos|busquen|buscarlo|buscarla|investiga|investigar|investigarlo|mira|consulta|" + _STT_VERBS + r")"
    r"\s+(?:(?:eso|esto|algo)\s+)?(?:en|por)\s+" + _MEDIUM + r"\b(?:\s+(?:sobre|acerca de|por))?"
    r"|\bgooglea(?:lo|me|la)?\b"
    r"|\b(?:(?:do|run|make)\s+(?:a\s+)?)?(?:quick\s+)?search\s+(?:(?:it|that|this)\s+)?" + _EN_MEDIUM + r"(?:\s+(?:for|about))?\b"
    r"|\b(?:quick\s+)?look\s+(?:(?:it|this|that|them)\s+)?up\s+" + _EN_MEDIUM + r"(?:\s+(?:for|about))?\b"
    r"|\b(?:(?:take|have)\s+a\s+)?(?:quick\s+)?look(?:\s+out)?\s+" + _EN_PREP_MEDIUM + r"(?:\s+(?:for|about))?\b"
    r"|\b(?:(?:do|run|make)\s+(?:a\s+)?)?(?:quick\s+)?web\s*search(?:\s+(?:on\s+(?:google|reddit|the\s+web|the\s+internet|the\s+browser)\s+)?(?:for|about)?)?\b"
    r"|\bfind(?:\s+out)?\s+(?:(?:it|this|that)\s+)?online(?:\s+(?:for|about))?\b"
    r"|\bcheck\s+(?:(?:it|that|this)\s+)?(?:online|(?:on|in)\s+(?:the\s+)?(?:web|internet|browser|google))(?:\s+(?:for|about))?\b"
    r"|\bgoogle\s+(?:it|that|this|for)\b"
)
_NEWS = re.compile(
    r"\bque hay de nuevo\b"
    r"|\bnoticias\s+(?:de|del|sobre|acerca|recientes|actuales|hoy)\b"
    r"|\b(?:que|cuales)\s+(?:son\s+las\s+)?(?:ultimas\s+)?noticias\b"
    r"|\bultimas\s+noticias\b"
    r"|\bultim[oa]s?\s+(?:posts?|tweets?|tuits?|publicacion(?:es)?|videos?)\b"
    r"|\blatest\s+(?:posts?|tweets?|videos?)\b"
    r"|\b(?:latest|any)\s+news\s+(?:on|about|from|for)\b"
    r"|\bwhat'?s\s+new\s+(?:with|in|on|about)\b"
)
_WAKE = re.compile(r"^\s*(?:oye\s+|hey\s+)?kira\b[\s,.:;!¡?¿]*")
_EDGE = " \t¿?¡!.,;:\"'«»"
# Sentence-initial imperative: matched at a sentence start, after optional
# filler and the wake word. Its span is what search_query strips.
_SENTENCE_END = re.compile(r"[.!?¡¿;]+\s*")
_LEAD = re.compile(
    r"(?:\s*(?:oye|hey|ok|okay|vale|bueno|ahora|entonces|y|pero|pues|kira|por favor|porfa"
    r"|please|pls|can\s+you|could\s+you|would\s+you|will\s+you|help\s+me"
    r"|puedes(?:\s+tu)?|podrias|podras|me\s+(?:puedes|podrias)|ayudame\s+a|quiero\s+que(?:\s+me)?)\b[\s,.:;!¡?¿]*)*\s*"
)
_IMPERATIVE = re.compile(
    r"(?:busca|buscame|buscanos|buscalo|buscala|buscalos|busquen|busques|" + _STT_VERBS + r"|"
    r"buscar|buscarlo|buscarla|buscarme|"
    r"investiga|investigame|investigalo|investigar|investigarlo)\b"
    r"(?P<medium>\s+(?:(?:en|por)\s+)?(?:internet|la\s+web|la\s+red|web|google|reddit|duckduckgo)\b)?"
    r"(?P<about>\s+(?:sobre|acerca de)\b)?"
    r"(?P<que>\s+que\b)?"
)
# K3: "busca/buscar si <algo> tiene|existe|sale|hay algo..." is an information
# query; "busca si hay enemigos" (in-game action) is not, so a bare "si" never
# triggers: the clause must ask about a fact (has/exists/released/news).
_SI_INFO = re.compile(
    r"\s+si\s+(?:\w+\s+){0,4}?(?:tiene|tienen|existe|existen|salio|sale|lanzo|lanzaron|anuncio"
    r"|hay\s+(?:algo|noticias|novedades|informacion|alguna|algun|nuevo))\b"
)
# K3: "buscarme si hay algo ... en la web": a few words may sit between the verb
# and the medium. Only unambiguous media (not "la red"/"linea").
_SPLIT_MEDIUM = re.compile(
    r"\b(?P<verb>buscarme|buscame|buscanos|busca|buscar|" + _STT_VERBS + r")\b(?P<mid>\s+(?:\w+\s+){0,8}?)"
    r"(?P<med>(?:en|por)\s+(?:internet|la\s+web|web|google))\b"
)
_EN_SPLIT_MEDIUM = re.compile(
    r"\b(?P<verb>(?:quick\s+)?(?:look\s+(?:(?:it|this|that|them)\s+)?up|find\s+out|search))\b"
    r"(?P<mid>\s+(?:[\w'’-]+\s+){1,8}?)"
    r"(?P<med>online|(?:in|on|at)\s+(?:the\s+)?(?:web|internet|browser|google))\b"
)
# A bare "buscar <topic>" is a lookup when the next few words name an
# information topic (trend, recommendation, recency, a date, AI models...).
# Game objects ("busca la espada en el cofre") carry none of these cues.
_TOPIC_CUE = re.compile(
    r"\b(?:tendencias?|recomiend\w*|recomendad\w*|actualmente|hoy en dia|ultim\w+|novedad\w*"
    r"|mejores|lo mejor|nuevos|nuevas|inteligencia artificial|modelos de|agentes de"
    r"|recientes|lanzad[oa]s?"
    r"|enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre"
    r"|(?:19|20)\d\d|dos mil)\b"
)
_TOPIC_WINDOW = re.compile(r"(?:\s+\S+){1,9}")
_FOLLOW = re.compile(
    r"\s+(?:quien|quienes|cuando|cual|cuales|cuanto|cuanta|cuantos|cuantas|como|por que"
    r"|noticias|informacion|info|datos|ultim[oa]s?)\b"
)

# Noun forms (web-trigger-vocab-20261010 T1): "búsqueda web de...", "búsqueda en internet..."
_NOUN = re.compile(
    r"\b(?:busqueda|busquedas)\s+"
    r"(?:web|(?:en\s+)?(?:internet|la\s+web|la\s+red|google|reddit|duckduckgo))\b"
    r"(?:\s+(?:de|sobre))?",
)

# Web as source (web-trigger-vocab-20261010 T1): "qué dice la web sobre...", "qué opinan en internet..."
_WEB_SOURCE = re.compile(
    r"\bque\s+(?:dice|dicen|opina|opinan|se\s+dice|se\s+comenta|comentan)\s+"
    r"(?:(?:la\s+gente\s+)?(?:en\s+)?|en\s+)"
    r"(?:la\s+web|internet|la\s+red|reddit|google)\b"
    r"(?:\s+sobre)?",
)

# Trailing ask (web-trigger-vocab-20261010 T1): "..., puedes buscarlo"
_TRAILING_ASK = re.compile(
    r"[,]?\s*(?:(?:puedes|podrias)\s+)?(?:buscarlo|buscarla|buscalo|buscala|investigarlo|investigalo|" + _STT_VERBS + r")"
    r"(?:\s+(?:en|por)\s+" + _MEDIUM + r")?"
    r"(?:\s+(?:por favor|porfa))?[\s.?!]*$",
    re.IGNORECASE,
)

# Leading fillers to strip in query cleanup (web-trigger-vocab-20261010 T2): "pero", "y", "oye", etc.
_LEADING_FILLER = re.compile(
    r"^\s*(?:(?:pero|pues|y|oye|hey|ok|okay|vale|bueno|ahora|entonces|por favor|porfa|puedes|podrias"
    r"|please|pls|can\s+you|could\s+you|would\s+you|will\s+you)\b[\s,.:;!¡?¿]*)+",
    re.IGNORECASE,
)

# Words that carry no subject. A query made only of these (and trigger words)
# is a bare follow-up such as "búscalo en internet por favor".
_FILLER = frozenset(
    "por favor porfa porfis eso esto lo la el algo si ok okay vale dale ya kira oye hey ahora "
    "tambien mejor entonces bueno pues y pero en de web internet google red reddit busca buscalo buscala "
    "busquela busquelo pusquela pusquelo puscala puscalo buscalos busquen buscar buscarlo buscarla investiga investigalo investigarla investigar "
    "investigarlo busqueda busquedas dice dicen opina opinan comentan sobre que "
    "puedes podrias ayudame quiero tu me te "
    "please it that this for now then and the on online browser websearch in at of up out you".split()
)


# A bare search command ("búscalo", "haz la búsqueda"): it names no subject, so
# the engine resolves one from the owner's recent turns. Not a rule `search`
# by itself: with no subject there is nothing to look up.
_BARE_LEAD = re.compile(
    r"^(?:(?:si|ok|okay|vale|dale|ya|bueno|oye|hey|kira|entonces|pues|pero|y|por favor|porfa"
    r"|puedes|podrias|yes|sure|please|pls|can\s+you|could\s+you|would\s+you|will\s+you)\b[\s,.:;!¡?¿]*)*"
)
_BARE_TAIL = r"(?:\s+(?:ahora|ya|de nuevo|otra vez|por favor|porfa|now|again|please|one\s+more\s+time))*"
_BARE_MEDIUM = (
    r"(?:\s+(?:online|(?:(?:en|por|in|on|at)\s+)?(?:the\s+)?(?:internet|la\s+web|la\s+red|web|google|reddit|duckduckgo|browser)))?"
)
_BARE_COMMAND = re.compile(
    r"^(?:(?:buscalo|buscala|buscarlo|buscarla|investigalo|busca\s+eso|buscame\s+eso|" + _STT_VERBS + r")"
    r"|(?:haz|hazme|realiza|realizame|ejecuta|lanza|hacer|realizar)\s+"
    r"(?:la|una|esa|otra|de\s+nuevo\s+la)\s+busqueda"
    r"|google\s+(?:it|that|this)"
    r"|look\s+(?:it|that|this)\s+up"
    r"|search\s+(?:it|that|this)"
    r"|web\s*search"
    r"|(?:search|look)\s+again"
    r"|check\s+online)"
    + _BARE_MEDIUM + _BARE_TAIL + _BARE_MEDIUM + _BARE_TAIL + r"$"
)


def is_bare_search_command(text) -> bool:
    """True for a search command that carries no subject of its own."""
    if not isinstance(text, str) or not text.strip():
        return False
    folded = _fold(text).strip(_EDGE)
    folded = folded[_BARE_LEAD.match(folded).end():].strip(_EDGE)
    return bool(_BARE_COMMAND.match(folded))


# Words that add no subject to a query (checked on the query only). Kept apart
# from _FILLER, which the rule's trailing-ask logic also reads.
_QUERY_FILLER = _FILLER | frozenset(
    "realiza realizar realizame haz hacer hazme ejecuta ejecutar lanza una un otra esa ese si "
    "can could would will quick search look find check google again".split()
)


def _fold(text: str) -> str:
    """Lowercase without accents, one output char per input char, so match
    spans line up with the original text."""
    out = []
    for ch in text:
        base = unicodedata.normalize("NFD", ch)[:1] or ch
        low = base.lower()
        out.append(low if len(low) == 1 else base)
    return "".join(out).replace("’", "'")


# Recency words that also appear in game talk ("los bichos que salieron del
# suelo", "la misión de esta semana"): they count only together with a medium
# or an information noun in the same window.
_WEAK_CUE = re.compile(r"\b(?:salieron|esta semana|este mes|este ano|del ano)\b")
_INFO_OR_MEDIUM = re.compile(
    r"\b(?:noticias|novedades|informacion|datos|modelos|juegos|peliculas|series|lanzamientos"
    r"|actualizaciones|precios?|articulos|resultados|internet|web|google|reddit|linea)\b"
)


def _topic_cue(folded: str, pos: int) -> bool:
    window = _TOPIC_WINDOW.match(folded, pos)
    if not window:
        return False
    text = window.group()
    return bool(_TOPIC_CUE.search(text) or (_WEAK_CUE.search(text) and _INFO_OR_MEDIUM.search(text)))


_TRAILING_MEDIUM = re.compile(
    r"(?:^|[,.;:!?]\s*)(?:(?:en|por|in|on|at)\s+)?(?:una\s+|la\s+|the\s+)?(?:internet|web|google|red|browser|online)[\s.!?]*$"
)
_BOUNDARY = re.compile(r"\s*[.,;:!]+\s*¿?\s*")
_MODAL = re.compile(r"\b(?:puedes|podrias|podras)\b")


def _boundary_topic(folded: str, lead_start: int, verb_start: int, pos: int):
    """End of the sentence boundary after a bare imperative when a topic
    follows, else None. STT puts a period after the verb ("Puedes buscar.
    Que plataformas..."). The next sentence must open with a question word,
    or the verb must follow a modal ("puedes/podrias") and the next
    sentence must carry a topic cue or an information noun."""
    gap = _BOUNDARY.match(folded, pos)
    if gap is None:
        return None
    rest = folded[gap.end():]
    if not rest.strip(_EDGE):
        return None
    if _FOLLOW.match(" " + rest) or re.match(r"que\b", rest):
        return gap.end()
    if _MODAL.search(folded[lead_start:verb_start]) and (
        _topic_cue(folded, gap.end()) or _INFO_OR_MEDIUM.search(rest)
    ):
        return gap.end()
    return None


def _imperative(folded: str):
    """``(start, end)`` of a sentence-initial imperative, leading filler
    included, or None."""
    for start in [0] + [m.end() for m in _SENTENCE_END.finditer(folded)]:
        lead_end = _LEAD.match(folded, start).end()
        head = _IMPERATIVE.match(folded, lead_end)
        if head is None:
            continue
        if not (head.group("medium") or head.group("about") or head.group("que")):
            bound_end = _boundary_topic(folded, start, lead_end, head.end())
            if bound_end is not None:
                return start, bound_end
        if head.group("medium") or head.group("about") or head.group("que") or _FOLLOW.match(folded, head.end()) or _SI_INFO.match(folded, head.end()) or _topic_cue(folded, head.end()):
            # "que" (like any question word) is part of the topic: not stripped.
            return start, head.start("que") if head.group("que") else head.end()
    return None


def _trailing_ask(folded: str) -> bool:
    m = _TRAILING_ASK.search(folded)
    if not m:
        return False
    prefix = folded[: m.start()].strip(_EDGE)
    if re.search(r"\b(?:en|por)\s+" + _MEDIUM + r"\b", m.group(0)) or re.search(r"\b" + _MEDIUM + r"\b", prefix):
        return True
    content_words = [w for w in re.findall(r"\w+", prefix) if w not in _FILLER]
    return len(content_words) >= 2 or bool(
        re.search(r"\b(?:quien|cuando|cual|cuales|como|donde|que|sobre|acerca de|noticias)\b", prefix)
    )


def is_explicit_web_request(text) -> bool:
    if not isinstance(text, str) or not text.strip():
        return False
    folded = _fold(text)
    return bool(
        _COMMANDS.search(folded)
        or _NEWS.search(folded)
        or _imperative(folded)
        or _SPLIT_MEDIUM.search(folded)
        or _EN_SPLIT_MEDIUM.search(folded)
        or _NOUN.search(folded)
        or _WEB_SOURCE.search(folded)
        or _trailing_ask(folded)
    )


def web_rule(text) -> str:
    return SEARCH if is_explicit_web_request(text) else NO_SEARCH


def search_query(text, *, year=None) -> str:
    """The query sent to the search engine (see the module docstring)."""
    cleaned = clean_text(text)
    query = cleaned
    folded = _fold(cleaned)
    m_trail = _TRAILING_ASK.search(folded)
    if m_trail:
        query = query[: m_trail.start()]
        cleaned = query
        folded = _fold(query)
    span = _imperative(folded)
    if span is None:
        m = _WEB_SOURCE.search(folded)
        span = (m.start(), m.end()) if m else None
    if span is None:
        m = _NOUN.search(folded)
        span = (m.start(), m.end()) if m else None
    if span is None:
        match = _COMMANDS.search(folded)
        if match:
            s = match.start()
            clause_starts = [0] + [end.end() for end in _SENTENCE_END.finditer(folded[:s])]
            lead = _LEAD.match(folded, clause_starts[-1])
            if lead and lead.end() >= s:
                s = lead.start()
            span = (s, match.end())
    if span is None:
        split = _SPLIT_MEDIUM.search(folded) or _EN_SPLIT_MEDIUM.search(folded)
        if split:
            # K3: drop the verb (with its lead) and the medium; the words
            # between them stay as the topic.
            s = split.start()
            lead = _LEAD.match(folded, ([0] + [e.end() for e in _SENTENCE_END.finditer(folded[:s])])[-1])
            if lead and lead.end() >= s:
                s = lead.start()
            query = (
                cleaned[:s] + " " + cleaned[split.end("verb"):split.start("med")]
                + " " + cleaned[split.end("med"):]
            )
    if span is not None:
        query = cleaned[: span[0]] + " " + cleaned[span[1]:]
        folded = _fold(query)
    while True:
        folded = _fold(query)
        wake = _WAKE.match(folded)
        if wake:
            query = query[wake.end():]
            continue
        m_lead = _LEADING_FILLER.match(folded)
        if m_lead:
            query = query[m_lead.end():]
            continue
        break
    # _fold keeps one char per char, so folded spans index the original text.
    for m_stt in reversed(list(_STT_VERB_RE.finditer(_fold(query)))):
        query = query[: m_stt.start()] + " " + query[m_stt.end():]
    query = clean_text(query).strip(_EDGE)
    # A medium fragment stranded after the verb was stripped ("... En Internet.
    # Una web."): the command phrase is gone, the leftover medium is not a topic.
    while True:
        m_frag = _TRAILING_MEDIUM.search(_fold(query))
        if not m_frag or m_frag.start() == 0:
            break
        query = query[: m_frag.start()].strip(_EDGE)
    if len(query) < 2 or all(word in _QUERY_FILLER for word in re.findall(r"\w+", _fold(query))):
        return ""
    # Long or recency-cued dictations are compacted (design D2); a short query
    # with no cue is returned exactly as before.
    if _subject.needs_compaction(query):
        query = _subject.compact(query, year=year)
    return query[:QUERY_MAX_CHARS].strip()

