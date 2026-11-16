"""Per-profile emotional warmth (K5, warmth-guardrails-runtime-20261006).

A profile carries ``warmth``, one of :data:`WARMTH_LEVELS`. ``cold`` is the
default and today's behaviour: no warmth block in the prompt and no extra
output check. Any missing or unknown value normalizes to ``cold``.

The active level is also held here as one immutable string swapped atomically,
so the output guard (stateless, called from many paths and threads) can read
it without every caller threading the profile through. The engine sets it on
``set_profile``. Nothing here reads disk.

This module must not import ``opencohost.config.validation`` (the guard
imports this module).
"""

from __future__ import annotations

WARMTH_COLD = "cold"
WARMTH_EMPATHETIC = "empathetic"
WARMTH_IN_CHARACTER = "in_character"
WARMTH_FREE = "free"
WARMTH_SENTIMENTAL = "sentimental"

# Ordered by emotional intensity (K8 added ``sentimental``, the most emotive).
WARMTH_LEVELS: tuple[str, ...] = (
    WARMTH_COLD,
    WARMTH_EMPATHETIC,
    WARMTH_IN_CHARACTER,
    WARMTH_FREE,
    WARMTH_SENTIMENTAL,
)
# Levels where Kira speaks about her own feelings in the first person; the
# machine-lecture output check ("soy un algoritmo", "I don't have feelings")
# runs only at these. ``empathetic`` shows warmth without claiming feelings.
FIRST_PERSON_FEELING_LEVELS: frozenset[str] = frozenset(
    {WARMTH_IN_CHARACTER, WARMTH_FREE, WARMTH_SENTIMENTAL}
)
DEFAULT_WARMTH = WARMTH_COLD

_active: str = DEFAULT_WARMTH


def normalize_warmth(value: object) -> str:
    """The level named by ``value``, or ``cold`` for anything else."""
    if not isinstance(value, str):
        return DEFAULT_WARMTH
    level = value.strip().lower()
    return level if level in WARMTH_LEVELS else DEFAULT_WARMTH


def set_active_warmth(value: object) -> str:
    """Make ``value`` (normalized) the active level; returns what was set."""
    global _active
    _active = normalize_warmth(value)
    return _active


def active_warmth() -> str:
    """The active profile's warmth level (``cold`` until a profile sets one)."""
    return _active


def is_warm(value: object = None) -> bool:
    """True when ``value`` (or the active level, when None) is not ``cold``."""
    level = active_warmth() if value is None else normalize_warmth(value)
    return level != WARMTH_COLD


def allows_first_person_feelings(value: object = None) -> bool:
    """True when ``value`` (or the active level, when None) lets Kira voice
    her own feelings (``in_character``, ``free``, ``sentimental``)."""
    level = active_warmth() if value is None else normalize_warmth(value)
    return level in FIRST_PERSON_FEELING_LEVELS
