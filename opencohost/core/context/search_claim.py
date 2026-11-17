"""False web-search claim guard (web-trigger-memory-relevance-20261014 T2).

Live log 2026-10-14 21:45:42: no web search ran for the turn, yet the reply
was "Busqué en internet '...' ... Los resultados no mostraron ..." with
invented results. The output guard is stateless and never knows whether a
search ran, so this module is applied by the generation path only, and only
on turns where no search ran: a reply that says it searched, or cites search
results, is blocked like any other guard hit (one retry with a nudge, then
the neutral line).

Pure module: no engine imports. Honest talk is allowed: "no busqué", "no he
buscado", "¿quieres que busque en internet?", "I can search online".
Callers own logging through ``log_search_claim``, which records counts only.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Optional

logger = logging.getLogger("OpenCohost")

SEARCH_CLAIM_RULE_ID = "no_false_search_claim"
SEARCH_CLAIM_REASON = (
    f"Non-negotiable violation [{SEARCH_CLAIM_RULE_ID}]: "
    "the reply claims a web search that did not run this turn"
)

_NON_WORD_RE = re.compile(r"[^0-9a-z]+")
# Applied to normalized text (lowercase, no accents, single spaces).
_NOT_NEGATED = (
    r"(?<!\bno )(?<!\bni )(?<!\bnunca )(?<!\bque )(?<!\bsi )(?<!\bnot )(?<!\bnever )"
    # "no lo busqué en internet": a clitic between the negation and the verb.
    r"(?<!\bno lo )(?<!\bno la )(?<!\bno los )(?<!\bno las )(?<!\bno se lo )(?<!\bno te lo )(?<!\bno me lo )"
    r"(?<!\bnunca lo )(?<!\bnunca la )"
    # "quieres que (te) (lo) busque en internet": the subjunctive of an offer,
    # which folds to the same letters as the past "busqué".
    r"(?<!\bque lo )(?<!\bque la )(?<!\bque los )(?<!\bque las )(?<!\bque te )(?<!\bque me )(?<!\bque se )"
    r"(?<!\bque te lo )(?<!\bque te la )(?<!\bque me lo )(?<!\bque se lo )"
)
_MEDIUM = r"(?:internet|la web|google|la red|linea|online|duckduckgo)"
# Past-tense lookup verbs (folded: no accents). A few words may sit between
# the verb and the medium ("busqué 'Elden Ring' en internet").
_PAST_VERB = r"(?:busque|investigue|consulte|revise|cheque|verifique)"
_PERFECT_VERB = r"(?:he (?:buscado|investigado|consultado|revisado|chequeado)|acabo de buscar|estuve buscando)"
_GAP = r"(?:\w+ ){0,6}?"
_CLAIM_RE = re.compile(
    # es: "busqué en internet", "he buscado en la web", "busqué X en internet"
    _NOT_NEGATED + r"\b" + _PAST_VERB + r" (?:ya )?" + _GAP + r"(?:en|por) " + _MEDIUM + r"\b"
    + r"|" + _NOT_NEGATED + r"\b" + _PERFECT_VERB + r" " + _GAP + r"(?:en|por) " + _MEDIUM + r"\b"
    # es: "(ya) busqué eso por ti" claims a lookup on the listener's behalf
    + r"|" + _NOT_NEGATED + r"\bbusque (?:ya )?(?:eso |esto |todo |algo |lo )?por ti\b"
    + r"|\bsegun (?:los|mis|los mis) resultados\b"
    + r"|\bsegun lo que (?:encontre|busque)\b"
    + r"|\blos resultados (?:que encontre|muestran|apuntan|no mostraron|no muestran|arrojan|indican)\b"
    + r"|\bresultados de (?:la |mi |esta )?busqueda\b"
    + r"|" + _NOT_NEGATED + r"\bencontre en " + _MEDIUM + r"\b"
    + r"|" + _NOT_NEGATED + r"\bencontre (?:informacion|datos|resultados) en "
    + r"(?:varias |algunas |distintas |diferentes |muchas )?(?:paginas|sitios|fuentes|webs|articulos)\b"
    # en: "I searched the web", "the search results"; the medium is required
    # so "I searched the chest" stays game talk.
    + r"|" + _NOT_NEGATED + r"\bi (?:just )?searched (?:\w+ ){0,5}?(?:online|the web|the internet|google)\b"
    + r"|" + _NOT_NEGATED + r"\bi (?:just )?googled\b"
    + r"|" + _NOT_NEGATED + r"\bi (?:looked (?:it |that |this )?up|checked) (?:online|on the (?:web|internet)|on google)\b"
    + r"|\baccording to (?:the|my) (?:search )?results\b"
    + r"|\bthe search results\b"
    + r"|" + _NOT_NEGATED + r"\bi found (?:it |that |this )?(?:online|on the (?:web|internet)|on google)\b"
)


# web-query-followup-and-retry T5 (owner decision O3): after a lookup that ran
# and failed, a reply that says Kira cannot search (live 2026-10-16 20:47:04,
# "No puedo buscar eso ahora mismo ...") is false: the failure is temporary.
CANNOT_SEARCH_RULE_ID = "no_cannot_search_claim"
CANNOT_SEARCH_REASON = (
    f"Non-negotiable violation [{CANNOT_SEARCH_RULE_ID}]: "
    "the reply says it cannot search the web after a temporary lookup failure"
)
_CANNOT_RE = re.compile(
    r"\b(?:no|tampoco) puedo (?:buscar|buscarlo|buscarla|hacer busquedas|acceder a (?:internet|la web|la red)"
    r"|conectarme a (?:internet|la web)|navegar)\b"
    r"|\bno tengo (?:acceso a (?:internet|la web)|conexion a internet|internet"
    r"|capacidad para (?:buscar|navegar|acceder a (?:internet|la web)))\b"
    r"|\bno cuento con (?:acceso a (?:internet|la web|la red)|conexion)\b"
    r"|\bno me es posible (?:buscar|acceder a (?:internet|la web|la red))\b"
    r"|\bi (?:cannot|can t|can not|am unable to|m unable to) (?:search|browse|access the (?:internet|web))\b"
    r"|\bi (?:do not|don t) have (?:internet|access to the (?:internet|web))\b"
)
# "no puedo buscar la llave si ..." is about something else: a search verb
# followed by an object that is not a web/lookup term is left alone.
_CANNOT_OBJECT_OK = re.compile(r"\bno puedo buscar (?:la|el|los|las|un|una) \b")


# runtime-fixes-20261110 T2 (C). Two more honesty rules, same family and same
# governance as the two above (applied by the generation path, one retry with a
# nudge, then the neutral line; not preset-level and not in the guardrail
# registry):
#   * a first-person claim of having FOUND something (or citing results) when
#     the lookup came back empty or failed;
#   * a first-person claim of having READ / OPENED a page when no page was
#     fetched this turn (neither a link read nor a search that fetched pages).
FALSE_RESULT_RULE_ID = "no_false_search_result"
FALSE_RESULT_REASON = (
    f"Non-negotiable violation [{FALSE_RESULT_RULE_ID}]: "
    "the reply claims findings although the web lookup came back empty or failed"
)
PAGE_CLAIM_RULE_ID = "no_false_page_claim"
PAGE_CLAIM_REASON = (
    f"Non-negotiable violation [{PAGE_CLAIM_RULE_ID}]: "
    "the reply claims to have read a page that was not fetched this turn"
)
_FOUND_RE = re.compile(
    _NOT_NEGATED + r"\bencontre (?:que|informacion|datos|resultados|articulos|paginas|fuentes|noticias|varias|algunas|algo)\b"
    + r"|\b(?:segun|de acuerdo con) (?:los resultados|lo que encontre|mi busqueda|la busqueda)\b"
    + r"|\blos resultados (?:muestran|indican|dicen|senalan|apuntan|sugieren|arrojan)\b"
    + r"|\b(?:la|mi) busqueda (?:arrojo|mostro|devolvio|revelo|encontro|indica) (?!nada\b|que no\b|ningun)"
    + r"|" + _NOT_NEGATED + r"\bi found (?:some |several |a few |the )?"
    + r"(?:information|results|articles?|pages?|sources?|news|data|reports?|that)\b"
    + r"|\bthe results (?:show|say|indicate|suggest|point)\b"
    + r"|\baccording to (?:my search|the search|what i found|the results)\b"
    + r"|\bmy search (?:showed|found|returned|turned up|revealed)\b"
)
_PAGE_NOUN = r"(?:enlace|link|pagina|articulo|sitio|web|url)"
_PAGE_DET = r"(?:el|la|ese|esa|este|esta|tu|su)"
_PAGE_RE = re.compile(
    _NOT_NEGATED + r"\b(?:ya )?(?:lei|abri|revise|visite) (?:ya )?(?:por completo |con calma )?"
    + _PAGE_DET + r" " + _PAGE_NOUN + r"\b"
    + r"|" + _NOT_NEGATED + r"\bhe (?:leido|abierto|visitado|revisado) " + _PAGE_DET + r" " + _PAGE_NOUN + r"\b"
    + r"|" + _NOT_NEGATED + r"\bacabo de (?:abrir|leer|visitar|revisar) " + _PAGE_DET + r" " + _PAGE_NOUN + r"\b"
    + r"|" + _NOT_NEGATED + r"\bi (?:just |have |ve )?(?:read|opened|visited|checked|went through) "
    + r"(?:the|that|this|your) (?:link|page|article|site|website|url)\b"
)


def claims_found_result(text: Optional[str]) -> bool:
    """True when ``text`` says, in the first person, that something was found
    (or cites results). Honest "found nothing" talk is not a claim."""
    for sentence in _SENTENCE_SPLIT.split(text or ""):
        if _FOUND_RE.search(_normalize(sentence)):
            return True
    return False


def claims_read_page(text: Optional[str]) -> bool:
    """True when ``text`` says, in the first person, that a page or link was
    read, opened or visited."""
    for sentence in _SENTENCE_SPLIT.split(text or ""):
        if _PAGE_RE.search(_normalize(sentence)):
            return True
    return False


def _normalize(text: str) -> str:
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text).lower())
    no_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NON_WORD_RE.sub(" ", no_accents).strip()


_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")
_CLAUSE_SPLIT = re.compile(r"[.,;!?\n]+")
# "Sí, busqué eso por ti": an affirmation, not the conditional "si busqué"
# that _NOT_NEGATED skips. The accent or a comma tells them apart before folding.
_AFFIRM_RE = re.compile(r"\bsí\b|\bsi\s*,", re.IGNORECASE)


def claims_web_search(text: Optional[str]) -> bool:
    """True when ``text`` says a web search ran or cites its results.

    Sentences are checked apart so a question that opens with the verb
    ("¿Busqué en internet antes? No lo recuerdo.") is not a claim."""
    for sentence in _SENTENCE_SPLIT.split(text or ""):
        match = _CLAIM_RE.search(_normalize(_AFFIRM_RE.sub("claro", sentence)))
        if match is None:
            continue
        is_question = "¿" in sentence or sentence.rstrip().endswith("?")
        if is_question and match.start() <= 3:
            continue
        return True
    return False


def claims_cannot_search(text: Optional[str]) -> bool:
    """True when ``text`` says Kira cannot search the web or has no internet."""
    # The object exception is scoped to its own clause, so a game object in one
    # clause cannot hide a real "no puedo acceder a la red" in the next.
    for clause in _CLAUSE_SPLIT.split(text or ""):
        norm = _normalize(clause)
        if not _CANNOT_RE.search(norm):
            continue
        if _CANNOT_OBJECT_OK.search(norm) and not re.search(r"\binternet\b|\bla web\b", norm):
            continue
        return True
    return False


def violated_rule(text: Optional[str], setup) -> Optional[str]:
    """The search-honesty rule ``text`` breaks on this turn, or None.

    A search claim is false on a turn without a lookup; a "cannot search"
    claim is false on a turn whose lookup ran and failed. With the web switch
    off there is no lookup step, so ``web_searched`` is False and a search
    claim is inspected (and false) like on any other no-search turn."""
    searched = bool(getattr(setup, "web_searched", False))
    failed = bool(getattr(setup, "web_failed", False))
    if not searched:
        if claims_web_search(text):
            return SEARCH_CLAIM_RULE_ID
    else:
        # A lookup ran: "I cannot search" is false whether it gave results or failed.
        if claims_cannot_search(text):
            return CANNOT_SEARCH_RULE_ID
        if failed and claims_found_result(text):
            return FALSE_RESULT_RULE_ID
    # Pages were fetched this turn by a link read, or by a search that returned results.
    pages_fetched = bool(getattr(setup, "link_read", False)) or (searched and not failed)
    if not pages_fetched and claims_read_page(text):
        return PAGE_CLAIM_RULE_ID
    return None


_REASONS = {
    SEARCH_CLAIM_RULE_ID: SEARCH_CLAIM_REASON,
    CANNOT_SEARCH_RULE_ID: CANNOT_SEARCH_REASON,
    FALSE_RESULT_RULE_ID: FALSE_RESULT_REASON,
    PAGE_CLAIM_RULE_ID: PAGE_CLAIM_REASON,
}


def reason_for(rule_id: str) -> str:
    """The guard reason string of one of this module's rules."""
    return _REASONS[rule_id]


