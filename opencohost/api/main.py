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

import concurrent.futures
import dataclasses
import os
import sqlite3
import threading
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Optional

import ollama
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from opencohost.api.dispatch import Dispatcher
from opencohost.api.engine_host import EngineHost
from opencohost.api.models import (
    AvatarConfigRequest,
    AvatarConfigResponse,
    ChatTurnRequest,
    CommandRequest,
    HealthResponse,
    HealthState,
    MemoriaStatsResponse,
    ModelsResponse,
    ObsConfigRequest,
    ObsConfigResponse,
    ObsTestResponse,
    ProfilesListResponse,
    StatusResponse,
    StreamChatLiveResponse,
    StreamConnectRequest,
    StreamLimitsRequest,
    SwitchProfileRequest,
    TTSConfigResponse,
)
from opencohost.avatar.avatar_config import VALID_STATES, load_avatar_config, save_avatar_config
from opencohost.avatar.obs_client import OBSClient
from opencohost.config.settings import (
    EDITORIAL_CARDS_DB,
    EXPERIMENTAL_HEAVY_TTS_ENABLED,
    MEMORIAS_DB,
    MEMORIAS_ENABLED,
    MODELS_CATALOG,
    _canonical_model_tag,
    load_piper_voice,
    load_tts_local_only,
    load_tts_speed,
    resolve_llm_tiers,
)
from opencohost.core.profiles import cargar_perfiles
from opencohost.smart_aggregator.url_parser import parse_chat_url

# GET /api/models external I/O bound — short client timeout so a stalled/
# unreachable Ollama daemon degrades the response instead of pinning a
# threadpool thread (design v2.1 Tier B resilience).
_OLLAMA_DISCOVERY_TIMEOUT_SECONDS = 2.5

# GET /api/memoria/stats sqlite reads — short, mirrors memoria_store.py's
# READ_TIMEOUT_SECONDS (bounded, fail-open to a zero count).
_STATS_DB_READ_TIMEOUT_SECONDS = 0.5


def _discover_ollama_models(timeout: float = _OLLAMA_DISCOVERY_TIMEOUT_SECONDS) -> list[str]:
    """Best-effort live Ollama discovery, bounded by a short client timeout.

    Mirrors the `ollama.Client(timeout=...)` pattern MotorVocalIA already
    uses for chat calls (llm_engine.py `_create_ollama_chat_client`).
    Degrades to `[]` on ANY failure (timeout, connection error, malformed
    response) — never raises, never hangs the request thread.
    """
    try:
        client = ollama.Client(timeout=timeout)
        response = client.list()
        models = getattr(response, "models", None)
        if models is None and isinstance(response, dict):
            models = response.get("models", [])
        tags = set()
        for model in models or []:
            if isinstance(model, dict):
                raw_tag = model.get("model") or model.get("name") or ""
            else:
                raw_tag = getattr(model, "model", None) or getattr(model, "name", None) or ""
            tag = _canonical_model_tag(raw_tag)
            if tag:
                tags.add(tag)
        return sorted(tags)
    except Exception:
        return []


def _count_sql(db_path: str, sql: str) -> int:
    """Bounded, fail-open COUNT(*) read. Never creates db_path as a side
    effect: a missing file (e.g. a standalone API process that never
    touched this store) returns 0 without connecting."""
    if not db_path or not os.path.exists(db_path):
        return 0
    try:
        with closing(sqlite3.connect(db_path, timeout=_STATS_DB_READ_TIMEOUT_SECONDS)) as conn:
            row = conn.execute(sql).fetchone()
            return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def _editorial_cards_by_status(db_path: str) -> dict:
    """Bounded, fail-open status->count aggregation. Same missing-file/error
    fail-open behavior as `_count_sql`; never returns row content."""
    if not db_path or not os.path.exists(db_path):
        return {}
    try:
        with closing(sqlite3.connect(db_path, timeout=_STATS_DB_READ_TIMEOUT_SECONDS)) as conn:
            rows = conn.execute("SELECT status, COUNT(*) FROM editorial_cards GROUP BY status").fetchall()
            return {status: count for status, count in rows}
    except sqlite3.Error:
        return {}

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


# POST /api/chat/turn value guard — mirrors `_validate_command_value`'s
# trust-boundary philosophy: reject empty/whitespace or unbounded text
# BEFORE it ever reaches the engine command_queue. 4000 chars is a sane cap
# for a single chat turn (process_context builds the full prompt from this).
_CHAT_TEXT_MAX_LENGTH = 4000


def _validate_chat_text(text: str) -> "str | None":
    """Returns None when `text` is acceptable, or a rejection reason string."""
    if not text.strip():
        return "text must not be empty or whitespace-only"
    if len(text) > _CHAT_TEXT_MAX_LENGTH:
        return f"text exceeds max length of {_CHAT_TEXT_MAX_LENGTH} characters"
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

