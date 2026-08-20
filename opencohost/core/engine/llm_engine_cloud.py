"""
opencohost/core/engine/llm_engine_cloud.py

Cloud-transport posture, failure handling, and the return-probe loop of
MotorVocalIA. Mixin extracted from llm_engine.py; all runtime state stays on
MotorVocalIA.
"""
import threading
import time
from typing import Optional

from opencohost.core.providers.cloud import cloud_llm_client

# `Optional` and `cloud_llm_client` are imported directly, not via `_eng`, because
# both appear where the CLASS BODY evaluates: `Optional` in annotations, and
# `cloud_llm_client.CLOUD_ERROR_TRANSIENT` as the default arg of
# `_handle_cloud_failure`. Those run during llm_engine's PARTIAL import, where
# `_eng.X` resolves only for names bound above its mixin-import line. Annotations
# want a direct import; runtime values want `_eng` -- which is why every other
# `cloud_llm_client` reference below stays on `_eng`. Both spellings reach the same
# module object anyway: test_llm_engine_model_trace.py:558 patches
# `...llm_engine.cloud_llm_client.send_chat_completion`, which MUTATES that shared
# object rather than rebinding the name. No cycle: cloud_llm_client imports only
# `requests`. The default arg was already frozen at class-definition time before
# the split, so this is behaviour-identical.
from opencohost.core import llm_engine as _eng

