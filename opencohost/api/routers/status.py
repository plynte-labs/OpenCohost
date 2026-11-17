"""GET /api/health, /api/status, /api/models
(moved verbatim from main.py, refactor_core_api_20260802 B4).

Handler bodies are byte-identical to the originals. The only seam changes:
`_discover_ollama_models()` and `load_provider_config()` calls in
`get_models` now go through `deps` (both are monkeypatched directly on
`opencohost.api.main` by tests/test_api_reads.py -- a plain top-level import
here would bind the pre-patch function object and silently ignore the
monkeypatch). `_display_model`, `_derive_session_mode`, `_ctx_telemetry_out`
are plain, never-monkeypatched functions living in `opencohost.api.shared`
(refactor_core_api_20260802 B5 Part B -- moved out of main.py to break the
routers<->main module-level import cycle), so they are imported directly.
"""

import dataclasses
import logging
from typing import Any, Optional

from fastapi import APIRouter, Request

from opencohost.api import deps
from opencohost.api.shared import (
    _ctx_telemetry_out,
    _derive_session_mode,
    _display_model,
    engine_startup_state,
)
from opencohost.api.models import (
    HealthResponse,
    HealthState,
    InferenceRuntimeState,
    ModelContextConfig,
    ModelReasoningConfig,
    ModelsResponse,
    StatusResponse,
    UpdateModelContextRequest,
    UpdateModelReasoningRequest,
)
from opencohost.config.model_parameters import (
    get_model_context_settings,
    get_model_reasoning_settings,
    resolve_auto_context_tokens,
    update_model_context_settings,
    update_model_reasoning_settings,
)
from opencohost.config.settings import MODELS_CATALOG, resolve_llm_tiers
from opencohost.core import install_identity
from opencohost.smart_aggregator.kira_agenda_controller import AgendaState

logger = logging.getLogger("OpenCohost")

router = APIRouter()


@router.get("/api/health", response_model=HealthResponse)
def get_health(request: Request) -> HealthResponse:
    # Fast liveness probe: no engine work, no queue touch. Truthful even
    # if the host has no motor yet (fresh process / no feeder).
    motor = getattr(request.app.state.host, "motor", None)
    is_alive = getattr(motor, "is_alive", None)
    engine_alive = bool(is_alive()) if callable(is_alive) else False
    return HealthResponse(
        status="ok",
        engine_alive=engine_alive,
        engine_state=engine_startup_state(request.app.state.host),
        build_version=install_identity.build_version(),
        install_id=install_identity.install_id(),
    )


@router.get("/api/status", response_model=StatusResponse)
def get_status(request: Request) -> StatusResponse:
    host = request.app.state.host
    # F4 (runtime_findings_batch_20260731 1.3): the engine's LIVE
    # effective posture, never `load_provider_config()` — that disk read
    # is exactly what let this endpoint keep reporting a stale cloud
    # provider/model through an active `_cloud_fallback_active` fallback.
    provider_state = host.motor.provider_runtime_state()
    is_ready = host.motor.is_ready
    is_speaking = host.motor.is_speaking
    is_processing = host.motor.is_processing
    # F4: derive coarse avatar state (motor has no AvatarStateBridge).
    # Mirrors the FE deriveAvatarState fallback so both agree.
    if is_speaking:
        avatar_state = "speaking"
    elif is_processing:
        avatar_state = "thinking"
    elif not is_ready:
        avatar_state = "sleeping"
    else:
        avatar_state = "idle"
    # FIX-B: real OBS connection state from the API-hosted ObsRuntime.
    # Guarded (getattr + try/except) so a host double without a runtime, or
    # a runtime error, degrades to None rather than failing the probe.
    runtime = getattr(host, "obs_runtime", None)
    obs_connected: Optional[bool] = None
    if runtime is not None:
        try:
            obs_connected = bool(runtime.is_connected)
        except Exception:
            obs_connected = None
    # Unit 2.5 (F13): session_mode is DERIVED here, every call — see
    # _derive_session_mode's docstring for the rule and its accepted
    # risks. `agenda is None` (no headless controller wired) counts as
    # OFF: there is no agenda to be "active", so it falls through to the
    # post-agenda/inactiva read on the booleans below, same as a real OFF.
    agenda = getattr(host, "agenda", None)
    agenda_off = agenda is None or agenda.state == AgendaState.OFF
    llm_generating = bool(getattr(host.motor, "llm_generating", False))
    pending_commands_count = host.motor.command_queue.qsize()
    session_mode = _derive_session_mode(
        agenda_off=agenda_off,
        is_speaking=is_speaking,
        is_processing=is_processing,
        llm_generating=llm_generating,
        pending_commands_count=pending_commands_count,
    )
    return StatusResponse(
        is_ready=is_ready,
        current_model=_display_model(host, provider_state),
        is_speaking=is_speaking,
        is_processing=is_processing,
        active_profile=host.motor._current_profile_name,
        active_profile_id=getattr(host.motor, "_current_profile_id", None),
        health=HealthState(**dataclasses.asdict(host.monitor.state)),
        state_version=request.app.state.dispatcher.state_version,
        ollama_warming=getattr(host, "ollama_warming", False),
        session_mode=session_mode,
        llm_generating=llm_generating,
        pending_commands_count=pending_commands_count,
        avatar_state=avatar_state,
        provider=provider_state["provider"],
        transport=provider_state["transport"],
        fallback_active=provider_state["fallback_active"],
        fallback_reason=provider_state.get("fallback_reason"),
        next_cloud_probe_in_seconds=provider_state.get("next_cloud_probe_in_seconds"),
        ctx_telemetry=_ctx_telemetry_out(host.motor),
        obs_connected=obs_connected,
    )


