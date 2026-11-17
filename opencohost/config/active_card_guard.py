"""Forbidden-phrase guard for active cards.

While cards are active, the output guard blocks any text that contains one of
their forbidden phrases, whether or not the card was injected into this turn's
prompt. Matching is normalized (accent- and case-insensitive, punctuation and
whitespace collapsed) and whole-word, so ``cura`` does not match ``curacion``.

State is one immutable tuple swapped atomically (the guard runs from several
threads and only ever reads one reference). Nothing is enforced until
``set_cards`` is called with phrases, so an install with no active card behaves
exactly as before. Logs carry card ids only, never the text or the phrases.

This module must not import ``opencohost.config.validation`` (the guard
imports this module).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Iterable, Optional

_NON_ALNUM = re.compile(r"[^0-9a-z]+")

# ((card_id, normalized phrases padded with spaces), ...); () when nothing is active.
_state: tuple[tuple[str, tuple[str, ...]], ...] = ()


def normalize(text: str) -> str:
    """Lowercase, strip accents, collapse non-alphanumerics to single spaces."""
    decomposed = unicodedata.normalize("NFKD", text or "")
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NON_ALNUM.sub(" ", stripped.lower()).strip()


def set_cards(cards: Iterable[tuple[str, Iterable[str]]]) -> None:
    """Enforce the forbidden phrases of exactly these ``(card_id, phrases)`` cards."""
    global _state
    entries = []
    for card_id, phrases in cards:
        normalized = tuple(f" {n} " for n in (normalize(p) for p in phrases) if n)
        if normalized:
            entries.append((card_id, normalized))
    _state = tuple(entries)


def clear() -> None:
    """Stop enforcing anything."""
    global _state
    _state = ()


def active_card_ids() -> tuple[str, ...]:
    """Ids of the cards currently enforced (those with at least one phrase)."""
    return tuple(card_id for card_id, _ in _state)


def check(text: str) -> Optional[str]:
    """Return the id of the first active card whose phrase appears in *text*."""
    state = _state
    if not state:
        return None
    haystack = f" {normalize(text)} "
    for card_id, phrases in state:
        for phrase in phrases:
            if phrase in haystack:
                return card_id
    return None
