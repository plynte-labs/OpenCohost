"""Per-stage durations of one foreground turn, printed on ``[TURN_LATENCY]``.

Every stage is measured where it already happens (memory recall, the web
lookup's fetch and filter, the buffered LLM call, the streamed LLM and
first-audio marks) and recorded into a sink that only the foreground turn
opens. Pregen, agenda and connector generation run outside a turn clock, so
their stages are dropped instead of leaking into a turn.

Values are whole milliseconds. A stage that did not run on the turn is absent
from the sink and prints as ``-``. Nothing here stores or logs text.
"""

from __future__ import annotations

import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

# Field order on the line; every key is always printed, as a number or "-".
STAGE_KEYS = ("recall_ms", "web_ms", "prompt_build_ms", "llm_ms", "ttfa_ms")

_local = threading.local()


@contextmanager
def turn_clock() -> Iterator[dict]:
    """Open the stage sink for the current thread's foreground turn."""
    previous = getattr(_local, "sink", None)
    sink: dict = {}
    _local.sink = sink
    try:
        yield sink
    finally:
        _local.sink = previous


def add(stage: str, ms: Optional[int]) -> None:
    """Add an already-measured duration to the current turn. No-op off-turn."""
    sink = getattr(_local, "sink", None)
    if sink is None or ms is None:
        return
    sink[stage] = sink.get(stage, 0) + max(0, int(ms))


# How the turn ended, kept in the same sink under a non-stage key so the
# per-turn value stays on the thread that runs the turn. Never printed by
# stage_fields(); read through outcome_of() on the ``[TURN_LATENCY]`` line.
OUTCOME_KEY = "outcome"


def mark_outcome(outcome: str) -> None:
    """Record how the foreground turn ended (``retried`` or ``fallback``).
    No-op off-turn, like ``add``."""
    sink = getattr(_local, "sink", None)
    if sink is not None:
        sink[OUTCOME_KEY] = outcome


def outcome_of(sink: dict) -> str:
    """The marked outcome of a finished turn, ``ok`` when nothing was marked."""
    return str(sink.get(OUTCOME_KEY, "ok"))


def mark(exclude: Optional[str] = None) -> tuple:
    """Start a span. ``exclude`` names a stage recorded inside the span, which
    close() subtracts so the span reports only its own time."""
    sink = getattr(_local, "sink", None)
    nested = sink.get(exclude, 0) if (sink is not None and exclude) else 0
    return (time.monotonic(), exclude, nested)


def close(token: tuple, stage: str) -> None:
    """Record the time since ``mark()`` as *stage*, minus any excluded stage."""
    started, exclude, nested_before = token
    sink = getattr(_local, "sink", None)
    if sink is None:
        return
    elapsed = int((time.monotonic() - started) * 1000)
    nested = (sink.get(exclude, 0) - nested_before) if exclude else 0
    add(stage, max(0, elapsed - nested))


def stage_fields(stage_ms: dict) -> str:
    """``recall_ms=N web_ms=- ...`` in STAGE_KEYS order, ``-`` where absent."""
    return " ".join(
        f"{key}={stage_ms[key]}" if key in stage_ms else f"{key}=-" for key in STAGE_KEYS
    )
