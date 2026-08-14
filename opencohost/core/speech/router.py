"""Speech router — step 3 of the landing sequence
(conductor/tracks/interruptible_speech_architecture_20260804/speech-router-design.md
§2, §3, §4, §5, §6, §8 step 3, §10 D3/D4, §11).

ONE daemon thread that is the ONLY caller of `_hablar` in the process. That
is the whole point: `_hablar_lock` stops contending, playback stops occupying
the engine thread, and every scheduling decision lives in one flat loop
instead of being implied by which thread happened to call what.

Step 3 arms the stack. `_stack` is a LIFO of SUSPENDED jobs (design §4):
push-back on pause is unconditional, depth is unbounded (a `[SPEECH_STACK]
depth=N` log is the only guard, at N>=4), and there is NO staleness/expiry —
D1 (owner, 2026-08-05) is literal: a suspended job waits indefinitely until
resumed or explicitly swept (cancel/emergency). `_ptt_held` blocks `_pick()`
entirely (INCOMING and STACK alike) while the mic is live. `hold_and_pause`
(PTT) and `pause_speech` (a D3-uniform priority-0 preempt) publish the
request ON THE VICTIM JOB (`job.pause_requested`, closure M3/M4) under
`_sched_lock` before cutting
— request-before-cut is load-bearing: the victim's reconcile reads it under
the same lock, so the ~50ms unwind race can never see a cut with no request
and DISCARD it; and because the request dies with its job, a late tap can
never leak into whatever `_pick()` finds next. The pick→arm gap the window
check cannot see is closed inside `_hablar_impl` via
`pause_pending_for_active` (closure B1).

D4 (§10, decided 2026-08-05): boundary-pair emission is OUTERMOST-ONLY with
TERMINAL-ONLY ends. `_boundary_owner` is the depth-0 job; only it ever emits
`speaking_start`/`speaking_end`, and `end` fires only at FINISHED/DISCARDED,
never at a pause. Preempting (inner) jobs are silent for their whole life.

Locking: `_sched_lock` is a LEAF (design §4 / I10). The router NEVER acquires
`_lock`, `_pq_lock`, `_prefetch_lock`, `_hablar_lock` or `agenda_lock` while
holding it, and NEVER emits a boundary event or runs a consumer callback
under it (§11 B6 — `speaking_end` consumers re-enter `submit()`). A depth
sweep (`sweep_sources`) never emits on the calling thread either — an owed
`speaking_end` is deferred to the router thread via `_owed_end` (§4 sweeps,
risk: the emergency-stop caller holds a non-reentrant `agenda_lock` a
synchronous emission would re-enter and hang).

Privacy: no telemetry line here carries fragment or chat text. Source tags,
job ids, indices and counts only.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from opencohost.config.settings import OWNER_BUNDLE_SOURCE

logger = logging.getLogger("OpenCohost")

# Internal wake sentinel put on the engine's `command_queue` at every job
# boundary (§11 B5). Deliberately NOT a member of `_DRAIN_SAFE_COMMANDS`: it is
# consumed by run()'s normal read, never applied by the boundary control drain.
SPEECH_BOUNDARY_COMMAND = "speech_boundary"

# Priority bands (design §3): 0 owner, 1 chat, 2 agenda. This orders PLAYBACK
# only. The dispatch `_priority_queue` moved to its own 4-tier model
# (core/turn_priority: PTT, direct, then stream vs agenda per streamer
# setting) — deliberately NOT mirrored here: two generated replies rarely
# coexist in the router, and direct-over-chat-over-agenda playback already
# matches the dispatch model's fixed part.
PRIORITY_OWNER = 0
PRIORITY_CHAT = 1
PRIORITY_AGENDA = 2


class SpeechJobState(Enum):
    """Explicit state (design §2/§8 point 8) — never inferred from position."""

    QUEUED = "queued"
    ACTIVE = "active"
    SUSPENDED = "suspended"  # step 3 only; nothing reaches it at step 2
    FINISHED = "finished"
    DISCARDED = "discarded"


@dataclass
class SpeechJob:
    """One unit of playback (design §3 field table).

    `text` is the payload `_hablar` is handed; `chunks` is None until the
    FIRST activation, when `_hablar_impl`'s own split comes back on the
    `SpeechOutcome` and is stored here — without it the cursor addresses
    nothing. `parent_job_id` is deliberately absent (REJECTED in §3: the stack
    is the linkage).
    """

    job_id: int
    text: str
    source: str
    priority: int
    chunks: Optional[list] = None
    cursor: int = 0
    state: SpeechJobState = SpeechJobState.QUEUED
    spoken: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    suspensions: int = 0
    created_at: float = field(default_factory=time.monotonic)
    connector: Optional[str] = None
    # Retry budget for a whole-invocation raise (design §2 reconcile). Not a
    # suspension: nothing played, so nothing is replayed.
    retries: int = 0
    # True once the job's ONE speaking_start has been emitted; a retry must not
    # emit a second pair.
    started: bool = False
    # Closure M3/M4 (2026-08-05): the pause request is JOB-LOCAL. Written by
    # `pause_speech` under `_sched_lock` for the job that was ACTIVE at
    # publish time; consumed (read+cleared) by that job's own window check
    # or reconcile. It dies with the job — a request landing after the
    # job's reconcile can never leak into whatever `_pick()` finds next.
    pause_requested: Optional[str] = None
    # Streaming (llm_output_streaming_20260813 design §2): False only for a
    # `submit_streaming` job whose `chunks` are still growing; True means
    # the chunk list is final. Defaulting True keeps every job built
    # anywhere else byte-identical in behavior.
    sealed: bool = True


def priority_for_source(source: str) -> int:
    """Playback band for a source tag. Owner content (typed or PTT, including
    a bundled owner turn) outranks chat, which outranks agenda."""
    if source.startswith("kira-agenda"):
        return PRIORITY_AGENDA
    if source in ("direct", "ptt", OWNER_BUNDLE_SOURCE):
        return PRIORITY_OWNER
    return PRIORITY_CHAT


class SpeechRouter:
    """The scheduler (design §2). One thread, one loop, one `_hablar` caller."""

    def __init__(self, motor):
        self._motor = motor
        self._sched_lock = threading.Lock()
        self._incoming: list = []
        self._active: Optional[SpeechJob] = None
        # LIFO of SUSPENDED jobs (design §4). Push-back on pause is
        # unconditional; no staleness/expiry (D1, owner, 2026-08-05: literal).
        self._stack: list = []
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._next_job_id = 0
        self._completed: list = []
        # High-water mark for the `[SPEECH_STACK] depth=N` guard log (design
        # §4): logged once per NEW maximum, never on every iteration.
        self._stack_high_water: int = 0
        # Step 3 state (design §2/§3/§10), all read/written under
        # `_sched_lock` only:
        # While the mic is live, `_pick()` returns None unconditionally --
        # blocks INCOMING as well as STACK (contract point 3).
        self._ptt_held: bool = False
        # `time.monotonic()` of the last engage, for the release line's
        # `held_ms` (2026-08-09). None when no hold was armed through
        # `hold_and_pause` -- the release then reports -1 rather than a lie.
        self._ptt_hold_started_monotonic: Optional[float] = None
        # D4: the depth-0 job. Only this job ever emits
        # speaking_start/speaking_end; a preempting (inner) job never claims.
        self._boundary_owner: Optional[SpeechJob] = None
        # Set by `sweep_sources` when a depth sweep removes the boundary
        # owner off the calling thread; consumed by `_loop`, which emits the
        # owed `speech_boundary_end` on the ROUTER thread (never the sweep's
        # caller -- see the module docstring's deadlock note).
        self._owed_end: bool = False
        # Step-3 kill switch: `interrupt_enabled` (a property below) reads
        # the motor's host flags LIVE — no build-time copy to go stale.

    @property
    def interrupt_enabled(self) -> bool:
        """Step-3 kill switch, read LIVE from the motor at every decision
        (judge closure 2026-08-05): the old build-time snapshot could not be
        disarmed in-process — the pause/resume entrypoints died while a
        priority-0 submit kept preempting — and left the two engine_host
        flag writes order-sensitive. Conjunction with the router flag so
        router-off strictly dominates: without the router as the playback
        path there is no stack, and step 3 must be inert."""
        motor = self._motor
        return bool(
            getattr(motor, "_speech_interrupt_enabled", False)
            and getattr(motor, "_speech_router_enabled", False)
        )

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        """Idempotent. The thread is a daemon: no shutdown wiring to get
        wrong, and a live utterance never blocks process exit any more than
        today's `_hablar` does."""
        with self._sched_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop, name="SpeechRouter", daemon=True
            )
            # Started INSIDE the lock: a constructed-but-unstarted Thread is
            # not alive, so a concurrent first caller passing the guard above
            # would spawn a SECOND loop — two `_hablar` callers, the exact
            # thing this class exists to prevent. Thread.start() acquires no
            # engine lock, so `_sched_lock` stays a leaf (I10).
            self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    # ── public surface ───────────────────────────────────────────────────

    def submit(self, text: str, source: str, priority: int) -> SpeechJob:
        """Non-blocking. Returns immediately with the job QUEUED — which is
        already enough for `has_work()`, closing the submit->pick window the
        §11 B2 audit found.

        D3 (design §10, RESOLVED uniform): a priority-0 (owner) submit
        preempts whatever is ACTIVE, unconditionally — including another
        priority-0 job. `pause_speech` is called AFTER the lock is released
        (it takes the same leaf lock itself, and interrupt_speaking() must
        never run while `_sched_lock` is held) and only when
        `interrupt_enabled` is True — the step-3 kill switch.
        """
        with self._sched_lock:
            self._next_job_id += 1
            job = SpeechJob(
                job_id=self._next_job_id, text=text, source=source, priority=priority
            )
            self._incoming.append(job)
            victim = self._active
            preempt = (
                self.interrupt_enabled
                and priority == PRIORITY_OWNER
                and victim is not None
            )
        self._wake.set()
        if preempt:
            # Closure M3: pass the job this submit OBSERVED. If it already
            # left ACTIVE by the time pause_speech runs, the pause aborts —
            # cutting "whatever is active now" would typically cut the
            # preemptor's own answer (audible stutter + spurious suspension).
            self.pause_speech("preempt", target=victim)
        return job

    def submit_streaming(self, source: str, priority: int) -> SpeechJob:
        """Streaming entry (llm_output_streaming_20260813 design §2): one
        growing job for the whole turn. Mirrors `submit` — including the
        single D3 priority-0 preempt — but the job is born UNSEALED with
        `chunks=[]` (non-None, so every activation takes the pre_split path
        and `_reconcile` never overwrites the list from an outcome) and
        `text=""` (chunks are the payload; there is no full text yet).
        The producer feeds it via `append_chunks` and closes it via `seal`.
        """
        with self._sched_lock:
            self._next_job_id += 1
            job = SpeechJob(
                job_id=self._next_job_id, text="", source=source,
                priority=priority, chunks=[], sealed=False,
            )
            self._incoming.append(job)
            victim = self._active
            preempt = (
                self.interrupt_enabled
                and priority == PRIORITY_OWNER
                and victim is not None
            )
        self._wake.set()
        if preempt:
            self.pause_speech("preempt", target=victim)
        return job

    def append_chunks(self, job: SpeechJob, chunks: list) -> bool:
        """Grow a streaming job (design §2). Leaf-lock only: no event, no
        callback. Returns False — dropping the chunks, with a log line —
        when the job is DISCARDED/FINISHED: the producer's cheap "is my
        turn still alive" signal. Appends touch `chunks`, never `cursor`,
        so a SUSPENDED job keeps growing on the stack and owes the new
        chunks on resume."""
        with self._sched_lock:
            refused = job.state in (SpeechJobState.DISCARDED, SpeechJobState.FINISHED)
            if not refused:
                job.chunks.extend(chunks)
            state_log = job.state.value
        if refused:
            # Privacy: counts only, never chunk text.
            logger.info(
                "[SPEECH_STREAM] append refused job=%d source=%s state=%s dropped=%d",
                job.job_id, job.source, state_log, len(chunks),
            )
            return False
        self._wake.set()
        return True

    def seal(self, job: SpeechJob) -> None:
        """Mark a streaming job's chunks as final (design §2). Idempotent;
        leaf-lock only. A starved job exits its wait and finishes once
        drained; a suspended one finishes after its resume drains."""
        with self._sched_lock:
            job.sealed = True
        self._wake.set()

    def has_work(self) -> bool:
        """ACTIVE ∨ INCOMING ∨ STACK ∨ an owed boundary end (design §11 B2).

        Self-caught (step 3): without the `_owed_end` clause, sweeping the
        boundary owner off the stack while an INNER job is still active lets
        that inner job's own `_finish` see an empty router and fire `idle`
        BEFORE the owner's owed `speaking_end` — the avatar goes idle, then
        an already-stale speaking_end arrives a moment later. The owed end
        is logically still-open work until `_loop` actually emits it.
        """
        with self._sched_lock:
            return bool(
                self._active is not None or self._incoming or self._stack or self._owed_end
            )

    def completed_job_ids(self) -> list:
        with self._sched_lock:
            return list(self._completed)

    def hold_and_pause(self, reason: str = "ptt") -> None:
        """PTT_DOWN (design §5.1): arm the mic-live hold AND stamp the active
        job's pause request in ONE `_sched_lock` acquisition (judge closure
        2026-08-05: `set_ptt_held(True)` + `pause_speech("ptt")` as two
        acquisitions left a hold-armed/no-request gap — a closure-B1 cut
        landing inside it reached reconcile with no request and DISCARDED
        the held job, whole utterance lost while the mic was live). The
        interrupt still fires OUTSIDE the lock (I10). `pause_speech`'s
        stale-cut residual cannot happen here: the hold set in the same
        acquisition blocks `_pick()`, so no successor can arm — a victim
        that finishes before the interrupt lands just finishes, and the
        stray interrupt clears `_speaking` with nothing active.

        The engage line names WHETHER a victim was cut (2026-08-09 live
        session): with nothing active this path emits no cut and no push, so
        `victim=none` is the ONLY record that the hold — not a stall — is
        what `_pick()` is refusing. Captured under the lock, logged after it
        (same discipline as the push/resume lines)."""
        with self._sched_lock:
            self._ptt_held = True
            self._ptt_hold_started_monotonic = time.monotonic()
            victim = self._active
            if victim is not None:
                victim.pause_requested = reason
            victim_log = victim.source if victim is not None else "none"
        logger.info("[SPEECH_STACK] ptt_hold engaged victim=%s", victim_log)
        self._wake.set()
        if victim is not None:
            self._motor.interrupt_speaking()

    def pause_speech(self, reason: str, target: Optional[SpeechJob] = None) -> None:
        """D3-preempt cut (design §2/§3) — the PTT press goes through
        `hold_and_pause` above instead. REQUEST-BEFORE-CUT is
        load-bearing: publish `pause_requested` on the VICTIM under
        `_sched_lock` FIRST, then release, THEN interrupt — so the victim's
        reconcile (which reads the request under the same lock) can never
        observe the cut without the request and fall through to the
        bare-interruption DISCARD default. A no-op when nothing is ACTIVE.

        `target` (closure M3): the job the caller observed as active. When
        it is no longer THE active job the whole pause aborts — including
        the interrupt — so a stale preempt never cuts the successor.

        Residual micro-window, stated: between the lock release below and
        `interrupt_speaking()` the victim can still finish and a successor
        arm — the stray cut then lands as a bare interrupt on the successor
        (one job discarded with its log line, no state corruption). Closing
        it fully would need the cut to run under `_sched_lock`, which I10
        (leaf lock) forbids."""
        with self._sched_lock:
            victim = self._active
            if victim is None:
                return
            if target is not None and victim is not target:
                return
            victim.pause_requested = reason
        self._motor.interrupt_speaking()

    def pause_pending_for_active(self) -> bool:
        """Closure B1: True while the mic is live or a pause is requested
        for the ACTIVE job. `_hablar_impl` consults this right after
        (re)arming `_speaking` — an interrupt that landed in the pick→arm
        gap was stomped by that assignment, and this read (under the same
        lock `pause_speech` publishes under) is what un-stomps it."""
        with self._sched_lock:
            active = self._active
            return self._ptt_held or (
                active is not None and active.pause_requested is not None
            )

    def set_ptt_held(self, held: bool) -> None:
        """Level-triggered mic-live flag (contract point 3): while True,
        `_pick()` returns None unconditionally -- nothing that becomes ready
        can reach the mixer. Production ARMS only through `hold_and_pause`
        (hold + request in one acquisition, judge closure 2026-08-05); this
        is the RELEASE side (`resume_speech_after_ptt`), where there is no
        request to pair and a bare transition is the whole job.

        A real True->False transition closes the hold and logs how long
        `_pick()` stayed refused (2026-08-09) — the idempotent double-clear
        every PTT exit path performs is not an event and stays silent."""
        with self._sched_lock:
            released = self._ptt_held and not held
            started = self._ptt_hold_started_monotonic
            self._ptt_held = held
            if released:
                self._ptt_hold_started_monotonic = None
        if released:
            held_ms = int((time.monotonic() - started) * 1000) if started is not None else -1
            logger.info("[SPEECH_STACK] ptt_hold released held_ms=%d", held_ms)
        self._wake.set()

    def sweep_sources(self, prefixes: tuple) -> None:
        """Discard every STACK entry whose source matches `prefixes`, at ANY
        depth, preserving the order of the rest (design §4 sweeps). Called
        from the emergency-stop / drop-pending paths; the ACTIVE job is NOT
        touched here -- the cancel token (set by the caller BEFORE this) plus
        `interrupt_speaking()` already route it through `_reconcile`'s
        `cancelled` branch.

        NEVER emits on the calling thread (module docstring): if the removed
        set included the boundary owner, the owed `speech_boundary_end` is
        deferred to `_owed_end`, consumed by the ROUTER thread at the top of
        `_loop` -- a synchronous emission here would re-enter
        `_handle_agenda_motor_event`'s `agenda_lock`, which the emergency-stop
        caller already holds (a non-reentrant plain Lock: instant hang).
        """
        with self._sched_lock:
            kept = []
            removed = []
            for job in self._stack:
                if str(job.source).startswith(prefixes):
                    removed.append(job)
                else:
                    kept.append(job)
            if not removed:
                return
            self._stack = kept
            owner_removed = False
            for job in removed:
                job.state = SpeechJobState.DISCARDED
                if self._boundary_owner is job:
                    owner_removed = True
            if owner_removed:
                self._boundary_owner = None
                self._owed_end = True
        for job in removed:
            pending = max(0, len(job.chunks) - job.cursor) if job.chunks else 0
            logger.info(
                "[SPEECH_DISCARD] job=%d source=%s from_idx=%d lost=%d reason=cancelled",
                job.job_id, job.source, job.cursor, pending,
            )
        if owner_removed:
            self._wake.set()

    # ── the loop ─────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            # The §4 depth high-water guard is sampled AT THE PUSH SITES
            # (`_push_suspended`), not here — a spike that forms and unwinds
            # inside one `_run_job` would dodge a loop-top sample entirely
            # (closure NIT).
            with self._sched_lock:
                owed_end = self._owed_end
                self._owed_end = False
            if owed_end:
                # §4 sweeps: a depth sweep removed the boundary owner off the
                # calling thread. The owed end is emitted HERE, on the router
                # thread, never on the sweep's caller (module docstring).
                try:
                    self._motor._speech_boundary_end()
                except Exception:
                    logger.exception("speaking_end consumer failed after a sweep")
                # Self-caught: `has_work()` counted this owed end as work
                # (so a still-active inner job's own `_finish` would not
                # idle BEFORE it) -- now that it is flushed, re-check exactly
                # as `_finish` does, or 'idle' never fires at all when
                # nothing else completes afterward.
                if not self.has_work():
                    try:
                        self._motor.ui_callback("idle")
                    except Exception:
                        logger.exception("ui callback failed after a sweep's owed end")

            job = self._pick()
            if job is None:
                self._wake.wait(0.25)
                self._wake.clear()
                continue
            try:
                self._run_job(job)
            except Exception:
                # The router thread outlives every failure: a dead router is
                # permanent silence with no error, the exact failure mode the
                # 2026-07-15 voice-death incident taught.
                logger.exception("speech router iteration failed")
                self._release(job, SpeechJobState.DISCARDED)

    def _pick(self) -> Optional[SpeechJob]:
        resume_log = None
        with self._sched_lock:
            if self._ptt_held:
                # Contract point 3: while the mic is live, nothing reaches
                # the mixer -- blocks INCOMING as well as STACK. Level-
                # triggered: re-checked every iteration.
                return None
            if self._incoming:
                # Lowest priority number wins; arrival order within a tie.
                # `job_id` doubles as the arrival stamp: a RETRY re-enters the
                # queue with its ORIGINAL id, so it naturally precedes any
                # same-priority job that arrived during its failed attempt.
                idx = min(
                    range(len(self._incoming)),
                    key=lambda i: (self._incoming[i].priority, self._incoming[i].job_id),
                )
                job = self._incoming.pop(idx)
            elif self._stack:
                # POP, not peek (deviation from the design §2 pseudocode's
                # comment, stated in the writer's report): a job then lives
                # in exactly ONE place at all times -- INCOMING, `_active`,
                # or STACK -- so `_finish`/`_release`/a depth sweep never
                # have to special-case "also remove it from the stack".
                # `_reconcile`'s SUSPENDED branch pushes it back
                # unconditionally, so observable LIFO order is unchanged.
                job = self._stack.pop()
                # F8c (runtime_findings_batch_20260807): the resume side of
                # the same gap -- no line existed here at all, so resumes
                # had to be inferred from "Sintetizando N fragmento(s)"
                # arithmetic. Captured here, logged OUTSIDE the lock (same
                # reason the depth>=4 WARNING below already defers).
                resume_log = (job.source, job.cursor, len(self._stack))
            else:
                return None
            job.state = SpeechJobState.ACTIVE
            self._active = job
        if resume_log is not None:
            logger.info(
                "[SPEECH_STACK] resume source=%s cursor=%d depth=%d", *resume_log,
            )
        return job

    def _run_job(self, job: SpeechJob) -> None:
        motor = self._motor
        # Emergency paths set the cancel token BEFORE interrupt_speaking()
        # (llm_engine `cancel_speech_for_sources`), so a straggler is refused
        # here — before any boundary event exists for consumers to react to.
        if motor._speech_cancelled(job.source):
            self._finish(job, SpeechJobState.DISCARDED, reason="cancelled")
            return

        # D4 (design §10): outermost-only, terminal-only. Only the job that
        # holds the boundary when the tower is empty ever emits a pair; a
        # preempting (inner) job claims nothing and stays silent for its
        # whole life. `not job.started` also guards a RETRY/RESUME of the
        # owner itself against a second `speaking_start`.
        claim = False
        with self._sched_lock:
            # Closure B2: collect a sweep's owed end ATOMICALLY with the
            # claim decision. Without this, a sweep landing between the
            # loop-top flush and this block clears `_boundary_owner`, the
            # fresh job claims, and its `speaking_start` lands BEFORE the
            # owed `speaking_end` — start,start,end,end, the §11 B6 poison
            # (the trailing end completes an agenda turn that never ran).
            owed_end = self._owed_end
            self._owed_end = False
            if self._boundary_owner is None and not job.started:
                self._boundary_owner = job
                claim = True
        if owed_end:
            try:
                motor._speech_boundary_end()
            except Exception:
                logger.exception("speaking_end consumer failed for a swept owner")
        if claim:
            try:
                motor._speech_boundary_start(job.source)
            except Exception:
                logger.exception("speaking_start consumer failed; job discarded")
                # `_finish` clears `_boundary_owner` for this job identity
                # regardless of `emit_end` -- no second guard needed here.
                self._finish(job, SpeechJobState.DISCARDED, reason="error", emit_end=False)
                return
            job.started = True

        # The pick->_hablar window (unlisted in §2/§8, closed here): a pause
        # or PTT hold landing AFTER `_pick()` returned this job but BEFORE
        # `_hablar_impl` sets its own `_speaking = True` would otherwise be
        # silently swallowed -- interrupt_speaking() clears a flag
        # `_hablar_impl` is about to unconditionally re-set, and Kira would
        # talk through the whole hold. The window spans the boundary-start
        # emission's full consumer fan-out too (agenda lock, a spawned
        # prefetch thread), so the check must run AFTER it, not before —
        # checking any earlier would miss a press landing DURING that
        # callback. Level-triggered, exactly like `_pick`'s own check;
        # `_boundary_owner` is left untouched (this job IS the owner if it
        # claimed above, and stays one across the suspension, same as any
        # other pause). The request read is JOB-LOCAL (closure M4): a stale
        # request from a job that already finished can never suspend this
        # one. The residual gap this check still leaves — a pause published
        # after it but before `_hablar_impl` arms — is closed by
        # `pause_pending_for_active` inside `_hablar_impl` (closure B1).
        depth_log = 0
        push_log = None
        with self._sched_lock:
            pending_pause = self._ptt_held or job.pause_requested is not None
            if pending_pause:
                depth_log = self._push_suspended(job)
                push_log = (job.source, job.cursor, len(self._stack))
        if pending_pause:
            if push_log:
                logger.info(
                    "[SPEECH_STACK] push source=%s cursor=%d depth=%d", *push_log,
                )
            if depth_log:
                logger.warning("[SPEECH_STACK] depth=%d", depth_log)
            return

        while True:
            outcome = None
            exc: Optional[BaseException] = None
            try:
                outcome = motor._hablar(
                    job.text,
                    source=job.source,
                    emit_boundary=False,
                    # Step 3 (design §1 resolution 1): resume with the router's
                    # OWN owed slice, never a re-chunked rejoin of the raw text.
                    # `cursor_base` (judge closure 2026-08-05) keeps
                    # `_speech_progress` TURN-relative on a resume — without it
                    # every played/total consumer (speech_remaining_estimate and
                    # every other progress reader) read the owed slice as the
                    # whole turn.
                    pre_split=job.chunks[job.cursor:] if job.chunks is not None else None,
                    cursor_base=job.cursor if job.chunks is not None else 0,
                )
            except Exception as e:  # noqa: BLE001 — reconcile decides, never the caller
                exc = e
                logger.exception("speech job raised out of _hablar")

            state, reason = self._reconcile(job, outcome, exc)
            if state in (SpeechJobState.QUEUED, SpeechJobState.SUSPENDED):
                # QUEUED: retried, requeued at the front, still `started`.
                # SUSPENDED: pushed back on the stack; no boundary end at a
                # pause (D4 terminal-only) — `_finish` never runs for this pick.
                return
            if state is SpeechJobState.ACTIVE:
                # Streaming (design §2): drained but UNSEALED — starved, not
                # finished. Wait for chunks/seal/cancel/hold; a None state
                # means "new chunks: next slice invocation".
                state, reason = self._starved_wait(job)
                if state is None:
                    continue
                if state is SpeechJobState.SUSPENDED:
                    return
            self._finish(job, state, reason=reason)
            return

    def _starved_wait(self, job: SpeechJob):
        """Streaming starved state (llm_output_streaming_20260813 design §2):
        the job has played every chunk it has but is not sealed — more may
        arrive. Block THIS (router) thread on `_wake` in a bounded loop
        (0.25 s tick, mirroring `_loop`) — not a regression: it blocks
        inside `_hablar` for the whole utterance today. The job stays
        ACTIVE, so `has_work()` holds, no `idle` fires, and the boundary
        pair stays open (D4: no end at a pause, none at a starve either).

        Precedence per tick, re-checked under `_sched_lock`:
          1. cancel token        -> (DISCARDED, "cancelled") for `_finish`
          2. `_ptt_held`/pause   -> `_push_suspended`; (SUSPENDED, None)
          3. new chunks          -> (None, None): next slice invocation
          4. sealed and drained  -> (FINISHED, None) for `_finish`
        """
        motor = self._motor
        while True:
            # I10: the cancel token lives behind the motor's `_lock` — read
            # it BEFORE taking the leaf lock, same as `_reconcile`.
            cancelled = motor._speech_cancelled(job.source)
            depth_log = 0
            push_log = None
            with self._sched_lock:
                if cancelled:
                    return SpeechJobState.DISCARDED, "cancelled"
                if self._ptt_held or job.pause_requested is not None:
                    depth_log = self._push_suspended(job)
                    push_log = (job.source, job.cursor, len(self._stack))
                elif job.cursor < len(job.chunks):
                    return None, None
                elif job.sealed:
                    return SpeechJobState.FINISHED, None
            if push_log:
                logger.info(
                    "[SPEECH_STACK] push source=%s cursor=%d depth=%d", *push_log,
                )
                if depth_log:
                    logger.warning("[SPEECH_STACK] depth=%d", depth_log)
                return SpeechJobState.SUSPENDED, None
            self._wake.wait(0.25)
            self._wake.clear()
            if self._stop.is_set():
                # `stop()` must not hang behind a producer that never seals:
                # unlike playback, starvation has no natural end.
                return SpeechJobState.DISCARDED, "stopped"

    def _push_suspended(self, job: SpeechJob) -> int:
        """Push-back on pause (design §4): unconditional, same object,
        LATEST cursor. Caller HOLDS `_sched_lock`. Returns the new depth
        when it is a fresh high-water mark at the §4 guard threshold (the
        caller logs it OUTSIDE the lock), else 0 — sampled at the push site
        itself, so a spike that forms and unwinds inside one iteration can
        no longer dodge the guard (closure NIT).

        F8b (runtime_findings_batch_20260807): the real push is also logged
        by the CALLER, OUTSIDE the lock — same reason the depth>=4 WARNING
        already defers. `job.source`/`job.cursor` and `len(self._stack)`
        read by the caller immediately after this call (still under the
        lock) are exactly this push's values, since nothing else touches
        the stack in between."""
        job.pause_requested = None
        job.state = SpeechJobState.SUSPENDED
        job.suspensions += 1
        self._stack.append(job)
        self._active = None
        depth = len(self._stack)
        if depth > self._stack_high_water:
            self._stack_high_water = depth
            if depth >= 4:
                return depth
        return 0

    def _reconcile(self, job, outcome, exc):
        """Design §2 reconcile + step 3's pause branch. Returns (state,
        reason); QUEUED means "retried", SUSPENDED means "pushed back on the
        stack"."""
        # Read the motor's cancel token BEFORE taking the leaf lock (I10).
        cancelled = self._motor._speech_cancelled(job.source)
        depth_log = 0
        push_log = None
        try:
            with self._sched_lock:
                # Closure M4: the pause request is JOB-LOCAL. Read+clear is
                # atomic with `pause_speech`'s write (same leaf lock), and a
                # request published for a job that already passed THIS point
                # dies with that job — it cannot leak into the next job's
                # reconcile or window check.
                pause_reason = job.pause_requested
                job.pause_requested = None
                if exc is not None:
                    # Closure MINOR (§2 reconcile order): a published pause
                    # outranks the raise — the cut was REQUESTED, so the job
                    # is owed a resume even though the unwind raised. An
                    # emergency cancel still wins.
                    if pause_reason in ("ptt", "preempt") and not cancelled:
                        depth_log = self._push_suspended(job)
                        push_log = (job.source, job.cursor, len(self._stack))
                        return SpeechJobState.SUSPENDED, None
                    # Retry-once, and ONLY when nothing played: there is no
                    # audible-replay-safe retry of a partly-spoken job.
                    if job.retries == 0 and job.cursor == 0 and not job.spoken:
                        job.retries += 1
                        job.state = SpeechJobState.QUEUED
                        # Position is irrelevant: `_pick` orders by (priority,
                        # job_id) and the retry keeps its original job_id.
                        self._incoming.append(job)
                        self._active = None
                        self._wake.set()
                        return SpeechJobState.QUEUED, None
                    return SpeechJobState.DISCARDED, "error"
                if outcome is None:
                    # No outcome to reconcile (a stubbed `_hablar`): nothing is
                    # known to be owed, so nothing is resumed.
                    return SpeechJobState.FINISHED, None
                if cancelled and not outcome.chunks and not outcome.spoken and not outcome.skipped:
                    # The token landed between the router's own check and
                    # `_hablar`'s: the invocation was REFUSED and no speech
                    # happened. Classifying that FINISHED would record a completed
                    # turn that never played and skip the discard log.
                    return SpeechJobState.DISCARDED, "cancelled"
                if job.chunks is None:
                    job.chunks = list(outcome.chunks)
                base = job.cursor
                job.spoken.extend(base + i for i in outcome.spoken)
                job.skipped.extend(base + i for i in outcome.skipped)
                job.cursor = base + outcome.cursor
                if job.cursor >= len(job.chunks):
                    if job.sealed:
                        return SpeechJobState.FINISHED, None
                    # Streaming (design §2): drained but UNSEALED is the
                    # STARVED state, not completion — ACTIVE tells `_run_job`
                    # to enter the starved wait. A pause request consumed
                    # above is re-published for the wait's precedence check:
                    # the cancel token is level-triggered and re-read there,
                    # but the request is edge-consumed and must die with the
                    # JOB, not with this slice.
                    job.pause_requested = pause_reason
                    return SpeechJobState.ACTIVE, None
                if outcome.cursor >= len(outcome.chunks) and not outcome.interrupted:
                    # The job GREW while this slice played (live-fire fix,
                    # 2026-08-13). `_run_job` hands `_hablar` a SNAPSHOT
                    # (`job.chunks[job.cursor:]`), so once the producer appends
                    # during playback the test above compares a cursor against
                    # a MOVING target and fails even though nothing went wrong
                    # — the slice drained, `_hablar` reports no interruption
                    # (it sets that flag ONLY from its `not self._speaking`
                    # cut guards), and there is simply a tail left. Without
                    # this branch that clean completion fell through to the
                    # `interrupted` default below and the whole tail was
                    # discarded: `[SPEECH_DISCARD] from_idx=1 lost=9
                    # reason=interrupted` on EVERY streamed turn, because
                    # generation always outruns playback.
                    #
                    # ACTIVE (never FINISHED — chunks are still owed) routes to
                    # `_starved_wait`, which re-applies the full
                    # cancel > pause > new-chunks > sealed precedence and
                    # returns immediately when a tail is already waiting.
                    #
                    # An EMPTY `outcome.chunks` must take this branch too, and
                    # guarding against it was a bug of its own (adversarial
                    # review, 2026-08-14): the router can pick a streaming job
                    # while `chunks` is still `[]` — `submit_streaming` wakes
                    # the loop before the producer's first `append_chunks` —
                    # so `_hablar` returns an empty outcome, the first append
                    # lands before this lock, and the job died at birth with
                    # the same `[SPEECH_DISCARD] ... reason=interrupted`.
                    # Re-slicing forever is not a risk: reaching here at all
                    # means `job.cursor < len(job.chunks)`, so the NEXT slice
                    # is non-empty; a truly idle job is caught by the drained
                    # branch above and parks on `_wake`. Refused invocations
                    # keep their terminals — a cancelled one is claimed above
                    # at the `not outcome.chunks` check, and anything else
                    # reaches `_starved_wait`, which re-reads the cancel token
                    # before it waits.
                    job.pause_requested = pause_reason
                    return SpeechJobState.ACTIVE, None
                if cancelled:
                    return SpeechJobState.DISCARDED, "cancelled"
                if pause_reason in ("ptt", "preempt"):
                    # Step 3 (design §2/§4): unfinished, not cancelled, and a
                    # pause was requested before the cut landed — push back
                    # onto the stack with its identity and its cursor (never
                    # overwritten elsewhere, so a job suspended twice resumes
                    # from its LATEST cursor, never its first).
                    depth_log = self._push_suspended(job)
                    push_log = (job.source, job.cursor, len(self._stack))
                    return SpeechJobState.SUSPENDED, None
            # Bare interruption with no pause request: stop means stop (design
            # §2 [CORRECTED] reconcile default). `error` only relabels the log.
            return SpeechJobState.DISCARDED, ("error" if outcome.error else "interrupted")
        finally:
            if push_log:
                logger.info(
                    "[SPEECH_STACK] push source=%s cursor=%d depth=%d", *push_log,
                )
            if depth_log:
                logger.warning("[SPEECH_STACK] depth=%d", depth_log)

    # ── completion ───────────────────────────────────────────────────────

    def _release(self, job, state) -> None:
        """Last-resort unwind for the loop's catch-all: drop the job and CLOSE
        its boundary. An open boundary would strand `_speaking` /
        `_current_speech_source` True forever — i.e. permanent silence, since
        `_speech_active` gates every pop."""
        with self._sched_lock:
            job.state = state
            if self._active is job:
                self._active = None
            if self._boundary_owner is job:
                self._boundary_owner = None
        if job.started:
            try:
                self._motor._speech_boundary_end()
            except Exception:
                logger.exception("speaking_end consumer failed during router unwind")

    def _finish(self, job, state, *, reason=None, emit_end=True) -> None:
        motor = self._motor
        with self._sched_lock:
            job.state = state
            if self._active is job:
                self._active = None
            if self._boundary_owner is job:
                self._boundary_owner = None
            self._completed.append(job.job_id)
            pending = max(0, len(job.chunks) - job.cursor) if job.chunks else 0
            from_idx = job.cursor

        if state is SpeechJobState.DISCARDED:
            # One line per DISCARDED job (the per-fragment `[SPEECH_LOST]`
            # lives in `_hablar_impl._drop`). Ids/tags/counts only.
            if reason == "error":
                logger.warning(
                    "[SPEECH_LOST] job=%d source=%s from_idx=%d lost=%d reason=error",
                    job.job_id, job.source, from_idx, pending,
                )
            else:
                logger.info(
                    "[SPEECH_DISCARD] job=%d source=%s from_idx=%d lost=%d reason=%s",
                    job.job_id, job.source, from_idx, pending, reason or "interrupted",
                )

        # NOTHING below runs under `_sched_lock` (§11 B6): every consumer here
        # may re-enter submit() on this very thread.
        if emit_end and job.started:
            try:
                motor._speech_boundary_end()
            except Exception:
                logger.exception("speaking_end consumer failed")

        # The chat spoken clock ticks where speech genuinely happened and
        # ended: FINISHED, or DISCARDED by a bare interruption (a cut turn
        # did partially speak — legacy ticked there too). DELIBERATE
        # divergence from legacy, which ticked on ANY non-raising return: a
        # raise, a cancel, AND a non-raising error return (queue_empty_timeout
        # — reachable with zero exceptions, §12) keep the gap growing. That
        # growing gap is what surfaces silent TTS failures to the operator.
        # Pinned by the nonraising-error clock test.
        if job.source == "chat" and (
            state is SpeechJobState.FINISHED
            or (state is SpeechJobState.DISCARDED and reason == "interrupted")
        ):
            callback = getattr(motor, "on_chat_turn_spoken", None)
            if callback is not None:
                try:
                    callback()
                except Exception:
                    pass

        # §11 B3: 'idle' left `_complete_processing_cycle` (it would fire
        # mid-sentence there) and lands here, once the router is out of work.
        if not self.has_work():
            try:
                motor.ui_callback("idle")
            except Exception:
                logger.exception("ui callback failed during router idle")

        # §11 B5: wake the engine loop so the post-boundary tail (control
        # drain, direct drain, pending model switch, priority-queue re-entry)
        # runs NOW instead of waiting out run()'s 1 s command_queue tick.
        # Enqueued AFTER the boundary event on purpose: the WU2 agenda consume
        # runs inside that event (engine_host.py:674-679) and must still land
        # BEFORE the engine's post-boundary pop, exactly as it does today.
        try:
            motor.command_queue.put((SPEECH_BOUNDARY_COMMAND, None))
        except Exception:
            logger.exception("could not wake the engine loop at a speech boundary")
