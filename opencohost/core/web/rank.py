"""Laya ranks search results and page chunks (web-fetch-laya-20261009).

Laya can only classify, so each item is one 2-option ``choice``
(``useful``/``not_useful``) and the ``useful`` probability is its score. The
owner's question goes into the question's instructions and the web text is
the state, so every item is one ordinary call to the existing decision
backend (``ask(state, questions, timeout_s)``): no worker protocol change.

Batch vs loop: the worker answers one state per call. Items are asked one
by one, inside the step's deadline. A cross-state batch would save the
per-call overhead but not the forward-pass compute, which dominates on CPU,
so the item caps (5 results, 3 chunks per page: at most 11 calls, about
2.2 s on CPU measured 2026-10-09) are what bound the cost. The lookup now
runs ``lexical_prefilter`` first (web-lookup-polish-20261010 T6), so Laya
sees at most ``LINK_CANDIDATES`` + ``CHUNK_CANDIDATES`` = 6 items.

Fail-open: no backend, a backend that is not ready, an error or a garbage
answer leaves an item unscored. A timeout stops asking (the worker is busy)
and leaves the rest unscored. Scored items at or above the floor come first,
best first; unscored items follow in their original order. With nothing
scored, that is simply the first items in order.

Privacy: the state and the question are handed to the backend only; nothing
here logs them.
"""
from __future__ import annotations

import re
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Callable, Optional, Sequence

from opencohost.core.web.extract import clean_text

USEFUL = "useful"
NOT_USEFUL = "not_useful"
LINK_QID = "web_link"
CHUNK_QID = "web_chunk"
KEEP_LINKS = 2
KEEP_CHUNKS = 3
LINK_FLOOR = 0.3
CHUNK_FLOOR = 0.3
ITEM_TIMEOUT_S = 1.5
QUESTION_MAX_CHARS = 200
# web-lookup-polish-20261010 T6: candidates Laya is asked about after the
# lexical prefilter (was every result and every chunk: up to 5 + 6 calls).
LINK_CANDIDATES = 3
CHUNK_CANDIDATES = 3
_STEM_CHARS = 5
_WORD = re.compile(r"\w+")
_STOPWORDS = frozenset(
    "que quien quienes cuando cual cuales como donde para por con sin sobre del los las una unos unas "
    "este esta esto ese esa eso hay muy mas pero porque busca buscame internet web google "
    "the and for with what when who which how about from that this are was have has does did "
    "can could would please quick search look find check online browser websearch "
    "you your out tell again its into onto".split()
)


@dataclass
class Ranked:
    kept: list = field(default_factory=list)
    scored: int = 0


def _stems(text) -> set[str]:
    folded = "".join(
        unicodedata.normalize("NFD", ch)[:1] for ch in clean_text(text).lower()
    )
    return {w[:_STEM_CHARS] for w in _WORD.findall(folded) if len(w) >= 3 and w not in _STOPWORDS}


def lexical_prefilter(hint: str, items: Sequence, texts: Sequence[str], *, keep: int) -> list:
    """At most ``keep`` items, chosen by how many of ``hint``'s word stems
    their text shares (ties: earlier first), returned in original order.
    Deterministic and cheap; it only trims what Laya is asked about."""
    items = list(items)
    if len(items) <= keep:
        return items
    wanted = _stems(hint)
    overlap = [len(wanted & _stems(text)) for text in texts]
    best = sorted(range(len(items)), key=lambda i: (-overlap[i], i))[:keep]
    return [items[i] for i in sorted(best)]


def _question(kind: str, question: str) -> dict:
    q = clean_text(question)[:QUESTION_MAX_CHARS].replace('"', "'")
    if kind == LINK_QID:
        return {
            "type": "choice",
            "instructions": (
                f'Someone asked: "{q}". Decide whether this web search result is likely '
                "to contain a useful answer to the question."
            ),
            "criteria": {
                USEFUL: "The result is likely to answer the question or give relevant, current information about it.",
                NOT_USEFUL: "The result is unrelated, an ad, a login or shop page, or too generic to help.",
            },
        }
    return {
        "type": "choice",
        "instructions": (
            f'Someone asked: "{q}". Decide whether this passage from a web page helps '
            "answer the question."
        ),
        "criteria": {
            USEFUL: "The passage contains facts that answer the question or are directly relevant to it.",
            NOT_USEFUL: "The passage is unrelated, navigation, an ad, a cookie notice or other boilerplate.",
        },
    }


def _ready(backend) -> bool:
    if backend is None:
        return False
    available = getattr(backend, "available", None)
    try:
        return bool(available()) if callable(available) else True
    except Exception:
        return False


def _useful_probability(answer) -> Optional[float]:
    if not isinstance(answer, dict):
        return None
    probs = answer.get("probabilities")
    if isinstance(probs, dict):
        p = probs.get(USEFUL)
        if isinstance(p, (int, float)) and not isinstance(p, bool):
            return min(1.0, max(0.0, float(p)))
    choice = answer.get("choice")
    conf = answer.get("answer_confidence", answer.get("confidence"))
    if choice in (USEFUL, NOT_USEFUL) and isinstance(conf, (int, float)) and not isinstance(conf, bool):
        conf = min(1.0, max(0.0, float(conf)))
        return conf if choice == USEFUL else 1.0 - conf
    return None


_STOP = object()


def _score(backend, state: str, qid: str, qdef: dict, deadline, clock):
    """p(useful), None (unscored) or ``_STOP`` (timeout / out of time)."""
    timeout = ITEM_TIMEOUT_S
    if deadline is not None:
        left = deadline - clock()
        if left <= 0:
            return _STOP
        timeout = min(timeout, left)
    try:
        response = backend.ask(state, {qid: qdef}, timeout)
    except Exception:
        return None
    if response is None:
        return _STOP
    if not isinstance(response, dict) or not response.get("ok"):
        return None
    answers = response.get("answers")
    return _useful_probability(answers.get(qid) if isinstance(answers, dict) else None)


def _rank(backend, qid, question, items, states, keep, floor, deadline, clock) -> Ranked:
    scores: list[Optional[float]] = [None] * len(items)
    scored = 0
    if _ready(backend):
        qdef = _question(qid, question)
        for i, state in enumerate(states):
            p = _score(backend, state, qid, qdef, deadline, clock)
            if p is _STOP:
                break
            if p is not None:
                scores[i] = p
                scored += 1
    good = sorted(
        (i for i, p in enumerate(scores) if p is not None and p >= floor),
        key=lambda i: (-scores[i], i),
    )
    order = good + [i for i, p in enumerate(scores) if p is None]
    return Ranked([items[i] for i in order[:keep]], scored)


def rank_links(
    backend, question: str, results: Sequence, *, deadline: Optional[float],
    clock: Callable[[], float] = time.monotonic, keep: int = KEEP_LINKS,
) -> Ranked:
    """The ``keep`` most useful search results (``SearchResult``)."""
    states = [f"{r.title}\n{r.snippet}\n{r.domain}" for r in results]
    return _rank(backend, LINK_QID, question, list(results), states, keep, LINK_FLOOR, deadline, clock)


def rank_chunks(
    backend, question: str, chunks: Sequence[tuple[str, str]], *, deadline: Optional[float],
    clock: Callable[[], float] = time.monotonic, keep: int = KEEP_CHUNKS,
) -> Ranked:
    """The ``keep`` most useful ``(domain, text)`` chunks."""
    states = [text for _, text in chunks]
    return _rank(backend, CHUNK_QID, question, list(chunks), states, keep, CHUNK_FLOOR, deadline, clock)