def _is_reasoning_model(host: Any, model: Optional[str]) -> bool:
    if not model:
        return False
    motor = getattr(host, "motor", None)
    resolver = getattr(motor, "_resolve_reasoning_classification", None)
    if callable(resolver):
        try:
            return bool(resolver(model))
        except Exception:
            pass
    name = model.lower()
    return any(marker in name for marker in ("qwen3", "e2b", "e4b", "think"))


def _extract_inference_runtime_state(host, active_model: Optional[str]) -> Optional[InferenceRuntimeState]:
    if not host or not getattr(host, "motor", None):
        return None
    model_name = active_model or getattr(host.motor, "current_model", "unknown")
    monitor = getattr(host.motor, "health_monitor", None) or getattr(host.motor, "_health_monitor", None)
    snap = getattr(monitor, "residency_snapshot", None) if monitor else None

    allocated_ctx = getattr(snap, "context_length", 0) if snap else 0
    if allocated_ctx <= 0:
        allocated_ctx = getattr(host.motor, "_model_ctx_limit", {}).get(model_name, 4096)

    tracker = getattr(host.motor, "inference_telemetry", None)
    ewma_tps = 0.0
    cal_state = "COLD"
    if tracker:
        prof = tracker.get_profile("local", model_name, allocated_context=allocated_ctx)
        ewma_tps = prof.ewma_tps
        cal_state = prof.state.value

    res_ratio = None
    spill = None
    if snap and getattr(snap, "size_bytes", 0) > 0:
        res_ratio = min(1.0, float(snap.size_vram_bytes) / float(snap.size_bytes))
        spill = max(0, snap.size_bytes - snap.size_vram_bytes)

    r_settings = get_model_reasoning_settings(model_name)
    # T2 (controls-cleanup-adaptive-reasoning-20260928): None under Auto --
    # this is a static config read, not a live read of what the adaptive
    # engine actually picked for the last turn, so there is nothing honest
    # to report as "effective" when there is no manual cap. See
    # ModelReasoningConfig / InferenceRuntimeState docstrings (api/models.py).
    effective_budget = r_settings.get("budget_tokens")

    return InferenceRuntimeState(
        effective_budget=effective_budget,
        allocated_context=allocated_ctx,
        calibration_state=cal_state,
        ewma_tps=ewma_tps,
        residency_ratio=res_ratio,
        spill_bytes=spill,
        clamp_reason=None,
    )


def _model_context_config_out(host: Any, model: Optional[str]) -> ModelContextConfig:
    """Build ``ModelContextConfig`` for ``model`` (T2, model_context_auto_config_20260928).

    Read-only, no side effects: ``native_context`` comes only from the
    engine's already-cached ``_model_ctx_limit`` (never a fresh
    ``ollama.show`` call from a GET handler), so it is ``None`` until some
    other path (warm-up, chat, discovery) has populated the cache. Same for
    ``effective_context`` -- computed only when a cached native ctx exists,
    via the engine's own ``_resolve_effective_ctx_limit`` (pure/read-only:
    it consults health-monitor/NVML state but never calls Ollama itself).
    """
    if not model:
        return ModelContextConfig(model="")
    try:
        settings = get_model_context_settings(model)
    except Exception:
        settings = {"policy": "runtime_truth", "requested_tokens": None}
    native_context: Optional[int] = None
    effective_context: Optional[int] = None
    motor = getattr(host, "motor", None) if host is not None else None
    if motor is not None:
        native_context = getattr(motor, "_model_ctx_limit", {}).get(model)
        resolver = getattr(motor, "_resolve_effective_ctx_limit", None)
        if native_context and callable(resolver):
            try:
                effective_context = resolver(model, native_context)
            except Exception:
                effective_context = None
    return ModelContextConfig(
        model=model,
        context_tokens=settings.get("requested_tokens") if isinstance(settings, dict) else None,
        auto_context_tokens=resolve_auto_context_tokens(native_context),
        native_context=native_context,
        effective_context=effective_context,
        policy=settings.get("policy", "runtime_truth") if isinstance(settings, dict) else "runtime_truth",
    )


