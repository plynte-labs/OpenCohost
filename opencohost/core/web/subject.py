"""Query compaction and follow-up subject for the web lookup
(web-query-followup-and-retry).

Pure and deterministic; no I/O. ``compact`` turns a long or noisy dictated
question into a short search query. ``resolve_followup_subject`` (below)
picks the subject of a bare search command from the owner's recent turns.
Only owner text is ever used; nothing here logs.
"""
from __future__ import annotations

import datetime as _dt
import re
import threading
import unicodedata
from typing import NamedTuple

COMPACT_MIN_WORDS = 8  # strictly more than this many words are compacted
COMPACT_MAX_WORDS = 14
QUERY_MAX_CHARS = 200

_TOKEN_EDGE = " \t\r\n¿?¡!.,;:\"'«»()[]"

_MEDIA = re.compile(
    r"\b(?:(?:en|por)\s+(?:la\s+)?(?:web|internet|google|red)|por\s+favor|porfa"
    r"|(?:in|on|at)\s+(?:the\s+)?(?:web|internet|google|browser)|online|please)\b"
)
_SCAFFOLD = re.compile(
    r"\b(?:quiero\s+que|necesito\s+que|me\s+gustaria\s+que|algo\s+asi\s+como"
    r"|que\s+sean?(?:\s+(?:lo|mas|muy|bastante|suficientemente))*"
    r"|lo\s+suficientemente|suficientemente|puedes|podrias"
    r"|i\s+(?:want|need)\s+(?:you\s+)?to|i\s+would\s+like(?:\s+you)?\s+to"
    r"|(?:can|could|would)\s+you|that\s+(?:is|are)(?:\s+(?:the|more|very|quite))*)\b"
)
_CAPAZ = re.compile(r"\bcapaz\s+(?:como\s+)?para\b")
# Only "este año" is the current year; "del año pasado / 2010 / que viene" and a
# bare "del año" name another year and are left alone.
_THIS_YEAR = re.compile(
    r"\b(?:(?:de\s+)?(?:ahora\s+)?este\s+ano(?!\s+(?:pasado|anterior|que\s+viene|proximo|(?:19|20)\d\d))"
    r"|(?:of\s+|for\s+|in\s+)?this\s+year(?!\s+(?:past|before|next|(?:19|20)\d\d)))\b"
)
_MAS_BEFORE_CUE = re.compile(
    r"\b(?:mas\s+(?=(?:recientes|nuevos|nuevas|actuales)\b)|more\s+(?=(?:recent|newest|current)\b))"
)
_LEAD_ARTICLE = re.compile(r"^(?:(?:un|una|unos|unas|a|an|some)\s+)")
_LEAD_GENERIC = re.compile(
    r"^(?:(?:opciones|cosas|resultados|algo|informacion|datos|options|things|results|something|info|information|data)\s+)(?=\S)"
)
_RECENCY_CUE = re.compile(
    r"\b(?:recientes|ultimos|nuevos|salieron|lanzados|actuales|recent|newest|current)\b"
)
_YEAR = re.compile(r"\b(?:19|20)\d\d\b")
_TIME_WORD = re.compile(
    r"\b(?:semana|mes|hoy|ano|mil|enero|febrero|marzo|abril|mayo|junio|julio|agosto"
    r"|septiembre|setiembre|octubre|noviembre|diciembre"
    r"|week|month|today|year|thousand|january|february|march|april|may|june|july|august"
    r"|september|october|november|december)\b"
)
_YEAR_TOKEN = "\x00year\x00"


def fold(text: str) -> str:
    """Lowercase without accents, one output char per input char."""
    out = []
    for ch in text:
        base = unicodedata.normalize("NFD", ch)[:1] or ch
        low = base.lower()
        out.append(low if len(low) == 1 else base)
    return "".join(out).replace("’", "'")


def _current_year() -> int:
    return _dt.date.today().year


