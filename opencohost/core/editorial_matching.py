"""Pure, stateless editorial card matching logic.

Functions in this module are import-safe and have no side effects.
They are deliberately kept separate from store and controller code so they
can be unit-tested in isolation and imported without heavyweight dependencies.
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from opencohost.core.editorial_cards import EditorialCard


# ---------------------------------------------------------------------------
# Stopwords (English + Spanish)
# ---------------------------------------------------------------------------

_STOPWORDS: frozenset[str] = frozenset({
    # Spanish
    "el", "la", "los", "las", "de", "del", "en", "un", "una", "y", "a",
    "al", "con", "por",
    # English
    "the", "of", "in", "an", "and", "for", "to", "on", "at", "is", "it",
    "was",
})


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _strip_accents(text: str) -> str:
    """Decompose to NFD and drop combining diacritic characters."""
    return "".join(
        ch for ch in unicodedata.normalize("NFD", text)
        if unicodedata.category(ch) != "Mn"
    )


def _strip_emojis(text: str) -> str:
    """Remove characters in the emoji/symbol Unicode ranges."""
    # Remove codepoints >= U+2600 (Miscellaneous Symbols and above)
    # This covers all common emoji ranges: 2600-27BF, 1F300+, etc.
    return "".join(ch for ch in text if ord(ch) < 0x2600)


def _split_on_boundaries(text: str) -> list[str]:
    """Split on letter/digit boundaries and non-alphanumeric characters.

    'GTA6' -> ['GTA', '6']
    'hello-world' -> ['hello', 'world']
    """
    # Insert a split point between letter->digit and digit->letter transitions
    text = re.sub(r"([a-zA-Z])(\d)", r"\1 \2", text)
    text = re.sub(r"(\d)([a-zA-Z])", r"\1 \2", text)
    # Split on any non-alphanumeric
    return re.split(r"[^a-zA-Z0-9]+", text)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def normalize_tokens(text: str) -> list[str]:
    """Return a cleaned, filtered token list from *text*.

    Pipeline:
    1. Strip emojis (codepoints >= U+2600)
    2. Lowercase
    3. Strip accents via NFD decomposition (ñ → n, é → e, etc.)
    4. Split on letter/digit boundaries (GTA6 → ['gta', '6'])
    5. Drop non-alphanumeric characters
    6. Remove English + Spanish stopwords
    7. Filter empty tokens
    """
    text = _strip_emojis(text)
    text = text.lower()
    text = _strip_accents(text)
    parts = _split_on_boundaries(text)
    tokens: list[str] = []
    for part in parts:
        # Keep only alphanumeric characters
        token = re.sub(r"[^a-z0-9]", "", part)
        if not token:
            continue
        if token in _STOPWORDS:
            continue
        tokens.append(token)
    return tokens


def match_score(topic_text: str, card: "EditorialCard") -> float:
    """Return a similarity score in [0.0, 1.0] between *topic_text* and *card*.

    Algorithm:
    1. Token overlap = |topic_tokens ∩ card_topic_tokens| / max(len(card_topic_tokens), 1)
    2. If any trigger's normalized tokens are ALL present in topic_token_set,
       raise score to max(score, 0.9).
    """
    topic_tokens = normalize_tokens(topic_text)
    card_tokens = normalize_tokens(card.topic)

    topic_set = set(topic_tokens)
    card_set = set(card_tokens)

    overlap = len(topic_set & card_set)
    score = overlap / max(len(card_set), 1)

    # Check triggers: all tokens in any single trigger must be present in topic
    for trigger in (card.triggers or []):
        trigger_tokens = normalize_tokens(trigger)
        if not trigger_tokens:
            continue
        if all(t in topic_set for t in trigger_tokens):
            score = max(score, 0.9)
            break

    return float(score)


def select_card(
    topic_text: str,
    cards: list["EditorialCard"],
) -> "EditorialCard | None":
    """Pick the best-matching card from *cards* for *topic_text*.

    Rules:
    - candidates: cards with match_score >= 0.8
    - SHORT-TITLE GUARD: if len(normalize_tokens(topic_text)) < 2, a card is
      only a candidate when it has a trigger hit OR its topic_slug/token-set
      exactly matches.
    - Among candidates, return the best only when the gap to the second-best
      is >= 0.1 OR there is exactly one candidate.
    - Ties or near-ties (gap < 0.1, multiple candidates) -> return None.
    """
    if not cards:
        return None

    topic_tokens = normalize_tokens(topic_text)
    short_title = len(topic_tokens) < 2
    topic_set = set(topic_tokens)

    scored: list[tuple[float, "EditorialCard"]] = []
    for card in cards:
        score = match_score(topic_text, card)
        if score < 0.8:
            continue

        # CARD-SIDE SHORT-TOKEN GUARD: a card whose own topic normalizes to
        # fewer than 2 tokens can trivially score 1.0 against any text
        # containing that single word.  Require a trigger hit to be eligible.
        card_tokens = normalize_tokens(card.topic)
        if len(card_tokens) < 2:
            has_trigger_hit = False
            for trigger in (card.triggers or []):
                trigger_tokens = normalize_tokens(trigger)
                if trigger_tokens and all(t in topic_set for t in trigger_tokens):
                    has_trigger_hit = True
                    break
            if not has_trigger_hit:
                continue

        # SHORT-TITLE GUARD (topic-side)
        if short_title:
            # Require: trigger hit OR slug/token-set exact match
            has_trigger_hit = False
            for trigger in (card.triggers or []):
                trigger_tokens = normalize_tokens(trigger)
                if trigger_tokens and all(t in topic_set for t in trigger_tokens):
                    has_trigger_hit = True
                    break

            card_tokens_set = set(normalize_tokens(card.topic))
            slug_match = card_tokens_set == topic_set

            if not has_trigger_hit and not slug_match:
                continue

        scored.append((score, card))

    if not scored:
        return None

    if len(scored) == 1:
        return scored[0][1]

    # Sort descending by score
    scored.sort(key=lambda x: x[0], reverse=True)
    best_score, best_card = scored[0]
    second_score, _ = scored[1]

    if best_score - second_score >= 0.1:
        return best_card

    # Gap too small — ambiguous
    return None
