"""GET /api/health, /api/status, /api/models
(moved verbatim from main.py, refactor_core_api_20260802 B4).

Handler bodies are byte-identical to the originals. The only seam changes:
`_discover_ollama_models()` and `load_provider_config()` calls in
`get_models` now go through `deps` (both are monkeypatched directly on
`opencohost.api.main` by tests/test_api_reads.py -- a plain top-level import
here would bind the pre-patch function object and silently ignore the
monkeypatch). `_display_model`, `_derive_session_mode`, `_ctx_telemetry_out`
are plain, never-monkeypatched module functions living in main.py, so they
are imported directly.
"""

import dataclasses
from typing import Optional

from fastapi import APIRouter, Request

from opencohost.api import deps
from opencohost.api.main import _ctx_telemetry_out, _derive_session_mode, _display_model
from opencohost.api.models import HealthResponse, HealthState, ModelsResponse, StatusResponse
from opencohost.config.settings import MODELS_CATALOG, resolve_llm_tiers
from opencohost.smart_aggregator.kira_agenda_controller import AgendaState

router = APIRouter()


@router.get("/api/health", response_model=HealthResponse)
def get_health(request: Request) -> HealthResponse:
    # Fast liveness probe: no engine work, no queue touch. Truthful even
    # if the host has no motor yet (fresh process / no feeder).
    motor = getattr(request.app.state.host, "motor", None)
    is_alive = getattr(motor, "is_alive", None)
    engine_alive = bool(is_alive()) if callable(is_alive) else False
    return HealthResponse(status="ok", engine_alive=engine_alive)


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
        return ModelsResponse(
            catalog={},
            discovered=[active_model] if active_model else [],
            current_model=active_model,
            tiers={},
            active_tier="cloud",
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
    return ModelsResponse(
        catalog=MODELS_CATALOG,
        discovered=discovered,
        current_model=_display_model(host),
        tiers=tiers,
        active_tier=host.motor.active_llm_tier,
    )