def has_recency_cue(text: str) -> bool:
    folded = fold(text)
    return bool(_RECENCY_CUE.search(folded) or _THIS_YEAR.search(folded))


def needs_compaction(text: str) -> bool:
    return len(text.split()) > COMPACT_MIN_WORDS or has_recency_cue(text)


def _tokens(text: str) -> list[str]:
    """Words with edge punctuation removed. Capitalised words are lowercased
    (acronyms like "IPO" stay), so an STT split that capitalises a fragment
    start gives the same query as the unsplit dictation."""
    out = []
    for raw in text.split():
        word = raw.strip(_TOKEN_EDGE)
        if word:
            if word[:1].isupper() and not word.isupper():
                word = word.lower()
            out.append(word)
    return out


def _drop(words: list[str], folded: list[str], pattern: re.Pattern, repl: str | None = None):
    """Apply ``pattern`` over the space-joined folded words and rebuild the
    parallel lists; a match becomes ``repl`` (one word) or disappears."""
    joined = " ".join(folded)
    starts, pos = [], 0
    for w in folded:
        starts.append(pos)
        pos += len(w) + 1
    out_w, out_f, i = [], [], 0
    spans = [(m.start(), m.end()) for m in pattern.finditer(joined)]
    for s, e in spans:
        while i < len(words) and starts[i] < s:
            out_w.append(words[i]); out_f.append(folded[i]); i += 1
        if repl is not None:
            out_w.append(repl); out_f.append(repl)
        while i < len(words) and starts[i] < e:
            i += 1
    out_w.extend(words[i:]); out_f.extend(folded[i:])
    return out_w, out_f


def compact(text: str, *, year: int | None = None) -> str:
    """A short search query from a long or noisy dictation (design D2).

    Sentence enders carry no meaning (the STT splits one request into
    fragments), so the text is treated as one phrase. Output is lowercase.
    """
    year = year or _current_year()
    words = _tokens(text)
    folded = [fold(w) for w in words]
    untouched = list(folded)
    words, folded = _drop(words, folded, _MEDIA)
    words, folded = _drop(words, folded, _SCAFFOLD)
    words, folded = _drop(words, folded, _CAPAZ, "capaz de")
    words, folded = _drop(words, folded, _MAS_BEFORE_CUE)
    words, folded = _drop(words, folded, _THIS_YEAR, _YEAR_TOKEN)
    joined = " ".join(folded)
    for lead in (_LEAD_ARTICLE, _LEAD_GENERIC):
        m = lead.match(joined)
        if m:
            drop = len(joined[: m.end()].split())
            words, folded = words[drop:], folded[drop:]
            joined = " ".join(folded)
    has_year = any(f == _YEAR_TOKEN or _YEAR.fullmatch(f) for f in folded)
    cue = bool(_RECENCY_CUE.search(" ".join(folded)))
    if cue and not has_year and not _TIME_WORD.search(" ".join(folded)):
        words.append(_YEAR_TOKEN); folded.append(_YEAR_TOKEN)
    words = [str(year) if w == _YEAR_TOKEN else w for w in words]
    folded = [str(year) if f == _YEAR_TOKEN else f for f in folded]
    if folded == untouched:
        return text.strip(_TOKEN_EDGE)[:QUERY_MAX_CHARS].strip()
    if len(words) > COMPACT_MAX_WORDS:
        head = words[:COMPACT_MAX_WORDS]
        tail_keep = [
            w for w, f in zip(words[COMPACT_MAX_WORDS:], folded[COMPACT_MAX_WORDS:])
            if _RECENCY_CUE.fullmatch(f) or _YEAR.fullmatch(f) or _TIME_WORD.fullmatch(f)
        ]
        head_f = " ".join(folded[:COMPACT_MAX_WORDS])
        tail_keep = [w for w in dict.fromkeys(tail_keep) if fold(w) not in head_f.split()]
        words = head[: COMPACT_MAX_WORDS - len(tail_keep)] + tail_keep
    return " ".join(words)[:QUERY_MAX_CHARS].strip()


