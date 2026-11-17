"""Prompt-echo guard (live-session-fixes-20261005 L1).

A local fine-tune can copy its own system prompt into the reply (live log
2026-10-05 13:37:53: the whole persona prompt was spoken and shown). The
output guard is stateless and never sees the prompt, so this module compares
each reply sentence against the instruction text of the SAME turn.

A sentence is an echo when, after normalization (case, accents, punctuation,
bullets and line breaks folded away), it is at least ``MIN_ECHO_CHARS`` long
and either is a substring of the reference or shares at least
``NGRAM_OVERLAP_THRESHOLD`` of its word n-grams with it. Short sentences and
common phrases never qualify, so a reply that quotes one short rule is fine.

vision-and-prompt-leaks-20261018 T3: the n-gram check cannot catch a short
internal marker inside a sentence (live 2026-10-17: "me pediste explícitamente
la web search this turn"), nor the English "the owner" of the prompt coming
back as "el dueño". ``leak_kind`` is the lexical check for both and rides the
same guard path (stream revert/truncate, buffered retry with a nudge, then the
neutral line). Markers leak on any source; the third-person owner check
applies to owner sources only ("el dueño de la tienda" passes).

Pure module: no engine imports. Callers own logging through ``log_prompt_echo``
and ``log_prompt_leak``, which record counts and codes only, never the prompt
or the reply text.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from typing import Any, Iterable, Optional

logger = logging.getLogger("OpenCohost")

PROMPT_ECHO_RULE_ID = "prompt_echo"
PROMPT_ECHO_REASON = (
    f"Non-negotiable violation [{PROMPT_ECHO_RULE_ID}]: "
    "the reply repeats the system instructions"
)

PROMPT_LEAK_RULE_ID = "prompt_leak"
PROMPT_LEAK_REASON = (
    f"Non-negotiable violation [{PROMPT_LEAK_RULE_ID}]: "
    "the reply mentions internal prompt markers or refers to the owner in third person"
)
LEAK_MARKER = "marker"
LEAK_OWNER_THIRD_PERSON = "owner_third_person"
# Sources whose replies address the owner directly (direct speech and PTT).
OWNER_LEAK_SOURCES = frozenset({"direct", "ptt"})

MIN_ECHO_CHARS = 40
NGRAM_SIZE = 4
NGRAM_OVERLAP_THRESHOLD = 0.6

_NON_WORD_RE = re.compile(r"[^0-9a-z]+")
# Sentence boundaries only: a leaked bullet list keeps its lines together, so
# the per-line fragments (each under the length floor) are judged as one.
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?…])\s+")


def normalize_for_echo(text: str) -> str:
    """Lowercase, strip accents, fold every non-alphanumeric run to a space."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", str(text).lower())
    no_accents = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NON_WORD_RE.sub(" ", no_accents).strip()


# Normalized (see ``normalize_for_echo``) fragments of the per-turn markers and
# rule names the assembler puts in the prompt. None is ordinary speech.
_INTERNAL_MARKERS = (
    "web search this turn",
    "screen look this turn",
    "vision off request this turn",
    "no look this turn",
    "web results rule",
    "screen context rule",
    "vision off rule",
    # J2: layout v2 per-turn vocabulary. Full phrase only: the directive line.
    # The turn-context vocabulary is matched on the raw text (``_RAW_MARKER_RE``).
    "reply length this turn",
    "busqueda web este turno",
    "busqueda web de este turno",
    "busqueda web en este turno",
)
_MARKER_RE = re.compile(r"\b(?:" + "|".join(re.escape(m) for m in _INTERNAL_MARKERS) + r")\b")
# R2-1: the v2 turn-context vocabulary is matched on the RAW reply, because
# ``normalize_for_echo`` folds punctuation into spaces and would make "Your turn.
# Context matters." look like the heading. ``turn_context`` covers the bare token
# and the ``<turn_context>`` / ``</turn_context>`` tags (underscore kept); the
# heading "TURN CONTEXT" is the two words joined by spaces or tabs only.
_RAW_MARKER_RE = re.compile(r"turn_context|\bturn[ \t]+context\b", re.IGNORECASE)
# "el dueño" / "the owner" about the person Kira is talking to; "el dueño de la
# tienda" and "the owner of the shop" are about someone else and pass.
_OWNER_THIRD_RE = re.compile(
    r"\b(?:(?:el|al|del|la) duen[oa]|the owner)\b(?! (?:de|del|of)\b)"
)


