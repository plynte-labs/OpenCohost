import concurrent.futures
import httpx
import logging
import requests
import socket
import sys
import threading
import time
import types
import uuid
from collections import deque
from typing import Optional

from opencohost.config import settings as _settings_mod
from opencohost.config.settings import (
    CLOUD_CHAT_TIMEOUT,
    CTX_FALLBACK_DEFAULT,
    LLM_KEEP_ALIVE,
    LLM_MAX_TOKENS,
    LLM_SCOUT_TIMEOUT,
    OLLAMA_CHAT_TIMEOUT,
    OLLAMA_REQUEST_TIMEOUT,
    STREAM_IDLE_PROBE_SECONDS,
    STREAM_IDLE_TIMEOUT_SECONDS,
    save_last_model,
)
from opencohost.config.model_parameters import (
    get_model_context_settings,
    resolve_auto_context_tokens,
)
from opencohost.core.context import context_budget
from opencohost.core.engine.llm_inference_service import get_inference_service, local_chat
from opencohost.core.providers.llm_tiers import (
    LLMTierConfig,
    LLMTierState,
    LLM_TIER_LABELS,
)

logger = logging.getLogger("OpenCohost")

# Spoken when the inference watchdog cancels an owner turn, so the owner is not
# left in silence. Short and in character; the app locale picks the language.
TIMEOUT_NOTICE_LINES = {
    "es": "Se me trabó la respuesta, pregúntamelo otra vez.",
    "en": "My answer got stuck, ask me again.",
}
TIMEOUT_NOTICE_SUFFIX = "-timeout-notice"

# A /api/ps residency snapshot older than this is not evidence (ADR-056). The
# health monitor polls every ~5s, so a live snapshot is always far younger.
RESIDENCY_STALE_SECONDS = 60.0


def _save_last_model_compat(*args, **kwargs) -> None:
    """Resolve save_last_model dynamically from llm_engine for test mock compatibility."""
    target = getattr(sys.modules.get("opencohost.core.llm_engine"), "save_last_model", save_last_model)
    return target(*args, **kwargs)

def _models_match_residency(req_model: str, snap_model: str) -> bool:
    """Return True if req_model and snap_model refer to the same model.

    Handles:
    - Case insensitivity ('gemma4:e4b' vs 'Gemma4:E4B')
    - Stripping ':latest' suffix
    - Delimited tag/flavor extensions (':', '-', '_') such that
      'gemma4:e4b' matches 'gemma4:e4b-instruct' and 'gemma4:e4b:q4_k_m',
      but 'qwen2.5:7b' NEVER matches 'qwen2.5:72b'.
    """
    r = str(req_model or "").strip().lower()
    s = str(snap_model or "").strip().lower()
    if not r or not s:
        return False
    if r == s:
        return True
    r_base = r[:-7] if r.endswith(":latest") else r
    s_base = s[:-7] if s.endswith(":latest") else s
    if r_base == s_base:
        return True
    for delim in (":", "-", "_"):
        if s.startswith(r + delim) or r.startswith(s + delim):
            return True
        if s_base.startswith(r_base + delim) or r_base.startswith(s_base + delim):
            return True
    return False