# --- follow-up subject (design D1) -------------------------------------------------------

_GENERIC_NOUNS = frozenset(
    "opciones cosas resultados algo informacion datos options things results something info information data".split()
)
_CHAT_WORDS = frozenset(
    "hola tal buenas buenos dias tardes noches gracias adios chao bien listo hey "
    "claro perfecto genial entendido exacto vale dale si no "
    "hi hello thanks thank bye goodbye good morning afternoon evening nice ok okay "
    "yes yeah yep yup sure alright fine nope nah cool great awesome gotcha understood".split()
)
_SCAFFOLD_WORDS = frozenset("este esta ano sean sea mas this that these those year more very some any".split())
_CUE_TOKEN = re.compile(
    r"(?:recientes|ultimos|nuevos|salieron|lanzados|actuales|semana|mes|hoy"
    r"|latest|recent|newest|released|launched|current|week|month|today|(?:19|20)\d\d)"
)


class LedgerTurn(NamedTuple):
    """One owner turn held in process memory (never persisted, never logged)."""

    ts: float
    query: str
    search_intent: bool = False
    refinement: bool = False


def _stop_words() -> frozenset:
    from opencohost.core.web.rank import _STOPWORDS
    from opencohost.core.web.trigger import _FILLER

    return frozenset(_FILLER | _STOPWORDS | {"a", "al", "un", "lo", "an", "in", "on", "of", "to"})


# Nouns that name no subject on their own; with a recency cue they make a refinement.
_REFINE_EXTRA = frozenset({"noticias", "modelos", "news", "models"})

# Predicates, question words, and generic attributes that ask *about* an omitted
# or anaphoric topic ("de que trata", "quien la hizo", "what is it about") without
# naming any concrete subject of their own.
_SUBJECTLESS_PREDICATES = frozenset(
    "se me te le les nos su sus mi mis tu tus llama llaman ve ven puede pueden "
    "alternativa alternativas opcion opciones juego pelicula serie libro anime tematica tema genero "
    "trata tratan va van es son fue fueron hizo hicieron creo crearon dirigio "
    "salio sale salen estrena estreno cuesta vale dura ver verla verlo verlas verlos "
    "jugar jugarla jugarlo significa consiste funciona sirve tiene tienen pasa paso "
    "ocurrio opina opinan dicen dice resumen sinopsis historia trama fecha precio "
    "temporadas capitulos episodios reparto actores director autor creador lanzamiento "
    "version actualizacion resena critica calificacion nota puntaje "
    "curiosidades mascota jefes personajes "
    "donde cuando quien quienes cuanto cuantos cuantas como cual cuales que porque por "
    "eso esto ese esa esos esas estos estas aquello ello "
    "about mean means is are was were did does do made created directed wrote "
    "release released comes come out cost costs worth long watch play work works "
    "happen happened plot summary synopsis story date price seasons episodes cast "
    "author creator rating reviews review facts trivia fun curiosities mascot bosses characters alternatives options "
    "where when who what how why which it its that this them they he she him her".split()
)


def _split(query: str, extra_generic: frozenset = frozenset()) -> tuple[list[str], list[str]]:
    """``(subject words, cue words)`` of a query, folded."""
    stop = _stop_words() | extra_generic
    subject, cues = [], []
    for w in re.findall(r"\w+", fold(query)):
        if _CUE_TOKEN.fullmatch(w):
            cues.append(w)
        elif w in stop or w in _SCAFFOLD_WORDS or w in _GENERIC_NOUNS or w in _CHAT_WORDS:
            continue
        else:
            subject.append(w)
    if subject in (["modelos"], ["models"]):
        subject = []
    return subject, cues


def content_word_count(query: str) -> int:
    return len(_split(query)[0])


