"""L1 pipeline memory — intra-session truncation ledger (MemoryDigest).

D1: Deterministic, lossy compaction of evicted conversation turns.
D2: Survives model switch / watchdog recovery; dies on profile change,
    clear_history, and app restart (RAM-only, never persisted to disk).
D3: Injected only into direct-path prompts, capped at max_chars, re-sanitized
    at build time (double gate).

FLAGGED EDGE CASES (deferred — owner decision required):
  E3 (double-gate breadth): Current default reuses _sanitize_history_context.
    The broader gate (Spanish markers, NFKC normalization, whole-digest scan)
    is NOT implemented here. Flag for owner: should the digest-level scan also
    check for Spanish injection markers ("olvida todo", "ahora eres") and apply
    NFKC normalization before sanitizing?
  E5 (was_spoken): History commits BEFORE TTS speaks, so an unspoken reply can
    enter the ledger. Default: inherit existing behavior (ledger from history
    as-is). Flag for owner: should the eviction capture be gated on a
    was_spoken flag to prevent unspoken replies from entering the digest?
"""

from __future__ import annotations

from typing import Callable, List, Optional


class MemoryDigest:
    """Bounded FIFO ledger of compacted conversation turns.

    Args:
        max_chars: Maximum total character length of all lines joined by
            newlines. Defaults to 600 to fit ~150 tokens for a 4096-ctx model.
    """

    DEFAULT_MAX_CHARS = 600

    def __init__(self, max_chars: int = DEFAULT_MAX_CHARS) -> None:
        self._max_chars = max_chars
        self._lines: List[str] = []

    @property
    def lines(self) -> List[str]:
        return self._lines

    def append(self, line: str) -> None:
        """Add a ledger line, evicting the oldest entries to stay within cap."""
        # Truncate the incoming line itself if it alone exceeds the cap
        if len(line) > self._max_chars:
            line = line[: self._max_chars]

        self._lines.append(line)

        # FIFO eviction: drop oldest lines until total fits within cap
        while self._lines and len("\n".join(self._lines)) > self._max_chars:
            self._lines.pop(0)

    def clear(self) -> None:
        """Reset the digest (profile change, clear_history)."""
        self._lines = []

    def build_block(
        self, sanitize_fn: Optional[Callable[[str], str]] = None
    ) -> str:
        """Return the digest as a single string block, optionally re-sanitized.

        Renders "[hace N turno(s)]" labels at build time so the counter always
        reflects distance-from-now: the most-recently stored line = "[hace 1
        turno]", next older = "[hace 2 turnos]", etc. N is bounded by the
        number of surviving lines.

        Args:
            sanitize_fn: A callable applied to each line before joining (double
                gate). If None, lines are joined as-is. Pass
                MotorVocalIA._sanitize_history_context as the default gate.

        Returns:
            Joined lines or empty string when the digest is empty.
        """
        if not self._lines:
            return ""

        n_lines = len(self._lines)
        labeled: List[str] = []
        # _lines[0] is oldest, _lines[-1] is newest.
        # oldest → n_lines, newest → 1
        for i, body in enumerate(self._lines):
            distance = n_lines - i          # n_lines down to 1
            label = "turno" if distance == 1 else "turnos"
            line_with_prefix = f"[hace {distance} {label}] {body}"
            if sanitize_fn is not None:
                line_with_prefix = sanitize_fn(line_with_prefix)
            labeled.append(line_with_prefix)

        return "\n".join(labeled)
