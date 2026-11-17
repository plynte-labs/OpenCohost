"""Explicit state of one foreground turn (turn-state-machine-20261112).

A foreground turn used to be implicit: a few ``MotorVocalIA`` booleans plus the
partial ``StreamAttemptState`` / ``SpeechJobState`` models. ``TurnState`` makes
it explicit: one declarative ``TRANSITIONS`` table, and one ``[TURN_STATE]`` log
line for every transition, so a failed turn can be traced from the log alone.

This module is pure and side-effect free apart from logging. It never raises
into the caller: an illegal transition is logged as a warning and keeps the
phase; any event after a terminal phase is ignored with a debug line.

The open turn is bound to the THREAD that opened it (``open_turn`` /
``close_turn``, like ``turn_timing.turn_clock``). Background pregen and agenda
generations run the same code paths on daemon threads; they see no bound turn,
so their hooks are no-ops and can never touch the foreground turn.

The terminal ``completed`` means the reply was handed to speech; playback goes
on in the speech router (speech-side lines are a separate task).

Privacy: a ``[TURN_STATE]`` line carries codes and integers only (turn id,
source code, phases, event, optional ``ms`` / ``attempt``). Never prompt, chat
or reply text. See ``docs/modules/turn-state.md``.
"""

from __future__ import annotations

import logging
import re
import sys
import threading
import time
import uuid
from enum import Enum
from typing import Any, Optional

from opencohost.core.engine import turn_events

logger = logging.getLogger("OpenCohost")

# Numeric fields a transition line may carry; anything else is dropped.
_NUMERIC_FIELDS = ("ms", "attempt")
_SOURCE_BAD = re.compile(r"[^A-Za-z0-9_.:-]+")
_SOURCE_MAX = 40
_local = threading.local()


class TurnPhase(str, Enum):
    ASSEMBLING = "assembling"
    GENERATING = "generating"
    STREAMING = "streaming"
    GUARD_BLOCKED = "guard_blocked"
    REVERTED = "reverted"
    RECOVERING = "recovering"
    COMPLETED = "completed"
    DROPPED = "dropped"
    FAILED = "failed"
    REFUSED = "refused"
    EXPIRED = "expired"


class TurnEvent(str, Enum):
    BEGIN = "begin"
    PROMPT_BUILT = "prompt_built"
    FIRST_AUDIO = "first_audio"
    GUARD_REVERT = "guard_revert"
    GUARD_BLOCK = "guard_block"
    GUARD_RETRY = "guard_retry"
    STREAM_TRUNCATE = "stream_truncate"
    STALL = "stall"
    ROLLBACK = "rollback"
    CLOUD_RETRY = "cloud_retry"
    COMPLETE = "complete"
    DROP = "drop"
    FAIL = "fail"
    REFUSE = "refuse"
    EXPIRE = "expire"
    SERVED_PREGEN = "served_pregen"


TERMINAL_PHASES = frozenset({
    TurnPhase.COMPLETED, TurnPhase.DROPPED, TurnPhase.FAILED,
    TurnPhase.REFUSED, TurnPhase.EXPIRED,
})

_P, _E = TurnPhase, TurnEvent

# Guard-side events recorded as the event's verdict codes.
_GUARD_EVENTS = frozenset({
    TurnEvent.GUARD_REVERT, TurnEvent.GUARD_BLOCK, TurnEvent.GUARD_RETRY,
    TurnEvent.STREAM_TRUNCATE,
})

