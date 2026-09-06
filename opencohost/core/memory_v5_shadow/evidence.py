"""WU1 evidence records — deterministic IDs, allowlist, immutable content."""

from __future__ import annotations

from dataclasses import dataclass

ALLOWED_SOURCES = frozenset({"direct", "ptt", "owner-bundle"})


@dataclass(frozen=True)
class CommittedTurnSnapshot:
    committed_turn_id: str
    profile_id: str
    run_id: str
    stream_sequence: int
    role: str  # user | assistant
    source: str
    occurred_at: str  # ISO8601 UTC
    content: str
    is_private: bool