@router.get("/api/models", response_model=ModelsResponse)
def get_models(request: Request) -> ModelsResponse:
    host = request.app.state.host
    provider_cfg = deps.load_provider_config()
    active_provider = provider_cfg.get("active_provider", "local")
    if active_provider != "local":
        # Cloud active (multi_provider_llm_20260723 Phase 5, spec D): no
        # `ollama.list` discovery, no download/install catalog (VRAM
        # tiers are a local-only concept) — only the ACTIVE profile's
        # own model, read from `profiles[active_provider]`, never
        # another profile's. Shared with /api/status via _display_model
        # so the two endpoints can never report different models again.
        # NOTE: _display_model now reads the engine's LIVE provider state
        # (1.3), not this `provider_cfg` disk read — the two can disagree
        # during an active `_cloud_fallback_active` fallback; the branch
        # selection above (whether to skip Ollama discovery) is still the
        # persisted config's call, deliberately unchanged by this unit.
        active_model = _display_model(host)
        r_settings = get_model_reasoning_settings(active_model)
        is_reasoning = _is_reasoning_model(host, active_model)
        return ModelsResponse(
            catalog={},
            discovered=[active_model] if active_model else [],
            current_model=active_model,
            tiers={},
            active_tier="cloud",
            is_reasoning_active=is_reasoning,
            reasoning_config=ModelReasoningConfig(**r_settings),
            runtime_state=_extract_inference_runtime_state(host, active_model),
            # T1 says nothing about cloud providers (CLOUD_CTX_BUDGET path,
            # unchanged); Auto/context config is a local-Ollama-only concept.
            context_config=ModelContextConfig(model=active_model or ""),
        )
    try:
        discovered = deps.discover_ollama_models()
    except Exception:
        # _discover_ollama_models already fails open internally; this
        # outer guard is a second layer so a mocked/monkeypatched or
        # future-refactored discovery call can never 500 this endpoint.
        discovered = []
    # Pass our bounded `discovered` in so resolve_llm_tiers never falls
    # back to its own unbounded, no-timeout `_discover_installed_model_tags()`
    # internally (settings.py) — one bounded discovery call per request.
    tiers = resolve_llm_tiers(installed_model_tags=discovered)
    current_model = _display_model(host)
    r_settings = get_model_reasoning_settings(current_model)
    is_reasoning = _is_reasoning_model(host, current_model)
    return ModelsResponse(
        catalog=MODELS_CATALOG,
        discovered=discovered,
        current_model=current_model,
        tiers=tiers,
        active_tier=host.motor.active_llm_tier,
        is_reasoning_active=is_reasoning,
        reasoning_config=ModelReasoningConfig(**r_settings),
        runtime_state=_extract_inference_runtime_state(host, current_model),
        context_config=_model_context_config_out(host, current_model),
    )


@router.put("/api/models/reasoning", response_model=ModelReasoningConfig)
def update_model_reasoning(
    request: Request, body: UpdateModelReasoningRequest
) -> ModelReasoningConfig:
    """T2 (controls-cleanup-adaptive-reasoning-20260928): ``budget_tokens``
    now carries three distinct states through one Optional Pydantic field,
    resolved via ``body.model_fields_set`` (the JSON keys actually present
    in the request, not merely non-None):
      - key ABSENT from the body -> leave the current value untouched (the
        pre-existing partial-update convention for this endpoint).
      - key present, value ``null`` -> explicit "clear to Auto"
        (``clear_budget=True``; mirrors PUT /api/models/context's own
        null-clears-to-Auto convention).
      - key present, a number -> the new manual cap.
    """
    budget_provided = "budget_tokens" in body.model_fields_set
    clear_budget = budget_provided and body.budget_tokens is None
    updated = update_model_reasoning_settings(
        enabled=body.enabled,
        budget_tokens=body.budget_tokens if (budget_provided and not clear_budget) else None,
        preset=body.preset,
        model=body.model,
        clear_budget=clear_budget,
    )
    return ModelReasoningConfig(**updated)


@router.put("/api/models/context", response_model=ModelContextConfig)
def update_model_context(
    request: Request, body: UpdateModelContextRequest
) -> ModelContextConfig:
    """T2 (model_context_auto_config_20260928). ``context_tokens=None``
    clears back to Auto. Validation follows the reasoning endpoint's own
    convention: an out-of-range value is silently CLAMPED
    (``_sanitize_context_tokens``), never a 422 -- only a wrong JSON type
    (e.g. a string) 422s, via ordinary Pydantic validation.

    Applying to the ACTIVE model (forcing a live Ollama reload) is
    best-effort: a motor that predates ``request_model_context_reload``
    (or any glitch while forcing it) never fails this write -- the config is
    already durably persisted by this point, and the new value still
    applies the next time that model loads.
    """
    update_model_context_settings(model=body.model, context_tokens=body.context_tokens)
    host = request.app.state.host
    motor = getattr(host, "motor", None)
    reload_fn = getattr(motor, "request_model_context_reload", None) if motor is not None else None
    if callable(reload_fn):
        try:
            reload_fn(body.model)
        except Exception as exc:
            logger.warning("Context reload glitch for %s (config already saved): %s", body.model, exc)
    return _model_context_config_out(host, body.model)
