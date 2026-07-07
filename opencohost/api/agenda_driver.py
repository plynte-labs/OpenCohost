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
from typing import Callable, Optional

from opencohost.smart_aggregator.kira_agenda_controller import AgendaState

_logger = logging.getLogger(__name__)

# States in which the driver must not tick at all (mirror app_shell.py:1625).
_INERT_STATES = frozenset({AgendaState.OFF, AgendaState.HARD_PAUSED})

# CTK cadence: reschedule every 4500ms (app_shell.py:1622/1632).
DEFAULT_TICK_SECONDS = 4.5


def enqueue_agenda_action(motor, action) -> None:
    """Enqueue one controller `AgendaAction` into the motor.

    Byte-for-byte mirror of `app_shell.py:1634-1646` (`_enqueue_kira_agenda_action`):
    a non-`enqueue` / empty-prompt action is a no-op; `kira-agenda*` sources use
    `replace_pending` so autonomous turns never stack; everything else enqueues
    with the honest `history_text`.  The `clear_prefetched_agenda` call is guarded
    because the headless host never prefetches (and fakes may omit it).

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
) -> None:
    """Route a motor status string into agenda-controller feedback.

    Mirror of the CTK motor-event handlers (`motor_event_handlers.py:352-356`
    for `speaking_start`, `:404-407` for `speaking_end`) — the SAME controller
    state guards decide whether the event belongs to an agenda-generated turn.
    `on_speech_complete` fires only when `mark_speech_complete` actually ran, so
    the caller can nudge the driver for an immediate re-tick (CTK's
    `kira_agenda_schedule_tick(1200)`).

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
            self._wake.wait(timeout=self._tick_seconds)
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
        enqueue_agenda_action(motor, action)
        # Auto-exit mirror of app_shell.py:1590-1601 — IDLE with nothing left to
        # do means the planned session is done; drop to OFF so the loop idles.
        if (
            action.kind == "none"
            and agenda.state == AgendaState.IDLE
            and agenda.active_topic is None
            and not agenda.queued_topics()
        ):
            agenda.state = AgendaState.OFF
