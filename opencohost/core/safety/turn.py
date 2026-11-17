"""Per-turn crisis and sensitive-care flags (guardrails-vulnerable-users S1 + contextual).

A turn is marked with a contextual safety severity by the engine or controller
for the duration of its generation. ``output_guard``, prompt assembly, and the
streaming guard read these ContextVars so callers never have to thread flags
through the guard stack and state never leaks between turns.
"""
from __future__ import annotations

import contextlib
import contextvars

_crisis_turn: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "opencohost_crisis_turn", default=False
)
_safety_severity: contextvars.ContextVar[str] = contextvars.ContextVar(
    "opencohost_safety_severity", default="none"
)


def is_crisis_turn() -> bool:
    """True while the current generation answers a CRISIS_INTERVENTION message."""
    return _crisis_turn.get() or _safety_severity.get() == "crisis_intervention"


def active_safety_severity() -> str:
    """Return the active per-turn contextual severity string."""
    if _crisis_turn.get() and _safety_severity.get() == "none":
        return "crisis_intervention"
    return _safety_severity.get()


def is_sensitive_care_turn() -> bool:
    """True when persona sarcasm/roasting must be stripped for care or crisis."""
    return is_crisis_turn() or _safety_severity.get() in {
        "sensitive_care",
        "crisis_intervention",
    }


@contextlib.contextmanager
def crisis_turn(active: bool = True):
    """Mark the enclosed generation as a crisis turn (re-entrant, restores on exit)."""
    flag = bool(active)
    t_crisis = _crisis_turn.set(flag)
    t_sev = _safety_severity.set("crisis_intervention" if flag else "none")
    try:
        yield
    finally:
        _safety_severity.reset(t_sev)
        _crisis_turn.reset(t_crisis)


@contextlib.contextmanager
def safety_turn(severity: object = "none"):
    """Mark the enclosed generation with a ``SafetySeverity`` (or its string value)."""
    raw = getattr(severity, "value", severity)
    sev_str = str(raw or "none").strip().lower() or "none"
    t_sev = _safety_severity.set(sev_str)
    t_crisis = _crisis_turn.set(sev_str == "crisis_intervention")
    try:
        yield
    finally:
        _crisis_turn.reset(t_crisis)
        _safety_severity.reset(t_sev)
