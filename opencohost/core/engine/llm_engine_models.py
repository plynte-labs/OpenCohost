import concurrent.futures  # not bare `concurrent`: the submodule attribute only
                           # exists because llm_engine.py:15 imports it, so a bare
                           # import works today by side effect and AttributeErrors
                           # the day that line goes away.
import httpx
import requests
import socket
import threading
import time
import uuid
from typing import Optional

from opencohost.config.settings import (
    LLM_SCOUT_TIMEOUT,
    STREAM_IDLE_PROBE_SECONDS,
    STREAM_IDLE_TIMEOUT_SECONDS,
)
from opencohost.core.providers.llm_tiers import LLMTierConfig

# These three are imported directly, not via `_eng`, because they appear where the
# class body evaluates: `LLMTierConfig`/`Optional` in annotations, LLM_SCOUT_TIMEOUT
# as a default arg on `_create_ollama_scout_client`. `_eng.X` there resolves only
# for names bound above llm_engine's mixin-import line. Nothing patches any of
# them, and the default arg was ALREADY frozen at class-definition time before the
# split, so this is behaviour-identical -- see llm_engine_memorias.py.
# The two STREAM_* budgets are read inside a method body, so `_eng.X` would be
# timing-safe -- but llm_engine.py does not re-export them, so it would also be an
# AttributeError. They are read live from this module's globals on every call, so
# a test (or a future settings reload) can rebind them here.
from opencohost.core import llm_engine as _eng