# The one state x event table. A (phase, event) pair not listed is illegal.
TRANSITIONS: dict = {
    (_P.ASSEMBLING, _E.PROMPT_BUILT): _P.GENERATING,
    # Queue outcomes: the turn never reached generation.
    (_P.ASSEMBLING, _E.SERVED_PREGEN): _P.COMPLETED,
    (_P.ASSEMBLING, _E.REFUSE): _P.REFUSED,
    (_P.ASSEMBLING, _E.EXPIRE): _P.EXPIRED,
    (_P.GENERATING, _E.FIRST_AUDIO): _P.STREAMING,
    (_P.GENERATING, _E.GUARD_REVERT): _P.REVERTED,
    (_P.GENERATING, _E.GUARD_BLOCK): _P.GUARD_BLOCKED,
    (_P.GENERATING, _E.STALL): _P.RECOVERING,
    (_P.GENERATING, _E.CLOUD_RETRY): _P.ASSEMBLING,
    (_P.STREAMING, _E.STREAM_TRUNCATE): _P.GUARD_BLOCKED,
    (_P.STREAMING, _E.STALL): _P.RECOVERING,
    (_P.REVERTED, _E.GUARD_BLOCK): _P.GUARD_BLOCKED,
    (_P.REVERTED, _E.STALL): _P.RECOVERING,
    (_P.REVERTED, _E.CLOUD_RETRY): _P.ASSEMBLING,
    (_P.GUARD_BLOCKED, _E.GUARD_RETRY): _P.GENERATING,
    (_P.GUARD_BLOCKED, _E.CLOUD_RETRY): _P.ASSEMBLING,
    (_P.RECOVERING, _E.ROLLBACK): _P.RECOVERING,
    (_P.RECOVERING, _E.CLOUD_RETRY): _P.ASSEMBLING,
}
# Every active phase can close in each terminal state.
for _phase in TurnPhase:
    if _phase not in TERMINAL_PHASES:
        TRANSITIONS[(_phase, _E.COMPLETE)] = _P.COMPLETED
        TRANSITIONS[(_phase, _E.DROP)] = _P.DROPPED
        TRANSITIONS[(_phase, _E.FAIL)] = _P.FAILED
del _phase


def new_turn_id() -> str:
    """A short opaque turn id: ``t`` + 8 hex chars.

    The ``t`` prefix keeps it apart from prompt_trace's per-call ``turn=`` id
    (bare hex), so log readers never confuse the two."""
    return "t" + uuid.uuid4().hex[:8]


def _source_code(source: Any) -> str:
    code = _SOURCE_BAD.sub("_", str(source or "unknown"))[:_SOURCE_MAX]
    return code or "unknown"


def _numeric_suffix(fields: dict) -> str:
    parts = []
    for name in _NUMERIC_FIELDS:
        value = fields.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            parts.append(f" {name}={value}")
    return "".join(parts)


