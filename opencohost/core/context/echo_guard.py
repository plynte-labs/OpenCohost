"""Echo guard (kira-personal-tone-20261007 P4).

A reply identical or near-identical to the previous assistant turn is a copy,
not an answer (live session 2026-10-07: 12:49 reply byte-identical to 12:48).
Pure helpers only: the engine decides what to do with the verdict.
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Iterable, Optional

ECHO_RULE_ID = "no_echo"
ECHO_MIN_CHARS = 40
ECHO_RATIO_THRESHOLD = 0.9

_NON_WORD_RE = re.compile(r"[\W_]+", re.UNICODE)


def normalize(text: Optional[str]) -> str:
    """Casefold, drop punctuation and collapse whitespace."""
    if not isinstance(text, str):
        return ""
    return _NON_WORD_RE.sub(" ", text.casefold()).strip()


def is_echo(candidate: Optional[str], previous: Optional[str]) -> tuple[bool, float]:
    """Return ``(is_echo, ratio)``. Replies under ``ECHO_MIN_CHARS`` normalized
    chars are exempt (a bare "sí" or "claro" may legitimately repeat)."""
    a = normalize(candidate)
    b = normalize(previous)
    if len(a) < ECHO_MIN_CHARS or not b:
        return False, 0.0
    if a == b:
        return True, 1.0
    ratio = difflib.SequenceMatcher(None, a, b, autojunk=False).ratio()
    return ratio >= ECHO_RATIO_THRESHOLD, ratio


def last_assistant_text(history: Optional[Iterable[Any]]) -> str:
    """Content of the most recent assistant turn in ``history`` ("" if none)."""
    if not history:
        return ""
    for msg in reversed(list(history)):
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            content = msg.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return ""
