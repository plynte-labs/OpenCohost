"""Regression: ChatReactionCore._recent_kira_lines must hold the engine's
_history_lock while snapshotting `historial` (Finding 1, judgment day
2026-08-13).

Bug: `_recent_kira_lines` ran `list(self._motor_ia.historial)` with no lock,
while the engine's worker/agenda-speaker threads append to that same deque
under `_history_lock` (llm_engine.py `_commit_history` /
`_build_generation_request`). A concurrent append during the unguarded
`list()` call raises `RuntimeError: deque mutated during iteration` --
swallowed by `engine_host._on_aggregated_context`'s outer except, so the
whole aggregated chat context is dropped silently.

`test_recent_kira_lines_holds_history_lock_during_iteration` is the
deterministic proof the task allows as a minimum bar: it asserts the lock is
actually HELD at the moment `historial` is iterated, rather than relying on a
timing-dependent thread interleave to happen to raise
`RuntimeError: deque mutated during iteration` in one particular run --
a background-thread race for this exact call was tried and, on a `list()`
over a small deque, the C-level iteration finishes fast enough that the
GIL rarely switches mid-call, so a wall-clock race is not a reliable
regression signal here (it passed against the unfixed code too). The
lock-holding assertion below fails deterministically pre-fix and passes
deterministically post-fix, which is what a regression test needs.
"""
from __future__ import annotations

import threading
from collections import deque
from types import SimpleNamespace

from opencohost.smart_aggregator.chat_reaction import ChatReactionCore


class _LockCheckingDeque(deque):
    """A deque that records whether `lock` was held at the moment iteration
    started -- lets a test assert "the lock is held during iteration"
    without depending on thread-scheduling luck."""

    def __init__(self, items, lock: threading.Lock):
        super().__init__(items)
        self._lock = lock
        self.was_locked_during_iter = None

    def __iter__(self):
        self.was_locked_during_iter = self._lock.locked()
        return super().__iter__()


def _fake_motor(historial):
    # SimpleNamespace mirrors the getattr-only contract _recent_kira_lines
    # relies on -- no real MotorVocalIA/threading.Thread machinery needed.
    return SimpleNamespace(historial=historial)


def test_recent_kira_lines_holds_history_lock_during_iteration():
    lock = threading.Lock()
    tracked = _LockCheckingDeque(
        [{"role": "assistant", "content": "hola", "source": "chat"}],
        lock,
    )
    core = ChatReactionCore(_fake_motor(tracked))
    motor = core._motor_ia
    motor._history_lock = lock

    core._recent_kira_lines(3)

    assert tracked.was_locked_during_iter is True, (
        "_recent_kira_lines iterated historial WITHOUT holding _history_lock -- "
        "a concurrent _commit_history append can now raise "
        "'RuntimeError: deque mutated during iteration'"
    )


def test_recent_kira_lines_degrades_safely_without_history_lock():
    """Several tests build a MotorVocalIA via __new__ (bypassing __init__),
    so _history_lock never gets set. This must not raise AttributeError."""
    historial = deque(
        [
            {"role": "user", "content": "q", "source": "chat"},
            {"role": "assistant", "content": "a", "source": "chat"},
        ]
    )
    core = ChatReactionCore(_fake_motor(historial))
    # No `_history_lock` attribute at all on this stand-in motor.

    result = core._recent_kira_lines(3)

    assert "a" in result
