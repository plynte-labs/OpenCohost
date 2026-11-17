"""Audience filter: the last line of defense between facts and a prompt."""
from __future__ import annotations

from typing import Sequence

from opencohost.core.self_knowledge.facts import OWNER, SelfFact


def filter_facts(facts: Sequence[SelfFact], audience: str) -> tuple[list[SelfFact], int]:
    """``(kept, withheld)``. The owner sees every fact; any other audience only
    ``viewer`` facts."""
    if audience == OWNER:
        return list(facts), 0
    kept = [f for f in facts if f.audience != OWNER]
    return kept, len(facts) - len(kept)