# Tier-C config write-lock (WS3 slice 2): guards every PUT to avatar.yaml
# (OBS + avatar config share the one file) so a read-modify-write is atomic
# across concurrent requests. Reads do not need it — only read-modify-write.
_config_lock = threading.Lock()

# POST /api/obs/test bound: OBSClient.test_connection() (read-only file) makes
# a real blocking socket connection with no timeout knob of its own. Bound it
# here with a short-lived worker thread instead of touching that file.
_OBS_TEST_TIMEOUT_SECONDS = 5.0


def _test_obs_connection_bounded(client: OBSClient, timeout: float = _OBS_TEST_TIMEOUT_SECONDS):
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(client.test_connection)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            return False, "OBS connection test timed out"


def _obs_config_response(cfg) -> ObsConfigResponse:
    return ObsConfigResponse(
        enabled=cfg.obs.enabled,
        host=cfg.obs.host,
        port=cfg.obs.port,
        source=cfg.obs.source_name,
        password_set=bool(cfg.obs.password),
    )


def _avatar_config_response(cfg) -> AvatarConfigResponse:
    return AvatarConfigResponse(
        enabled=cfg.enabled,
        mode=cfg.mode,
        assets_folder=str(cfg.assets_folder),
        state_images={state: str(path) for state, path in cfg.state_images.items()},
    )


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

    @app.get("/api/models", response_model=ModelsResponse)
    def get_models(request: Request) -> ModelsResponse:
        host = request.app.state.host
        try:
            discovered = _discover_ollama_models()
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
            current_model=host.motor.current_model,
            tiers=tiers,
            active_tier=host.motor.active_llm_tier,
        )

    @app.get("/api/tts/config", response_model=TTSConfigResponse)
    def get_tts_config(request: Request) -> TTSConfigResponse:
        host = request.app.state.host
        return TTSConfigResponse(
            piper_voice=load_piper_voice(),
            local_only=load_tts_local_only(),
            speed=load_tts_speed(),
            engine=host.motor.motor_tts,
            heavy_available=EXPERIMENTAL_HEAVY_TTS_ENABLED,
        )

    @app.get("/api/memoria/stats", response_model=MemoriaStatsResponse)
    def get_memoria_stats(request: Request) -> MemoriaStatsResponse:
        host = request.app.state.host
        # R8: reuse the ONLY provenance gate (memory_inspector_snapshot
        # already applies _DIGEST_CAPTURE_SOURCES) — take counts only, never
        # touch entry["content"] / digest line text.
        snapshot = host.motor.memory_inspector_snapshot()
        session_turns = len(snapshot["entries"])
        digest_entries = snapshot["digest"]["line_count"]

        saved_memorias = 0
        pinned = 0
        if MEMORIAS_ENABLED:
            saved_memorias = _count_sql(MEMORIAS_DB, "SELECT COUNT(*) FROM memorias")
            pinned = _count_sql(MEMORIAS_DB, "SELECT COUNT(*) FROM memorias WHERE pinned = 1")

        return MemoriaStatsResponse(
            session_turns=session_turns,
            digest_entries=digest_entries,
            saved_memorias=saved_memorias,
            pinned=pinned,
            editorial_cards_by_status=_editorial_cards_by_status(EDITORIAL_CARDS_DB),
        )

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

    def _stream_state(agg) -> StreamChatLiveResponse:
        # R8: STATE + LIMITS ONLY — never read anything but the accessors
        # below (no message/user/text field exists on Aggregator's public
        # surface for this to leak from).
        source = agg._source
        connected = source is not None and source.is_connected()
        return StreamChatLiveResponse(
            connected=connected,
            platform=source.platform if source is not None else None,
            source_id=source._source_id if source is not None else None,
            threshold_per_second=agg.activity.threshold_per_second,
            cooldown_seconds=agg.activity.cooldown_seconds,
            max_messages_per_user=agg._spam_max_messages,
            filter_policy=agg.get_filter_policy(),
        )

    @app.get("/api/stream/chat-live", response_model=StreamChatLiveResponse)
    def get_stream_chat_live(request: Request):
        agg = getattr(request.app.state.host, "aggregator", None)
        if agg is None:
            return JSONResponse(status_code=503, content={"detail": "stream_unavailable"})
        return _stream_state(agg)

    @app.post("/api/stream/chat-live/connect", response_model=StreamChatLiveResponse)
    def post_stream_connect(request: Request, body: StreamConnectRequest):
        host = request.app.state.host
        agg = getattr(host, "aggregator", None)
        if agg is None:
            return JSONResponse(status_code=503, content={"detail": "stream_unavailable"})
        try:
            parsed = parse_chat_url(body.url)
        except ValueError:
            return JSONResponse(status_code=422, content={"detail": "invalid_url"})
        if not host.aggregator_lock.acquire(blocking=False):
            return JSONResponse(status_code=409, content={"detail": "busy"})
        try:
            agg.connect(parsed["source_id"], platform=parsed["platform"])
        finally:
            host.aggregator_lock.release()
        return _stream_state(agg)

    @app.post("/api/stream/chat-live/disconnect", response_model=StreamChatLiveResponse)
    def post_stream_disconnect(request: Request):
        host = request.app.state.host
        agg = getattr(host, "aggregator", None)
        if agg is None:
            return JSONResponse(status_code=503, content={"detail": "stream_unavailable"})
        # Single-flight with connect: both mutate agg._source, so serialize
        # them on the same lock (mirrors post_stream_connect) to avoid a
        # disconnect racing an in-flight connect on _source.
        if not host.aggregator_lock.acquire(blocking=False):
            return JSONResponse(status_code=409, content={"detail": "busy"})
        try:
            agg.disconnect()
        finally:
            host.aggregator_lock.release()
        return _stream_state(agg)

    @app.put("/api/stream/chat-live/limits", response_model=StreamChatLiveResponse)
    def put_stream_limits(request: Request, body: StreamLimitsRequest):
        agg = getattr(request.app.state.host, "aggregator", None)
        if agg is None:
            return JSONResponse(status_code=503, content={"detail": "stream_unavailable"})
        if body.threshold_per_second is not None or body.cooldown_seconds is not None:
            agg.set_activity_limits(
                threshold_per_second=body.threshold_per_second,
                cooldown_seconds=body.cooldown_seconds,
            )
        if body.max_messages_per_user is not None:
            agg.set_spam_limits(max_messages_per_user=body.max_messages_per_user)
        if body.filter_policy is not None:
            try:
                agg.set_filter_policy(body.filter_policy)
            except ValueError:
                return JSONResponse(status_code=422, content={"detail": "invalid_filter_policy"})
        return _stream_state(agg)

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

    @app.post("/api/chat/turn")
    def post_chat_turn(request: Request, body: ChatTurnRequest):
        # R8-CRITICAL: this handler must never return `body.text` (or any
        # derived dialogue) in the response, and must never log it. The ack
        # below carries only accepted/command_id/status/state_version.
        rejection = _validate_chat_text(body.text)
        if rejection is not None:
            return JSONResponse(status_code=422, content={"detail": rejection})
        key = request.headers.get("Idempotency-Key") or body.idempotency_key
        dispatcher = request.app.state.dispatcher
        result = dispatcher.dispatch("process_context", body.text, key)
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

    @app.get("/api/obs/config", response_model=ObsConfigResponse)
    def get_obs_config() -> ObsConfigResponse:
        return _obs_config_response(load_avatar_config())

    @app.put("/api/obs/config", response_model=ObsConfigResponse)
    def put_obs_config(body: ObsConfigRequest) -> ObsConfigResponse:
        with _config_lock:
            cfg = load_avatar_config()
            if body.enabled is not None:
                cfg.obs.enabled = body.enabled
            if body.host is not None:
                cfg.obs.host = body.host
            if body.port is not None:
                cfg.obs.port = body.port
            if body.source is not None:
                cfg.obs.source_name = body.source
            if body.password is not None:
                cfg.obs.password = body.password
            save_avatar_config(cfg)
            return _obs_config_response(cfg)

    @app.post("/api/obs/test", response_model=ObsTestResponse)
    def post_obs_test(body: Optional[ObsConfigRequest] = None) -> ObsTestResponse:
        cfg = load_avatar_config()
        obs_cfg = cfg.obs
        if body is not None:
            obs_cfg = dataclasses.replace(
                obs_cfg,
                host=body.host if body.host is not None else obs_cfg.host,
                port=body.port if body.port is not None else obs_cfg.port,
                password=body.password if body.password is not None else obs_cfg.password,
            )
        client = OBSClient(config=obs_cfg, assets_folder=cfg.assets_folder)
        ok, message = _test_obs_connection_bounded(client)
        return ObsTestResponse(ok=ok, error=None if ok else message)

    @app.get("/api/avatar/config", response_model=AvatarConfigResponse)
    def get_avatar_config() -> AvatarConfigResponse:
        return _avatar_config_response(load_avatar_config())

    @app.put("/api/avatar/config", response_model=AvatarConfigResponse)
    def put_avatar_config(body: AvatarConfigRequest):
        if body.state_images is not None:
            unknown = sorted(set(body.state_images) - VALID_STATES)
            if unknown:
                return JSONResponse(
                    status_code=422, content={"detail": f"unknown avatar state(s): {unknown}"}
                )
        with _config_lock:
            cfg = load_avatar_config()
            if body.enabled is not None:
                cfg.enabled = body.enabled
            if body.mode is not None:
                cfg.mode = body.mode
            if body.state_images is not None:
                new_state_images = dict(cfg.state_images)
                for state, path in body.state_images.items():
                    new_state_images[state] = Path(path)
                cfg.state_images = new_state_images
            save_avatar_config(cfg)
            return _avatar_config_response(cfg)

    return app


app = create_app()