def is_refinement(query) -> bool:
    """Little subject of its own plus a recency cue: "opciones más recientes
    este año" narrows the previous request instead of starting a new one."""
    if not isinstance(query, str) or not query.strip():
        return False
    subject, cues = _split(query, _REFINE_EXTRA)
    return not subject and bool(cues)


def is_subjectless_followup(query) -> bool:
    """A follow-up question that names no topic of its own (e.g. "de que trata",
    "quien la hizo", "what is it about") and therefore needs the previous owner
    turn's subject before searching."""
    if not isinstance(query, str) or not query.strip():
        return False
    tokens = re.findall(r"\w+", fold(query))
    if not tokens:
        return False
    subject, _cues = _split(query, _SUBJECTLESS_PREDICATES)
    return not subject


_TOPIC_LEAD = re.compile(
    r"^(?:(?:oye|hey|kira|por\s+favor|porfa|please)\b[\s,.:;!¡?¿]*)*"
    r"(?:sabes\s+(?:que|quien)\s+(?:es|son|fue|fueron)"
    r"|(?:que|quien)\s+(?:es|son|fue|fueron)"
    r"|has\s+(?:visto|jugado|escuchado|probado|leido)(?:\s+(?:de|sobre|a))?"
    r"|(?:conoces|verias|viste|jugaste|escuchaste|probaste|recomiendas)"
    r"|(?:hablame|cuentame|dime)\s+(?:de|sobre|acerca\s+de)"
    r"|do\s+you\s+(?:know(?:\s+(?:what|who)\s+(?:is|are))?|recommend)"
    r"|(?:what|who)(?:'s|\s+(?:is|are|was|were))"
    r"|tell\s+me\s+about"
    r"|explain(?:\s+to\s+me)?(?:\s+about)?"
    r"|have\s+you\s+(?:played|seen|watched|heard\s+of|read|tried)"
    r"|would\s+you\s+(?:watch|play))\s+",
    re.IGNORECASE,
)

_DEFINITIONAL_NEW = re.compile(
    r"^(?:(?:que|quien|quienes)\s+(?:es|son|fue|fueron)"
    r"|(?:what|who)(?:'s|\s+(?:is|are|was|were)))\s+(?!(?:el|la|los|las|the)\b)\S",
    re.IGNORECASE,
)

_INTERROGATIVE_LEAD = re.compile(
    r"^(?:que|cual|cuales|cuanto|cuantos|cuantas|quien|quienes|donde|cuando|como"
    r"|what|which|who|where|when|how)\b",
    re.IGNORECASE,
)

_DEFINITE_ARTICLE = re.compile(r"\b(?:el|la|los|las|del|al|the)\b", re.IGNORECASE)
_POSSESSIVE_ANAPHORA = re.compile(
    r"\b(?:su|sus|ese|esa|esos|esas|eso|esto|aquel|aquella|its|their|that|this|them)\b",
    re.IGNORECASE,
)


def extract_topic(turn_text: str) -> str:
    """Strip conversational openers ('sabes que es elden ring?') -> 'elden ring'."""
    if not isinstance(turn_text, str):
        return ""
    cleaned = " ".join(w.strip(_TOKEN_EDGE) for w in turn_text.split() if w.strip(_TOKEN_EDGE))
    if not cleaned:
        return ""
    folded = fold(cleaned)
    m = _TOPIC_LEAD.match(folded)
    if m:
        # Preserve original casing from the same character offset (fold is 1:1 char).
        tail = cleaned[m.end():].strip(_TOKEN_EDGE).strip()
        if tail:
            return tail[:QUERY_MAX_CHARS]
    return cleaned[:QUERY_MAX_CHARS]