def log_claim(
    rule_id: str, *, stage: str, source: str, chars: int, sentence_index: Optional[int] = None
) -> None:
    """One warning per caught claim, whichever rule. Counts only."""
    if rule_id == SEARCH_CLAIM_RULE_ID:
        log_search_claim(stage=stage, source=source, chars=chars, sentence_index=sentence_index)
    elif rule_id == CANNOT_SEARCH_RULE_ID:
        log_cannot_search(stage=stage, source=source, chars=chars, sentence_index=sentence_index)
    else:
        tag = "SEARCH_RESULT_CLAIM" if rule_id == FALSE_RESULT_RULE_ID else "PAGE_CLAIM"
        logger.warning(
            "[%s] stage=%s source=%s sentence_index=%s chars=%d",
            tag, stage, source, "-" if sentence_index is None else sentence_index, chars,
        )


def log_cannot_search(
    *, stage: str, source: str, chars: int, sentence_index: Optional[int] = None
) -> None:
    """One warning per caught claim. Counts only: never the reply text."""
    logger.warning(
        "[CANNOT_SEARCH_CLAIM] stage=%s source=%s sentence_index=%s chars=%d",
        stage,
        source,
        "-" if sentence_index is None else sentence_index,
        chars,
    )


def log_search_claim(
    *, stage: str, source: str, chars: int, sentence_index: Optional[int] = None
) -> None:
    """One warning per caught claim. Counts only: never the reply text."""
    logger.warning(
        "[SEARCH_CLAIM] stage=%s source=%s sentence_index=%s chars=%d",
        stage,
        source,
        "-" if sentence_index is None else sentence_index,
        chars,
    )