class TurnState:
    """The explicit state of one foreground turn. Engine thread only."""

    # Accumulator for the one JSONL event written at close (turn_events.py):
    # codes and integers only, see ``to_event``.
    __slots__ = (
        "turn_id", "source", "phase", "model", "phases", "counts", "guards",
        "attempts", "cloud_retry", "timings", "latency_outcome", "path",
        "reasons", "speech", "started_ms",
    )

    def __init__(self, turn_id: str, source: str) -> None:
        self.turn_id = str(turn_id)
        self.source = _source_code(source)
        self.phase = TurnPhase.ASSEMBLING
        self.model: Optional[str] = None
        self.phases: list = [TurnPhase.ASSEMBLING.value]
        self.counts: dict = {}
        self.guards: list = []
        self.attempts = 0
        self.cloud_retry = False
        self.timings: dict = {}
        self.latency_outcome: Optional[str] = None
        self.path: Optional[str] = None
        self.reasons: list = []
        self.speech: Optional[str] = None
        self.started_ms = int(time.time() * 1000)

    def _accumulate(self, event: TurnEvent, target: TurnPhase, numeric: dict) -> None:
        """Fold one applied transition into the event accumulator. Never raises."""
        try:
            if len(self.phases) < 64:
                self.phases.append(target.value)
            self.counts[event.value] = self.counts.get(event.value, 0) + 1
            if event in _GUARD_EVENTS and len(self.guards) < 16:
                self.guards.append(event.value)
            if event is TurnEvent.CLOUD_RETRY:
                self.cloud_retry = True
            if event in (TurnEvent.PROMPT_BUILT, TurnEvent.GUARD_RETRY):
                self.attempts += 1
            attempt = numeric.get("attempt")
            if isinstance(attempt, int) and not isinstance(attempt, bool):
                self.attempts = max(self.attempts, attempt)
        except Exception:
            logger.debug("[TURN_STATE] accumulate failed", exc_info=True)

    def add_reason(self, reason: str, exc_name: Optional[str] = None) -> None:
        """Record a failure reason code and the exception CLASS name; never a message."""
        try:
            item = {"reason": str(reason), "exc": str(exc_name or "none")}
            if len(self.reasons) < 8 and item not in self.reasons:
                self.reasons.append(item)
        except Exception:
            logger.debug("[TURN_STATE] reason failed", exc_info=True)

    def to_event(self) -> dict:
        """The raw (pre-allowlist) event dict; ``turn_events.sanitize`` filters it."""
        return {
            "ts": self.started_ms,
            "tid": self.turn_id,
            "source": self.source.lower(),
            "model": self.model,
            "outcome": self.phase.value,
            "latency_outcome": self.latency_outcome,
            "path": self.path,
            "phases": list(self.phases),
            "events": dict(self.counts),
            "guards": list(self.guards),
            "attempts": self.attempts,
            "cloud_retry": self.cloud_retry,
            "timings": dict(self.timings),
            "reasons": list(self.reasons),
            "speech": self.speech,
        }

    @property
    def is_terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    def begin(self) -> TurnPhase:
        """Log the turn's start line (``start->assembling event=begin``)."""
        logger.info(
            "[TURN_STATE] tid=%s source=%s start->%s event=%s",
            self.turn_id, self.source, self.phase.value, TurnEvent.BEGIN.value,
        )
        return self.phase

    def apply(self, event: TurnEvent, **numeric: Any) -> TurnPhase:
        """Apply ``event`` and return the (possibly unchanged) phase. Never raises."""
        try:
            event = TurnEvent(event)
        except (ValueError, TypeError):
            logger.debug("[TURN_STATE] unknown event ignored tid=%s", self.turn_id)
            return self.phase
        if self.is_terminal:
            logger.debug(
                "[TURN_STATE] ignored tid=%s phase=%s event=%s",
                self.turn_id, self.phase.value, event.value,
            )
            return self.phase
        target: Optional[TurnPhase] = TRANSITIONS.get((self.phase, event))
        if target is None:
            logger.warning(
                "[TURN_STATE] illegal tid=%s source=%s phase=%s event=%s",
                self.turn_id, self.source, self.phase.value, event.value,
            )
            return self.phase
        previous, self.phase = self.phase, target
        self._accumulate(event, target, numeric)
        logger.info(
            "[TURN_STATE] tid=%s source=%s %s->%s event=%s%s",
            self.turn_id, self.source, previous.value, target.value, event.value,
            _numeric_suffix(numeric),
        )
        return self.phase


def current() -> Optional[TurnState]:
    """The turn bound to the calling thread, or None."""
    return getattr(_local, "turn", None)


def current_tid() -> Optional[str]:
    """The tid of the turn bound to the calling thread, or None. Never raises.

    The speech router stamps it on a ``SpeechJob`` at submit (``turn_uid``)."""
    try:
        turn = current()
        return turn.turn_id if turn is not None else None
    except Exception:
        return None


def exc_site(exc: BaseException) -> str:
    """``file.py:123:function`` of where ``exc`` was raised; never its message.

    An exception message can quote the prompt, the chat or a provider reply, so
    log lines name the class (``type(exc).__name__``) and this site instead of
    ``str(exc)`` or a traceback. Never raises."""
    try:
        tb = exc.__traceback__
        if tb is None:
            return "unknown"
        while tb.tb_next is not None:
            tb = tb.tb_next
        code = tb.tb_frame.f_code
        name = code.co_filename.replace("\\", "/").rsplit("/", 1)[-1]
        return _SOURCE_BAD.sub("_", f"{name}:{tb.tb_lineno}:{code.co_name}")[:80]
    except Exception:
        return "unknown"


def log_speech(tid: Optional[str], job: Any, state: str, reason: str) -> None:
    """One ``[TURN_SPEECH]`` line (codes only); a no-op without a tid.

    ``job`` is the router job id, or None for the router-off inline path
    (logged as ``job=-``). Never raises. Callers must not hold the router's
    ``_sched_lock``."""
    try:
        if tid:
            logger.info(
                "[TURN_SPEECH] tid=%s job=%s state=%s reason=%s",
                tid, "-" if job is None else job, state, reason,
            )
            # Known at close only for the router-off inline path (same thread);
            # router-thread outcomes land after the event is written.
            turn = current()
            if turn is not None and turn.turn_id == tid:
                turn.speech = str(state)
    except Exception:
        pass