def _has_named_entity(query: str) -> bool:
    """True if query contains a non-initial capitalized word or alphanumeric model/number token."""
    words = [w.strip(_TOKEN_EDGE) for w in query.split() if w.strip(_TOKEN_EDGE)]
    for idx, w in enumerate(words):
        if any(ch.isdigit() for ch in w):
            return True
        if idx > 0 and w[:1].isupper() and any(ch.isalpha() for ch in w):
            return True
        if len(w) >= 2 and w.isupper():
            return True
    return False


def needs_topic_carry(query) -> bool:
    """True when a search query lacks its own named topic and refers to the
    recent conversation ('que jefes son los mas dificiles segun la comunidad',
    'cuales son sus alternativas', 'el juego', 'what are the hardest bosses')."""
    if not isinstance(query, str) or not query.strip():
        return False
    if is_subjectless_followup(query):
        return True
    cleaned = " ".join(w.strip(_TOKEN_EDGE) for w in query.split() if w.strip(_TOKEN_EDGE))
    folded = fold(cleaned)
    if _DEFINITIONAL_NEW.match(folded):
        return False
    if _has_named_entity(cleaned):
        return False
    if _POSSESSIVE_ANAPHORA.search(folded):
        return True
    if _INTERROGATIVE_LEAD.match(folded) and _DEFINITE_ARTICLE.search(folded):
        return True
    return False


_CLARIFICATION_RE = re.compile(
    r"^(?:(?:i\s+meant|meant|me\s+referia\s+a|me\s+refiero\s+a)\s+)?"
    r"(?:(?:al|a\s+la|el|la|los|las|del|de\s+la|de|sobre\s+el|sobre\s+la|the|about\s+the)\s+)?"
    r"(?:lenguaje(?:\s+de\s+programacion)?|programacion|juego|videojuego|serie|pelicula|peli|anime|manga|libro|banda|cancion|modelo|herramienta|libreria|framework"
    r"|language|programming\s+language|game|video\s+game|series|tv\s+series|show|tv\s+show|movie|film|book|band|song|model|tool|library)$",
    re.IGNORECASE,
)

_CLARIFICATION_STRIP = re.compile(
    r"^(?:(?:i\s+meant|meant|me\s+referia\s+a|me\s+refiero\s+a)\s+)?"
    r"(?:(?:al|a\s+la|el|la|los|las|del|de\s+la|de|sobre\s+el|sobre\s+la|the|about\s+the)\s+)?",
    re.IGNORECASE,
)


def is_clarification(text) -> bool:
    """Short category disambiguator ('al lenguaje', 'the language', 'I meant the language')
    that refines the current active topic rather than replacing it."""
    if not isinstance(text, str) or not text.strip():
        return False
    cleaned = " ".join(w.strip(_TOKEN_EDGE) for w in text.split() if w.strip(_TOKEN_EDGE))
    if not cleaned or len(cleaned.split()) > 6:
        return False
    return bool(_CLARIFICATION_RE.match(fold(cleaned)))


class ActiveTopicStack:
    """Thread-safe LRU stack of recent explicit conversation topics (in memory
    only, no TTL amnesia, immune to confirmations like 'si' or 'buscalas')."""

    def __init__(self, maxlen: int = 5) -> None:
        self._maxlen = max(1, int(maxlen))
        self._topics: list[str] = []
        self._lock = threading.Lock()

    def current(self) -> str:
        with self._lock:
            return self._topics[-1] if self._topics else ""

    def clear(self) -> None:
        with self._lock:
            self._topics.clear()

    def observe(self, turn_text: str) -> None:
        """Update the LRU stack from one owner turn (never raises)."""
        if not isinstance(turn_text, str) or not turn_text.strip():
            return
        from opencohost.core.web.trigger import is_bare_search_command

        cleaned = " ".join(w.strip(_TOKEN_EDGE) for w in turn_text.split() if w.strip(_TOKEN_EDGE))
        if not cleaned or is_bare_search_command(cleaned):
            return
        # Short clarification ('al lenguaje', 'el juego') enriches the active top topic.
        if is_clarification(cleaned):
            qual = _CLARIFICATION_STRIP.sub("", fold(cleaned)).strip()
            if not qual:
                return
            with self._lock:
                if not self._topics:
                    return
                top = self._topics[-1]
                if qual not in fold(top).split():
                    self._topics[-1] = f"{top} {qual}"[:QUERY_MAX_CHARS].strip()
            return
        # Follow-ups without a new subject ask *about* the active topic — do not replace it.
        if needs_topic_carry(cleaned):
            return
        topic = extract_topic(cleaned)
        if not topic or len(topic) < 2:
            return
        if is_subjectless_followup(topic) or is_bare_search_command(topic):
            return
        words = _split(topic)[0]
        if not words:
            return
        folded_topic = fold(topic)
        with self._lock:
            self._topics = [t for t in self._topics if fold(t) != folded_topic]
            self._topics.append(topic[:QUERY_MAX_CHARS].strip())
            if len(self._topics) > self._maxlen:
                self._topics = self._topics[-self._maxlen:]


