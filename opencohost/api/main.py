"""Standalone FastAPI app exposing Kira's engine control surface (Phase 1).

Run form (REQUIRED — see design v2.1 B-SF2):

    uvicorn opencohost.api.main:app --host 127.0.0.1 --port 8765 --workers 1

WARNING: binding `--host 0.0.0.0` exposes the engine control surface to the
LAN. CORS only defends BROWSER callers — it does nothing against curl or a
script hitting the port directly. Keep this process on loopback unless a
separate authenticating proxy sits in front of it. `--workers` MUST stay at
1: a second worker means a second `MotorVocalIA` (second Ollama load +
audio device grab) racing the first. `EngineHost`'s lockfile enforces this
across processes; the `WEB_CONCURRENCY`/`UVICORN_WORKERS` env check and the
in-process `_host_active` guard below are the best-effort in-process layers.

R8 (carried non-negotiable, binds Phase 2+): no raw viewer chat may ever be
exposed over HTTP. Any future `/api/memorias` or `/api/history` endpoint
MUST reuse the T1 provenance gate verbatim — the `_DIGEST_CAPTURE_SOURCES`
allowlist (opencohost/core/llm_engine.py) and the `memory_inspector_snapshot`
policy are the ONLY provenance gates; never re-derived.

This package NEVER imports `opencohost.ui` or `customtkinter`. The engine
is constructed only inside `lifespan()` — never at import time — so
importing this module has zero side effects on hardware/VRAM/Ollama.
"""

import dataclasses
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from opencohost.api.dispatch import Dispatcher
from opencohost.api.engine_host import EngineHost
from opencohost.api.models import (
    CommandRequest,
    HealthResponse,
    HealthState,
    ProfilesListResponse,
    StatusResponse,
    SwitchProfileRequest,
)
from opencohost.core.profiles import cargar_perfiles

# Server-side verb whitelist for POST /api/commands — exactly the verbs
# MotorVocalIA._dispatch_command (opencohost/core/llm_engine.py) handles.
# A command NOT in this set is rejected before it ever reaches the queue.
_COMMAND_WHITELIST = frozenset(
    {
        "clear_history",
        "set_tts_local_only",
        "set_tts_speed",
        "set_piper_voice",
        "set_motor_tts",
        "switch_model",
        "switch_llm_tier",
    }
)


def _engine_command_payload(command: str, payload: dict):
    """Translate the wire-level `{payload: dict}` body into the raw scalar
    (or None) each engine verb actually consumes — `_dispatch_command`
    reads `payload` directly as a str/float/bool/None per verb, never a
    dict. `"value"` is the one wire key used until a verb needs more than
    one field.
    """
    if command == "clear_history":
        return None
    return payload.get("value")


def _validate_command_value(command: str, value) -> "str | None":
    """Validate `value` against the per-verb contract `_dispatch_command`
    (opencohost/core/llm_engine.py) actually expects. Returns None when the
    value is acceptable, or a rejection reason string when it is not.

    This is the trust-boundary fix for the DoS where an uncaught `TypeError`
    (e.g. `float(None)` for `set_tts_speed`) inside `_dispatch_command`
    kills the engine command-loop thread — that call sits outside the
    `queue.Empty` try in `run()`. Rejecting bad values here means they are
    never enqueued in the first place.
    """
    if command == "clear_history":
        return None
    if command == "set_tts_speed":
        try:
            float(value)
        except (TypeError, ValueError):
            return "set_tts_speed requires a numeric value"
        return None
    if command in ("switch_model", "switch_llm_tier", "set_piper_voice", "set_motor_tts"):
        if not isinstance(value, str):
            return f"{command} requires a non-None string value"
        return None
    if command == "set_tts_local_only":
        if not isinstance(value, bool):
            return "set_tts_local_only requires a boolean value"
        return None
    return None


_DEFAULT_CORS_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:1420",
    "http://127.0.0.1:1420",
    "http://tauri.localhost",
    "tauri://localhost",
]

# In-process double-start guard. Layered defense, weakest to strongest:
# lockfile (cross-process, primary, in engine_host.py) -> this flag
# (in-process) -> env var check below (best-effort) -> docs.
_host_active = False


