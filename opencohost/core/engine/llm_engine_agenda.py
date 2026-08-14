import threading
import time
from typing import Optional

# `Optional` is imported directly, not via `_eng`: it is used in ANNOTATIONS, which
# evaluate at class-body time during llm_engine's PARTIAL import, where `_eng.X`
# resolves only for names bound above the mixin-import line. Annotations want a
# direct import; runtime values want `_eng`. See llm_engine_memorias.py.
from opencohost.core import llm_engine as _eng

class AgendaStashMixin:
    
    def _freeze_agenda_stash_locked(self) -> bool:
        """Move the cached NEXT-turn agenda draft out of the active pregen slot
        into `_frozen_stash`, so a direct-turn slot handover (`pregenerate`'s
        `source == "direct"` branch) can use the freed slot and the
        agenda draft survives the detour to be resumed. No epoch bump —
        freezing is not invalidation (there is no in-flight worker for an
        already-cached draft). Resets the detour counter. Returns True iff a
        draft was frozen (nothing to return to otherwise — normal flow resumes).

        Caller holds `_prefetch_lock` (threading.Lock is not reentrant); the
        sole production caller is `pregenerate`'s slot-handover branch.
        """
        cached = self._prefetched_agenda
        if cached is None or not str(cached.get("source", "")).startswith("kira-agenda"):
            return False
        frozen = dict(cached)
        frozen["connector"] = None
        self._frozen_stash = frozen
        # R1: stamp the freeze time so the driver can bound the hold.
        self._frozen_stash_at = time.monotonic()
        self._prefetched_agenda = None
        self._prefetch_done.clear()
        self._detour_turns = 0
        return True

    def has_frozen_stash(self) -> bool:
        with self._prefetch_lock:
            return self._frozen_stash is not None

    def frozen_stash_age_seconds(self) -> Optional[float]:
        """R1 seam: seconds since the current stash was frozen, or None when no
        stash is pending (or no freeze time was recorded). The driver reads this
        instead of the engine internals to enforce the FROZEN_STASH_MAX_HOLD
        deadline backstop."""
        with self._prefetch_lock:
            if self._frozen_stash is None or self._frozen_stash_at is None:
                return None
            return time.monotonic() - self._frozen_stash_at

    def detour_exceeded(self) -> bool:
        """D2 skip: more than RETURN_MAX_DETOUR_TURNS interactive turns chained
        since the freeze — a real conversation started, so skip the return."""
        with self._prefetch_lock:
            return self._detour_turns > _eng.RETURN_MAX_DETOUR_TURNS

    def detour_started(self) -> bool:
        """F1: True once at least one interactive turn has been noted since the
        freeze. The return must HOLD until then — the interruption answer rides the
        command queue (invisible to the driver's return gates), so a speaking_end
        from the cut itself must never fire the connector return BEFORE the answer
        speaks. The detour counter increments as the answer turn is selected."""
        with self._prefetch_lock:
            return self._detour_turns > 0

    def discard_frozen_stash(self) -> None:
        """D2 skip (driver-side): drop the frozen stash and reset the detour
        counter so the return is suppressed and normal next_action resumes."""
        with self._prefetch_lock:
            self._frozen_stash = None
            self._frozen_stash_at = None
            self._detour_turns = 0

    def _invalidate_frozen_stash(self) -> None:
        """D2 epoch skip: a profile/model switch or a session stop invalidates a
        pending return (a stashed draft built under the old persona/history must
        never be resumed). Called from set_profile, switch_llm_tier, and the
        agenda drop path (drop_pending_sources)."""
        with self._prefetch_lock:
            self._frozen_stash = None
            self._frozen_stash_at = None
            self._detour_turns = 0

    def restore_frozen_stash(self, tema: str = "") -> Optional[tuple]:
        """WU5 D2/D3 RETURN: move the frozen agenda draft back into the active
        pregen slot (marked `resumed`, connector resolved) so the normal
        consume+pop+speak path resumes it. The connector is the ready generated
        upgrade if one landed during the answer's TTS, else the parameterized
        pool floor (D3). Returns (payload, source, priority) for the driver to
        requeue, or None when there is no frozen stash (a skip fired)."""
        with self._prefetch_lock:
            stash = self._frozen_stash
            upgrade = stash.get("connector") if stash is not None else None
        connector = upgrade if upgrade else self._pick_connector_floor(tema)
        with self._prefetch_lock:
            stash = self._frozen_stash
            if stash is None:
                return None
            restored = dict(stash)
            restored["connector"] = connector
            restored["resumed"] = True
            # F5: bump the pregen epoch as the stash re-enters the active slot (same
            # pattern as _commit_history) so any in-flight generation keyed to the
            # pre-restore epoch is invalidated — it must never overwrite the resumed
            # draft. The restored draft is placed directly (not via a worker store),
            # so the bump kills only OTHER stale workers, never this draft.
            self._prefetch_epoch += 1
            self._prefetched_agenda = restored
            self._frozen_stash = None
            self._frozen_stash_at = None
            self._detour_turns = 0
            self._prefetch_done.set()
        # The stash always stores its real priority; the fallback is only for a
        # legacy dict shape and must track the LIVE agenda tier, not literal 2.
        return (restored["payload"], restored.get("source", "kira-agenda"), restored.get("priority", _eng.turn_priority.agenda_priority()))

    def _pick_connector_floor(self, tema: str) -> str:
        """D3 floor: the next es-AR connector template, rotated without immediate
        repetition, parameterized with the live topic title."""
        templates = _eng.i18n_active.connector_templates()
        if not templates:
            return ""
        with self._prefetch_lock:
            last = self._connector_last_idx
            idx = 0 if last is None else (last + 1) % len(templates)
            self._connector_last_idx = idx
        try:
            return templates[idx].format(tema=tema or "eso")
        except (KeyError, IndexError):
            return templates[idx]

    def _note_detour_turn(self, source: str) -> None:
        """WU5 D2: count an interactive turn toward the detour budget when a
        return is pending. No-op when no return is pending or for an agenda-source
        turn (the resume turn itself never counts). Called as the turn is selected
        to speak — the connector UPGRADE is triggered separately, at speaking_start
        (during TTS playback), so it never races the turn's own generation."""
        if not source or source.startswith("kira-agenda"):
            return
        with self._prefetch_lock:
            if self._frozen_stash is None:
                return
            self._detour_turns += 1

    def _maybe_generate_connector_upgrade(self) -> None:
        """D3 upgrade: while the interruption answer's TTS plays (GPU free),
        generate a one-line contextual connector into `_frozen_stash['connector']`.
        LOWEST priority by construction: it writes to a SEPARATE field (never the
        `_prefetched_agenda` slot), and refuses to even spawn while a real pregen
        occupies or is in flight for that slot — so it can never evict or delay a
        real interactive/agenda pregen (AC5.6). Best-effort: a late/rejected/absent
        upgrade just falls back to the pool floor at the return (timeout-0 read).

        Step 4 batch 2 (audit item 3): armed, a priority-0 owner question can
        pop and GENERATE under this very playback (widened §5.2 gate), so two
        armed-only refusals below keep the upgrade off the single runner then.
        Residual: a question arriving AFTER the upgrade's request is in flight
        still serializes behind it for up to ~CONNECTOR_UPGRADE_TIMEOUT_SECONDS
        client-side — longer server-side on a stalling model, since the
        watchdog abandon does not free the runner — unclosable without a
        cancellable transport.
        """
        # F1 Pregen Cloud Gate (multi_provider_llm_20260723): the connector
        # upgrade is speculative generation — it calls _generar_dialogo directly,
        # bypassing pregenerate()'s gate. OFF by default on cloud (billable
        # tokens); local short-circuits so all existing behavior is byte-identical.
        # Same two-condition idiom as the pregenerate gate.
        if not self._is_local and not self._provider_config.get("pregen_enabled", False):
            return
        # R3 spawn gate: the upgrade is cosmetic (the pool floor is a complete
        # connector). Only spawn when the remaining-playback estimate comfortably
        # covers a bounded generation — otherwise the upgrade could outlive the
        # answer's playback and start a second concurrent Ollama call behind the
        # real turn (heavy/stalling models). An unknown/short estimate -> skip; the
        # floor stands. (At speaking_start the answer's own progress is not yet
        # measurable, so this commonly skips and the floor is used — by design.)
        remaining = self.speech_remaining_estimate()
        if remaining is None or remaining <= _eng.CONNECTOR_UPGRADE_MIN_REMAINING_SECONDS:
            return
        # Step 4 batch 2 (audit item 3): armed, a queued priority-0 owner
        # question WILL pop under this very playback — spawning the cosmetic
        # upgrade now would race the owner's answer for the single runner.
        # Unarmed this gate stays dead: a legacy owner question waits for the
        # boundary, and playback IS the legacy GPU-free window.
        if (
            self._speech_interrupt_enabled
            and self._speech_router_enabled
            and self.has_pending_priority_before(1)
        ):
            return
        # llm_output_streaming_20260813 §7: streaming breaks the "the GPU is
        # free during playback" premise every gate above was built on — decode
        # now overlaps the first sentences' playback on the single Ollama
        # runner, so an upgrade spawned here would queue behind, and compete
        # with, the very generation whose tokens are being spoken.
        #
        # Flag-gated because this refusal is NOT inert on the buffered path.
        # Armed, `_hablar_impl` runs on the router's playback thread while the
        # engine worker can already have popped a widened priority-0 turn and
        # started its FOREGROUND generation: `has_pending_priority_before(1)`
        # no longer sees it (it popped) and `_pregen_inflight` never covered it
        # (it is not a pregen), so `_llm_generating` can be True right here
        # today — and today the spawn still happens, with only the worker's own
        # claim below standing between it and the LLM. Refusing unconditionally
        # would close the spawn->worker window on a working buffered gate,
        # which is out of this unit's scope. Flag OFF keeps that byte-identical.
        # Flag ON the refusal is slightly wider than "streamed turns only" — a
        # streaming-INELIGIBLE direct/ptt turn gets it too — which costs at most
        # a cosmetic upgrade that falls back to the pool floor.
        if _eng.LLM_STREAMING_ENABLED and self.llm_generating:
            return
        with self._prefetch_lock:
            stash = self._frozen_stash
            if stash is None or stash.get("connector") is not None:
                return
            # Yield to any real pregen — never compete for the single Ollama runner.
            if self._prefetched_agenda is not None or self._pregen_inflight is not None:
                return

        def worker() -> None:
            # F3: claim single-Ollama occupancy ATOMICALLY before generating. The
            # spawn-time yield checks above are racy (a foreground/pregen generation
            # can start after them), so gate on `_llm_generating` under the same lock
            # the generation path uses; if a generation is already in flight, skip —
            # the pool floor is an acceptable connector, never queue or retry.
            with self._lock:
                if self._llm_generating:
                    return
                # Step 4 batch 2 (audit item 3): armed, `_processing` True
                # means a widened pop is dispatching a REAL foreground turn —
                # including the [pop -> `_llm_generating` set] gap and retry
                # gaps the flag check above misses. Claiming now would hold
                # the runner against the owner's priority-0 answer. Unarmed
                # this branch stays dead: the legacy blocking path holds
                # `_processing` through the parent turn's whole PLAYBACK, and
                # refusing on it would disable the upgrade outright.
                if (
                    self._speech_interrupt_enabled
                    and self._speech_router_enabled
                    and self._processing
                ):
                    return
                self._llm_generating = True
            # R4 ownership token: _generar_dialogo self-brackets `_llm_generating`
            # (sets it, clears it in its own finally), so once it runs it is the SOLE
            # releaser of THIS claim. The outer finally must clear the flag ONLY on an
            # early error/skip BEFORE _generar_dialogo ran — clearing after it returned
            # would clobber a THIRD party's fresh claim taken in the release window
            # (making `_llm_generating` read False during a live Ollama call, so the
            # WU3 GPU-free predicate misfires).
            generation_started = False
            try:
                prompt = (
                    "Generá UNA sola línea muy corta de transición para retomar "
                    "lo que venías diciendo antes de que te interrumpieran. Natural, "
                    "sin anunciar estructura, sin saludar, sin cerrar."
                )
                generation_started = True
                # R3: hard-bound the generation so a heavy/stalling model abandons
                # the cosmetic upgrade (watchdog_timeout suppresses the heavyweight
                # stall recovery too) instead of racing the real turn.
                line = self._generar_dialogo(
                    prompt, source="kira-agenda", commit_history=False,
                    log_prefix="Connector", watchdog_timeout=_eng.CONNECTOR_UPGRADE_TIMEOUT_SECONDS,
                )
                if not line:
                    return
                if not self._preview_accept_agenda_output(line):
                    return
                # F6: only land the connector on the SAME stash this worker was
                # spawned for. A discard+new-freeze between spawn and completion
                # must not stamp this now-stale connector onto a fresh stash.
                with self._prefetch_lock:
                    if self._frozen_stash is stash and stash.get("connector") is None:
                        stash["connector"] = line
            except Exception:
                _eng.logger.exception("connector upgrade generation failed")
            finally:
                if not generation_started:
                    with self._lock:
                        self._llm_generating = False

        threading.Thread(target=worker, daemon=True).start()

    def _pregen_retry_gate_seconds(self) -> float:
        """T2(a) [v5]: the adaptive retry-gate threshold for a rejected
        background pregen — 1.2x the last COMPLETED generation's duration
        (foreground or pregen), falling back to the flat
        RETRY_MIN_REMAINING_SECONDS constant on a cold start (no generation
        measured yet this session). Design-spec adaptive gate, constant as
        cold-start fallback.
        """
        last = self._pregen_last_gen_duration
        if last is None:
            return _eng.RETRY_MIN_REMAINING_SECONDS
        return last * 1.2

    def _speech_ms_for_boundary(self) -> int:
        """T1(a) [v5]: the PREVIOUS turn's speech duration in ms, for the
        `speech_ms=` field of a "Pregen boundary:" telemetry line. -1 while
        unknown (no turn has finished speaking yet this session).
        """
        with self._lock:
            duration = self._last_speech_duration_ms
        return duration if duration is not None else -1
    
    def _accept_agenda_output(self, dialogo: str) -> bool:
        validator = getattr(self, "agenda_output_validator", None)
        if validator is None:
            return True
        try:
            return bool(validator(dialogo))
        except Exception:
            _eng.logger.exception("Agenda output validator failed")
            return False

    def _preview_accept_agenda_output(self, dialogo: str) -> bool:
        validator = getattr(self, "agenda_output_preview_validator", None)
        if validator is None:
            validator = getattr(self, "agenda_output_validator", None)
        if validator is None:
            return True
        try:
            return bool(validator(dialogo))
        except Exception:
            _eng.logger.exception("Agenda preview output validator failed")
            return False

    def _format_agenda_rejection(self) -> str:
        """Return a compact rejection reason string for logging (Phase 0a)."""
        ctl = getattr(self, "agenda_controller", None)
        if ctl is None or not ctl.rejection_log:
            return "guardrails"
        last = ctl.rejection_log[-1]
        grd = last.get("guardrail", last.get("error", "UNKNOWN"))
        ov = last.get("overlap_pct")
        phrase = last.get("matched_phrase")
        if ov is not None:
            return f"{grd} (overlap {ov}%)"
        if phrase:
            return f"{grd} (\"{phrase[:50]}...\")"
        return str(grd)

    def _agenda_rejection_code(self) -> str:
        """WU4 4b: the rejection CODE only (e.g. "contains_internal_leak").

        Unlike `_format_agenda_rejection` (the human log line), this NEVER
        includes the overlap percentage or matched-phrase snippet — those can
        carry a fragment of generated dialogue. Safe to ride an event's
        `action` string.
        """
        ctl = getattr(self, "agenda_controller", None)
        if ctl is None or not ctl.rejection_log:
            return "unknown"
        last = ctl.rejection_log[-1]
        return str(last.get("guardrail", last.get("error", "unknown")))

    def _record_accepted_agenda_output(self, dialogo: str) -> None:
        recorder = getattr(self, "agenda_output_recorder", None)
        if recorder is None:
            return
        try:
            recorder(dialogo)
        except Exception:
            _eng.logger.exception("Agenda output recorder failed")