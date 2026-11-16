"""Rank active cards against a turn: MiniLM cosine OR the lexical matcher.

Pure functions, no I/O. A card qualifies when

- its stored vector and the turn's query vector reach ``min_score`` (cosine), or
- the existing lexical editorial matcher (``select_card`` with this one card,
  i.e. topic tokens or a trigger, with its short-text guards) accepts it.

Lexical hits lead (a deterministic topic/trigger match is the stronger signal),
then semantic hits by descending cosine; at most ``top_k`` are returned. A card
that qualifies both ways reports ``semantic`` when its cosine qualifies.

With no query vector (semantic off, worker down, no embedding yet) the result is
exactly the lexical matcher's, which is what keeps the semantic-off path
byte-identical to it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from opencohost.core.editorial.editorial_cards import EditorialCard
from opencohost.core.editorial.editorial_matching import match_score, select_card


@dataclass(frozen=True)
class CardMatch:
    card: EditorialCard
    via: str  # "semantic" | "lexical"
    score: float


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity; 0.0 for a dimension mismatch or a zero vector."""
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def rank_active_cards(
    text: str,
    cards: Sequence[EditorialCard],
    *,
    query_vec: Optional[Sequence[float]],
    vectors: Mapping[str, Sequence[float]],
    min_score: float,
    top_k: int,
) -> list[CardMatch]:
    """The best ``top_k`` qualifying cards for *text*, best first."""
    lexical: list[CardMatch] = []
    semantic: list[CardMatch] = []
    for card in cards:
        sem = 0.0
        if query_vec is not None:
            vec = vectors.get(card.id)
            if vec is not None:
                sem = cosine(query_vec, vec)
        lex_hit = select_card(text, [card]) is not None
        if sem >= min_score:
            match = CardMatch(card, "semantic", sem)
            (lexical if lex_hit else semantic).append(match)
        elif lex_hit:
            lexical.append(CardMatch(card, "lexical", match_score(text, card)))
    lexical.sort(key=lambda m: m.score, reverse=True)
    semantic.sort(key=lambda m: m.score, reverse=True)
    return (lexical + semantic)[: max(0, int(top_k))]