def _check_single_worker() -> None:
    for var in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        value = os.environ.get(var, "").strip()
        if value and value not in ("0", "1"):
            raise RuntimeError(f"opencohost.api requires a single worker (found {var}={value!r})")


def create_app(host_factory=EngineHost, cors_origins=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        global _host_active
        _check_single_worker()
        if _host_active:
            raise RuntimeError("An OpenCohost API engine host is already active in this process")
        _host_active = True
        host = host_factory()
        try:
            host.start()
            app.state.host = host
            app.state.dispatcher = Dispatcher(host.motor.command_queue)
            yield
        finally:
            host.stop()
            _host_active = False

    app = FastAPI(lifespan=lifespan, debug=False)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins if cors_origins is not None else _DEFAULT_CORS_ORIGINS,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Idempotency-Key"],
        allow_credentials=False,
    )

    @app.get("/api/health", response_model=HealthResponse)
    def get_health(request: Request) -> HealthResponse:
        # Fast liveness probe: no engine work, no queue touch. Truthful even
        # if the host has no motor yet (fresh process / no feeder).
        motor = getattr(request.app.state.host, "motor", None)
        is_alive = getattr(motor, "is_alive", None)
        engine_alive = bool(is_alive()) if callable(is_alive) else False
        return HealthResponse(status="ok", engine_alive=engine_alive)

    @app.get("/api/status", response_model=StatusResponse)
    def get_status(request: Request) -> StatusResponse:
        host = request.app.state.host
        return StatusResponse(
            is_ready=host.motor.is_ready,
            current_model=host.motor.current_model,
            is_speaking=host.motor.is_speaking,
            is_processing=host.motor.is_processing,
            active_profile=host.motor._current_profile_name,
            health=HealthState(**dataclasses.asdict(host.monitor.state)),
            state_version=request.app.state.dispatcher.state_version,
        )

    @app.get("/api/perfiles", response_model=ProfilesListResponse)
    def list_perfiles() -> ProfilesListResponse:
        perfiles = cargar_perfiles()
        if not isinstance(perfiles, dict):
            return ProfilesListResponse(profiles=[])
        return ProfilesListResponse(profiles=list(perfiles.keys()))

    @app.post("/api/perfiles/switch")
    def switch_profile(request: Request, body: SwitchProfileRequest):
        key = request.headers.get("Idempotency-Key") or body.idempotency_key
        perfiles = cargar_perfiles()
        if not isinstance(perfiles, dict) or body.name not in perfiles:
            return JSONResponse(status_code=404, content={"detail": "profile not found"})
        data = dict(perfiles[body.name])
        data["_profile_name"] = body.name
        result = request.app.state.dispatcher.dispatch("set_profile", data, key)
        if result.state in ("accepted", "replay"):
            return {"accepted": True, "command_id": result.command_id, "status": "queued"}
        if result.state == "conflict":
            return JSONResponse(status_code=409, content={"accepted": False, "reason": "conflict"})
        return JSONResponse(status_code=429, content={"accepted": False, "reason": "queue_full"})

    @app.post("/api/commands")
    def post_command(request: Request, body: CommandRequest):
        if body.command not in _COMMAND_WHITELIST:
            return JSONResponse(status_code=422, content={"detail": "unknown command"})
        engine_payload = _engine_command_payload(body.command, body.payload)
        rejection = _validate_command_value(body.command, engine_payload)
        if rejection is not None:
            return JSONResponse(status_code=422, content={"detail": rejection})
        key = request.headers.get("Idempotency-Key") or body.idempotency_key
        dispatcher = request.app.state.dispatcher
        result = dispatcher.dispatch(body.command, engine_payload, key)
        if result.state in ("accepted", "replay"):
            return {
                "accepted": True,
                "command_id": result.command_id,
                "status": "queued",
                "state_version": dispatcher.state_version,
            }
        if result.state == "conflict":
            return JSONResponse(status_code=409, content={"accepted": False, "reason": "conflict"})
        return JSONResponse(status_code=429, content={"accepted": False, "reason": "queue_full"})

    return app


app = create_app()
