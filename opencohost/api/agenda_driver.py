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

from opencohost.config.settings import FROZEN_STASH_MAX_HOLD_SECONDS
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
    # WU2 (design-fase2.md §2.2/§2.3): match-aware clear. On the consume path this
    # enqueues a ready draft's OWN (prompt, source); clearing the engine cache
    # here would nuke the draft before the worker's pop can match it. The engine's
    # _clear_prefetch_unless_matches skips the clear for that self-match and still
    # supersedes on any genuine new turn. Fall back to the unconditional clear on
    # a stand-in motor that lacks the match-aware primitive.
    clear_unless = getattr(motor, "_clear_prefetch_unless_matches", None)
    if callable(clear_unless):
        try:
            clear_unless(action.prompt, action.source)
        except Exception:
            _logger.exception("match-aware prefetch clear failed during agenda enqueue")
    else:
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
    on_agenda_speaking_end: Optional[Callable[[], None]] = None,
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

    `on_agenda_speaking_end` fires only after `mark_speech_complete` ran (an
    agenda turn just finished), BEFORE `on_speech_complete`/nudge. WU2
    (design-fase2.md §2.4) uses it to consume a ready draft SYNCHRONOUSLY on the
    worker thread, so the enqueue lands before the worker's post-turn pop — no
    accumulation-flush race window (the Judge A finding).

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
            # Consume BEFORE the nudge so the draft is enqueued before the
            # worker's post-turn _process_priority_queue runs (§2.4).
            if on_agenda_speaking_end is not None:
                on_agenda_speaking_end()
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
        # Prefetch stash for the next turn: written under `agenda_lock` by BOTH
        # writers — the driver tick (`_maybe_consume_prefetch`) AND WU2's
        # consume-at-event, which calls `_maybe_consume_prefetch` from the ENGINE
        # thread inside `_hablar`'s speaking_end tail (engine_host.py:528-548).
        # `_next_wait` is a one-shot short re-tick hint: both writers set it while
        # holding `agenda_lock`; `_run`'s read is intentionally unlocked (a benign
        # hint). Worst case of a torn read is one ~4.5s cadence wait instead of a
        # 0.25s re-tick — harmless, because the nudge that follows every consume
        # wakes the driver anyway, so no re-tick is ever actually lost.
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
        self._log_prefetch_desync(agenda, motor)
        # WU5 (design-fase2.md §3 WU5): a pending interruption-return takes
        # priority over normal scheduling — resume the stashed beat (or skip it
        # deterministically) before next_action can generate a fresh turn.
        if self._maybe_return_frozen_stash(agenda, motor):
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

    def _log_prefetch_desync(self, agenda, motor) -> None:
        """WU4 4d structural pairing guard (design-fase2.md §3 WU4-4d).

        Debug-log only (never raise) when the driver's own stash and the
        engine's own draft state disagree: a stash present while the engine
        reports neither a ready cache nor an in-flight worker (an orphan
        stash), or a ready engine draft with no driver stash while the
        agenda is not inert (an orphan engine draft the driver forgot).
        Cheap insurance, not a correctness gate — false positives at debug
        level are acceptable (e.g. an unrelated interactive pregen in flight
        can make `prefetch_pending()` true even though it has nothing to do
        with the driver's own stash).
        """
        try:
            has_cache = bool(motor.wait_prefetched_agenda(0))
            pending = getattr(motor, "prefetch_pending", None)
            # WU5: a frozen interruption-return holds the draft outside the active
            # slot — the driver's mirror stash is intentionally present with no
            # engine cache/pending, so exempt it from the desync warning.
            has_frozen = getattr(motor, "has_frozen_stash", None)
            frozen = bool(callable(has_frozen) and has_frozen())
            has_cache_or_pending = has_cache or bool(callable(pending) and pending()) or frozen
            stash_present = self._prefetch is not None
            if stash_present and not has_cache_or_pending:
                _logger.debug(
                    "AgendaDriver prefetch desync: stash present but the engine has "
                    "no cache/pending draft"
                )
            elif not stash_present and has_cache_or_pending and agenda.state not in _INERT_STATES:
                _logger.debug(
                    "AgendaDriver prefetch desync: engine has a cache/pending draft "
                    "but no driver stash (state=%s)",
                    agenda.state,
                )
        except Exception:
            _logger.exception("AgendaDriver prefetch desync guard failed")

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
        # WU5: while an interruption-return is pending the engine holds the
        # stashed draft in its detour-proof frozen slot; the return path
        # (_maybe_return_frozen_stash) owns it. Consuming/clearing here would
        # drop the driver's mirror stash and strand the return.
        has_frozen = getattr(motor, "has_frozen_stash", None)
        if callable(has_frozen) and has_frozen():
            return False
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
            # WU4 4a boundary telemetry: no draft ever reached this boundary.
            # T1(c) [v5]: route the INFO line through the motor so it reaches
            # the SAME surface as every other "Pregen boundary:" line
            # (motor._log also writes to the UI log_queue) — this module's
            # own `_logger` keeps a DEBUG-level trace only, for module-local
            # diagnostics.
            source = getattr(stash.action, "source", "kira-agenda")
            speech_ms = -1
            speech_ms_fn = getattr(motor, "_speech_ms_for_boundary", None)
            if callable(speech_ms_fn):
                try:
                    speech_ms = speech_ms_fn()
                except Exception:
                    speech_ms = -1
            boundary_line = (
                f"Pregen boundary: draft=none source={source} gap_ms=-1 "
                f"gen_ms=-1 speech_ms={speech_ms}"
            )
            log_fn = getattr(motor, "_log", None)
            if callable(log_fn):
                try:
                    log_fn(boundary_line)
                except Exception:
                    _logger.exception("motor._log failed for the driver's Pregen boundary line")
            _logger.debug(boundary_line)
            self._clear_prefetch(motor)
            return False
        # Adopt (staleness re-validation) then ROUTE the draft through the single
        # queue+worker instead of a parallel speaker thread (WU2, design-fase2.md
        # §2.2/§2.3): enqueue the draft's own (payload, source) via the normal
        # replace_pending path. The worker pops it and hits the pregen cache
        # (_take_pregen_if_match), speaking it with no second LLM call — and the
        # two-mics race is impossible by construction (single dispatch path). A
        # pop-time cache miss (raced clear) falls back to plain generation on the
        # worker: same net behavior as the old play-False branch, worker-side.
        # enqueue_agenda_action drops the driver stash internally (driver=self).
        if not agenda.start_prefetched_action(stash.action):
            self._clear_prefetch(motor)
            return False
        enqueue_agenda_action(motor, stash.action, driver=self)
        return True

    def _maybe_return_frozen_stash(self, agenda, motor) -> bool:
        """WU5 D2/D3 (design-fase2.md §3 WU5): resume a stashed agenda beat after
        a PTT interruption, or skip it deterministically.

        Caller holds `agenda_lock`. Returns True when it HANDLED the tick (resumed
        the beat OR held for an ongoing detour), False when there is no pending
        frozen return and the normal tick should proceed.

        The engine parked the next-turn agenda draft in a detour-proof frozen slot
        at the cut; the driver kept its own `_prefetch` mirror (topic_id). The
        return fires at a clean post-speech boundary once the interactive detour
        is done, restoring the frozen draft into the engine's active slot (with a
        connector resolved) and routing it through the normal consume+pop path.

        D2 skips (fall through to normal next_action): the topic changed/closed,
        the agenda went inert/stopped, or too many interactive turns chained
        (`detour_exceeded`). Profile/model switch and session-stop invalidate the
        engine's frozen stash directly, so `has_frozen_stash()` is already False.
        """
        has_frozen = getattr(motor, "has_frozen_stash", None)
        if not (callable(has_frozen) and has_frozen()):
            return False
        stash = self._prefetch
        topic = getattr(agenda, "active_topic", None)
        state = agenda.state
        # R2: yield to health auto-recovery. PAUSED_NEEDS_OPERATOR is neither inert
        # nor WAITING_SIGNAL, so every gate below would HOLD forever and the normal
        # tick's can_auto_resume branch (app_shell parity) would be unreachable — a
        # health pause after a PTT cut could never auto-recover. Keep the stash
        # FROZEN (do not discard): it survives recovery and returns at the next
        # clean boundary (the deadline backstop below bounds a stash that never does).
        if state == AgendaState.PAUSED_NEEDS_OPERATOR:
            return False
        # D2 topic/state skip: topic gone/swapped, closing, or inert.
        if (
            stash is None
            or topic is None
            or getattr(topic, "id", None) != stash.topic_id
            or state in _INERT_STATES
            or state == AgendaState.TOPIC_CLOSING
        ):
            self._discard_frozen(motor)
            return False
        # D2 detour-count skip (the engine owns the counter).
        detour_exceeded = getattr(motor, "detour_exceeded", None)
        if callable(detour_exceeded) and detour_exceeded():
            self._discard_frozen(motor)
            return False
        # F1: HOLD the return until at least one interactive turn has completed
        # since the cut. The interruption answer rides the command queue (dispatched
        # as a process_context command — invisible to every gate here), so a
        # speaking_end from the cut itself must NOT fire the connector return before
        # the answer speaks. The engine's detour counter increments as the answer
        # turn is selected; until then, hold the tick so no fresh turn generates.
        detour_started = getattr(motor, "detour_started", None)
        if callable(detour_started) and not detour_started():
            # R1 deadline backstop: the engine increments the detour counter as the
            # interruption answer is selected to speak. If that answer never reaches
            # a _note_detour_turn site (dropped by the is_ready gate, TTL-swept
            # before its pop, or lost when dispatch raised after the cut froze the
            # stash), this hold would last forever and the agenda would stay silent.
            # Bound it: once the stash has been held past FROZEN_STASH_MAX_HOLD_SECONDS
            # with no detour, discard it (the discard path resets counters/mirrors)
            # and fall through to a fresh turn. Code-only telemetry, never dialogue.
            age_fn = getattr(motor, "frozen_stash_age_seconds", None)
            age = age_fn() if callable(age_fn) else None
            if age is not None and age > FROZEN_STASH_MAX_HOLD_SECONDS:
                _logger.info("Frozen stash: hold_timeout")
                self._discard_frozen(motor)
                return False
            return True
        # Not yet at a clean post-speech boundary, or the detour is still
        # ongoing (interactive work speaking/queued ahead) -> HOLD: handle the
        # tick so next_action never generates a fresh turn over the pending
        # return, but do not requeue yet.
        if state != AgendaState.WAITING_SIGNAL or self._has_non_agenda_audio_work(motor):
            return True
        has_pending = getattr(motor, "has_pending_priority_before", None)
        priority = getattr(stash.action, "priority", 2)
        if callable(has_pending) and has_pending(priority):
            return True
        # RETURN: adopt first, then restore. F7: only consume the frozen stash AFTER
        # adoption succeeds, so a failed adoption leaves the draft frozen (a later
        # nudge retries; the deterministic skips discard it) instead of orphaning it
        # in the active slot with no owner. Adoption is side-effect-free on failure.
        if not agenda.start_prefetched_action(stash.action):
            return True
        # Adoption succeeded — restore the frozen draft into the engine's active slot
        # (connector resolved from the ready upgrade or the pool floor) and route it
        # through the queue exactly like the normal consume path — the worker pops
        # it, hits the cache, and speaks the connector + stashed dialogo as ONE turn.
        # enqueue_agenda_action drops the driver mirror stash internally.
        restore = getattr(motor, "restore_frozen_stash", None)
        key = restore(getattr(topic, "title", "") or "") if callable(restore) else None
        if key is None:
            # R5: a concurrent _invalidate_frozen_stash (set_profile / switch_llm_tier
            # / drop_pending_sources — none hold agenda_lock) landed between the top
            # has_frozen_stash() check and here, AFTER adoption already advanced the
            # controller to GENERATING. restore() lost the draft, so next_action would
            # return none() for GENERATING and the agenda would stall with no turn
            # enqueued. Enqueue a FRESH turn for the adopted action instead — the beat
            # regenerates from scratch (slower, never stuck). Code-only telemetry.
            _logger.info("Frozen stash: restore_lost_fallback")
        # Whether restore succeeded (draft back in the active slot, worker pops the
        # cache) or was lost to a concurrent invalidation (fresh regeneration), route
        # the adopted action through the queue so the GENERATING controller always
        # gets a turn. enqueue_agenda_action drops the driver mirror stash internally.
        enqueue_agenda_action(motor, stash.action, driver=self)
        return True

    def _discard_frozen(self, motor) -> None:
        """Drop the engine's frozen stash AND the driver mirror (D2 skip)."""
        discard = getattr(motor, "discard_frozen_stash", None)
        if callable(discard):
            try:
                discard()
            except Exception:
                _logger.exception("discard_frozen_stash failed during return skip")
        self._prefetch = None

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
        """Drop the driver stash AND invalidate the ENGINE'S OWN agenda draft.

        WU4 F1 (WU3 follow-up): source-aware — prefers
        `clear_prefetched_agenda_only`, which clears the engine's cache slot
        ONLY when it currently holds an agenda draft. An interactive
        (chat/PTT) occupant pregenerated by WU3's own trigger must survive a
        driver yield/drop; it has nothing to do with the driver's stash.
        Falls back to the unconditional `clear_prefetched_agenda` for a
        stand-in motor that lacks the source-aware primitive.
        """
        self._prefetch = None
        clear_only = getattr(motor, "clear_prefetched_agenda_only", None)
        if callable(clear_only):
            try:
                clear_only()
            except Exception:
                _logger.exception("clear_prefetched_agenda_only failed during prefetch drop")
            return
        clear = getattr(motor, "clear_prefetched_agenda", None)
        if callable(clear):
            try:
                clear()
            except Exception:
                _logger.exception("clear_prefetched_agenda failed during prefetch drop")

    def _drop_prefetch_stash(self) -> None:
        """Drop the driver stash only (the engine draft was consumed/cleared)."""
        self._prefetch = None