def leak_kind(text: Any, source: str) -> Optional[str]:
    """``marker`` / ``owner_third_person`` when *text* leaks, else None. Never raises."""
    try:
        if not isinstance(text, str) or not text:
            return None
        norm = normalize_for_echo(text)
        if _MARKER_RE.search(norm) or _RAW_MARKER_RE.search(text):
            return LEAK_MARKER
        if source in OWNER_LEAK_SOURCES and _OWNER_THIRD_RE.search(norm):
            return LEAK_OWNER_THIRD_PERSON
        return None
    except Exception:
        return None


def _ngrams(words: list[str], size: int) -> list[tuple[str, ...]]:
    return [tuple(words[i:i + size]) for i in range(len(words) - size + 1)]


class PromptEchoDetector:
    """Precomputed view of one turn's instruction text."""

    def __init__(self, reference: str) -> None:
        self._reference = normalize_for_echo(reference)
        self._reference_ngrams = frozenset(
            _ngrams(self._reference.split(), NGRAM_SIZE)
        )

    def is_echo(self, sentence: str) -> bool:
        """True when this single sentence repeats the reference."""
        if not self._reference:
            return False
        normalized = normalize_for_echo(sentence)
        if len(normalized) < MIN_ECHO_CHARS:
            return False
        if normalized in self._reference:
            return True
        grams = _ngrams(normalized.split(), NGRAM_SIZE)
        if not grams:
            return False
        shared = sum(1 for gram in grams if gram in self._reference_ngrams)
        return shared / len(grams) >= NGRAM_OVERLAP_THRESHOLD

    def text_has_echo(self, text: str) -> bool:
        """True when any sentence of a whole reply repeats the reference."""
        if not self._reference or not text:
            return False
        return any(
            self.is_echo(part)
            for part in _SENTENCE_BOUNDARY_RE.split(str(text))
            if part.strip()
        )


def _system_contents(messages: Iterable[Any]) -> str:
    parts = []
    for message in messages or ():
        if isinstance(message, dict) and message.get("role") == "system":
            content = message.get("content")
            if isinstance(content, str) and content:
                parts.append(content)
    return "\n\n".join(parts)


def echo_reference_for(setup: Any) -> str:
    """The instruction text a reply must not repeat for this turn's setup.

    Prefers the assembler's ``prompt_echo_reference`` (persona and code-built
    rules, without the data blocks Kira is meant to quote: memories, cards,
    self-knowledge facts). Falls back to the system message content.
    """
    reference = getattr(setup, "prompt_echo_reference", None)
    if isinstance(reference, str) and reference:
        return reference
    return _system_contents(getattr(setup, "messages", None) or ())


def detector_for(setup: Any) -> PromptEchoDetector:
    return PromptEchoDetector(echo_reference_for(setup))


def log_prompt_echo(
    *, stage: str, source: str, chars: int, sentence_index: Optional[int] = None
) -> None:
    """One warning per caught echo. Counts only: never the prompt or reply."""
    logger.warning(
        "[PROMPT_ECHO] stage=%s source=%s sentence_index=%s chars=%d",
        stage,
        source,
        "-" if sentence_index is None else sentence_index,
        chars,
    )


def log_prompt_leak(
    *, stage: str, source: str, kind: str, chars: int, sentence_index: Optional[int] = None
) -> None:
    """One warning per caught leak. Codes and counts only: never the reply text."""
    logger.warning(
        "[PROMPT_LEAK] stage=%s source=%s kind=%s sentence_index=%s chars=%d",
        stage,
        source,
        kind,
        "-" if sentence_index is None else sentence_index,
        chars,
    )
