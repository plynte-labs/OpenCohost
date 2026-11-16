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
    "hi hello thanks thank bye goodbye good morning afternoon evening nice ok okay".split()
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
    if now - last.ts <= PLAIN_TURN_TTL_S and content_word_count(last.query) >= 2:
        return last.query
    return ""