def merge_followup_query(base: str, followup: str) -> str:
    """Attach a follow-up question to the extracted previous owner topic."""
    clean_base = extract_topic(base)
    clean_followup = " ".join(
        w.strip(_TOKEN_EDGE) for w in followup.split() if w.strip(_TOKEN_EDGE)
    )
    if not clean_base:
        return clean_followup[:QUERY_MAX_CHARS].strip()
    if not clean_followup or fold(clean_followup) in fold(clean_base):
        return clean_base[:QUERY_MAX_CHARS].strip()
    if fold(clean_base) in fold(clean_followup):
        return clean_followup[:QUERY_MAX_CHARS].strip()
    return f"{clean_base} {clean_followup}"[:QUERY_MAX_CHARS].strip()


def _cue_words(query: str) -> list[str]:
    return [w for w in query.split() if _CUE_TOKEN.fullmatch(fold(w).strip(_TOKEN_EDGE))]


def merge_refinement(base: str, refinement: str, *, year: int | None = None) -> str:
    """``base`` plus the recency cues of ``refinement`` that it lacks."""
    year = year or _current_year()
    have = {fold(w) for w in base.split()}
    out = base.split()
    cues = _cue_words(refinement)
    if cues and not any(_YEAR.fullmatch(fold(c)) for c in cues + out):
        if not any(_TIME_WORD.fullmatch(fold(c)) for c in cues + out):
            cues.append(str(year))
    for c in cues:
        if fold(c) not in have:
            have.add(fold(c))
            out.append(c)
    return " ".join(out)[:QUERY_MAX_CHARS].strip()


PLAIN_TURN_TTL_S = 45.0


def resolve_followup_subject(
    turns, *, now: float, ttl: float, year: int | None = None, strict: bool = False
) -> str:
    """The subject of a bare search command, from owner turns oldest first
    (design D1). "" when nothing safe can be found.

    A search-intent chain lives for ``ttl``. The fallback to the latest plain
    (non-search) owner turn lives only ``PLAIN_TURN_TTL_S`` and is skipped when
    ``strict`` (used by refinements, which may only build on a search)."""
    live = [t for t in turns if now - t.ts <= ttl]
    chain = []
    for t in reversed(live):
        if not (t.search_intent or t.refinement):
            break
        chain.append(t)
    if chain:
        base = next((t for t in chain if not t.refinement and content_word_count(t.query) >= 2), None)
        if base is None:
            return ""
        result = base.query
        for t in reversed(chain):  # oldest refinement first
            if t.refinement:
                result = merge_refinement(result, t.query, year=year)
        return result
    if strict or not live:
        return ""
    last = live[-1]
    if now - last.ts <= PLAIN_TURN_TTL_S:
        if content_word_count(last.query) >= 2:
            return last.query
        topic = extract_topic(last.query)
        if topic and len(topic) >= 3 and not is_subjectless_followup(topic):
            return topic
    return ""
