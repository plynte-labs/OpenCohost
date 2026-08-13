"""
opencohost/core/engine/llm_engine_pregen.py

Pregeneration cache, prefetch slot, and epoch invalidation of MotorVocalIA.
Mixin extracted from llm_engine.py; all runtime state stays on MotorVocalIA.
"""
import threading
import time
from typing import Optional

# `Optional` is imported directly, not via `_eng`: it is used in ANNOTATIONS, which
# evaluate at class-body time during llm_engine's PARTIAL import, where `_eng.X`
# resolves only for names bound above the mixin-import line. Annotations want a
# direct import; runtime values want `_eng`. See llm_engine_memorias.py.
from opencohost.core import llm_engine as _eng

class PregenCacheMixin:
    def _head_snapshot_locked(self):
        """(priority, payload, source, history_text) of the queue HEAD, or None.

        Caller MUST hold `_pq_lock`. Extracted so the pregen completion
        re-trigger (Step 2, direct_turn_preemption_20260803) builds the snapshot
        in exactly the same shape `enqueue()`'s tail does.
        """
        head = self._priority_queue[0] if self._priority_queue else None
        if head is None:
            return None
        return (head[0], head[2], head[3], head[4] if len(head) > 4 else None)

    def _retrigger_interactive_pregen(self, just_finished: tuple) -> None:
        """Step 2 (direct_turn_preemption_20260803): re-evaluate the queue head
        when a pregen worker releases the slot.

        The interactive trigger fires ONLY from `enqueue()`'s tail, and its
        GPU-free gate refuses while a generation is in flight. That is the exact
        measured gap: on 2026-08-03 at 14:33:12 a direct turn was enqueued while
        the agenda's own pregen was mid-flight (14:33:11 -> 14:33:25), the gate
        refused, and nothing ever re-fired — the turn reached its boundary with
        `Pregen boundary: draft=none source=direct` and generated serially.

        `just_finished` is this spawn's own (payload, source). A spawn whose own
        item is STILL the head must not re-fire: for a stored draft the
        occupancy check in `_maybe_trigger_interactive_pregen` already no-ops,
        but for a REJECTED/discarded one (which stores nothing) it would spawn
        the identical generation again — and again at its completion — an
        unbounded regeneration loop for as long as the speech lasts.
        """
        with self._pq_lock:
            head_snapshot = self._head_snapshot_locked()
        if head_snapshot is None:
            return
        if (head_snapshot[1], head_snapshot[2]) == just_finished:
            return
        self._maybe_trigger_interactive_pregen(head_snapshot)

    def _maybe_trigger_interactive_pregen(self, head_snapshot) -> None:
        """WU3 interactive trigger (design-fase2.md §2.3): pregenerate the queue
        HEAD in the background while TTS plays, so a typed/PTT reply speaks
        near-instantly at the turn boundary instead of after a silent generation.

        GPU-free predicate: speaking now AND Ollama not mid-call
        (`_llm_generating` False — a generation already in flight blocks a second,
        the single-Ollama rule). Excludes `source="accumulated"` (its payload does
        not exist until flush). Head-tracking (AC3.5): skip when the head already
        matches the slot occupant; otherwise `pregenerate()` evicts a strictly-
        lower-priority occupant, so a higher-priority head landing in front (e.g.
        a PTT behind a chat pregen) displaces the now-stale one and pregenerates
        the new head. Spawn-only: `pregenerate()` starts a daemon thread and
        returns immediately, so the enqueue caller never blocks.

        Two callers: `enqueue()`'s tail (arrival) and
        `_retrigger_interactive_pregen` (Step 2, direct_turn_preemption_20260803
        — a pregen worker releasing the slot). Every gate below applies
        identically to both; nothing here interrupts anything, it only decides
        whether to start a background generation.
        """
        if head_snapshot is None:
            return
        priority, payload, source, history_text = head_snapshot
        if source == "accumulated":
            return
        # Rule 2 (interruptible_speech_architecture_20260804 §2c) — the
        # load-bearing half of the pair. With >=2 owner questions pending, the
        # next boundary bundles them into ONE request; drafting just the head
        # would re-serialize the burst, because Rule 1 always speaks a draft
        # that exists: answer 1's playback covers a draft for question 2, whose
        # playback covers a draft for question 3, and so on. Every question ends
        # up individually drafted and individually spoken, the bundle never
        # forms, and the busier the session the LESS bundling fires — the exact
        # inversion of the feature, and the measured 2026-08-03 incident (one
        # question per ~108s turn at 99.8% utilization). This removes spawns,
        # never adds them: a draft that already exists or is in flight is
        # untouched, because paid GPU work is always spoken.
        #
        # Race-sound while speaking: the count is a fresh _pq_lock read and the
        # refusal is stateless, so staleness only matters within THIS call.
        # Unarmed, pops cannot happen while _speaking and the count only GROWS
        # in this window. Armed (§5.2 step 4), a priority-0 pop CAN land
        # mid-speech (and the TTL sweep runs there too), so the count may also
        # SHRINK — both directions stay benign: a stale-high read refuses a
        # draft whose burst the pop-time bundler (§4 step 10) absorbs anyway
        # (the boundary just arrived early); a stale-low read spawns a draft
        # the pop either consumes (_take_pregen_if_match /
        # _wait_or_invalidate_pregen) or the F6 still-head re-check refuses.
        # Same TOCTOU posture as the F6 re-check below, with the same
        # foreground-commit epoch backstop.
        if source in _eng._OWNER_QUESTION_SOURCES and self.pending_owner_questions() >= 2:
            return
        if not (self.is_speaking and not self.llm_generating):
            return
        # Step 4 batch 2 (audit item 2, REAL_DEFECT): armed, a widened §5.2 pop
        # dispatches a REAL foreground generation UNDER playback, and
        # `_llm_generating` alone misses its [pop -> flag set] dispatch gap and
        # every retry gap — a trigger passing there spawns a SECOND concurrent
        # generation on the single runner (the delayed foreground call can trip
        # the inference watchdog into a spurious heavy-model rollback).
        # `_processing` spans the armed foreground turn end to end, so refuse
        # on it too. Conjunction-gated because the SAME flag means something
        # else unarmed: the legacy blocking path holds `_processing` through
        # the parent turn's whole PLAYBACK — exactly WU3's GPU-free window —
        # so an unconditional check would disable interactive pregen outright.
        # Residual: the [pop -> `_processing` set] window. When the pop finds
        # no draft and no in-flight pregen it is a few instructions wide;
        # when `_wait_or_invalidate_pregen` waits (bounded by the inference
        # watchdog), the window spans that wait BUT the occupied
        # `_pregen_inflight` slot makes pregenerate()'s F2 guard and the
        # connector's own slot check refuse every spawn — the window is
        # guarded by the SLOT there, not by narrowness. Accepted, same TOCTOU
        # posture as F6 above.
        if (
            self._speech_interrupt_enabled
            and self._speech_router_enabled
            and self.is_processing
        ):
            return
        # Already pregenerating / pregenerated exactly this head -> don't re-trigger.
        # F4 [v4]: occupancy is `_pregen_inflight is not None` (set under the lock
        # BEFORE the worker spawns), not thread.is_alive() (False in the tiny
        # assign->start window, which double-spawns).
        with self._prefetch_lock:
            cached = self._prefetched_agenda
            if cached is not None:
                occ_payload, occ_source = cached.get("payload"), cached.get("source")
            elif self._pregen_inflight is not None:
                occ_payload = self._pregen_inflight.get("payload")
                occ_source = self._pregen_inflight.get("source")
            else:
                occ_payload = occ_source = None
        if occ_payload == payload and occ_source == source:
            return
        # F6 [v4]: shrink the trigger-vs-pop TOCTOU window — re-check the head is
        # STILL the queue head right before spawning (the worker may already have
        # popped it, making this pregen a wasted duplicate). The epoch bump on the
        # foreground commit remains the backstop for the residual window.
        with self._pq_lock:
            cur = self._priority_queue[0] if self._priority_queue else None
            still_head = cur is not None and (cur[0], cur[2], cur[3]) == (priority, payload, source)
        if not still_head:
            return
        self.pregenerate(payload, priority, source, history_text=history_text)
        
    def prefetch_agenda(self, payload: str, priority: Optional[int] = None, source: str = "kira-agenda") -> bool:
        """Generate agenda text in the background without starting TTS.

        WU3 (design-fase2.md §2.1): thin alias over the generalized `pregenerate`
        after the kira-agenda source guard. CTK callers
        (agenda_audio_controller.py) are untouched — the guard + agenda-vs-agenda
        equal-priority refusal preserve the exact pre-WU3 behavior.

        `priority=None` resolves the LIVE agenda tier (turn_priority): the old
        hardcoded 2 would mismatch queue items minted at 3 under the default
        stream-over-agenda order, corrupting the strictly-lower-priority
        eviction compare in `pregenerate`.
        """
        if not payload or not source.startswith("kira-agenda"):
            return False
        if priority is None:
            priority = _eng.turn_priority.agenda_priority()
        return self.pregenerate(payload, priority, source)

    def pregenerate(
        self,
        payload: str,
        priority: int = 1,
        source: str = "chat",
        history_text: Optional[str] = None,
    ) -> bool:
        """Generalized background fill (design-fase2.md §2.1 [v2] / §2.3).

        `prefetch_agenda`'s worker body with the source gate REMOVED: any source
        may pregenerate. Agenda sources (`kira-agenda*`) additionally run the
        `_preview_accept_agenda_output` guardrail exactly as before; non-agenda
        (interactive) sources skip that preview because their transform/veto
        steps (`output_guard` for all sources, the chat repetition guard for
        chat, `_sanitize_agenda_output` for agenda) already run INSIDE
        `_generar_dialogo` — so the stored dialogo is byte-equivalent to what the
        foreground path would have said. History is deferred
        (`commit_history=False`) and committed at pop in `_speak_pregenerated`;
        `history_text` (F1 [v4]) rides along so the commit uses the honest turn
        text, not the raw prompt template.

        Slot occupancy [v4 — F2]: eviction applies to CACHED occupants ONLY. A
        request with STRICTLY higher priority (lower number) than a cached draft
        epoch-invalidates it and takes the slot (Step 2,
        direct_turn_preemption_20260803: a `direct` winner FREEZES a displaced
        `kira-agenda*` draft instead of discarding it — see the branch below);
        an equal-or-lower request is
        refused. While a worker is IN FLIGHT (marker set, nothing stored yet) its
        Ollama call is uncancellable, so ANY new request is refused — spawning a
        replacement would leave a zombie worker running concurrently whose finally
        would poison the replacement's shared bookkeeping.
        """
        # Pregen Cloud Gate (multi_provider_llm_20260723): speculative
        # generation is OFF by default on cloud (billable tokens). Local
        # short-circuits (`_is_local` True), so all existing behavior is
        # byte-identical; only an explicit PUT {"pregen_enabled": true} opts in.
        if not self._is_local and not self._provider_config.get("pregen_enabled", False):
            return False
        if not payload:
            return False
        is_agenda = source.startswith("kira-agenda")
        displaced_source: Optional[str] = None
        displaced_gen_ms: int = -1
        displaced_frozen: bool = False
        with self._prefetch_lock:
            # F2 [v4]: an in-flight worker (marker set, nothing stored yet) is
            # uncancellable -> refuse rather than spawn a second concurrent
            # generation. Once it has stored (cached), the priority eviction below
            # applies instead.
            if self._pregen_inflight is not None and self._prefetched_agenda is None:
                return False
            cached = self._prefetched_agenda
            if cached is not None:
                occupant_priority = cached.get("priority")
                if occupant_priority is not None and priority < occupant_priority:
                    displaced_source = str(cached.get("source", ""))
                    displaced_gen_ms = cached.get("gen_ms", -1)
                    # OQ-2 (direct_turn_preemption_20260803, Step 2): a DIRECT
                    # turn taking the slot FREEZES the agenda draft it displaces
                    # instead of discarding it — 11-14s of already-paid GPU work
                    # that the driver then resumes with a connector
                    # (AgendaDriver._maybe_return_frozen_stash), rather than
                    # regenerating the lost beat into dead air.
                    #
                    # Scoped to source=="direct", NOT to every interactive
                    # source, on purpose: `_frozen_stash` has exactly ONE
                    # consumer, api/agenda_driver.py, and `direct` is produced
                    # only by /api/chat/turn — i.e. only on the surface that
                    # runs that driver. A CTK-only `chat` winner would freeze a
                    # beat with nothing to return it, stranding it forever,
                    # which is strictly worse than the eviction it replaces.
                    # An already-frozen stash is never overwritten either — that
                    # would destroy the beat the driver is currently holding
                    # ticks for; evict as before in that case.
                    if (
                        source == "direct"
                        and self._frozen_stash is None
                        and self._freeze_agenda_stash_locked()
                    ):
                        # _freeze_agenda_stash_locked already cleared the slot and
                        # _prefetch_done. No epoch bump — freezing is not
                        # invalidation (there is no worker generating this draft;
                        # it is already cached).
                        displaced_frozen = True
                    else:
                        # Evict the strictly-lower-priority CACHED occupant: clear it
                        # and bump the epoch (defensive — no in-flight worker here).
                        self._prefetched_agenda = None
                        self._prefetch_done.clear()
                        self._prefetch_epoch += 1
                else:
                    return False
            self._prefetch_done.clear()
            epoch = self._prefetch_epoch
            # T5 [v5]: a local reference to THIS spawn's own marker — the
            # worker's finally compares against it by identity (not just
            # "is _pregen_inflight set") so a successor spawn's marker is
            # never wiped by a stale worker finishing in the store-to-finally
            # window (see the finally block below).
            inflight_marker = {"payload": payload, "source": source, "priority": priority}
            self._pregen_inflight = inflight_marker
            # WU4 4c: one retry-on-reject per spawn (reset here, under the same
            # lock as the rest of this spawn's bookkeeping).
            self._pregen_retried = False

        if displaced_source is not None:
            # WU4 4a boundary telemetry: the DISPLACED occupant's source, not the
            # new (winning) request's. draft=evicted means its turn falls back to
            # plain generation when it eventually pops; draft=frozen (Step 2)
            # means the draft survives and returns with a connector instead.
            self._log(
                f"Pregen boundary: draft={'frozen' if displaced_frozen else 'evicted'} "
                f"source={displaced_source} gap_ms=-1 "
                f"gen_ms={displaced_gen_ms} speech_ms={self._speech_ms_for_boundary()}"
            )

        log_prefix = "Agenda prefetch" if is_agenda else "Pregen"

        def worker() -> None:
            try:
                while True:
                    gen_start = time.monotonic()
                    dialogo = self._generar_dialogo(
                        payload, source=source, commit_history=False, log_prefix=log_prefix
                    )
                    if not dialogo:
                        return
                    if is_agenda and not self._preview_accept_agenda_output(dialogo):
                        self._log(f"Agenda: prefetch rechazado ({self._format_agenda_rejection()}).", level="warning")
                        code = self._agenda_rejection_code()
                        # T1(b) [v5]: a per-attempt notice at DEBUG only — the
                        # single INFO "Pregen boundary:" line for this spawn is
                        # emitted at its TERMINAL outcome (below), never once
                        # per rejected attempt.
                        _eng.logger.debug("Pregen retry attempt rejected: source=%s", source)
                        if self.on_guardrail_rejected is not None:
                            try:
                                self.on_guardrail_rejected(code)
                            except Exception:
                                _eng.logger.exception("on_guardrail_rejected callback failed")
                        # WU4 4c / T2(a) [v5]: retry once when the remaining
                        # speech window comfortably covers another generation —
                        # adaptive gate (1.2x the last COMPLETED generation),
                        # falling back to the flat constant on a cold start.
                        if not self._pregen_retried:
                            estimate = self.speech_remaining_estimate()
                            if estimate is not None and estimate > self._pregen_retry_gate_seconds():
                                # T6 [v5]: re-check the spawn's epoch is still
                                # current right before paying for a second
                                # generation — a stale/superseded spawn
                                # abandons the retry instead of generating for
                                # an already-doomed draft.
                                with self._prefetch_lock:
                                    still_current = self._prefetch_epoch == epoch
                                if still_current:
                                    self._pregen_retried = True
                                    continue
                                _eng.logger.debug(
                                    "Pregen retry abandoned: epoch stale (source=%s)", source
                                )
                                return
                        self._log(
                            f"Pregen boundary: draft=rejected source={source} gap_ms=-1 "
                            f"gen_ms=-1 speech_ms={self._speech_ms_for_boundary()}"
                        )
                        return
                    with self._prefetch_lock:
                        if self._prefetch_epoch != epoch:
                            self._log("Pregen descartado (invalidado por clear/stop/commit).", level="warning")
                            return
                        # T1(a) [v5]: the draft's own generation duration,
                        # recorded at store time — -1 (N/A) for a draft that
                        # never reaches this point (rejected/discarded).
                        gen_ms = int((time.monotonic() - gen_start) * 1000)
                        self._prefetched_agenda = {
                            "payload": payload,
                            "dialogo": dialogo,
                            "priority": priority,
                            "source": source,
                            "history_text": history_text,
                            "gen_ms": gen_ms,
                        }
                    if self._test_store_to_finally_hook is not None:
                        self._test_store_to_finally_hook()
                    return
            finally:
                # F4 [v4] / T5 [v5]: clear the in-flight marker (occupancy's
                # source of truth) under the lock BEFORE signalling done, so a
                # pop-side waiter waking on _prefetch_done never observes a
                # phantom in-flight marker — but ONLY when the marker is still
                # THIS spawn's own (identity check). Between this worker's
                # store above and this finally, a higher-priority request can
                # evict the just-stored draft and spawn a successor (F2 only
                # refuses while nothing is stored yet — once cached, priority
                # eviction applies), replacing _pregen_inflight with the
                # successor's OWN marker. Without this check, a stale worker's
                # unconditional clear+set here would wipe that successor's
                # marker and falsely signal _prefetch_done before the
                # successor has actually finished.
                slot_released = False
                with self._prefetch_lock:
                    if self._pregen_inflight is inflight_marker:
                        self._pregen_inflight = None
                        self._prefetch_done.set()
                        slot_released = True
                # Step 2 (direct_turn_preemption_20260803): the slot is free and
                # Ollama is idle again — re-evaluate the queue head, which may
                # have changed while this worker ran (the direct turn that
                # arrived mid-flight and whose trigger the GPU-free gate
                # refused). Outside _prefetch_lock, and only when THIS spawn is
                # the one that released the slot: if a successor already owns
                # the marker it is still generating, and pregenerate() would
                # refuse anyway.
                if slot_released:
                    self._retrigger_interactive_pregen((payload, source))

        thread = threading.Thread(target=worker, daemon=True)
        with self._prefetch_lock:
            self._prefetch_thread = thread
        # WU4 F5 (WU3 follow-up): a raising thread.start() must not leave the
        # in-flight marker stuck — that would refuse every future pregenerate()
        # forever. Clear it under the lock and report failure instead.
        try:
            thread.start()
        except Exception:
            _eng.logger.exception("Pregen worker thread failed to start")
            with self._prefetch_lock:
                self._pregen_inflight = None
            return False
        return True

    def wait_prefetched_agenda(self, timeout: float = 0.0) -> bool:
        if timeout > 0:
            self._prefetch_done.wait(timeout)
        with self._prefetch_lock:
            cached = self._prefetched_agenda
            # F5 [v4]: this is the CTK legacy agenda consume path — report ready
            # ONLY for an agenda-source draft. An interactive (chat/ptt) occupant
            # is invisible here; it pops through the worker's own queue path.
            return cached is not None and str(cached.get("source", "")).startswith("kira-agenda")

    def prefetch_pending(self) -> bool:
        """True while a prefetch worker is still generating (no draft yet).

        Distinguishes "draft is late, keep waiting" (worker alive, no cache) from
        "worker finished with nothing — fall back now" (thread dead / no cache).
        A ready draft returns False (it's no longer pending — consume it).
        """
        with self._prefetch_lock:
            thread = self._prefetch_thread
            return bool(thread is not None and thread.is_alive() and self._prefetched_agenda is None)

    def has_pending_priority_before(self, priority: int) -> bool:
        """Return True when queued work should run before a cached agenda draft."""
        with self._pq_lock:
            return any(item[0] < priority for item in self._priority_queue)


    def clear_prefetched_agenda(self) -> None:
        with self._prefetch_lock:
            self._prefetched_agenda = None
            self._prefetch_done.clear()
            self._prefetch_epoch += 1

    def clear_prefetched_agenda_only(self) -> None:
        """WU4 F1 (WU3 follow-up): source-aware clear for the driver's
        yield/drop paths (design-fase2.md §3 WU4-4d).

        `AgendaDriver._clear_prefetch` used to call the unconditional
        `clear_prefetched_agenda`, which could clobber a live INTERACTIVE
        (chat/PTT) pregen the driver has nothing to do with. This clears the
        slot ONLY when it currently holds an agenda draft; an interactive
        occupant survives untouched. `clear_prefetched_agenda` itself stays
        unconditional — the CTK legacy consume path depends on it as-is.

        T4 [v5]: also covers the slot-EMPTY case — an agenda worker still IN
        FLIGHT (marker set, nothing stored yet) whose eventual store would
        land into a slot the driver just deliberately dropped. Bumping the
        epoch here invalidates that incoming store (the worker's own
        store-time epoch check in `pregenerate()`'s worker discards it), so
        an orphaned agenda draft never survives a drop it was never meant to
        see. An interactive in-flight worker is untouched either way — it has
        nothing to do with the driver's agenda-only drop.
        """
        with self._prefetch_lock:
            cached = self._prefetched_agenda
            if cached is not None:
                if not str(cached.get("source", "")).startswith("kira-agenda"):
                    return
                self._prefetched_agenda = None
                self._prefetch_done.clear()
                self._prefetch_epoch += 1
                return
            inflight = self._pregen_inflight
            if inflight is not None and str(inflight.get("source", "")).startswith("kira-agenda"):
                self._prefetch_epoch += 1

    def play_prefetched_agenda(self) -> bool:
        """Speak cached agenda text, if available, without another LLM call."""
        with self._prefetch_lock:
            item = self._prefetched_agenda
            # F5 [v4]: pop ONLY agenda-source drafts. An interactive occupant is
            # left intact (never spoken as an agenda turn) for its own worker-path
            # pop — return False without popping.
            if not item or not str(item.get("source", "")).startswith("kira-agenda"):
                return False
            self._prefetched_agenda = None
            self._prefetch_done.clear()

        def speaker() -> None:
            payload = item["payload"]
            dialogo = item["dialogo"]
            self._commit_history(payload, dialogo, source=item.get("source", "kira-agenda"))
            self._record_accepted_agenda_output(dialogo)
            self._log("Agenda: usando respuesta prefabricada durante el audio anterior.")
            self.log_queue.put(f"\n🧠 [Kira]: {dialogo}\n")
            self._emit_dialogue(dialogo, item.get("source", "kira-agenda"))
            self._speak_or_submit(dialogo, source=item.get("source", "kira-agenda"))

        if self._speech_router_enabled:
            # Step 2: the detached speaker thread existed only so playback
            # would not block this caller. `submit` is non-blocking, so it is
            # gone — one fewer thread that can outlive the turn that spawned it.
            speaker()
        else:
            threading.Thread(target=speaker, daemon=True).start()
        return True

    def _take_pregen_if_match(self, payload: str, source: str) -> Optional[dict]:
        """Pop the cached pregen draft iff it matches this (payload, source).

        WU2 (design-fase2.md §2.2): consulted at the worker's queue-pop boundary.
        A hit means the popped turn was pregenerated during the prior turn's TTS,
        so the worker speaks the cache instead of generating. A miss (empty or
        different cache) leaves the cache untouched and the worker generates
        normally. Consume-only (no epoch bump — no in-flight worker at pop time),
        mirroring play_prefetched_agenda's own pop semantics.
        """
        with self._prefetch_lock:
            cached = self._prefetched_agenda
            if cached is None:
                return None
            if cached.get("payload") == payload and cached.get("source") == source:
                self._prefetched_agenda = None
                self._prefetch_done.clear()
                return cached
            return None

    def _clear_prefetch_unless_matches(self, payload: str, source: str) -> None:
        """Supersede the cached draft UNLESS it IS this exact (payload, source).

        WU2 (design-fase2.md §2.2/§2.3): the consume path enqueues a ready
        draft's own (payload, source) to route it through the queue. Clearing the
        cache on that enqueue would nuke the draft before the worker's pop can
        match it. Skip the clear for that self-match; any mismatch is a genuine
        supersede and still clears + bumps the invalidation epoch (unchanged).
        """
        with self._prefetch_lock:
            cached = self._prefetched_agenda
            if cached is not None and cached.get("payload") == payload and cached.get("source") == source:
                return
            self._prefetched_agenda = None
            self._prefetch_done.clear()
            self._prefetch_epoch += 1

    def _clear_prefetch_if_matches(self, payload: str, source: str) -> None:
        """F7 [v4]: clear a cached pregen draft matching (payload, source).

        Called when the queue item that would have consumed the draft expires
        (TTL sweep): leaving the orphaned draft in the single slot would block
        every new prefetch until an unrelated enqueue happens to clear it. Bumps
        the epoch so a matching in-flight worker also discards its store.

        WU4 F4 (WU3 follow-up, TOCTOU): skip the clear entirely when a pregen
        worker is currently IN FLIGHT for this exact (payload, source) — a
        fresh replacement generation already superseding the stale entry.
        Clearing (and bumping the epoch) here would invalidate that fresh
        worker's imminent store for no reason; let it land naturally instead.
        """
        with self._prefetch_lock:
            inflight = self._pregen_inflight
            if (
                inflight is not None
                and inflight.get("payload") == payload
                and inflight.get("source") == source
            ):
                return
            cached = self._prefetched_agenda
            if cached is not None and cached.get("payload") == payload and cached.get("source") == source:
                self._prefetched_agenda = None
                self._prefetch_done.clear()
                self._prefetch_epoch += 1

    def _invalidate_pregen_epoch(self) -> None:
        """WU4 F3 (WU3 follow-up): unconditional epoch bump + slot clear.

        Called at every non-committing FOREGROUND fallback return in
        `_generar_dialogo` (watchdog timeout, transport error, empty dialogo,
        guardrail-no-fallback, chat-repetition fallback, agenda reject, the
        outer exception handler). Those returns skip `_commit_history` — the
        usual epoch-bump backstop (AC3.3) — so without this, a pregen worker
        that started before this failed turn could still land a stale store
        after it, silently surviving into the next pop.
        """
        with self._prefetch_lock:
            self._prefetched_agenda = None
            self._prefetch_done.clear()
            self._prefetch_epoch += 1

    def _speak_pregenerated(self, cached: dict, *, already_reported_boundary: bool = False) -> None:
        """Speak a pop-time cache hit on the WORKER thread (design-fase2.md §2.2).

        play_prefetched_agenda's speaker body, minus the parallel thread: the
        worker already owns the turn (single dispatch path), so deferred-commit +
        emit + _hablar run inline. History commits HERE, at playback, in spoken
        order (commit-once invariant — the pregen used commit_history=False).

        `already_reported_boundary` (WU4 4a): True when the caller already
        emitted this pop's "Pregen boundary:" line (the "late" wait-then-hit
        path) — skips the "used" line here so exactly one boundary line is
        logged per turn boundary.
        """
        payload = cached["payload"]
        dialogo = cached["dialogo"]
        source = cached.get("source", "kira-agenda")
        # WU5 D3 (design-fase2.md §3 WU5): a RESUMED agenda draft carries a
        # connector — prepend it so the connector + stashed dialogo are spoken
        # AND committed as ONE turn (the connector is part of the spoken turn).
        connector = cached.get("connector")
        if connector:
            dialogo = f"{connector} {dialogo}".strip()
            # clause_sanitizer V1: the guardrails ran INSIDE _generar_dialogo,
            # before this concatenation, and nothing is re-applied here (see the
            # comment below) — so the connector/draft junction is the one place a
            # clause can duplicate after the seam. Repair-only: there is no
            # regeneration machinery at playback, so a reject verdict here could
            # only stall the turn.
            if source in _eng.CLAUSE_SANITIZER_SOURCES:
                san = _eng.sanitize_clause_repetition(dialogo)
                # Logged for every verdict, including "clean" — otherwise the
                # clean count is unobtainable from the log (ADR-039 gate).
                self._log_clause_sanitizer(san, source, stage="pregen_connector")
                dialogo = san.text
        # F1 [v4]: forward the honest history_text (PTT/direct turns) exactly as
        # the foreground path does — else the raw prompt template leaks into
        # historial + memoria capture (the memoria_quality regression).
        self._commit_history(payload, dialogo, source=source, history_text=cached.get("history_text"))
        # F7 [v4]: source-aware log line — agenda keeps its historical wording;
        # interactive sources log their own, never the "Agenda:" prefix.
        if source.startswith("kira-agenda"):
            self._record_accepted_agenda_output(dialogo)
            self._log("Agenda: usando respuesta pregenerada.")
        else:
            self._log(f"Respuesta pregenerada [{source}] lista.")
        self.log_queue.put(f"\n🧠 [Kira]: {dialogo}\n")
        # WU3 interactive parity (design-fase2.md §3 WU3): mirror the foreground
        # _ejecutar_inferencia post-generation steps for non-agenda sources — the
        # emit forwards the TRUE source and a chat turn advances the spoken
        # clock. The dialogo already passed every transform/veto INSIDE
        # _generar_dialogo (sanitizer, output_guard, chat repetition guard), so
        # nothing is re-applied here.
        #
        # Parity is the whole point of this line: it emits the source unrelabeled
        # exactly like the foreground path now does. A pregenerated chat reply and
        # a foreground one are the same turn to every consumer, so if one of these
        # two sites ever relabels and the other does not, a cache hit and a cache
        # miss start reporting different origins for identical work.
        self._emit_dialogue(dialogo, source)
        if not already_reported_boundary:
            # WU4 4a boundary telemetry: an immediate pop-time cache hit is a
            # "used" draft. gap_ms = ms since the PREVIOUS turn's speaking_end;
            # -1 if unknown (e.g. the very first turn of the session). gen_ms
            # is this draft's own recorded generation duration (T1(a) [v5],
            # tracked in the slot dict at store time); speech_ms is the
            # previous turn's own speech duration.
            with self._lock:
                last_end = self._last_speaking_end_monotonic
            gap_ms = int((time.monotonic() - last_end) * 1000) if last_end is not None else -1
            gen_ms = cached.get("gen_ms", -1)
            # WU5: a resumed frozen draft is labelled draft=resumed (else used).
            draft_label = "resumed" if cached.get("resumed") else "used"
            self._log(
                f"Pregen boundary: draft={draft_label} source={source} gap_ms={gap_ms} "
                f"gen_ms={gen_ms} speech_ms={self._speech_ms_for_boundary()}"
            )
        # Step 2: the chat spoken clock moved to the router's job-completion
        # path (design §11 B1/B5) — it must fire only when the job actually
        # FINISHED with speech delivered, which a non-blocking submit can no
        # longer tell from here.
        self._speak_or_submit(dialogo, source=source)

    def _pregen_in_flight_for(self, payload: str, source: str) -> bool:
        """True when a pregen worker is currently generating THIS exact
        (payload, source) and has not stored a draft yet (design-fase2.md AC3.2).

        F4 [v4]: occupancy is `_pregen_inflight is not None` (set under the lock
        before spawn, cleared in the worker's finally under the lock), NOT
        thread.is_alive() — the same source of truth as every other occupancy check.
        """
        with self._prefetch_lock:
            inflight = self._pregen_inflight
            return bool(
                self._prefetched_agenda is None
                and inflight is not None
                and inflight.get("payload") == payload
                and inflight.get("source") == source
            )

    def _pregen_wait_bound(self) -> float:
        """T3 [v5]: the pop-side wait ceiling for a same-item in-flight
        pregen — `_inference_watchdog_timeout` itself (1x), not a multiple.

        On timeout the pop falls back to foreground regardless of whether the
        worker is still alive: a worker outliving the watchdog is already the
        declared transient-overlap class (design-fase2.md §4 [v4] — the rare
        watchdog-timeout window where a foreground turn may transiently
        overlap an unrelated in-flight pregen's Ollama call, bounded and
        self-healing via the epoch-bump backstop). In the API host the
        `_generar_dialogo` retry loop cannot legitimately still be running
        past this bound for THIS item's pop-side wait: `_speaking` is False
        while the pop is waiting here (no turn is being spoken), so nothing
        can race a second generation for the same item behind this wait — the
        pathological >watchdog worker is a stuck/hung Ollama call, not the
        loop's own internal (`max_intentos = 2`) retry, which the previous
        (`2x + 1.0`) docstring overstated as this wait's own ceiling.
        """
        return self._inference_watchdog_timeout

    def _wait_or_invalidate_pregen(self, payload: str, source: str) -> Optional[dict]:
        """AC3.2 [v4] wait-or-fallback at a pop-time cache miss.

        F3 [v4]: when a pregen worker is generating THIS exact item, starting a
        foreground generation is strictly worse than waiting — the single Ollama
        runner would queue the foreground BEHIND the in-flight pregen (and the
        watchdog could fire and silently drop the turn). So ALWAYS wait on
        `_prefetch_done`, bounded by `_pregen_wait_bound()` — T3 [v5]:
        `_inference_watchdog_timeout` itself (see `_pregen_wait_bound`'s
        docstring for why 1x is honest here). On wake: take the draft if it
        landed (epoch-valid), else fall back to foreground regardless of
        whether the worker is still alive — by then either the worker has
        finished (the GPU is free, a healthy pregen resolves to a cache hit)
        or it is the declared pathological transient-overlap class, and the
        foreground turn's own `_commit_history` epoch bump remains the
        backstop that discards any late store either way.
        """
        if not self._pregen_in_flight_for(payload, source):
            return None
        wait_start = time.monotonic()
        self._prefetch_done.wait(self._pregen_wait_bound())
        cached = self._take_pregen_if_match(payload, source)
        if cached is not None:
            # WU4 4a boundary telemetry: the pop WAITED on an in-flight pregen
            # and it landed — "late" (gap_ms is the actual wait duration).
            wait_ms = int((time.monotonic() - wait_start) * 1000)
            gen_ms = cached.get("gen_ms", -1)
            self._log(
                f"Pregen boundary: draft=late source={source} gap_ms={wait_ms} "
                f"gen_ms={gen_ms} speech_ms={self._speech_ms_for_boundary()}"
            )
            return cached
        self._log("Pregen en vuelo sin borrador válido; generando en vivo.")
        return None