def open_turn(source: str) -> Optional[TurnState]:
    """Open a foreground turn on the calling thread and log its begin line.

    A turn still bound here (a missed close) is closed as failed first, so it
    still ends in exactly one terminal line. Never raises; None on error."""
    try:
        if current() is not None:
            close_turn()
        turn = TurnState(new_turn_id(), source)
        _local.turn = turn
        turn.begin()
        return turn
    except Exception:
        _local.turn = None
        logger.debug("[TURN_STATE] open failed", exc_info=True)
        return None


def close_turn() -> None:
    """Unbind the calling thread's turn; one still open is closed as failed.

    Call it from the owner's ``finally`` so every turn gets exactly one
    terminal line, even on an exception. Never raises."""
    turn = current()
    _local.turn = None
    if turn is None:
        return
    try:
        if not turn.is_terminal:
            # Called from the owner's ``finally``: the in-flight exception, if any.
            aborting = sys.exc_info()[1]
            turn.add_reason(
                "turn_aborted" if aborting is not None else "turn_unclosed",
                type(aborting).__name__ if aborting is not None else None,
            )
            turn.apply(TurnEvent.FAIL)
    except Exception:
        logger.debug("[TURN_STATE] close failed", exc_info=True)
    try:
        turn_events.write(turn.to_event())
    except Exception:
        logger.debug("[TURN_STATE] event write failed", exc_info=True)


def emit(event: TurnEvent, **numeric: Any) -> None:
    """Apply ``event`` to the calling thread's open turn, if any. Never raises.

    The one-liner hook for engine call sites: no bound turn (no foreground
    turn on this thread) is a no-op, and a tracker error is swallowed with a
    debug line so instrumentation can never break a turn.
    """
    try:
        turn = current()
        if turn is not None:
            turn.apply(event, **numeric)
    except Exception:
        logger.debug("[TURN_STATE] tracker error swallowed", exc_info=True)


def note(**fields: Any) -> None:
    """Set accumulator fields (``model``) on the calling thread's open turn.

    No bound turn is a no-op. Values are validated by ``turn_events.sanitize``
    at close, so a bad value is dropped there. Never raises."""
    try:
        turn = current()
        if turn is None:
            return
        if "model" in fields:
            turn.model = str(fields["model"]) if fields["model"] else None
    except Exception:
        logger.debug("[TURN_STATE] note failed", exc_info=True)


def record_latency(
    *, engine_ms: int, queue_wait_ms: Optional[int], stage_ms: dict, path: str, outcome: str
) -> None:
    """Copy the ``[TURN_LATENCY]`` numbers onto the open turn. Never raises."""
    try:
        turn = current()
        if turn is None:
            return
        timings = {"engine_ms": int(engine_ms)}
        if queue_wait_ms is not None:
            timings["queue_wait_ms"] = int(queue_wait_ms)
            timings["total_ms"] = int(queue_wait_ms) + int(engine_ms)
        for key in turn_events.TIMING_KEYS:
            if key in stage_ms:
                timings[key] = int(stage_ms[key])
        turn.timings = timings
        turn.latency_outcome = str(outcome)
        turn.path = str(path)
    except Exception:
        logger.debug("[TURN_STATE] latency record failed", exc_info=True)


def fail_reason(code: str, exc: Optional[BaseException] = None) -> None:
    """Record a failure reason code on the calling thread's open turn.

    ``code`` is a fixed member of ``turn_events.REASON_CODES`` (anything else is
    dropped when the event is written); ``exc`` contributes its CLASS name only,
    never its message. No bound turn (pregen, agenda background) is a no-op.
    Never raises."""
    try:
        turn = current()
        if turn is not None:
            turn.add_reason(code, type(exc).__name__ if exc is not None else None)
    except Exception:
        logger.debug("[TURN_STATE] reason failed", exc_info=True)