class ModelManagementMixin:
    def _check_ollama_service(self, *, notify_unavailable: bool = True):
        if not self._is_local:
            # Cloud provider active: no local Ollama service to probe or warm.
            # Cloud reachability is the health monitor's concern (Phase 5).
            self.is_ready = True
            return True
        try:
            self.ollama.list()
        except Exception as e:
            self.is_ready = False
            if notify_unavailable:
                self._log(f"Ollama no esta disponible: {e}", level="warning")
                self.ui_callback("ollama_unavailable")
            return False

        self.is_ready = True
        self._log("Ollama disponible. Preparando modelo...")
        if self._pending_model_switch and self._pending_model_switch != self.current_model:
            self._log(f"Aplicando modelo pendiente: {self._pending_model_switch}")
            self._apply_model_switch(self._pending_model_switch)
        else:
            self._prepare_model(self.current_model)
            if self._pending_model_switch == self.current_model:
                self._pending_model_switch = None
                self._pending_switch_not_ready_logged = False
        self._log("Motor IA listo.")
        return True

    def _switch_and_prepare_model(self, new_model: str) -> None:
        previous_model = self.current_model
        # D1 (model_switch_memory_continuity_20260723): no historial.clear()
        # here — owner-chosen SEAMLESS continuity, conversation carries over
        # verbatim across a model switch.

        if not self._prepare_model(new_model):
            raise RuntimeError("target_model_unavailable")

        if self._is_local and previous_model != new_model:
            self._log(f"Liberando memoria del modelo: {previous_model}...")
            try:
                self.ollama.generate(model=previous_model, prompt='', keep_alive=0)
            except Exception as e:
                _eng.logger.warning(f"No se pudo liberar modelo {previous_model}: {e}")

        self.current_model = new_model
        self._awaiting_first_success_after_switch = previous_model != new_model
        self._log(f"🔄 Modelo cambiado a: {new_model}")

    @property
    def active_llm_tier(self) -> str:
        return self.llm_tiers.active_tier

    @active_llm_tier.setter
    def active_llm_tier(self, tier: str) -> None:
        if self.llm_tiers.config.is_available(tier):
            self.llm_tiers.active_tier = tier

    def configure_llm_tiers(
        self,
        config: LLMTierConfig,
        active_tier: Optional[str] = None,
    ) -> None:
        """Replace manual tier slots without changing prompt or conversation memory."""
        tier = active_tier or self._infer_active_tier(self.current_model, config)
        self.llm_tiers = _eng.LLMTierState(config=config, active_tier=tier)

    @staticmethod
    def _infer_active_tier(model: str, config: LLMTierConfig) -> str:
        for tier, tier_model in config.as_dict().items():
            if tier_model == model:
                return tier
        return config.first_available_tier() or "balanced"

    def switch_llm_tier(self, target_tier: str) -> bool:
        """Manually switch active LLM tier for future requests.

        The previous active tier/model is restored if the target slot is empty or
        model preparation fails. Conversation history and profile prompt are not
        changed by tier switching.
        """
        previous_tier = self.llm_tiers.active_tier
        previous_model = self.current_model
        previous_loaded_model = self._loaded_model
        previous_warmed_model = self._warmed_model
        previous_owns_ollama_model = self._owns_ollama_model
        target_model = self.llm_tiers.config.model_for(target_tier)

        if (
            target_model is not None
            and target_tier == previous_tier
            and self._is_model_switch_noop(target_model)
        ):
            return True

        if target_model is None:
            self._last_switch_failure = {
                "requested_tier": target_tier,
                "requested": None,
                "current_tier": previous_tier,
                "current": previous_model,
                "reason": "tier_unavailable",
            }
            self._log(
                f"Tier LLM '{target_tier}' no disponible; "
                f"se mantiene {previous_tier} ({previous_model}).",
                level="warning",
            )
            self.ui_callback("llm_tier_switch_failed")
            return False

        try:
            if self.is_ready and not self._prepare_model(target_model):
                raise RuntimeError("target_model_unavailable")
            self.llm_tiers.switch_to(target_tier)
            self.current_model = target_model
            self._desired_model = target_model
            self._awaiting_first_success_after_switch = previous_model != target_model
            self._loaded_model = (
                target_model
                if self._warmed_model == target_model
                else self._loaded_model
            )
            if self._is_local and previous_model != target_model:
                try:
                    self.ollama.generate(model=previous_model, prompt='', keep_alive=0)
                except Exception as e:
                    _eng.logger.warning(f"No se pudo liberar modelo {previous_model}: {e}")
            _eng.save_last_model(target_model, source="llm_tier_switch")
        except Exception as e:
            self.llm_tiers.active_tier = previous_tier
            self.current_model = previous_model
            self._desired_model = previous_model
            self._loaded_model = previous_loaded_model
            self._warmed_model = previous_warmed_model
            self._owns_ollama_model = previous_owns_ollama_model
            self._last_switch_failure = {
                "requested_tier": target_tier,
                "requested": target_model,
                "current_tier": previous_tier,
                "current": previous_model,
                "reason": f"switch_error: {e}",
            }
            self._log(
                f"Tier LLM {previous_tier} -> {target_tier} ({target_model}) fallido: {e}. "
                f"Se mantiene {previous_model}.",
                level="error",
            )
            self.ui_callback("llm_tier_switch_failed")
            return False

        label = _eng.LLM_TIER_LABELS.get(target_tier, target_tier)
        self._last_switch_failure = None
        self._log(f"LLM tier changed: {previous_tier} -> {target_tier} ({target_model})")
        _eng.logger.info("Manual LLM tier changed to %s (%s)", label, target_model)
        self.ui_callback("llm_tier_switch_applied")
        return True

    def _check_pending_model_switch(self) -> None:
        """Attempt pending model switch if conditions are met (non-blocking)."""
        if self._pending_model_switch is None:
            return
        if time.monotonic() < self._pending_switch_next_at:
            return  # not time yet
        if self._processing or self._speech_active:
            return  # still busy

        model = self._pending_model_switch

        if self._is_model_switch_noop(model):
            self._pending_model_switch = None
            self._pending_switch_not_ready_logged = False
            return

        if not self.is_ready:
            self._pending_switch_next_at = time.monotonic() + 2.0
            if self._check_ollama_service(notify_unavailable=False):
                return
            if not self._pending_switch_not_ready_logged:
                self._log(f"Switch a {model} sigue pendiente: Ollama no está listo.", level="debug")
                self._pending_switch_not_ready_logged = True
            return

        self._apply_model_switch(model)

    def _is_model_switch_noop(self, new_model: str) -> bool:
        """Return True when the requested model is already the effective target."""
        return (
            bool(new_model)
            and new_model == self.current_model
            and new_model == self._desired_model
            and (
                not self.is_ready
                or self._warmed_model == new_model
                or self._loaded_model == new_model
            )
        )

    def _apply_model_switch(self, new_model: str, *, persist_source: str = "user_switch") -> bool:
        """Execute model switch and persist on success."""
        if self._is_model_switch_noop(new_model):
            if self._pending_model_switch == new_model:
                self._pending_model_switch = None
                self._pending_switch_not_ready_logged = False
            return True

        previous_model = self.current_model
        self._pending_model_switch = None
        self._pending_switch_not_ready_logged = False
        try:
            self._switch_and_prepare_model(new_model)
            self._desired_model = new_model
            _eng.save_last_model(new_model, source=persist_source)
            self.ui_callback("model_switch_applied")
            return True
        except Exception as e:
            self._log(f"Switch a {new_model} fallido: {e}", level="error")
            self.current_model = previous_model
            self._desired_model = previous_model
            self._last_switch_failure = {
                "requested": new_model,
                "current": self.current_model,
                "reason": f"switch_error: {e}",
            }
            self.ui_callback("model_switch_failed")
            return False

    def _prepare_model(self, model: str) -> bool:
        """Warm the selected Ollama model so first real response is not a cold load."""
        if not self._is_local:
            # Cloud provider active: no local model to warm. Report "ready" so
            # switch/tier machinery stays non-crashing; the cloud request path
            # never depends on a warmed local model.
            return True
        if not self.is_ready or not model or self._warmed_model == model:
            return self._warmed_model == model

        self.ui_callback("model_warming")
        self._log(f"Preparando modelo {model} en memoria...")
        start = time.time()
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    self.ollama.generate,
                    model=model,
                    prompt="Responde solo: ok",
                    keep_alive=_eng.LLM_KEEP_ALIVE,
                    options={"num_predict": 1, "temperature": 0},
                )
                future.result(timeout=120)
        except Exception as e:
            self._log(f"No se pudo preparar modelo {model}: {e}", level="warning")
            _eng.logger.warning("No se pudo preparar modelo %s: %s", model, e)
            self._loaded_model = None
            self._owns_ollama_model = False
            self.ui_callback("ready")
            return False

        elapsed = time.time() - start
        self._warmed_model = model
        self._loaded_model = model
        self._owns_ollama_model = True
        self.ui_callback("ready")
        self._log(f"Modelo {model} preparado en {elapsed:.2f}s. Primera respuesta ya no deberia cargar en frio.")
        return True

    def release_owned_ollama_model(self, timeout: float = 2.0) -> bool:
        """Best-effort unload for the model warmed by this OpenCohost session."""
        model = self._loaded_model or self._warmed_model
        if not model or not self._owns_ollama_model or not hasattr(self, "ollama"):
            _eng.logger.info("Ollama model release skipped; no OpenCohost-owned model recorded")
            return False

        result = {"released": False}

        def unload() -> None:
            try:
                self.ollama.generate(model=model, prompt="", keep_alive=0)
                result["released"] = True
            except Exception as exc:
                _eng.logger.warning("No se pudo liberar modelo Ollama %s: %s", model, exc)

        thread = threading.Thread(target=unload, name="OllamaModelRelease", daemon=True)
        thread.start()
        thread.join(timeout=timeout)
        if thread.is_alive():
            _eng.logger.warning("Timeout liberando modelo Ollama %s; cierre continua", model)
            return False
        if result["released"]:
            self._loaded_model = None
            self._warmed_model = None
            self._owns_ollama_model = False
            _eng.logger.info("Modelo Ollama %s liberado", model)
        return result["released"]

    def _download_model_worker(self, model_tag):
        if not self._is_local:
            # F6: cloud provider active — no local ollama.pull. Surface the
            # refusal so the operator gets feedback (download_error, the same
            # signal the failure path emits) instead of a silent no-op.
            self._log(
                "Descarga de modelos no disponible con un proveedor cloud activo.",
                level="warning",
            )
            self.ui_callback("download_error")
            return
        self._downloading = True
        self.ui_callback("download_start")
        self._log(f"📥 Descargando modelo '{model_tag}'... Esto puede tardar varios minutos.")

        try:
            last_pct = -1
            for progress in self.ollama.pull(model_tag, stream=True):
                status = getattr(progress, 'status', str(progress))
                total = getattr(progress, 'total', None)
                completed = getattr(progress, 'completed', None)

                if total is not None and completed is not None and total > 0:
                    pct = int((completed / total) * 100)
                    if pct >= last_pct + 10:
                        last_pct = pct
                        size_mb = completed / (1024 * 1024)
                        total_mb = total / (1024 * 1024)
                        self.log_queue.put(
                            f"[Descarga] {model_tag}: {pct}% ({size_mb:.0f}/{total_mb:.0f} MB) - {status}"
                        )
                elif 'success' in str(status).lower():
                    self._log(f"✅ Modelo '{model_tag}' descargado exitosamente.")
                else:
                    status_str = str(status)
                    if status_str and status_str != last_pct:
                        self.log_queue.put(f"[Descarga] {model_tag}: {status_str}")

            # D2 (model_switch_memory_continuity_20260723): no historial.clear()
            # here either — same "model changed mid-session" event family as
            # switch_model, same seamless-continuity contract.
            self.current_model = model_tag
            self._desired_model = model_tag
            self._loaded_model = model_tag
            _eng.save_last_model(model_tag, source="download")
            self._log(f"🔄 Modelo activo cambiado a: {model_tag}")
            self.ui_callback("download_done")

        except Exception as e:
            self._log(f"ERROR descargando '{model_tag}': {e}", level="error")
            _eng.logger.exception(f"Error descargando modelo {model_tag}")
            self.ui_callback("download_error")
        finally:
            self._downloading = False
            
    def _create_ollama_chat_client(self, ollama_module, timeout: float = None):
        """Build (or reuse) the chat client whose HTTP timeout is *timeout*.

        Memoized by timeout value in ``self._ollama_chat_clients`` -- never
        builds a fresh ``ollama.Client`` for a timeout already seen. In
        practice there are only two distinct callers: the steady-state
        default (``OLLAMA_CHAT_TIMEOUT``, 180s) built here with no argument,
        and the post-switch watchdog budget (45s) pre-warmed once in
        ``run()``. Root cause fix (heavy_model_inference_recovery follow-up):
        a chat client pinned at 180s regardless of a 45s watchdog budget kept
        Ollama's single runner busy well past the watchdog giving up, so a
        rollback right after could not get a runner. See
        ``_resolve_chat_watchdog_timeout`` / ``_ollama_chat``.
        """
        if timeout is None:
            timeout = _eng.OLLAMA_CHAT_TIMEOUT
        cache = getattr(self, "_ollama_chat_clients", None)
        if cache is None:
            cache = {}
            self._ollama_chat_clients = cache
        cached = cache.get(timeout)
        if cached is not None:
            return cached
        client_factory = getattr(ollama_module, "Client", None)
        if client_factory is None:
            return None
        try:
            client = client_factory(timeout=timeout)
        except TypeError as e:
            self._log(f"Ollama Client no soporta timeout de chat; usando cliente por defecto: {e}", level="warning")
            return None
        cache[timeout] = client
        return client

    def _create_ollama_scout_client(self, ollama_module, *, timeout: float = LLM_SCOUT_TIMEOUT):
        """Dedicated short-timeout client for auxiliary, non-persona generations.

        Built with ``LLM_SCOUT_TIMEOUT`` (not the 180s chat timeout) so that when
        the HTTP timeout expires the socket closes and Ollama cancels the
        generation, releasing the single runner slot in ~LLM_SCOUT_TIMEOUT. That
        socket-level cancel is the ONLY real abort path: the watchdog's worker is
        a plain daemon thread with no cancellation, so without an HTTP timeout a
        timed-out call keeps the single runner busy.

        *timeout* (memory_promotion_20260725) generalises it for the draft-
        promotion judge, whose budget is adaptive (``_judge_timeout_seconds``).
        The default keeps the Topic Scout's caller byte-identical.
        """
        client_factory = getattr(ollama_module, "Client", None)
        if client_factory is None:
            return None
        try:
            return client_factory(timeout=timeout)
        except TypeError as e:
            self._log(f"Ollama Client no soporta timeout de scout; usando cliente por defecto: {e}", level="warning")
            return None

    def _ollama_chat(self, *, provider_cfg=None, is_local=None, chat_timeout=None, **kwargs):
        cfg = provider_cfg if provider_cfg is not None else self._provider_config
        # F2: honor the EFFECTIVE posture threaded from `_generar_dialogo`'s
        # entry snapshot; only stand-alone callers (is_local=None) re-derive it
        # live (config OR active fallback).
        if is_local is None:
            is_local = self._cfg_is_local(cfg) or self._cloud_fallback_active
        if not is_local:
            return self._cloud_chat(provider_cfg=cfg, is_local=is_local, **kwargs)
        client = self._select_ollama_chat_client(chat_timeout)
        return client.chat(**kwargs)

    def _select_ollama_chat_client(self, chat_timeout):
        """Pick the client memoized for *chat_timeout* (the resolved watchdog
        budget this call is running under), falling back to the steady-state
        default client / raw ``self.ollama`` when none was pre-built for it.

        Deliberately a pure lookup -- never builds a client here. Building
        happens only in ``_create_ollama_chat_client`` (called from ``run()``),
        so a test double standing in for ``self.ollama`` (a plain MagicMock,
        not the real module) is never silently routed through an unrelated
        auto-mocked ``self.ollama.Client()``.
        """
        cache = getattr(self, "_ollama_chat_clients", None)
        client = cache.get(chat_timeout) if cache and chat_timeout is not None else None
        return client or self._ollama_chat_client or self.ollama

    def _ollama_scout_chat(self, *, provider_cfg=None, is_local=None, **kwargs):
        cfg = provider_cfg if provider_cfg is not None else self._provider_config
        if is_local is None:
            is_local = self._cfg_is_local(cfg) or self._cloud_fallback_active
        if not is_local:
            return self._cloud_chat(provider_cfg=cfg, is_local=is_local, **kwargs)
        client = self._ollama_scout_client or self.ollama
        return client.chat(**kwargs)

    def _ollama_judge_chat(self, *, provider_cfg=None, is_local=None, **kwargs):
        """Transport for the memoria promotion judge — ALWAYS local.

        Owner decision 2026-08-08 (F16): memoria draft content stays on this
        machine; the judge is the only place in the memoria pipeline that could
        send it over the network, so — unlike ``_ollama_scout_chat`` and
        ``_ollama_chat`` — this NEVER falls through to ``_cloud_chat``,
        regardless of the active provider or ``_cloud_fallback_active``. It
        always uses the dedicated short-timeout client built with the adaptive
        judge budget, so a timed-out judgment actually releases the single
        Ollama runner instead of leaving it generating into a closed watchdog.
        ``provider_cfg``/``is_local`` are accepted only to keep the same call
        signature as the other ``_ollama_*_chat`` transports; both are unused.
        """
        client = self._ollama_judge_client or self.ollama
        return client.chat(**kwargs)

    def _call_with_watchdog(self, call, *, timeout: float, label: str = "OllamaChatWatchdog", **kwargs):
        """Run *call* on a daemon thread and stop waiting on it after *timeout*.

        Deliberately separate from ``_ollama_chat_with_watchdog``: the chat
        transport gets swapped — by the cloud fallback and by tests — and
        swapping it must NOT also swap the ``ollama.show`` metadata probe, which
        is a different call with a different budget. Sharing one seam for both
        made the probe return chat-shaped responses.

        Streaming calls are refused outright. This watchdog measures "did
        ``call(**kwargs)`` return within budget", and calling a function that
        returns a generator returns WITHOUT executing its body -- the wait
        succeeds in microseconds and the watchdog blesses an un-started
        generator. The stall recovery validated on 2026-06-17 and again on
        2026-08-13 would become a guaranteed false success, silently. Streaming
        has its own seam that iterates on the calling thread; routing it here
        instead must fail loudly at the first call, not degrade the watchdog.
        """
        if kwargs.get("stream"):
            raise ValueError(
                "_call_with_watchdog cannot supervise stream=True: a generator-returning "
                "call returns before its body runs, so the watchdog would always succeed. "
                "Use the dedicated streaming seam, which iterates on the calling thread."
            )

        result = {}
        done = threading.Event()

        def worker() -> None:
            try:
                result["response"] = call(**kwargs)
            except httpx.TimeoutException:
                # The chat client's HTTP timeout can now fire at ~the same
                # moment as the watchdog (both are derived from the same
                # budget). Translate it into the SAME contract the pure
                # wait-based timeout below raises, so `_is_watchdog_timeout_error`
                # still recognizes it and automatic recovery still fires --
                # otherwise this raw transport exception would propagate
                # instead and silently kill the whole recovery path.
                result["error"] = TimeoutError(f"watchdog_timeout:{timeout:.2f}s")
            except Exception as exc:
                result["error"] = exc
            finally:
                done.set()

        thread = threading.Thread(
            target=worker,
            name=f"{label}-{uuid.uuid4().hex[:8]}",
            daemon=True,
        )
        thread.start()
        if not done.wait(timeout=max(0.1, float(timeout))):
            raise TimeoutError(f"watchdog_timeout:{timeout:.2f}s")
        if "error" in result:
            raise result["error"]
        return result.get("response")

    def _ollama_chat_with_watchdog(self, *, timeout: float, chat_callable=None, **kwargs):
        # Thread the resolved watchdog budget into `_ollama_chat`'s own client
        # selection (as `chat_timeout`), but ONLY for the default transport --
        # an explicit `chat_callable` (scout/judge) already carries its own
        # dedicated timeout-scoped client and must stay byte-identical.
        call = chat_callable or (lambda **kw: self._ollama_chat(chat_timeout=timeout, **kw))
        return self._call_with_watchdog(call, timeout=timeout, **kwargs)

    def _ollama_chat_streaming(self, *, timeout: float, **kwargs):
        """Yield raw chunks from a local ``ollama.chat(stream=True)``, bounded.

        The streaming counterpart of ``_ollama_chat_with_watchdog``, and
        deliberately NOT routed through ``_call_with_watchdog`` -- which refuses
        ``stream=True`` outright, because that watchdog measures "did the call
        return within budget" and a generator-returning call returns WITHOUT
        executing its body, so it would bless an un-started generator. This seam
        iterates on the CALLING thread instead: no daemon thread exists to
        orphan, and every abort is an exception or return in the consumer's own
        frame.

        Two budgets, both raising the SAME
        ``TimeoutError("watchdog_timeout:<budget>s")`` string the buffered path
        raises, so ``_is_watchdog_timeout_error`` still recognises them, they
        land in the attempt loop's existing handler unchanged, and automatic
        recovery (``_recover_from_stalled_inference``) still fires:

        1. *Idle / per-read* -- ``STREAM_IDLE_TIMEOUT_SECONDS``, enforced by the
           transport itself. Under ``stream=True`` the httpx timeout is a
           per-read timeout and every chunk is one read (the first included), so
           riding the client memoized at that value IS the idle watchdog. Client
           construction is left entirely to ``_create_ollama_chat_client`` /
           ``_select_ollama_chat_client`` (``a8830bb``, production-validated on
           2026-08-13); the httpx -> ``watchdog_timeout:`` translation is
           replicated from ``_call_with_watchdog``'s worker because streaming
           bypasses that worker.
        2. *Total wall clock* -- ``timeout``, the budget
           ``_resolve_chat_watchdog_timeout`` already resolves (45s post-switch,
           180s steady, 75s cloud), re-checked per chunk. This is the hard
           ceiling on runaway generation, which matters precisely because
           ``num_predict`` is popped for reasoning-classified models like
           gemma4: an idle-only watchdog would let an infinite decode run
           forever.

        On a streamed post-switch turn the 40s idle budget fires 5s BEFORE the
        45s total cap -- earlier detection on the same path, never later.

        ``finally: stream.close()`` on EVERY exit path -- normal completion,
        either timeout, a consumer that abandons the generator (guard trip,
        cancel token, any raise downstream). Closing the socket is the ONLY real
        server-side abort for Ollama and is what frees the single runner slot
        (see ``_create_ollama_scout_client``); ``a8830bb`` measured exactly that
        for the non-streaming case -- the rollback's ``_prepare_model`` got a
        runner in 8.57s right after the timed-out call's socket closed.

        Local transport only: phase 1 does not stream the cloud path.
        """
        idle_budget = float(STREAM_IDLE_TIMEOUT_SECONDS)
        # Build-then-select, not a bare lookup: `run()` pre-warms only the 180s
        # and 45s clients, so a pure lookup would always miss and fall back to
        # the steady-state client -- leaving the idle budget unenforced at the
        # transport, which is the one place it can be enforced at all.
        self._create_ollama_chat_client(self.ollama, timeout=idle_budget)
        client = self._select_ollama_chat_client(idle_budget)

        started = time.monotonic()
        first_chunk_pending = True
        stream = None
        try:
            stream = client.chat(stream=True, **kwargs)
            for chunk in stream:
                if first_chunk_pending:
                    first_chunk_pending = False
                    waited = time.monotonic() - started
                    if waited > STREAM_IDLE_PROBE_SECONDS:
                        # Log-only, never an abort. Measured after the fact on
                        # this thread rather than armed on a timer: same
                        # information one probe-interval late, with no extra
                        # thread and no way to false-positive on a stream that
                        # was merely slow to start. METADATA ONLY -- raw
                        # dialogue never reaches the logs.
                        _eng.logger.warning(
                            "[STREAM_IDLE_PROBE] first_chunk_wait_s=%.2f threshold_s=%.2f "
                            "idle_budget_s=%.2f total_budget_s=%.2f",
                            waited,
                            float(STREAM_IDLE_PROBE_SECONDS),
                            idle_budget,
                            float(timeout),
                        )
                if time.monotonic() - started > timeout:
                    # Checked BEFORE handing the chunk over: a chunk released
                    # after the budget blew could still close a sentence and put
                    # audio on air for a turn that is already being declared dead.
                    raise TimeoutError(f"watchdog_timeout:{timeout:.2f}s")
                yield chunk
        except httpx.TimeoutException as exc:
            raise TimeoutError(f"watchdog_timeout:{idle_budget:.2f}s") from exc
        finally:
            if stream is not None:
                stream.close()

    def _resolve_chat_watchdog_timeout(self, request_model: str, *, provider_cfg=None, is_local=None) -> float:
        cfg = provider_cfg if provider_cfg is not None else self._provider_config
        # F2: honor the threaded entry posture; stand-alone callers re-derive.
        if is_local is None:
            is_local = self._cfg_is_local(cfg) or self._cloud_fallback_active
        if not is_local:
            # Cloud latency, not local GPU stall detection (spec C).
            return _eng.CLOUD_CHAT_TIMEOUT
        if self._awaiting_first_success_after_switch and request_model == self.current_model:
            return self._post_switch_watchdog_timeout
        return self._inference_watchdog_timeout


    @staticmethod
    def _is_watchdog_timeout_error(exc: Exception) -> bool:
        return isinstance(exc, TimeoutError) and str(exc).startswith("watchdog_timeout:")

    def _mark_model_generation_success(self, model: str) -> None:
        self._last_known_good_model = model
        if self.current_model == model:
            self._awaiting_first_success_after_switch = False

    def _recover_from_stalled_inference(self, *, request_model: str, source: str, timeout: float) -> None:
        message = f"watchdog_timeout after {timeout:.2f}s"
        self._last_llm_failure = {
            "model": request_model,
            "source": source,
            "attempt": 1,
            "reason": "watchdog_timeout",
            "message": message,
        }
        self._log(
            f"Timeout de inferencia con {request_model} tras {timeout:.2f}s. Iniciando recuperación...",
            level="error",
        )
        _eng.logger.warning(
            "Inference watchdog timeout: model=%s source=%s timeout=%.2fs",
            request_model,
            source,
            timeout,
        )

        if self._pending_model_switch:
            self._pending_switch_next_at = time.monotonic()
            self._log(
                f"Recuperación: se aplicará el modelo pendiente {self._pending_model_switch} al quedar libre.",
                level="warning",
            )
        else:
            self._rollback_to_last_known_good_model(request_model)

        self.ui_callback("llm_timeout_recovered")

    def _rollback_to_last_known_good_model(self, failed_model: str) -> bool:
        rollback_model = self._last_known_good_model
        if not rollback_model or rollback_model == failed_model:
            return False

        self._log(
            f"Recuperación: rollback automático de {failed_model} a {rollback_model}.",
            level="warning",
        )
        if self._apply_model_switch(rollback_model, persist_source="recovery_rollback"):
            self._awaiting_first_success_after_switch = False
            return True
        if self.current_model != rollback_model:
            _eng.logger.warning(
                "Rollback after stalled inference failed: failed_model=%s rollback_model=%s",
                failed_model,
                rollback_model,
            )
            return False
        self._awaiting_first_success_after_switch = False
        return True

    @staticmethod
    def _uses_reasoning_token_budget(model: str) -> bool:
        """Return whether a model should avoid fixed num_predict limits.

        Qwen3 and Gemma E models can spend part of the budget on internal
        reasoning. A hard low cap can yield empty or visibly truncated answers.
        """
        name = model.lower()
        return any(marker in name for marker in ("qwen3", "e2b", "e4b", "think"))

    def _resolve_effective_ctx_limit(self, model: str, native_ctx: int) -> int:
        """Return OpenCohost's runtime ctx cap for ``model`` without changing discovery."""
        tier = None
        tiers = getattr(self, "llm_tiers", None)
        if tiers is not None:
            active_model = tiers.active_model
            if active_model == model:
                tier = tiers.active_tier
            else:
                for candidate_tier, candidate_model in tiers.config.as_dict().items():
                    if candidate_model == model:
                        tier = candidate_tier
                        break
        tier_cap = _eng.LLM_TIER_EFFECTIVE_CTX_CAPS.get(tier, _eng.CTX_FALLBACK_DEFAULT)
        try:
            native = int(native_ctx)
        except (TypeError, ValueError):
            native = _eng.CTX_FALLBACK_DEFAULT
        if native <= 0:
            native = _eng.CTX_FALLBACK_DEFAULT
        return min(native, tier_cap)

    def _discover_model_ctx(self, model: str) -> int:
        """Layer 1: return ``model``'s native context length from ``ollama.show``.

        Sole owner of the ``ollama.show`` RPC for both context discovery and the
        reasoning-capability check. Calls ``ollama.show`` once per model lifetime,
        caches the parsed context length in ``self._model_ctx_limit`` and the raw
        response in ``self._ctx_show_cache`` so ``_check_capabilities_reasoning``
        can read ``capabilities`` from the same response. Any failure degrades to
        ``CTX_FALLBACK_DEFAULT`` — never a crash.
        """
        cache = getattr(self, "_model_ctx_limit", None)
        if cache is None:
            cache = {}
            self._model_ctx_limit = cache
        cached = cache.get(model)
        if cached is not None:
            return cached
        try:
            resp = self._fetch_show(model)
            ctx = _eng.context_budget.parse_model_ctx(resp, fallback=_eng.CTX_FALLBACK_DEFAULT)
        except Exception:
            ctx = _eng.CTX_FALLBACK_DEFAULT
        cache[model] = ctx
        return ctx

    def _fetch_show(self, model: str):
        """Fetch and memoise the raw ``ollama.show`` response for ``model``.

        Shared by ``_discover_model_ctx`` (context length) and
        ``_check_capabilities_reasoning`` (capabilities) so a single RPC serves
        both. The raw response is cached in ``self._ctx_show_cache``.
        """
        if not self._is_local:
            # Cloud provider active: no local Ollama to introspect. Return an
            # empty response so `_discover_model_ctx` degrades to the fallback
            # ctx and `_check_capabilities_reasoning` reads no capabilities —
            # the sole ollama.show call site, so this gate covers both callers.
            return {}
        cache = getattr(self, "_ctx_show_cache", None)
        if cache is None:
            cache = {}
            self._ctx_show_cache = cache
        if model in cache:
            return cache[model]
        import ollama
        # Bounded by the SAME watchdog the chat call uses. ollama-python builds
        # its default client with timeout=None — its own docstring says so, and in
        # httpx that means wait forever — and `ollama.show(model)` takes no
        # timeout argument, so there is nowhere to pass one. This probe runs on
        # the foreground turn path BEFORE the inference watchdog is armed, so
        # without a bound of its own a busy daemon parks the turn with no
        # recovery. A hang raises nothing, which is why the callers' try/except
        # never covered it. Timeout is the metadata budget (/api/tags class),
        # not the 180s generation budget.
        # ponytail: a stall costs 2x this, because _check_capabilities_reasoning
        # calls _discover_model_ctx and then _fetch_show, and a failure is not
        # cached here. Bounded and once-per-model, so not worth restructuring.
        resp = self._call_with_watchdog(
            ollama.show,
            timeout=_eng.OLLAMA_REQUEST_TIMEOUT,
            label="OllamaShowProbe",
            model=model,
        )
        cache[model] = resp
        return resp

    def _check_capabilities_reasoning(self, model: str) -> bool:
        """Layer 1: ask Ollama whether ``model`` advertises a 'thinking' capability.

        Augments (does not replace) the name heuristic so reasoning models whose
        name lacks a known marker (e.g. gemma4:12b) are still detected. Shares the
        single ``ollama.show`` RPC owned by ``_discover_model_ctx`` (via
        ``_fetch_show``) and populates ``self._model_ctx_limit[model]`` as a side
        effect. Wrapped defensively: any error, missing field, or older Ollama
        without the capabilities field degrades to False — never a crash.
        """
        try:
            # Populate the ctx-limit cache from the same RPC (RPC-ownership §Layer 1).
            self._discover_model_ctx(model)
            info = self._fetch_show(model)
            caps = info.get("capabilities") if isinstance(info, dict) else getattr(info, "capabilities", None)
            return bool(caps) and "thinking" in caps
        except Exception:
            return False

    def _resolve_reasoning_classification(self, model: str) -> bool:
        """Resolve whether ``model`` should drop the num_predict cap.

        Cache first (Layer 3); on miss combine the name heuristic with the Ollama
        capabilities check (Layer 1). The name heuristic short-circuits the ``or``
        so a known reasoning model never pays the ollama.show RPC. The resolved
        value is cached per model name.
        """
        cached = self._reasoning_model_cache.get(model)
        if cached is not None:
            return cached
        result = self._uses_reasoning_token_budget(model) or self._check_capabilities_reasoning(model)
        self._reasoning_model_cache[model] = result
        return result

    @staticmethod
    def _is_ollama_transport_error(exc: Exception) -> bool:
        if isinstance(exc, (TimeoutError, ConnectionError, socket.timeout, requests.exceptions.RequestException)):
            return True
        class_names = {cls.__name__ for cls in type(exc).__mro__}
        return any("Timeout" in name or "Connect" in name or "Connection" in name for name in class_names)
    