"""Speech router — step 2 of the landing sequence
(conductor/tracks/interruptible_speech_architecture_20260804/speech-router-design.md
§2, §3, §4, §8 step 2, §11).

ONE daemon thread that is the ONLY caller of `_hablar` in the process. That
is the whole point: `_hablar_lock` stops contending, playback stops occupying
the engine thread, and every scheduling decision lives in one flat loop
instead of being implied by which thread happened to call what.

Step 2 is BEHAVIOR-PRESERVING. There is no stack, no pause/resume, no
`_ptt_held`, no preemption of the active job: jobs run in priority band,
then arrival order within a band (`_pick`, design §3's PRIORITY_* order).
Divergence from legacy's plain call-order is bounded to sub-ms simultaneity
races between two submitters -- nothing a human-perceptible schedule can
distinguish from arrival order. The `_stack` list and
`SpeechJobState.SUSPENDED` exist and stay empty/unreached so step 3 inherits
the shape (design §8) without a second edit to `_speech_active`.

Locking: `_sched_lock` is a LEAF (design §4 / I10). The router NEVER acquires
`_lock`, `_pq_lock`, `_prefetch_lock`, `_hablar_lock` or `agenda_lock` while
holding it, and NEVER emits a boundary event or runs a consumer callback
under it (§11 B6 — `speaking_end` consumers re-enter `submit()`).

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

# Priority bands (design §3): 0 owner, 1 chat, 2 agenda. Same ordering the
# dispatch `_priority_queue` already uses — this one orders PLAYBACK.
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
        # Step 3's suspended stack. Always empty at step 2 — kept so
        # `_speech_active`'s third clause is inherited, not re-derived.
        self._stack: list = []
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._next_job_id = 0
        self._completed: list = []

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
        §11 B2 audit found."""
        with self._sched_lock:
            self._next_job_id += 1
            job = SpeechJob(
                job_id=self._next_job_id, text=text, source=source, priority=priority
            )
            self._incoming.append(job)
        self._wake.set()
        return job

    def has_work(self) -> bool:
        """ACTIVE ∨ INCOMING ∨ STACK (design §11 B2)."""
        with self._sched_lock:
            return bool(self._active is not None or self._incoming or self._stack)

    def completed_job_ids(self) -> list:
        with self._sched_lock:
            return list(self._completed)

    # ── the loop ─────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
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
        with self._sched_lock:
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
                job = self._stack[-1]  # step 3; peek only
            else:
                return None
            job.state = SpeechJobState.ACTIVE
            self._active = job
            return job

    def _run_job(self, job: SpeechJob) -> None:
        motor = self._motor
        # Emergency paths set the cancel token BEFORE interrupt_speaking()
        # (llm_engine `cancel_speech_for_sources`), so a straggler is refused
        # here — before any boundary event exists for consumers to react to.
        if motor._speech_cancelled(job.source):
            self._finish(job, SpeechJobState.DISCARDED, reason="cancelled")
            return

        if not job.started:
            try:
                motor._speech_boundary_start(job.source)
            except Exception:
                logger.exception("speaking_start consumer failed; job discarded")
                self._finish(job, SpeechJobState.DISCARDED, reason="error", emit_end=False)
                return
            job.started = True

        outcome = None
        exc: Optional[BaseException] = None
        try:
            outcome = motor._hablar(job.text, source=job.source, emit_boundary=False)
        except Exception as e:  # noqa: BLE001 — reconcile decides, never the caller
            exc = e
            logger.exception("speech job raised out of _hablar")

        state, reason = self._reconcile(job, outcome, exc)
        if state is SpeechJobState.QUEUED:
            return  # retried: requeued at the front, still `started`
        self._finish(job, state, reason=reason)

    def _reconcile(self, job, outcome, exc):
        """Design §2 reconcile, minus the pause branch (there is no pause at
        step 2). Returns (state, reason); QUEUED means "retried"."""
        # Read the motor's cancel token BEFORE taking the leaf lock (I10).
        cancelled = self._motor._speech_cancelled(job.source)
        with self._sched_lock:
            if exc is not None:
                # Retry-once, and ONLY when nothing played: step 2 has no
                # resume slicing, so replaying a partly-spoken job would
                # repeat audio — an audible change this step forbids.
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
                return SpeechJobState.FINISHED, None
        if cancelled:
            return SpeechJobState.DISCARDED, "cancelled"
        # Bare interruption with no pause reason: stop means stop (design §2
        # [CORRECTED] reconcile default). `error` only relabels the log line.
        return SpeechJobState.DISCARDED, ("error" if outcome.error else "interrupted")

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

        # The chat spoken clock preserves the LEGACY rule exactly: it advanced
        # whenever `_hablar` RETURNED without raising — including a turn cut
        # mid-playback, which did (partially) speak. So: FINISHED, or
        # DISCARDED by a bare interruption. A raise (reason=error) or a cancel
        # keeps the gap growing — that growing gap is what surfaces silent TTS
        # failures to the operator.
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