class CloudFallbackMixin:
    def provider_runtime_state(self) -> dict:
        """Live provider/transport truth (F4, runtime_findings_batch_20260731 1.3).

        Read-only snapshot of the EFFECTIVE posture — the same
        `_cfg_is_local(...) or _cloud_fallback_active` posture `_is_local`
        computes — so status/telemetry can never again drift from what a live
        generation would actually use. Deliberately never reads
        `llm_provider.json` from disk: that disk read is exactly what let
        `_display_model` (opencohost/api/main.py) keep reporting the stale
        cloud model name through an active `_cloud_fallback_active` fallback.

        Returns a dict with:
        - `provider`: "local" when the effective transport is local (whether
          by config or by fallback); otherwise the persisted cloud provider id.
        - `transport`: "local" | "cloud".
        - `fallback_active`: the raw `_cloud_fallback_active` flag.
        - `fallback_reason` (unit 2.2): the 1.1 class the fallback engaged
          for, or None outside fallback.
        - `provider_epoch` (unit 2.2): the running count of provider
          transitions this process has made.
        - `next_cloud_probe_in_seconds` (unit 2.2): seconds until the next
          background return probe, or None when no probe is scheduled
          (no active fallback, or a non-probeable class).
        - `generation_model`: the model a request dispatched right now would
          actually use — `current_model` locally, the active cloud profile's
          `model` on cloud (None if unset/missing, same graceful degradation
          `_display_model` already had).
        """
        cfg = self._provider_config
        with self._lock:
            fallback_active = self._cloud_fallback_active
            fallback_reason = self._cloud_fallback_reason
            provider_epoch = self.provider_epoch
            probe_next_at = self._cloud_probe_next_at
        next_probe_in = None if probe_next_at is None else max(0.0, probe_next_at - time.monotonic())
        is_local = self._cfg_is_local(cfg) or fallback_active
        if is_local:
            return {
                "provider": "local",
                "transport": "local",
                "fallback_active": fallback_active,
                "fallback_reason": fallback_reason,
                "provider_epoch": provider_epoch,
                "next_cloud_probe_in_seconds": next_probe_in,
                "generation_model": self.current_model,
            }
        profile = self._cfg_active_profile(cfg) or {}
        return {
            "provider": cfg.get("active_provider") or "local",
            "transport": "cloud",
            "fallback_active": fallback_active,
            "fallback_reason": fallback_reason,
            "provider_epoch": provider_epoch,
            "next_cloud_probe_in_seconds": next_probe_in,
            "generation_model": str(profile.get("model") or "") or None,
        }
        
    # ── Provider gating (multi_provider_llm_20260723 Phase 3) ───────────────
    @staticmethod
    def _cfg_is_local(cfg: dict) -> bool:
        """Posture of a SPECIFIC provider-config snapshot (local Ollama or absent).

        PURE over `cfg` ONLY — it does NOT consult the runtime
        `_cloud_fallback_active` flag (F2, multi_provider_llm_20260723). The
        EFFECTIVE posture (`_cfg_is_local(cfg) or _cloud_fallback_active`) is
        computed ONCE at `_generar_dialogo` entry and threaded through the whole
        generation + dispatch, so a flag flip (or PUT) landing mid-turn can never
        tear a running generation between local/cloud. Non-generation call sites
        read the effective posture live via the `_is_local` property.
        """
        return (cfg.get("active_provider") or "local") == "local"

    def _cfg_active_profile(self, cfg: dict) -> Optional[dict]:
        """Active cloud profile dict for a SPECIFIC config snapshot, or None when local."""
        if self._cfg_is_local(cfg):
            return None
        provider = cfg.get("active_provider")
        profiles = cfg.get("profiles") or {}
        profile = profiles.get(provider)
        return profile if isinstance(profile, dict) else None

    @property
    def _is_local(self) -> bool:
        """True when local Ollama is the EFFECTIVE backend (spec B) — the active
        provider is local/absent OR an auto-fallback has engaged.

        The single gate for all local-only machinery at NON-generation call
        sites (command guards on download/switch, connector/scout/pregen gates,
        etc.), so it reads the runtime `_cloud_fallback_active` flag LIVE. A
        generation instead snapshots the EFFECTIVE posture ONCE at
        `_generar_dialogo` entry (`_cfg_is_local(cfg) or _cloud_fallback_active`)
        and threads that bool through its whole body + dispatch, so a
        `set_provider_config` swap or a flag flip racing an in-flight generation
        only takes effect on the NEXT call — the running call is pinned to its
        snapshot.
        """
        return self._cfg_is_local(self._provider_config) or self._cloud_fallback_active

    def _active_profile(self) -> Optional[dict]:
        """The active cloud profile dict (`base_url`/`model`/`preset`), or None when local."""
        return self._cfg_active_profile(self._provider_config)

    def set_provider_config(self, cfg: dict) -> None:
        """Live-swap the provider config (PUT /api/llm/provider, no restart).

        Attribute swap under `_lock`: the swap is a single atomic rebind, so a
        racing in-flight generation finishes on its original provider and only
        the next call observes the change (design 'Provider Config Surface').
        """
        if not isinstance(cfg, dict):
            return
        provider_changed = False
        with self._lock:
            # F5 (multi_provider_llm_20260723): clear the runtime fallback flag
            # ONLY when the PUT actually CHANGES active_provider — an explicit
            # provider change is the operator's intent to retry a (possibly
            # different) backend. An unrelated PUT (e.g. a pregen toggle that
            # keeps the same active_provider) must NOT silently un-fallback into
            # a still-dead cloud. Compare BEFORE the swap, normalizing absent ->
            # "local" like `_cfg_is_local`.
            previous_provider = (self._provider_config.get("active_provider") or "local")
            incoming_provider = (cfg.get("active_provider") or "local")
            self._provider_config = cfg
            if incoming_provider != "local":
                self.is_ready = True
            if incoming_provider != previous_provider:
                provider_changed = True
                self._cloud_fallback_active = False
                # Unit 2.2: a manual re-arm (this IS the documented re-arm path
                # for ambiguous_429/bad_key) clears the reason/schedule and is
                # itself a provider transition, so it bumps the epoch too.
                self._cloud_fallback_reason = None
                self._cloud_probe_next_at = None
                self.provider_epoch += 1
        if provider_changed:
            # Unit 2.2 fix (finding 3): invalidate speculative drafts FIRST,
            # immediately after the swap/flag clear above -- BEFORE the
            # potentially ~2s blocking join inside _stop_cloud_prober below.
            # Plan step 6 mandates drafts invalidated before the transition
            # completes; leaving the (up to 2s) join in front of this let a
            # consumer thread (play_prefetched_agenda / restore_frozen_stash)
            # pop a draft made under the OLD provider during that window.
            # Mirrors the invalidate-then-transition order _handle_cloud_failure
            # and _on_cloud_probe_success already use.
            #
            # F12 (runtime_findings_batch_20260731): a provider switch must
            # invalidate speculative drafts -- unconditionally, on every
            # transition, rather than tagging each stash entry with
            # provider_epoch and checking it at consumption. The contract only
            # requires that no draft survive a provider switch in either
            # direction, not that a survivor be identifiable by provider; the
            # unconditional invalidate is the simpler choice that already
            # satisfies it, so no consumption-path changes are needed.
            self._invalidate_pregen_epoch()
            self._invalidate_frozen_stash()
            # Cancel any running background return probe cleanly (outside the
            # lock above -- _stop_cloud_prober takes it again itself).
            self._stop_cloud_prober()

    def _handle_cloud_failure(
        self,
        source: str,
        *,
        failure_class: str = cloud_llm_client.CLOUD_ERROR_TRANSIENT,
        retry_after_seconds: Optional[int] = None,
    ) -> None:
        """Cloud-only failure response (spec C / design 'Cloud Failure Flow').

        Invoked from the cloud branch of a watchdog-timeout failure — the
        branch where `_recover_from_stalled_inference` (local model rollback)
        is deliberately skipped for cloud (spec B: a cloud stall must never
        roll back a local model). `fallback_mode=manual` only surfaces the
        error and keeps routing to cloud — the caller's existing `""` failure
        contract is unchanged either way. `fallback_mode=auto` (default)
        degrades THIS PROCESS to local for every SUBSEQUENT generation via the
        runtime-only `_cloud_fallback_active` flag; the persisted
        `active_provider` selector is left untouched (design 'Fallback switch
        semantics'). Warm + speak run on a background daemon thread (mirrors
        `play_prefetched_agenda`'s `speaker()` closure) so a slow local warm-up
        (~10-20s, spec C) never blocks the caller or this already-failed turn.

        Unit 2.2 (runtime_findings_batch_20260731 F3/F12): `failure_class` is
        the 1.1 taxonomy class for THIS failure, computed by the caller from
        the exception in scope — never re-derived from the possibly-stale
        `_last_cloud_failure_class` instance attribute, which a watchdog
        timeout or an empty-response return (neither carries an exception)
        would leave holding a class from an earlier, unrelated turn.
        Recorded as `_cloud_fallback_reason` and used to schedule (or
        deliberately not schedule) the background return probe.
        """
        provider_cfg = self._provider_config
        fallback_mode = provider_cfg.get("fallback_mode", "auto")
        if fallback_mode == "manual":
            self._log(
                f"Cloud LLM failure (source={source}); fallback_mode=manual, staying on cloud.",
                level="error",
            )
            self.ui_callback("cloud_llm_error")
            return

        # F12/2.2 precondition: invalidate speculative drafts BEFORE any state
        # flips, in the cloud->local direction too ("in either direction" is
        # the contract) -- a draft generated under cloud must never survive
        # into local playback. Unconditional (not provider-tagged): simpler,
        # and sufficient (see set_provider_config's identical note).
        self._invalidate_pregen_epoch()
        self._invalidate_frozen_stash()

        # Set the flag OPTIMISTICALLY so a generation racing the warm-up already
        # routes local; the worker CLEARS it again if the local backend turns out
        # to be unavailable (F3).
        with self._lock:
            self._cloud_fallback_active = True
            self._cloud_fallback_reason = failure_class
            self.provider_epoch += 1
        self._log(
            f"Cloud LLM failure (source={source}); auto-falling back to local Ollama "
            f"(reason={failure_class}).",
            level="warning",
        )
        self.ui_callback("cloud_fallback_engaged")
        self._start_cloud_prober(failure_class, retry_after_seconds)
        fallback_model = self._last_known_good_model or self.current_model

        def worker() -> None:
            # F3: _prepare_model never raises (it returns False on failure), so
            # its RESULT is the only signal that local actually warmed. On a
            # cloud-only box without Ollama it returns False — clearing the
            # optimistic flag, suppressing the false "switching to local" notice,
            # and surfacing the double-failure to the operator instead.
            try:
                warmed = self._prepare_model(fallback_model)
            except Exception:
                _eng.logger.exception("Cloud fallback: local model warm-up failed")
                warmed = False
            if not warmed:
                # Unit 2.2 fix (finding 5): this un-fallback is a provider
                # TRANSITION (effective transport flips back to cloud, since
                # _cloud_fallback_active clears) exactly like
                # set_provider_config/_on_cloud_probe_success -- it must
                # invalidate speculative drafts and bump provider_epoch the
                # same way ("in either direction" per the F12 precondition,
                # and per provider_epoch's own docstring). Invalidate BEFORE
                # flipping the flag, same order as the other two transitions,
                # so a consumer can never pop a draft made during the local
                # warm-up window under the now-stale local posture.
                self._invalidate_pregen_epoch()
                self._invalidate_frozen_stash()
                with self._lock:
                    self._cloud_fallback_active = False
                    self._cloud_fallback_reason = None
                    self._cloud_probe_next_at = None
                    self.provider_epoch += 1
                # Neither backend works -- a probe would only re-discover that;
                # the flag clearing above already sends the NEXT turn straight
                # back at cloud, which re-enters this whole method on failure.
                self._stop_cloud_prober()
                self._log(
                    f"Cloud fallback (source={source}): local warm-up failed; "
                    "neither cloud nor local is usable.",
                    level="warning",
                )
                self.ui_callback("cloud_llm_error")
                return
            try:
                # EXPLICIT priority (design §11, the audit's rider): this notice
                # inherits the FAILED TURN's source, which may be `direct`/`ptt`.
                # Letting it inherit the owner band (priority 0) would make a
                # system notice preemptive at step 3 under owner decision D3
                # (uniform priority-0 preemption). It is chat-band work.
                self._speak_or_submit(
                    _eng.i18n_active.provider_fallback_notice(), source=source, priority=1
                )
            except Exception:
                _eng.logger.exception("Cloud fallback: could not speak provider_fallback_notice")

        threading.Thread(target=worker, name="CloudFallbackWarm", daemon=True).start()

    def _stop_cloud_prober(self) -> None:
        """Unit 2.2: cancel any running background return probe.

        Best-effort, bounded join -- a probe mid network-call cannot be
        interrupted, so it is left to finish on its own daemon thread; it
        checks the stop event both before scheduling its next wait and right
        after its network call returns, and drops the result if set, so a
        late-finishing probe can never mutate state a caller here already
        moved past.

        Fix (finding 1): the thread handle, stop event and generation bump
        are ALL captured/mutated inside ONE `_lock` critical section (the
        prior code read/nulled `_cloud_prober_thread` outside the lock while
        `_start_cloud_prober` writes it inside -- a torn read/write could
        either starve a fresh prober behind a stale `already_running`, or
        have this call null out a thread a concurrent `_start_cloud_prober`
        had just assigned). The generation bump makes the stopped prober
        stale for every guarded write it might still make even if the join
        below times out; `_cloud_probe_next_at` is always cleared here too
        (finding 6) so a frozen countdown can never survive a stop.
        """
        with self._lock:
            stop_event = self._cloud_prober_stop
            thread = self._cloud_prober_thread
            self._cloud_prober_stop = None
            self._cloud_prober_generation += 1
            self._cloud_probe_next_at = None
        if stop_event is None:
            return
        stop_event.set()
        if thread is not None:
            thread.join(timeout=_eng.CLOUD_PROBER_JOIN_TIMEOUT_SECONDS)
        with self._lock:
            # Compare-and-clear: only null the handle if nothing newer (a
            # fresh _start_cloud_prober racing this join) already replaced it.
            if self._cloud_prober_thread is thread:
                self._cloud_prober_thread = None

    def _initial_cloud_probe_wait(
        self, failure_class: str, retry_after_seconds: Optional[int]
    ) -> Optional[float]:
        """Unit 2.2 per-class policy: seconds until the FIRST probe, or None
        when this class never gets an automatic probe loop.

        WU3 (cloud_rearm_20260801, owner decision D-A): `ambiguous_429` now
        ALSO gets a conservative automatic probe loop, gated by
        `CLOUD_AUTO_RETURN_AMBIGUOUS_429_ENABLED` (one-line off-switch).
        `bad_key` stays manual-only in every variant -- waiting cannot fix a
        bad credential. Either way, the manual trigger
        (`trigger_cloud_probe_now`, WU1) bypasses this table entirely."""
        if failure_class == _eng.cloud_llm_client.CLOUD_ERROR_RATE_LIMITED:
            wait = (
                retry_after_seconds
                if retry_after_seconds is not None
                else _eng.CLOUD_AUTO_RETURN_RATE_LIMIT_FLOOR_SECONDS
            )
            return float(
                min(
                    max(wait, _eng.CLOUD_AUTO_RETURN_RATE_LIMIT_FLOOR_SECONDS),
                    _eng.CLOUD_AUTO_RETURN_RATE_LIMIT_CAP_SECONDS,
                )
            )
        if failure_class == _eng.cloud_llm_client.CLOUD_ERROR_TRANSIENT:
            return float(_eng.CLOUD_AUTO_RETURN_TRANSIENT_BASE_SECONDS)
        if (
            failure_class == _eng.cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429
            and _eng.CLOUD_AUTO_RETURN_AMBIGUOUS_429_ENABLED
        ):
            return float(_eng.CLOUD_AUTO_RETURN_AMBIGUOUS_429_BASE_SECONDS)
        return None

    def _next_cloud_probe_wait(self, failure_class: str, previous_wait: float) -> float:
        """Exponential backoff on a FAILED probe, capped per class."""
        if failure_class == _eng.cloud_llm_client.CLOUD_ERROR_RATE_LIMITED:
            cap = _eng.CLOUD_AUTO_RETURN_RATE_LIMIT_CAP_SECONDS
        elif failure_class == _eng.cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429:
            # WU3: its own cap, independent of transient's (reuses the
            # rate-limit ceiling by design -- see settings.py).
            cap = _eng.CLOUD_AUTO_RETURN_AMBIGUOUS_429_CAP_SECONDS
        else:
            cap = _eng.CLOUD_AUTO_RETURN_TRANSIENT_CAP_SECONDS
        return float(min(previous_wait * 2, cap))

    def _cloud_probe_max_attempts(self, failure_class: str) -> Optional[int]:
        """WU3 (cloud_rearm_20260801): probe-count ceiling for a class's
        background loop. None means unbounded (rate_limited/transient keep
        retrying forever, unchanged). Only ambiguous_429's conservative
        auto-return (owner decision D-A) is bounded, and only while the
        flag is on -- when off, `_initial_cloud_probe_wait` never arms it
        in the first place, so this is never consulted for that case."""
        if (
            failure_class == _eng.cloud_llm_client.CLOUD_ERROR_AMBIGUOUS_429
            and _eng.CLOUD_AUTO_RETURN_AMBIGUOUS_429_ENABLED
        ):
            return _eng.CLOUD_AUTO_RETURN_AMBIGUOUS_429_MAX_ATTEMPTS
        return None

    def _notify_cloud_probe_gave_up(self) -> None:
        """WU3: the background probe loop exhausted its attempts budget
        without a successful probe. No detail payload -- privacy gate, same
        as cloud_fallback_engaged/cloud_restored (see
        engine_host._MOTOR_EVENT_WHITELIST)."""
        self.ui_callback("cloud_probe_gave_up")

    def _notify_cloud_probe_scheduled(self, wait: float) -> None:
        hook = getattr(self, "on_cloud_probe_scheduled", None)
        if hook is not None:
            try:
                hook({"seconds": wait})
            except Exception:
                _eng.logger.exception("on_cloud_probe_scheduled callback failed")
        self.ui_callback("cloud_probe_scheduled")

    def _cloud_probe_once(self, provider_cfg: dict) -> bool:
        """Unit 2.2: single bounded probe through the SAME cloud client/config
        real turns use. Static minimal message only -- NO history, NO
        persona, NO user content (privacy contract) -- and `num_predict=1`
        (mapped to `max_tokens`) to minimize cost. Success = a well-formed
        response; any exception (timeout, non-2xx, malformed body) is a
        failure, exactly like a real turn's transport-error classification."""
        try:
            self._cloud_chat(
                provider_cfg=provider_cfg,
                messages=[{"role": "user", "content": "ping"}],
                options={"num_predict": 1},
                is_local=False,
            )
            return True
        except Exception:
            return False

    def _on_cloud_probe_success(self, generation: int) -> None:
        """Unit 2.2 RETURN sequence, in the mandated order: (a) invalidate
        speculative drafts FIRST, (b) bump provider_epoch, (c) clear the
        fallback flag/reason under the lock, (d) narrate. History and
        persistent memoria are untouched by design -- continuity across the
        switch is the point (F15).

        Fix (finding 1/2): `generation` is the caller prober's OWN generation,
        captured when it was started. Checked under `_lock` before doing
        anything, and again right before the state flip, so a superseded
        (stopped/replaced) prober whose network call was already in flight
        can never resurrect the fallback flag or clobber a newer failure
        under a manual provider choice.
        """
        with self._lock:
            if generation != self._cloud_prober_generation:
                return
        self._invalidate_pregen_epoch()
        self._invalidate_frozen_stash()
        with self._lock:
            if generation != self._cloud_prober_generation:
                return
            self.provider_epoch += 1
            self._cloud_fallback_active = False
            self._cloud_fallback_reason = None
            self._cloud_probe_next_at = None
            self._cloud_prober_stop = None
        self._log("Cloud restored; returning from local fallback.", level="info")
        self.ui_callback("cloud_restored")

    def _run_cloud_prober(
        self,
        stop_event: threading.Event,
        failure_class: str,
        wait: float,
        generation: int,
        attempts_left: Optional[int] = None,
    ) -> None:
        """Unit 2.2: the background probe loop. Runs on its OWN daemon
        thread (started by `_start_cloud_prober`), NEVER inside a
        user-facing turn. `stop_event.wait` (not `time.sleep`) so a manual
        provider PUT (`_stop_cloud_prober`) interrupts the wait immediately
        instead of only being noticed on the next tick.

        Fix (findings 1/2/4/6): `generation` is THIS prober's own id,
        captured by `_start_cloud_prober` at creation. Every state-mutating
        write below re-checks `generation == self._cloud_prober_generation`
        under `_lock` first (alongside the pre-existing stop_event check) --
        a superseded prober (stopped outright, or replaced by a fresh
        failure's class/schedule) keeps running harmlessly to completion
        but can never reschedule a countdown or trigger a return once stale.

        WU3 (cloud_rearm_20260801): `attempts_left` is None for every
        caller except a bounded class (ambiguous_429, when its flag is on)
        -- None means unbounded, so rate_limited/transient never give up,
        exactly as before this unit. When bounded, a failed probe consumes
        one attempt; reaching zero gives up instead of rescheduling, via
        `_notify_cloud_probe_gave_up`, leaving `_cloud_fallback_reason`
        untouched (still local) but `_cloud_probe_next_at` cleared -- the
        manual trigger (WU1) remains the door back in.
        """
        while True:
            if stop_event.wait(timeout=wait):
                return  # stop requested during the wait
            with self._lock:
                if stop_event.is_set() or generation != self._cloud_prober_generation:
                    return
                provider_cfg = self._provider_config
            ok = self._cloud_probe_once(provider_cfg)
            if stop_event.is_set():
                return  # superseded by a manual re-arm while probing
            if ok:
                self._on_cloud_probe_success(generation)
                return
            if attempts_left is not None:
                attempts_left -= 1
                if attempts_left <= 0:
                    with self._lock:
                        if stop_event.is_set() or generation != self._cloud_prober_generation:
                            return
                        self._cloud_probe_next_at = None
                        self._cloud_prober_stop = None
                    self._log(f"Cloud probe gave up: clase={failure_class}", level="warning")
                    self._notify_cloud_probe_gave_up()
                    return
            if wait == 0:
                # Post-WU3 fix: a manual trigger (WU1) arms with wait=0 for
                # "probe immediately". Doubling zero is a fixed point (0*2
                # == 0), which would otherwise re-probe in a tight loop
                # forever. Hand off to the class's own auto-return cadence
                # instead. Only reached for a class WITH an auto policy --
                # a no-policy class (bad_key always; ambiguous_429 flag-off)
                # was already exhausted by the one-shot attempts budget
                # `_arm_cloud_prober` gives it, above.
                wait = self._initial_cloud_probe_wait(failure_class, None)
            else:
                wait = self._next_cloud_probe_wait(failure_class, wait)
            with self._lock:
                if stop_event.is_set() or generation != self._cloud_prober_generation:
                    return
                self._cloud_probe_next_at = time.monotonic() + wait
            self._notify_cloud_probe_scheduled(wait)

    def _arm_cloud_prober(self, failure_class: str, wait: float) -> None:
        """WU1 (cloud_rearm_20260801): the stop-old/generation-bump/
        thread-start tail extracted from `_start_cloud_prober` (unchanged
        code, just named) so `trigger_cloud_probe_now` can reuse the exact
        same thread-safety choreography for an immediate manual probe
        without a second prober flavor to keep in sync. Never called with
        `wait=None` -- the caller decides whether this class/situation gets
        a probe at all.

        `_stop_cloud_prober` makes this safe even though its join is
        bounded and best-effort: it bumps `_cloud_prober_generation` before
        returning, so the old prober -- even if its join times out and it
        keeps running -- becomes a harmless zombie the moment this method's
        own generation bump below runs; none of its writes can land.

        WU3: `attempts_left` is computed HERE from `failure_class` alone,
        so it is uniform for both callers -- an automatic arm
        (`_start_cloud_prober`) and a manual one (`trigger_cloud_probe_now`)
        get the identical (fresh) attempts budget for the class, never a
        stale leftover count from a prior give-up.

        Post-WU3 fix: a class with NO automatic policy at all (bad_key
        always; ambiguous_429 when its flag is off) gets a ONE-SHOT budget
        here. Without this, a manual trigger's wait=0 on a failed probe
        would loop forever in `_run_cloud_prober` (0*2 is a fixed point) --
        this class was never meant to retry unattended, so one attempt then
        `cloud_probe_gave_up` (manual re-trigger still works after) is the
        correct manual-probe contract, not silent infinite spin.
        """
        self._stop_cloud_prober()
        attempts_left = self._cloud_probe_max_attempts(failure_class)
        if attempts_left is None and self._initial_cloud_probe_wait(failure_class, None) is None:
            attempts_left = 1
        with self._lock:
            self._cloud_prober_generation += 1
            generation = self._cloud_prober_generation
            stop_event = threading.Event()
            thread = threading.Thread(
                target=self._run_cloud_prober,
                args=(stop_event, failure_class, wait, generation, attempts_left),
                name="CloudProber",
                daemon=True,
            )
            self._cloud_prober_stop = stop_event
            self._cloud_prober_thread = thread
            self._cloud_probe_next_at = time.monotonic() + wait
        self._log(
            f"Cloud probe armed: clase={failure_class} wait={int(wait)}s "
            f"attempts_left={'unbounded' if attempts_left is None else attempts_left}",
            level="info",
        )
        self._notify_cloud_probe_scheduled(wait)
        thread.start()

    def _start_cloud_prober(
        self, failure_class: str, retry_after_seconds: Optional[int]
    ) -> None:
        """Unit 2.2: reconcile the background return probe with a NEW
        failure (fix for findings 2/4). No-op (after stopping any live
        prober) for ambiguous_429/bad_key -- no automatic probe loop, manual
        re-arm only. For a probeable class, UNCONDITIONALLY replaces
        whatever prober is already running with a fresh one for the new
        class/wait: a second failure must never be silently swallowed by
        the old `already_running` single-flight gate, which let a
        non-probeable second failure leave a live prober able to auto-return
        under a reason it forbids (finding 2), and let a probeable second
        failure's own class/schedule (e.g. `rate_limited`'s Retry-After) be
        discarded in favour of the stale one (finding 4).

        WU1 (cloud_rearm_20260801): thin wrapper now -- this method only
        decides WHETHER to arm (per-class policy via
        `_initial_cloud_probe_wait`); the stop/generation-bump/thread
        choreography lives in `_arm_cloud_prober`, shared with the manual
        `trigger_cloud_probe_now` path.
        """
        wait = self._initial_cloud_probe_wait(failure_class, retry_after_seconds)
        if wait is None:
            self._stop_cloud_prober()
            return
        self._arm_cloud_prober(failure_class, wait)

    def trigger_cloud_probe_now(self) -> dict:
        """WU1 (cloud_rearm_20260801): manual probe trigger -- arms an
        immediate probe (wait~=0) bypassing `_initial_cloud_probe_wait`'s
        per-class table, so `ambiguous_429`/`bad_key` (manual-re-arm-only
        classes) can also be probed on demand. Backs
        `POST /api/llm/provider/probe` (WU2); synchronous and never touches
        `command_queue`/the dispatcher, so it is usable mid-agenda,
        side-stepping the locked model selector.

        No-op (200-style, not an error) when there is nothing to probe:
        `not_in_fallback` (nothing to return from) or `no_cloud_profile` (no
        cloud profile configured to probe against). Otherwise collapses any
        already-scheduled probe to now via the same `_arm_cloud_prober` tail
        `_start_cloud_prober` uses, so success runs through the untouched
        `_on_cloud_probe_success` -- never a second prober flavor.
        """
        with self._lock:
            in_fallback = self._cloud_fallback_active
            provider_cfg = self._provider_config
            failure_class = self._cloud_fallback_reason
        if not in_fallback:
            return {"armed": False, "reason": "not_in_fallback"}
        if self._cfg_active_profile(provider_cfg) is None:
            return {"armed": False, "reason": "no_cloud_profile"}
        self._arm_cloud_prober(failure_class, 0.0)
        return {"armed": True, "reason": None}

    def _cloud_api_key(self, profile_id) -> str:
        """Read the per-profile cloud key from the OAuthStore (LLM_KEYS_FILE)."""
        token = _eng.OAuthStore(_eng.LLM_KEYS_FILE).load(profile_id)
        if isinstance(token, dict):
            return str(token.get("api_key") or "")
        return ""

    def _cloud_chat(self, *, provider_cfg=None, model=None, messages, options=None, is_local=None, **_ignored):
        """Dispatch a chat request to the OpenAI-compatible cloud client.

        Resolves profile + provider_id + key from a SINGLE provider-config
        snapshot (`provider_cfg`, threaded from `_generar_dialogo`'s entry) so a
        mid-generation `set_provider_config` swap can never pair provider A's
        base_url/model with provider B's key (F2). Falls back to live config for
        stand-alone (non-generation) callers. Uses the ACTIVE profile's
        `base_url`/`model`/key — never the local `current_model` the call site
        passes. Ollama-only kwargs (`keep_alive`) are ignored here.
        """
        cfg = provider_cfg if provider_cfg is not None else self._provider_config
        profile = self._cfg_active_profile(cfg)
        if profile is None:
            # F7: degrade like every other cloud failure. A RequestException
            # subclass is caught by the transport-error contract and returns ''
            # instead of propagating out through the noisy outer catch-all.
            raise _eng.cloud_llm_client.CloudLLMResponseError(
                "cloud provider active but no profile configured"
            )
        provider_id = cfg.get("active_provider")
        return _eng.cloud_llm_client.send_chat_completion(
            base_url=str(profile.get("base_url") or ""),
            api_key=self._cloud_api_key(provider_id),
            model=str(profile.get("model") or ""),
            messages=messages,
            options=options or {},
            timeout=self._resolve_chat_watchdog_timeout(model, provider_cfg=cfg, is_local=is_local),
        )