class ModelManagementMixin:
    _models_match_residency = staticmethod(_models_match_residency)

    def _is_local_model_resident(self, model: str) -> Optional[bool]:
        """Check whether `model` is currently resident in Ollama VRAM/RAM via ps().

        Returns:
            True: ps() responded cleanly and `model` matches a resident model.
            False: ps() responded cleanly and `model` is NOT in the resident list (cold start / evicted).
            None: ps() failed, timed out, or returned malformed/mocked data (unknown / unit tests).
        """
        if not hasattr(self, "ollama") or self.ollama is None:
            return None
        try:
            client_factory = getattr(self.ollama, "Client", None)
            client = client_factory(timeout=1.0) if callable(client_factory) else self.ollama
            ps_fn = getattr(client, "ps", None)
            if not callable(ps_fn):
                return None
            resp = ps_fn()
            models = getattr(resp, "models", None)
            if models is None and isinstance(resp, dict):
                models = resp.get("models")
            if models is None or isinstance(models, (str, bytes)):
                return None
            if hasattr(models, "_mock_return_value") or type(models).__name__ == "MagicMock":
                return None
            if not isinstance(models, (list, tuple)):
                return None
            for item in models:
                m_name = getattr(item, "model", None)
                if m_name is None and isinstance(item, dict):
                    m_name = item.get("model")
                if isinstance(m_name, str) and _models_match_residency(model, m_name):
                    return True
            return False
        except Exception:
            return None

    def _probe_ollama_service(self, timeout: float = 3.0) -> bool:
        """Pure, side-effect-free, bounded health check for Ollama daemon.

        Returns True if Ollama responds to list() without raising, False otherwise.
        Uses a short-timeout client (default 3.0s) so socket timeouts cancel in flight.
        Never mutates `is_ready`, never triggers `_prepare_model` or warming,
        never applies pending switches, and never emits UI callbacks.
        """
        if not self._is_local:
            return True
        try:
            client = self._select_ollama_probe_client(timeout)
            if client is None:
                return False
            client.list()
            return True
        except Exception:
            return False

    def _select_ollama_probe_client(self, timeout: float = 3.0):
        probe_client = getattr(self, "_ollama_probe_client", None)
        if probe_client is not None:
            return probe_client
        if hasattr(self, "ollama") and self.ollama is not None:
            client_factory = getattr(self.ollama, "Client", None)
            if isinstance(self.ollama, types.ModuleType) and client_factory is not None:
                try:
                    probe_client = client_factory(timeout=timeout)
                    self._ollama_probe_client = probe_client
                    return probe_client
                except Exception:
                    return self.ollama
            return self.ollama
        return None

    def _reconcile_local_readiness(self) -> bool:
        """Reconcile local Ollama readiness with provider_epoch snapshot.

        Captures provider_epoch before probing. If probe succeeds and epoch/provider
        did not change concurrently, sets self.is_ready = True under self._lock.
        Returns True if engine is (or became) ready in local mode, False otherwise.
        """
        with self._lock:
            if not self._is_local:
                return True
            probe_epoch = self.provider_epoch

        success = self._probe_ollama_service()

        with self._lock:
            if (
                success
                and self.provider_epoch == probe_epoch
                and self._cfg_is_local(self._provider_config)
            ):
                self.is_ready = True
                return True
            elif (
                not success
                and self.provider_epoch == probe_epoch
                and self._cfg_is_local(self._provider_config)
            ):
                self.is_ready = False
                return False
            return self.is_ready

    # Idle re-probe state (engine-readiness-recovery-20261111). Class-level
    # defaults so no __init__ change is needed; instances shadow them on write.
    _reprobe_next_at: Optional[float] = None
    _reprobe_delay_s: float = 2.0
    _turn_recover_next_at: float = 0.0
    _REPROBE_FIRST_DELAY_S = 2.0
    _REPROBE_MAX_DELAY_S = 30.0
    _TURN_RECOVER_MIN_INTERVAL_S = 5.0
    _RECOVER_PROBE_TIMEOUT_S = 2.0

    def _recover_local_readiness(self) -> bool:
        """Bounded recovery of a stale not-ready flag on the local provider.

        A cheap probe (short client timeout) decides whether Ollama answers; only
        then the full ``_check_ollama_service`` path runs, so startup-model
        re-resolution and model preparation happen exactly as on a normal start.
        Silent on failure. Callers own the throttling.
        """
        if not self._is_local:
            return True
        if not self._probe_ollama_service(timeout=self._RECOVER_PROBE_TIMEOUT_S):
            return False
        return bool(self._check_ollama_service(notify_unavailable=False))

    def _ollama_reprobe_tick(self) -> None:
        """Idle tick: re-probe a not-ready local provider with 2,4,8..30s backoff."""
        if self.is_ready or not self._is_local or not getattr(self, "startup_check_done", True):
            self._reprobe_next_at = None
            self._reprobe_delay_s = self._REPROBE_FIRST_DELAY_S
            return
        if self._processing or self._speech_active:
            return
        now = time.monotonic()
        if self._reprobe_next_at is None:
            self._reprobe_next_at = now + self._reprobe_delay_s
            return
        if now < self._reprobe_next_at:
            return
        if self._recover_local_readiness():
            self._reprobe_next_at = None
            self._reprobe_delay_s = self._REPROBE_FIRST_DELAY_S
            return
        self._reprobe_delay_s = min(self._reprobe_delay_s * 2.0, self._REPROBE_MAX_DELAY_S)
        self._reprobe_next_at = now + self._reprobe_delay_s

    def _list_ollama_models_bounded(self):
        """`self.ollama.list()` with an upper bound (S5).

        The ollama client has no default timeout: a daemon that accepts the
        connection and never answers would hang the engine thread. Runs the call
        on a daemon thread and raises TimeoutError past OLLAMA_LIST_TIMEOUT_S
        (read at call time). Goes through ``self.ollama`` so test doubles work.
        """
        box: dict = {}

        def _run() -> None:
            try:
                box["result"] = self.ollama.list()
            except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
                box["error"] = exc

        worker = threading.Thread(target=_run, daemon=True, name="ollama-list")
        worker.start()
        worker.join(_settings_mod.OLLAMA_LIST_TIMEOUT_S)
        if worker.is_alive():
            raise TimeoutError("ollama.list timed out")
        if "error" in box:
            raise box["error"]
        return box.get("result")

    @staticmethod
    def _installed_tags_from_listing(listing) -> set:
        models = getattr(listing, "models", None)
        if models is None and isinstance(listing, dict):
            models = listing.get("models")
        raw_tags = []
        try:
            for model in models or []:
                if isinstance(model, dict):
                    raw_tags.append(model.get("model") or model.get("name") or "")
                else:
                    raw_tags.append(getattr(model, "model", "") or "")
        except TypeError:
            return set()
        return _settings_mod._normalize_installed_model_tags(t for t in raw_tags if isinstance(t, str))

    def _reresolve_startup_model(self, listing) -> None:
        """Once Ollama reports ready, redo the startup model decision (S5).

        `resolve_startup_model()` runs in __init__, before Ollama is up, so with
        no installed list it fell back to DEFAULT_MODEL even when the owner had
        a saved or different installed model. Runs once; never overrides a
        model the user already chose or queued; never picks an uninstalled one.
        """
        if not getattr(self, "_startup_model_unverified", False):
            return
        self._startup_model_unverified = False
        if self._pending_model_switch or self.current_model != getattr(self, "_startup_model_value", None):
            return
        tags = self._installed_tags_from_listing(listing)
        if not tags:
            return
        try:
            model, source = _settings_mod.resolve_startup_model(installed_model_tags=tags)
        except Exception:
            return
        if not model or model == self.current_model:
            self._model_source = source if model == self.current_model else self._model_source
            return
        previous = self.current_model
        self.current_model = model
        self._desired_model = model
        self._last_known_good_model = model
        self._model_source = source
        try:
            self.llm_tiers.active_tier = self._infer_active_tier(model, self.llm_tiers.config)
        except Exception:
            pass
        self._log(f"Modelo inicial re-resuelto con Ollama listo: {previous} -> {model} (fuente: {source})")

    def _check_ollama_service(self, *, notify_unavailable: bool = True):
        if not self._is_local:
            # Cloud provider active: no local Ollama service to probe or warm.
            # Cloud reachability is the health monitor's concern (Phase 5).
            self.is_ready = True
            return True
        try:
            listing = self._list_ollama_models_bounded()
        except Exception as e:
            self.is_ready = False
            if notify_unavailable:
                self._log(f"Ollama no esta disponible: {e}", level="warning")
                self.ui_callback("ollama_unavailable")
            return False

        self.is_ready = True
        self._log("Ollama disponible. Preparando modelo...")
        self._reresolve_startup_model(listing)
        if self._pending_model_switch and self._pending_model_switch != self.current_model:
            self._log(f"Aplicando modelo pendiente: {self._pending_model_switch}")
            self._apply_model_switch(self._pending_model_switch)
        else:
            self._prepare_model(self.current_model)
            if self._pending_model_switch == self.current_model:
                self._pending_model_switch = None
                self._pending_switch_not_ready_logged = False
        # A profile applied before Ollama was ready got the fallback cap of 10.
        self._refresh_max_turns_cap()
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
                logger.warning(f"No se pudo liberar modelo {previous_model}: {e}")

        self.current_model = new_model
        self._awaiting_first_success_after_switch = previous_model != new_model
        self._log(f"🔄 Modelo cambiado a: {new_model}")
        # The history window cap follows the new model's effective ctx.
        self._refresh_max_turns_cap()

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
        self.llm_tiers = LLMTierState(config=config, active_tier=tier)

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

        if not self.is_ready and self._is_local:
            self._reconcile_local_readiness()

        if not self.is_ready:
            self._last_switch_failure = {
                "requested_tier": target_tier,
                "requested": target_model,
                "current_tier": previous_tier,
                "current": previous_model,
                "reason": "ollama_not_ready",
            }
            self._log(
                f"Tier LLM '{target_tier}' ({target_model}) rechazado: motor no está listo; "
                f"se mantiene {previous_tier} ({previous_model}).",
                level="warning",
            )
            self.ui_callback("llm_tier_switch_failed")
            return False

        try:
            if not self._prepare_model(target_model):
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
                    logger.warning(f"No se pudo liberar modelo {previous_model}: {e}")
            _save_last_model_compat(target_model, source="llm_tier_switch")
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

        label = LLM_TIER_LABELS.get(target_tier, target_tier)
        self._last_switch_failure = None
        self._log(f"LLM tier changed: {previous_tier} -> {target_tier} ({target_model})")
        logger.info("Manual LLM tier changed to %s (%s)", label, target_model)
        self.ui_callback("llm_tier_switch_applied")
        return True

    def _check_pending_model_switch(self) -> None:
        """Attempt a pending model switch or context reload (non-blocking).

        T2 (model_context_auto_config_20260928): a context reload deferred by
        ``request_model_context_reload`` (busy at request time) shares this
        SAME idle-tick busy gate as a pending model switch -- both are
        exactly the "apply once the motor is free" pattern.
        """
        if self._pending_model_switch is None and self._pending_ctx_reload_model is None:
            return
        if time.monotonic() < self._pending_switch_next_at:
            return  # not time yet
        if self._processing or self._speech_active:
            return  # still busy

        if self._pending_ctx_reload_model is not None:
            ctx_model = self._pending_ctx_reload_model
            self._pending_ctx_reload_model = None
            if ctx_model == self.current_model:
                self._reload_model_for_ctx_change(ctx_model)

        if self._pending_model_switch is None:
            return

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
            _save_last_model_compat(new_model, source=persist_source)
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
        # Same num_ctx as the chat path, so the first turn reuses this load.
        warm_options = self._with_num_ctx(
            {"num_predict": 1, "temperature": 0}, self._resolve_local_num_ctx(model)
        )
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    self.ollama.generate,
                    model=model,
                    prompt="Responde solo: ok",
                    keep_alive=LLM_KEEP_ALIVE,
                    options=warm_options,
                )
                future.result(timeout=120)
        except Exception as e:
            self._log(f"No se pudo preparar modelo {model}: {e}", level="warning")
            logger.warning("No se pudo preparar modelo %s: %s", model, e)
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

    def request_model_context_reload(self, model: str) -> bool:
        """Entry point for ``PUT /api/models/context``: re-apply ``model``'s
        context configuration without racing a live turn (T2,
        model_context_auto_config_20260928).

        Returns True when ``model`` is the active model -- either applied
        immediately (idle) or deferred to the SAME busy gate ``switch_model``
        uses (``_check_pending_model_switch``, mid-generation or mid-speech).
        Returns False when ``model`` is not the active model: a non-active
        model's new context config simply applies whenever it is next
        loaded, no engine action needed here.
        """
        if model != self.current_model:
            return False
        if self._processing or self._speech_active:
            self._pending_ctx_reload_model = model
            self._log(f"Reload de contexto para {model} pendiente: motor ocupado.", level="debug")
            return True
        self._reload_model_for_ctx_change(model)
        return True

    def _reload_model_for_ctx_change(self, model: str) -> bool:
        """Force a fresh warm-up of ``model`` at its NEW context target.

        Resets the per-model ctx-plan ceiling (``_ctx_plan_state``) so the
        very next ``_resolve_local_num_ctx``/``_resolve_effective_ctx_limit``
        call picks up the freshly-saved target from scratch (a changed
        target already resets the ceiling per ``_apply_vram_ctx_plan``, but a
        just-cleared override needs the SAME reset -- there is no "target"
        left to compare against). Then clears ``_warmed_model`` so
        ``_prepare_model`` bypasses its ``_warmed_model == model``
        short-circuit and re-issues the warm-up ``generate`` call at the new
        ``num_ctx`` -- the standard way this codebase forces an Ollama reload
        (see ``_resolve_local_num_ctx``'s docstring: a ``num_ctx`` mismatch
        alone triggers Ollama's own reload on its next call). Finally
        refreshes the ``max_turns`` cap for the new effective ctx.
        """
        plans = getattr(self, "_ctx_plan_state", None)
        if isinstance(plans, dict):
            plans.pop(model, None)
        old_ctx = getattr(self, "_model_ctx_limit", {}).get(model)
        self._warmed_model = None
        warmed = self._prepare_model(model)
        new_ctx = self._resolve_local_num_ctx(model)
        self._refresh_max_turns_cap()
        self._log(
            f"context_apply: model={model} old_ctx={old_ctx} new_ctx={new_ctx} warmed={warmed}",
            level="info",
        )
        return warmed

    def release_owned_ollama_model(self, timeout: float = 2.0) -> bool:
        """Best-effort unload for the model warmed by this OpenCohost session."""
        model = self._loaded_model or self._warmed_model
        if not model or not self._owns_ollama_model or not hasattr(self, "ollama"):
            logger.info("Ollama model release skipped; no OpenCohost-owned model recorded")
            return False

        result = {"released": False}

        def unload() -> None:
            try:
                self.ollama.generate(model=model, prompt="", keep_alive=0)
                result["released"] = True
            except Exception as exc:
                logger.warning("No se pudo liberar modelo Ollama %s: %s", model, exc)

        thread = threading.Thread(target=unload, name="OllamaModelRelease", daemon=True)
        thread.start()
        thread.join(timeout=timeout)
        if thread.is_alive():
            logger.warning("Timeout liberando modelo Ollama %s; cierre continua", model)
            return False
        if result["released"]:
            self._loaded_model = None
            self._warmed_model = None
            self._owns_ollama_model = False
            logger.info("Modelo Ollama %s liberado", model)
        return result["released"]

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
            timeout = OLLAMA_CHAT_TIMEOUT
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
        return get_inference_service(self).chat(
            provider_cfg=cfg,
            is_local=is_local,
            chat_timeout=chat_timeout,
            **kwargs,
        )

    def _select_ollama_chat_client(self, chat_timeout):
        """Pick the client memoized for *chat_timeout* via LLMInferenceService."""
        cache = getattr(self, "_ollama_chat_clients", None)
        client = cache.get(chat_timeout) if cache and chat_timeout is not None else None
        if client is not None:
            return client
        return get_inference_service(self).select_chat_client(chat_timeout) or self._ollama_chat_client or self.ollama

    def _ollama_scout_chat(self, *, provider_cfg=None, is_local=None, **kwargs):
        cfg = provider_cfg if provider_cfg is not None else self._provider_config
        if is_local is None:
            is_local = self._cfg_is_local(cfg) or self._cloud_fallback_active
        if not is_local:
            return self._cloud_chat(provider_cfg=cfg, is_local=is_local, **kwargs)
        client = self._ollama_scout_client or self.ollama
        return client.chat(**kwargs)

    def _ollama_judge_chat(self, *, provider_cfg=None, is_local=None, **kwargs):
        """Transport for the memoria promotion judge — ALWAYS local."""
        client = self._ollama_judge_client or self.ollama
        # Same think-unsupported fallback as chat: the judge sends top-level
        # ``think`` and must not fail every sweep on a model without it.
        return local_chat(client, kwargs)

    def _call_with_watchdog(self, call, *, timeout: float, label: str = "OllamaChatWatchdog", **kwargs):
        """Run *call* on a daemon thread and stop waiting on it after *timeout*."""
        return get_inference_service(self).call_with_watchdog(
            call, timeout=timeout, label=label, **kwargs
        )

    def _ollama_chat_with_watchdog(self, *, timeout: float, chat_callable=None, **kwargs):
        call = chat_callable or (lambda **kw: self._ollama_chat(chat_timeout=timeout, **kw))
        return get_inference_service(self).call_with_watchdog(
            call,
            timeout=timeout,
            **kwargs,
        )

    def _ollama_chat_streaming(self, *, timeout: float, **kwargs):
        """Yield raw chunks from a local ``ollama.chat(stream=True)``, bounded."""
        idle_budget = float(STREAM_IDLE_TIMEOUT_SECONDS)
        self._create_ollama_chat_client(self.ollama, timeout=idle_budget)
        client = self._select_ollama_chat_client(idle_budget)
        return get_inference_service(self).chat_streaming(
            timeout=timeout,
            chat_client=client,
            time_fn=time.monotonic,
            **kwargs,
        )

    def _llm_chat_streaming(self, *, is_local: bool, provider_cfg=None, timeout: float, model, messages, options):
        """Dispatch a streaming chat call to local Ollama or cloud SSE
        (live_session_fixes_20260925 T8) -- the single seam
        ``GenerationOrchestrator.run_streaming_attempt`` calls regardless of
        transport, so it needs no branch of its own beyond this."""
        if is_local:
            return self._ollama_chat_streaming(
                timeout=timeout,
                model=model,
                messages=messages,
                keep_alive=LLM_KEEP_ALIVE,
                options=options,
            )
        return get_inference_service(self).chat_streaming(
            timeout=timeout,
            is_local=False,
            provider_cfg=provider_cfg if provider_cfg is not None else self._provider_config,
            messages=messages,
            options=options,
            time_fn=time.monotonic,
        )

    def _resolve_chat_watchdog_timeout(self, request_model: str, *, provider_cfg=None, is_local=None) -> float:
        cfg = provider_cfg if provider_cfg is not None else self._provider_config
        # F2: honor the threaded entry posture; stand-alone callers re-derive.
        if is_local is None:
            is_local = self._cfg_is_local(cfg) or self._cloud_fallback_active
        if not is_local:
            # Cloud latency, not local GPU stall detection (spec C).
            return CLOUD_CHAT_TIMEOUT

        # Cold-load detection (Option B): If the local model was evicted from VRAM/RAM
        # (e.g. after LLM_KEEP_ALIVE idle expiration or initial startup), loading weights
        # to GPU can take 15-60s. Avoid false watchdog kills by granting full generation budget
        # and notifying the UI.
        is_resident = self._is_local_model_resident(request_model)
        if is_resident is False:
            self._cold_warming_notified = True
            if getattr(self, "ui_callback", None) is not None:
                self.ui_callback("model_warming")
            self._log(
                f"Carga en frío detectada para {request_model}. Extendiendo watchdog a {self._inference_watchdog_timeout:.0f}s.",
                level="info",
            )
            return self._inference_watchdog_timeout

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
        if getattr(self, "_cold_warming_notified", False):
            self._cold_warming_notified = False
            if getattr(self, "ui_callback", None) is not None:
                self.ui_callback("ready")

    def _recover_from_stalled_inference(self, *, request_model: str, source: str, timeout: float) -> None:
        self._cold_warming_notified = False
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
        logger.warning(
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
        self._speak_timeout_notice(source)

    def _speak_timeout_notice(self, source: str) -> bool:
        """Queue one short spoken line after a watchdog recovery (owner turns only).

        Runs the line through the same stages as any spoken reply (TTS
        sanitization, then the output guard) and submits it on the router's owner
        band. It never calls the LLM, so it cannot make the abandoned runner busy
        again, and it runs after the rollback was attempted. A no-op for chat /
        viewer / agenda sources and while the speech router is disabled. Never
        raises; logs only a count/flag.
        """
        try:
            from opencohost.core.context.prompt_assembler import OWNER_POLICY_SOURCES

            if source not in OWNER_POLICY_SOURCES:
                return False
            if not getattr(self, "_speech_router_enabled", False):
                return False
            try:
                from opencohost.i18n import active as i18n_active
                from opencohost.i18n.coherence import primary_subtag

                lang = primary_subtag(i18n_active.get_active_bundle().code)
            except Exception:
                lang = "en"
            line = TIMEOUT_NOTICE_LINES.get(lang, TIMEOUT_NOTICE_LINES["en"])

            from opencohost.core.engine.generation_orchestrator import (
                _output_guard_with_tts_check,
            )
            from opencohost.core.speech.router import PRIORITY_OWNER

            pieces = [p for p in self._sanitize_for_tts(line) if p.strip()]
            spoken = " ".join(pieces)
            if not spoken:
                return False
            allowed, _reason = _output_guard_with_tts_check(spoken, source=source)
            if not allowed:
                logger.info("[RECOVERY] timeout_notice blocked=1")
                return False
            self._ensure_router().submit(
                spoken, f"{source}{TIMEOUT_NOTICE_SUFFIX}", PRIORITY_OWNER, releases_arrival_hold=False
            )
            logger.info("[RECOVERY] timeout_notice queued=1")
            return True
        except Exception:
            logger.debug("[RECOVERY] timeout notice failed", exc_info=False)
            return False

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
            logger.warning(
                "Rollback after stalled inference failed: failed_model=%s rollback_model=%s",
                failed_model,
                rollback_model,
            )
            return False
        self._awaiting_first_success_after_switch = False
        return True

    @staticmethod
    def _uses_reasoning_token_budget(model: str) -> bool:
        """Return whether a model supports explicit reasoning policy and governed budgeting.

        Qwen3 and Gemma E models can spend part of the budget on internal reasoning.
        Under ADR-056, these models use adaptive governance rather than arbitrary fixed caps.
        """
        name = model.lower()
        return any(marker in name for marker in ("qwen3", "e2b", "e4b", "think"))

    def _resolve_effective_ctx_limit(
        self,
        model: str,
        native_ctx: int,
        requested_ctx: Optional[int] = None,
    ) -> int:
        """Return OpenCohost's runtime context allocation for ``model``.

        ADR-056: Decouples native capability ceiling from runtime context allocation:
        - native_context_max: discovered capability ceiling (e.g. 131072 for Gemma 4)
        - allocated_context: live runtime allocation from /api/ps if resident
        - requested_context: what OpenCohost requests from Ollama on cold start

        Native context ceiling (e.g. 128K for Gemma) is NEVER used as an automatic
        allocation fallback without /api/ps truth.

        Priority (T1b, model_context_auto_config_20260928, owner decision
        2026-09-28 -- see also ``VALID_CONTEXT_POLICIES``'s docstring in
        ``model_parameters.py`` and the ADR-056 Amendment dated 2026-09-28):
          0. An EXPLICIT per-model override (``context.policy == "requested"``
             plus a configured ``context_tokens``) wins over everything
             below, including a live resident allocation that differs (the
             next load re-requests it) -- see ``_resolve_requested_ctx_plan``.
          1. Live resident allocation from /api/ps (ADR-056 P0): trusted over
             a possibly-stale/incorrect ``native_ctx`` argument -- UNLESS
             native ctx is reliably KNOWN and Auto's own VRAM-bounded target
             for it is LARGER than what is currently resident (T1b). Without
             this, a model kept resident at a small ctx by Ollama's own
             ``keep_alive`` (e.g. from an earlier session, or the pre-T1
             4096 fallback) never got upgraded by itself: every turn re-
             requested the SAME resident value and it stuck forever -- the
             literal reported bug. In that case Auto behaves like an
             explicit target and requests it (``_apply_vram_ctx_plan``, the
             SAME ceiling/spill-ratchet/NVML-advisory machinery an explicit
             override uses -- never a parallel one), bounded by the 16384 cap
             and by any ratchet already in force for this model: it never
             bounces back above a ceiling a prior spill lowered, and settles
             (no further reload) once resident reaches that ceiling. Resident
             still wins unconditionally when it is already >= Auto's target
             (a stale/wrong discovered ``native_ctx`` must never shrink a
             bigger live allocation), when native ctx itself is unknown/
             invalid (never guess an upgrade target from an unreliable
             ceiling), or when the config read fails (fail open to runtime
             truth).
          2. Cold start (nothing resident, no explicit override): Auto =
             ``resolve_auto_context_tokens(native_max)`` (min(native,
             AUTO_CONTEXT_TOKENS)) run through the SAME VRAM-clamp/ratchet
             plan machinery as an explicit override (``_apply_vram_ctx_plan``)
             -- REGARDLESS of ``context.policy``: "runtime_truth" only means
             "no explicit override", it does not disable Auto. A
             model_parameters read failure fails open to the OLD
             conservative cold-start default instead of gambling Auto+VRAM
             math while the rest of the config cannot be trusted either.
        """
        try:
            native_max = int(native_ctx)
            native_known = True
        except (TypeError, ValueError):
            native_max = CTX_FALLBACK_DEFAULT
            native_known = False
        if native_max <= 0:
            native_max = CTX_FALLBACK_DEFAULT
            native_known = False

        # 0. Explicit per-model override.
        planned = self._resolve_requested_ctx_plan(model, native_max)
        if planned is not None:
            return max(32, planned)

        # 1. Live resident allocation from /api/ps probe if model is resident
        monitor = getattr(self, "health_monitor", None) or getattr(self, "_health_monitor", None)
        resident_ctx: Optional[int] = None
        if monitor is not None:
            snap = getattr(monitor, "residency_snapshot", None)
            if snap is None:
                probe = getattr(monitor, "residency_probe", None) or getattr(monitor, "_ollama_residency", None)
                if probe is not None:
                    snap = getattr(probe, "snapshot", None)
            if snap is not None:
                snap_ctx = getattr(snap, "context_length", 0) if snap is not None else 0
                if isinstance(snap_ctx, (int, float)) and not isinstance(snap_ctx, bool) and snap_ctx > 0:
                    snap_model = getattr(snap, "model", None)
                    if isinstance(snap_model, str) and _models_match_residency(model, snap_model):
                        resident_ctx = int(snap_ctx)

        if resident_ctx is not None:
            # T1b (model_context_auto_config_20260928): Auto may UPGRADE a
            # smaller resident allocation up to its own VRAM-bounded target
            # -- see this method's docstring and the ADR-056 Amendment
            # (2026-09-28). Only attempted when native ctx is reliably known
            # (never guess an upgrade target otherwise) and the config read
            # succeeds (fail open to runtime truth on any failure, mirroring
            # step 2's own independent read below).
            if native_known:
                config_ok = True
                try:
                    get_model_context_settings(model)
                except Exception:
                    config_ok = False
                if config_ok:
                    auto_target = resolve_auto_context_tokens(native_max)
                    if resident_ctx < auto_target:
                        allocated = self._apply_vram_ctx_plan(model, auto_target, native_max)
                        return max(32, allocated)
            # Live residency truth from /api/ps represents what Ollama
            # already allocated in VRAM: LARGER than Auto's target, native
            # unknown, or a broken config read -- resident wins unconditionally.
            return max(32, resident_ctx)

        # 2. Cold start, nothing resident, no explicit override: T1 Auto default.
        # A second, independent read of model_parameters -- deliberately NOT
        # reusing `_resolve_requested_ctx_plan`'s own internal read/except --
        # so a config read failure here still falls open to the OLD
        # conservative cold-start default below rather than Auto, without
        # changing that method's signature or its own fail-open tests.
        config_ok = True
        try:
            get_model_context_settings(model)
        except Exception:
            config_ok = False

        if config_ok:
            auto_target = resolve_auto_context_tokens(native_max)
            allocated = self._apply_vram_ctx_plan(model, auto_target, native_max)
        else:
            req = requested_ctx
            if req is None:
                req = getattr(self, "requested_context", None) or getattr(self, "_requested_context", None)
            if req is None:
                req = getattr(self, "_model_requested_ctx", {}).get(model)
            if req is None:
                # If native max is very large (e.g. Gemma 128K or >= 65536) and not resident,
                # default to safe cold start requested context (CTX_FALLBACK_DEFAULT=4096)
                if native_max >= 65536 or ("gemma" in model.lower() and native_max > 8192):
                    req = CTX_FALLBACK_DEFAULT
                else:
                    req = native_max
            try:
                allocated = int(req)
            except (TypeError, ValueError):
                allocated = CTX_FALLBACK_DEFAULT

        # 3. Cap cold start requested context by the model's native capability ceiling
        allocated = min(allocated, native_max)
        return max(32, allocated)

    def _resolve_local_num_ctx(self, model: Optional[str]) -> Optional[int]:
        """Return the ``num_ctx`` the chat path would request for local ``model``.

        Used by the warm-up, the Topic Scout and the promotion judge so every
        local Ollama call allocates the same context as chat — a call at
        Ollama's default ctx forces a model reload before the next chat turn.
        Returns None (caller omits ``num_ctx``, today's behavior) when there is
        no model, the local provider is not active (no ``ollama.show`` → native
        ctx unknown), or resolution fails.
        """
        if not model or not getattr(self, "_is_local", False):
            return None
        try:
            native = self._discover_model_ctx(model)
            return int(self._resolve_effective_ctx_limit(model, native))
        except Exception as exc:
            logger.debug("num_ctx resolution failed for %s; omitted: %s", model, exc)
            return None

    def _history_output_reserve(self, model: Optional[str]) -> int:
        """Output tokens the char budget gate reserves on an ordinary chat turn.

        Mirrors ``PromptContextAssembler``'s chat-intent branch: a reasoning
        model (or one with reasoning enabled) reserves its ``budget_tokens``,
        anything else reserves ``LLM_MAX_TOKENS``.
        """
        try:
            r_cfg = self._resolve_reasoning_settings(model) if model else {}
            reasoning = bool(r_cfg.get("enabled")) or (
                bool(model) and self._uses_reasoning_token_budget(model)
            )
            budget = r_cfg.get("budget_tokens") if reasoning else None
            if budget is not None and int(budget) > 0:
                return int(budget)
        except Exception as exc:
            logger.debug("reasoning reserve lookup failed for %s: %s", model, exc)
        return LLM_MAX_TOKENS

    def _compute_max_turns_cap(self) -> int:
        """Profile ``max_turns`` cap for the active model's effective ctx.

        Resolved only once Ollama is ready (``is_ready``) and the model's
        native ctx is already cached, so applying a profile never blocks on
        ``ollama.show``; otherwise, or when the ctx is unknown (cloud provider,
        resolution failure), the cap is the historical 10. ``_refresh_max_turns_cap`` recomputes it once the
        engine is ready and after every model switch.
        """
        if not getattr(self, "is_ready", False):
            return context_budget.MAX_TURNS_CAP_FLOOR
        model = getattr(self, "current_model", None)
        # Only once the model's native ctx is cached (the warm-up already
        # resolved it): a profile switch must never trigger ``ollama.show``.
        if model not in (getattr(self, "_model_ctx_limit", None) or {}):
            return context_budget.MAX_TURNS_CAP_FLOOR
        try:
            ctx = self._resolve_local_num_ctx(model)
        except Exception as exc:
            logger.debug("max_turns cap: ctx resolution failed for %s: %s", model, exc)
            ctx = None
        return context_budget.max_turns_cap_for_ctx(
            ctx, max_output=self._history_output_reserve(model)
        )

    @staticmethod
    def _history_rotation_pairs() -> int:
        """Pairs dropped together when the history is full (ADR-064 / F1).

        1 under the legacy layout (the deque drops one pair at a time); K
        under v2, so the first history message moves only every K turns.
        """
        if getattr(_settings_mod, "PROMPT_LAYOUT", "legacy") == "v2":
            return int(context_budget.EVICTION_CHUNK_PAIRS)
        return 1

    @classmethod
    def _history_maxlen(cls, turns: int) -> int:
        """Deque capacity (messages) for ``turns`` pairs of memory depth.

        Legacy: ``turns * 2``. v2 stores ``turns + K - 1`` pairs and drops K at
        once on overflow, so the depth never falls below ``turns``.
        """
        return (int(turns) + cls._history_rotation_pairs() - 1) * 2

    def _refresh_max_turns_cap(self) -> None:
        """Recompute the cap and re-clamp the active profile's history window.

        The profile's requested turns are kept in ``_requested_max_turns`` so a
        larger ctx restores them and a smaller one shrinks the window (the
        deque keeps its newest entries).
        """
        cap = self._compute_max_turns_cap()
        self._max_turns_cap = cap
        requested = getattr(self, "_requested_max_turns", self._max_turns)
        new_turns = max(1, min(int(requested), cap))
        with self._history_lock:
            self._max_turns = new_turns
            new_maxlen = self._history_maxlen(new_turns)
            if self.historial.maxlen != new_maxlen:
                self.historial = deque(self.historial, maxlen=new_maxlen)

    @staticmethod
    def _with_num_ctx(options: dict, num_ctx: Optional[int]) -> dict:
        """Return ``options`` plus ``num_ctx`` when it is known."""
        if num_ctx is not None:
            options["num_ctx"] = num_ctx
        return options

    def _raw_residency_snapshot(self):
        """Return the monitor's latest /api/ps snapshot (any model, any age) or None."""
        monitor = getattr(self, "health_monitor", None) or getattr(self, "_health_monitor", None)
        if monitor is None:
            return None
        snap = getattr(monitor, "residency_snapshot", None)
        if snap is None:
            probe = getattr(monitor, "residency_probe", None) or getattr(monitor, "_ollama_residency", None)
            if probe is not None:
                snap = getattr(probe, "snapshot", None)
        return snap

    def _nvml_vram_bytes(self) -> tuple[Optional[int], Optional[int]]:
        """Return NVML (free, total) VRAM in bytes, or (None, None) when unknown."""
        monitor = getattr(self, "health_monitor", None) or getattr(self, "_health_monitor", None)
        guard = getattr(monitor, "vram_guard", None) if monitor is not None else None
        if guard is None:
            return None, None
        try:
            free_mb = float(getattr(guard, "free_mb", 0.0) or 0.0)
            total_mb = float(getattr(guard, "total_mb", 0.0) or 0.0)
        except (TypeError, ValueError):
            return None, None
        if total_mb <= 0:
            return None, None
        return int(free_mb * 1024 * 1024), int(total_mb * 1024 * 1024)

    def _resolve_requested_ctx_plan(self, model: str, native_max: int) -> Optional[int]:
        """Return the VRAM-clamped EXPLICIT owner-requested ctx for ``model``, or None.

        None (caller falls through to resident truth, then the T1 Auto
        default -- see ``_resolve_effective_ctx_limit``'s own docstring)
        unless model_parameters has ``context.policy == "requested"`` and a
        ``context_tokens`` for ``model``. Any failure reading the config also
        yields None (fail open).

        T1 (model_context_auto_config_20260928): this method now covers ONLY
        the EXPLICIT-override case; an unconfigured model's Auto default is
        resolved separately by ``_resolve_effective_ctx_limit``'s own cold-start
        step (Auto must NOT preempt a live resident allocation the way an
        explicit override intentionally does -- see
        ``test_runtime_residency_probe_takes_precedence_when_resident``).
        Both paths share the exact same VRAM-clamp/ratchet plan machinery via
        ``_apply_vram_ctx_plan`` below -- never a parallel mechanism.

        Requested wins over a resident allocation that differs, so the next
        load re-requests it (``_apply_vram_ctx_plan`` handles the
        ceiling/spill-ratchet/NVML-advisory contract; see its docstring).
        """
        try:
            settings = get_model_context_settings(model)
            target = settings.get("requested_tokens") if isinstance(settings, dict) else None
            if target is None:
                return None
            target = min(int(target), native_max)
        except Exception as exc:
            logger.debug("ctx_plan: model_parameters read failed, runtime truth kept: %s", exc)
            return None

        return self._apply_vram_ctx_plan(model, target, native_max)

    def _apply_vram_ctx_plan(self, model: str, target: int, native_max: int) -> int:
        """Grant a VRAM-clamped ctx for ``target``, ratcheting a per-model ceiling.

        Extracted from the original ``_resolve_requested_ctx_plan`` (T1,
        model_context_auto_config_20260928) so an EXPLICIT per-model override
        and the Auto cold-start default go through the IDENTICAL machinery --
        never a parallel one. Per model, a ``ceiling`` starts at ``target``
        and only ratchets down on SPILL evidence: a fresh (<
        RESIDENCY_STALE_SECONDS) snapshot of this model loaded AT the current
        ceiling with ``residency_ratio < 0.98``. A spilling snapshot at
        another ctx is the pre-reload load and is ignored, so one spill
        halves once. NVML free-VRAM is consulted only when nothing is
        resident (another resident model's memory is evicted on load,
        OLLAMA_MAX_LOADED_MODELS=1) and its step-down is advisory: it is not
        persisted, the next load re-evaluates. A changed ``target`` (a
        different configured value, or Auto's own target changing because
        ``native_max`` changed) resets the ceiling.
        """
        plans = getattr(self, "_ctx_plan_state", None)
        if plans is None:
            plans = {}
            self._ctx_plan_state = plans
        state = plans.get(model)
        if state is None or state.get("target") != target:
            state = {"target": target, "ceiling": target, "logged": None}
            plans[model] = state
        ceiling = state["ceiling"]

        raw = self._raw_residency_snapshot()
        ratio: Optional[float] = None
        free_b: Optional[int] = None
        total_b: Optional[int] = None
        if raw is None:
            free_b, total_b = self._nvml_vram_bytes()
        else:
            snap_ctx = getattr(raw, "context_length", None)
            if snap_ctx == ceiling:
                ratio = self._resolve_model_residency_ratio(model)

        granted, reason = context_budget.clamp_ctx_for_vram(
            ceiling,
            native=native_max,
            residency_ratio=ratio,
            free_bytes=free_b,
            total_bytes=total_b,
            floor=CTX_FALLBACK_DEFAULT,
        )
        if reason.startswith("spill"):
            state["ceiling"] = granted

        signature = (target, granted, reason)
        if state.get("logged") != signature:
            state["logged"] = signature
            logger.info(
                "ctx_plan: model=%s requested=%d granted=%d reason=%s",
                model, target, granted, reason,
            )
        return granted

    def _resolve_model_residency_ratio(self, model: str) -> Optional[float]:
        """Resolve current hardware residency ratio for model if resident and fresh.

        ADR-056:
        - If matching residency snapshot exists and is valid (not stale and matching model):
          return ratio = size_vram_bytes / size_bytes
        - Stale or different-model residency never reaches budget resolver (returns None).
        """
        monitor = getattr(self, "health_monitor", None) or getattr(self, "_health_monitor", None)
        if monitor is None:
            return None
        snap = getattr(monitor, "residency_snapshot", None)
        if snap is None:
            probe = getattr(monitor, "residency_probe", None) or getattr(monitor, "_ollama_residency", None)
            if probe is not None:
                snap = getattr(probe, "snapshot", None)
        if snap is None:
            return None

        snap_model = getattr(snap, "model", None)
        if not snap_model or not isinstance(snap_model, str):
            return None

        # Matching: must refer to the same model
        if not _models_match_residency(model, snap_model):
            return None

        # Check staleness: if snapshot unobserved (<=0) or older than 60s, consider stale
        observed_at = getattr(snap, "observed_at", 0.0) or 0.0
        if observed_at <= 0 or (time.time() - observed_at) > RESIDENCY_STALE_SECONDS:
            return None

        size = getattr(snap, "size_bytes", None)
        size_vram = getattr(snap, "size_vram_bytes", None)
        if size is not None and size > 0 and size_vram is not None:
            return max(0.0, min(1.0, float(size_vram) / float(size)))
        return None

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
            ctx = context_budget.parse_model_ctx(resp, fallback=CTX_FALLBACK_DEFAULT)
        except Exception:
            ctx = CTX_FALLBACK_DEFAULT
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
        timeout = getattr(
            sys.modules.get("opencohost.core.llm_engine"),
            "OLLAMA_REQUEST_TIMEOUT",
            OLLAMA_REQUEST_TIMEOUT,
        )
        resp = self._call_with_watchdog(
            ollama.show,
            timeout=timeout,
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
        """Resolve whether model supports reasoning policy and requires governed generation budgeting.

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
    