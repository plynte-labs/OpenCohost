"""Headless agenda driver for the Kira FastAPI API layer (FIX-C).

`KiraAgendaController` is passive by design: it owns state and prompt
construction but never enqueues anything or talks to the motor.  In the CTK
app, `app_shell.py` drives it with a ~4.5s Tk `after` tick loop
(`_kira_agenda_tick`) plus motor-event feedback (`speaking_start` ->
`mark_generation_accepted`, `speaking_end` -> `mark_speech_complete`).  The
headless host has no Tk main loop, so this module replicates that driver with
a single daemon thread and two small pure helpers shared with the API routes.

Thread-safety: every entry point that touches the controller runs under the
host's `agenda_lock` (the controller is plain lists/attrs — not thread-safe).
The driver thread takes the lock for the whole tick body; the engine-thread
guardrail callbacks and the motor-event router take the SAME lock.  Hold times
stay short — `next_action` is pure in-memory and the motor enqueue/replace is
a bounded list operation.  The engine thread never holds a motor lock while it
acquires `agenda_lock` (guardrails run post-dequeue, motor events fire outside
`_lock`), so there is no lock-order inversion with the driver, which acquires
motor locks only after `agenda_lock`.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable, Optional

from opencohost.smart_aggregator.kira_agenda_controller import AgendaState

_logger = logging.getLogger(__name__)

# States in which the driver must not tick at all (mirror app_shell.py:1625).
_INERT_STATES = frozenset({AgendaState.OFF, AgendaState.HARD_PAUSED})

# CTK cadence: reschedule every 4500ms (app_shell.py:1622/1632).
DEFAULT_TICK_SECONDS = 4.5

# Headless analog of CTK's 50ms `after` poll: when a turn ends before its
# prefetched draft is ready, re-tick this soon instead of waiting the cadence.
PREFETCH_RETICK_SECONDS = 0.25


@dataclass(frozen=True)
class PrefetchState:
    """A draft agenda turn generated in the background during the prior speech.

    `action` is the previewed `AgendaAction` (adopted verbatim via
    `start_prefetched_action`); `topic_id` pins it to the topic that was active
    when it was spawned, so a topic change between speaking_start and
    speaking_end invalidates the draft (staleness class #344).
    """

    action: object
    topic_id: Optional[str]


def enqueue_agenda_action(motor, action, *, driver: Optional["AgendaDriver"] = None) -> None:
    """Enqueue one controller `AgendaAction` into the motor.

    Byte-for-byte mirror of `app_shell.py:1634-1646` (`_enqueue_kira_agenda_action`):
    a non-`enqueue` / empty-prompt action is a no-op; `kira-agenda*` sources use
    `replace_pending` so autonomous turns never stack; everything else enqueues
    with the honest `history_text`.  The `clear_prefetched_agenda` call supersedes
    any stale draft — a genuine new generation replaces it.  A valid
    same-continuation draft never reaches here: `_tick_locked` consumes it BEFORE
    `next_action`, so an enqueue only happens when there is no draft to keep.

    Callers MUST already hold `agenda_lock` — this reads `action` (a frozen
    dataclass produced under the lock) and calls the motor, whose own locks are
    always acquired AFTER `agenda_lock`.
    """
    if motor is None or action is None:
        return
    if getattr(action, "kind", None) != "enqueue" or not getattr(action, "prompt", ""):
        return
    clear = getattr(motor, "clear_prefetched_agenda", None)
    if callable(clear):
        try:
            clear()
        except Exception:
            _logger.exception("clear_prefetched_agenda failed during agenda enqueue")
    if driver is not None:
        driver._drop_prefetch_stash()
    if action.source.startswith("kira-agenda") and hasattr(motor, "replace_pending"):
        motor.replace_pending(action.prompt, priority=action.priority, source=action.source)
    else:
        motor.enqueue(
            action.prompt,
            priority=action.priority,
            source=action.source,
            history_text=action.history_text,
        )


def route_motor_event_to_agenda(
    agenda,
    status: str,
    *,
    on_speech_complete: Optional[Callable[[], None]] = None,
    on_agenda_speaking_start: Optional[Callable[[], None]] = None,
) -> None:
    """Route a motor status string into agenda-controller feedback.

    Mirror of the CTK motor-event handlers (`motor_event_handlers.py:352-356`
    for `speaking_start`, `:404-407` for `speaking_end`) — the SAME controller
    state guards decide whether the event belongs to an agenda-generated turn.
    `on_speech_complete` fires only when `mark_speech_complete` actually ran, so
    the caller can nudge the driver for an immediate re-tick (CTK's
    `kira_agenda_schedule_tick(1200)`).

    `on_agenda_speaking_start` fires only after `mark_generation_accepted`
    actually advanced the controller to SPEAKING — i.e. the turn now playing is
    a controller-generated agenda turn (the #344 source gate). The caller uses
    it to spawn a background prefetch of the NEXT turn while this one plays.

    Callers MUST already hold `agenda_lock`.
    """
    if agenda is None:
        return
    if status == "speaking_start":
        if agenda.state in {
            AgendaState.SPEAKING,
            AgendaState.GENERATING,
            AgendaState.TOPIC_CLOSING,
        }:
            agenda.mark_generation_accepted()
            if on_agenda_speaking_start is not None:
                on_agenda_speaking_start()
    elif status == "speaking_end":
        if agenda.state in {AgendaState.SPEAKING, AgendaState.GENERATING}:
            agenda.mark_speech_complete()
            if on_speech_complete is not None:
                on_speech_complete()


class AgendaDriver:
    """Single daemon thread that drives a passive `KiraAgendaController`.

    Replicates the CTK tick loop (`app_shell.py:1539-1632`) without Tk: a
    `threading.Event` wait-with-timeout gives the ~4.5s cadence while letting
    `enable`/`soft_stop`/`emergency_stop`/`speaking_end` `nudge()` an immediate
    re-tick.  Everything is injectable so tests can drive `tick_once()`
    synchronously with a fake motor and a real controller.
    """

    def __init__(
        self,
        *,
        get_agenda: Callable[[], object],
        get_motor: Callable[[], object],
        agenda_lock: threading.Lock,
        tick_seconds: float = DEFAULT_TICK_SECONDS,
        log: Optional[Callable[[str], None]] = None,
    ) -> None:
        self._get_agenda = get_agenda
        self._get_motor = get_motor
        self._agenda_lock = agenda_lock
        self._tick_seconds = tick_seconds
        self._log = log
        self._wake = threading.Event()
        self._shutdown = threading.Event()
        self._thread: Optional[threading.Thread] = None
        # Prefetch stash for the next turn: written/read only under `agenda_lock`
        # (inside the tick body). `_next_wait` is a one-shot short re-tick request
        # set under the lock in `_maybe_consume_prefetch` and consumed by `_run`
        # on the SAME driver thread, so it needs no lock of its own.
        self._prefetch: Optional[PrefetchState] = None
        self._next_wait: Optional[float] = None

    # ── lifecycle ──────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None:
            return
        self._shutdown.clear()
        self._thread = threading.Thread(target=self._run, name="agenda-driver", daemon=True)
        self._thread.start()

    def nudge(self) -> None:
        """Wake the driver for an immediate tick without waiting out the cadence."""
        self._wake.set()

    def stop(self, timeout: float = 2.0) -> None:
        """Signal shutdown and join the thread (best-effort, bounded)."""
        self._shutdown.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None

    def is_running(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    # ── loop ───────────────────────────────────────────────────────────
    def _run(self) -> None:
        while not self._shutdown.is_set():
            # A pending short re-tick (late prefetch draft) overrides the cadence
            # for exactly one loop; consumed here so it never sticks.
            wait = self._next_wait if self._next_wait is not None else self._tick_seconds
            self._next_wait = None
            self._wake.wait(timeout=wait)
            self._wake.clear()
            if self._shutdown.is_set():
                break
            try:
                self.tick_once()
            except Exception:
                _logger.exception("AgendaDriver tick failed")

    def tick_once(self) -> None:
        """Run exactly one tick body under `agenda_lock` (mirror of _kira_agenda_tick)."""
        with self._agenda_lock:
            self._tick_locked()

    def _tick_locked(self) -> None:
        agenda = self._get_agenda()
        motor = self._get_motor()
        if agenda is None or motor is None:
            return
        state = agenda.state
        if state in _INERT_STATES:
            # A parked/stopped agenda can never speak a stashed draft.
            if self._prefetch is not None:
                self._clear_prefetch(motor)
            return
        # Consume a ready prefetched draft BEFORE next_action, so a valid
        # same-continuation draft is spoken (no second LLM call) and never
        # clobbered by the enqueue path below.
        if self._maybe_consume_prefetch(agenda, motor):
            return
        # Auto-recovery: PAUSED_NEEDS_OPERATOR mirrors app_shell.py:1544-1563.
        # can_auto_resume() flips state to IDLE on success (falls through to
        # next_action) or to HARD_PAUSED / leaves it PAUSED on failure (return).
        if state == AgendaState.PAUSED_NEEDS_OPERATOR:
            if not agenda.can_auto_resume():
                return
        action = agenda.next_action(
            motor_busy=bool(getattr(motor, "is_processing", False)),
            kira_speaking=bool(getattr(motor, "is_speaking", False)),
        )
        enqueue_agenda_action(motor, action, driver=self)
        # Auto-exit mirror of app_shell.py:1590-1601 — IDLE with nothing left to
        # do means the planned session is done; drop to OFF so the loop idles.
        if (
            action.kind == "none"
            and agenda.state == AgendaState.IDLE
            and agenda.active_topic is None
            and not agenda.queued_topics()
        ):
            agenda.state = AgendaState.OFF

    # ── prefetch (Fase 1: no-dead-air) ─────────────────────────────────────
    def maybe_start_prefetch(self, agenda, motor) -> None:
        """Spawn a background generation of the NEXT agenda turn (speaking_start).

        Caller holds `agenda_lock`. Starts only for genuine agenda speech, never
        when interactive work is queued ahead (#338) or a draft already exists.
        `prefetch_agenda` merely spawns a worker thread and returns, so the lock
        is never held across generation.
        """
        if agenda is None or motor is None or self._prefetch is not None:
            return
        source = getattr(motor, "current_speech_source", None)
        if not (isinstance(source, str) and source.startswith("kira-agenda")):
            return  # a human/other path owns the mic (#344)
        has_pending = getattr(motor, "has_pending_priority_before", None)
        if callable(has_pending) and has_pending(2):
            return  # interactive work is queued ahead of agenda (#338)
        preview = getattr(agenda, "prefetch_action_after_current_speech", None)
        if not callable(preview):
            return
        action = preview()
        if getattr(action, "kind", None) != "enqueue" or not getattr(action, "prompt", ""):
            return
        started = motor.prefetch_agenda(
            action.prompt,
            priority=getattr(action, "priority", 2),
            source=getattr(action, "source", "kira-agenda"),
        )
        if started:
            topic = getattr(agenda, "active_topic", None)
            self._prefetch = PrefetchState(action=action, topic_id=getattr(topic, "id", None))

    def _maybe_consume_prefetch(self, agenda, motor) -> bool:
        """Speak a ready prefetched draft at the post-speech boundary.

        Caller holds `agenda_lock`. Returns True when it HANDLED the tick (played
        the draft or requested a short re-tick for a late one); False when there
        is nothing to consume and the normal tick should proceed.
        """
        stash = self._prefetch
        if stash is None:
            return False
        topic = getattr(agenda, "active_topic", None)
        # Topic gone or swapped under us -> the draft is stale.
        if topic is None or getattr(topic, "id", None) != stash.topic_id:
            self._clear_prefetch(motor)
            return False
        # Not yet at the post-speech boundary (still SPEAKING the prior turn):
        # keep the draft, let the tick fall through (it no-ops while the motor is
        # busy). This is why a mid-speech cadence tick can't drop the draft.
        if agenda.state != AgendaState.WAITING_SIGNAL:
            return False
        # Interactive work queued ahead of this draft's priority -> yield (#338).
        has_pending = getattr(motor, "has_pending_priority_before", None)
        priority = getattr(stash.action, "priority", 2)
        if callable(has_pending) and has_pending(priority):
            self._clear_prefetch(motor)
            return False
        # A human/direct path owns processing or speech -> yield (#344).
        if self._has_non_agenda_audio_work(motor):
            self._clear_prefetch(motor)
            return False
        # Readiness — NON-BLOCKING (never wait on generation under the lock).
        if not motor.wait_prefetched_agenda(0):
            pending = getattr(motor, "prefetch_pending", None)
            if callable(pending) and pending():
                # Draft still generating: re-tick soon (CTK's 50ms poll analog).
                self._next_wait = PREFETCH_RETICK_SECONDS
                return True
            # Worker finished with nothing (error/reject) -> fall back.
            self._clear_prefetch(motor)
            return False
        # Adopt (staleness re-validation) then play the cached text.
        if not agenda.start_prefetched_action(stash.action):
            self._clear_prefetch(motor)
            return False
        if not motor.play_prefetched_agenda():
            # Engine cache raced empty after adoption. Restore the post-speech
            # state and re-tick so the next pass regenerates cleanly, instead of
            # leaving the controller stuck in GENERATING (a latent CTK race).
            # For a stop draft `start_prefetched_action` already flipped the topic
            # to CLOSING; we leave that as-is — WAITING_SIGNAL + CLOSING self-heals
            # on the next tick (next_action re-reaches the closing action), which
            # is safer than restoring a status we did not snapshot.
            agenda.state = AgendaState.WAITING_SIGNAL
            self._drop_prefetch_stash()
            self._next_wait = PREFETCH_RETICK_SECONDS
            return True
        self._drop_prefetch_stash()
        return True

    def _has_non_agenda_audio_work(self, motor) -> bool:
        """Mirror of agenda_audio_controller.kira_agenda_has_non_agenda_audio_work."""
        proc_source = str(getattr(motor, "current_processing_source", "") or "")
        speech_source = str(getattr(motor, "current_speech_source", "") or "")
        if bool(getattr(motor, "is_processing", False)) and not proc_source.startswith("kira-agenda"):
            return True
        if bool(getattr(motor, "is_speaking", False)) and not speech_source.startswith("kira-agenda"):
            return True
        return False

    def _clear_prefetch(self, motor) -> None:
        """Drop the driver stash AND invalidate the engine draft (epoch bump)."""
        self._prefetch = None
        clear = getattr(motor, "clear_prefetched_agenda", None)
        if callable(clear):
            try:
                clear()
            except Exception:
                _logger.exception("clear_prefetched_agenda failed during prefetch drop")

    def _drop_prefetch_stash(self) -> None:
        """Drop the driver stash only (the engine draft was consumed/cleared)."""
        self._prefetch = None
