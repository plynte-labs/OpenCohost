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
import mimetypes
import os
import re
import sqlite3
import threading
import uuid
from contextlib import asynccontextmanager, closing
from pathlib import Path
from typing import Optional

import ollama
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from opencohost.api.dispatch import Dispatcher
from opencohost.api.engine_host import EngineHost
from opencohost.api.models import (
    AgendaMetrics,
    AgendaResponse,
    AgendaSessionActionRequest,
    AgendaSessionRequest,
    AgendaSessionSettings,
    AgendaTopicActionRequest,
    AgendaTopicOut,
    AgendaTopicRequest,
    AvatarConfigRequest,
    AvatarConfigResponse,
    ChatLastReplyResponse,
    ChatTurnRequest,
    CommandRequest,
    HealthResponse,
    HealthState,
    MemoriaDeleteRequest,
    MemoriaFlagsRequest,
    MemoriaListResponse,
    MemoriaListItem,
    MemoriaCaptureRequest,
    MemoriaMutationResponse,
    MemoriaPurgeRequest,
    MemoriaPurgeResponse,
    MemoriaStatsResponse,
    MemoriaUpdateRequest,
    ModelsResponse,
    MusicFadeRequest,
    MusicImportRequest,
    MusicImportResponse,
    MusicLibraryResponse,
    MusicMoodRequest,
    MusicMoodResponse,
    MusicStateResponse,
    MusicTrackOut,
    ObsConfigRequest,
    ObsConfigResponse,
    ObsTestResponse,
    ProfileCreateRequest,
    ProfileDetailResponse,
    ProfileUpdateRequest,
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
from opencohost.core.memoria_store import MemoriaStore
from opencohost.core.music_library import (
    ALLOWED_AUDIO_EXTENSIONS,
    KNOWN_MOODS,
    is_supported_audio_path,
)
from opencohost.core.profiles import cargar_perfiles, guardar_perfiles
from opencohost.smart_aggregator.url_parser import parse_chat_url

# GET /api/models external I/O bound — short client timeout so a stalled/
# unreachable Ollama daemon degrades the response instead of pinning a
# threadpool thread (design v2.1 Tier B resilience).
_OLLAMA_DISCOVERY_TIMEOUT_SECONDS = 2.5

# GET /api/memoria/stats sqlite reads — short, mirrors memoria_store.py's
# READ_TIMEOUT_SECONDS (bounded, fail-open to a zero count).
_STATS_DB_READ_TIMEOUT_SECONDS = 0.5

# POST /api/memoria/purge — mirrors memoria_store.py's WRITE_TIMEOUT_SECONDS.
_MEMORIA_WRITE_TIMEOUT_SECONDS = 1.0


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


def _list_memoria_metadata(db_path: str, profile_id: str) -> list[dict]:
    """Bounded, fail-open metadata read for GET /api/memoria/list (R8).

    The SELECT names ONLY metadata columns — title/content are never read
    off disk in the first place, so there is nothing to filter out of the
    row before it reaches MemoriaListItem (defense in depth, not just a
    field omission on the Pydantic model)."""
    if not db_path or not os.path.exists(db_path):
        return []
    try:
        with closing(sqlite3.connect(db_path, timeout=_STATS_DB_READ_TIMEOUT_SECONDS)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, created_at, updated_at, revision, pinned, private, inactive FROM memorias "
                "WHERE profile_id = ? ORDER BY updated_at DESC, id ASC",
                (profile_id,),
            ).fetchall()
            return [dict(row) for row in rows]
    except sqlite3.Error:
        return []


def _purge_memoria(db_path: str, profile_id: str) -> int:
    """Bounded, fail-open hard-delete for POST /api/memoria/purge."""
    if not db_path or not os.path.exists(db_path):
        return 0
    try:
        with closing(sqlite3.connect(db_path, timeout=_MEMORIA_WRITE_TIMEOUT_SECONDS)) as conn, conn:
            cur = conn.execute("DELETE FROM memorias WHERE profile_id = ?", (profile_id,))
            return cur.rowcount
    except sqlite3.Error:
        return 0


# Lazy module-level MemoriaStore singleton (design D1). MemoriaStore.__init__
# mkdirs + runs CREATE TABLE / PRAGMA writes, so it must NEVER be constructed
# at import time (module contract: importing has zero side effects). One shared
# instance is thread-safe across FastAPI's threadpool — the store opens a fresh
# connection per operation and guards its warn-once state with its own lock —
# and preserves that warn-once episode state (a per-request instance would
# reset it every call).
_memoria_store: "MemoriaStore | None" = None
_memoria_store_lock = threading.Lock()


def _get_memoria_store() -> MemoriaStore:
    global _memoria_store
    with _memoria_store_lock:
        if _memoria_store is None:
            _memoria_store = MemoriaStore(MEMORIAS_DB)
        return _memoria_store

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

# POST /api/memoria/update length caps — mirror _CHAT_TEXT_MAX_LENGTH's
# trust-boundary bound. update_row whitespace-normalizes but never validates
# length/emptiness, so both checks are API-side before the write.
_MEMORIA_TITLE_MAX_LENGTH = 200
_MEMORIA_CONTENT_MAX_LENGTH = 4000


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

# profiles.json write-lock (WU5): guards every read-modify-write of the
# profiles file (create/update/delete). Deliberately NOT `_config_lock`,
# which is avatar.yaml-specific — a separate file gets a separate lock (D4).
_profiles_lock = threading.Lock()

# POST/PUT /api/perfiles bounds — profiles.json is loaded whole per request,
# so cap the write to keep a single profile from ballooning the file.
_PROFILE_NAME_MAX_LENGTH = 100
_PROFILE_PROMPT_MAX_LENGTH = 20000

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


# POST /api/agenda/topic/action verb whitelist — mirrors _COMMAND_WHITELIST.
# "reject" is not a KiraAgendaController method (only approve/queue/remove/
# move exist) — omitted rather than mapped to a lossy approximation.
_AGENDA_ACTION_WHITELIST = frozenset({"approve", "queue", "remove", "move"})

# POST /api/agenda/session/action verb whitelist — the three KiraAgendaController
# mode controls. None of them raise, so the handler needs no try/except.
_AGENDA_SESSION_ACTION_WHITELIST = frozenset({"enable", "soft_stop", "emergency_stop"})

# POST /api/agenda/topic raw-constraint DoS bound = 2x controller MAX_CONSTRAINTS
# (12). add_topic sanitizes EVERY submitted constraint (regex chain) BEFORE
# slicing to MAX_CONSTRAINTS, so an unbounded list is unbounded regex work at a
# trust boundary. Truncation-to-12 stays the controller's contract; this only
# caps the raw count before any sanitization runs.
_AGENDA_CONSTRAINTS_RAW_MAX = 24


def _agenda_topic_out(topic) -> AgendaTopicOut:
    return AgendaTopicOut(
        id=topic.id,
        title=topic.title,
        angle=topic.angle,
        priority=topic.priority,
        response_length=topic.response_length,
        status=topic.status.value,
        turns_spoken=topic.turns_spoken,
        confidence=topic.confidence,
        source=topic.source,
    )


def _agenda_response(agenda) -> AgendaResponse:
    metrics = agenda.get_metrics()
    return AgendaResponse(
        state=agenda.state.value,
        active_topic=_agenda_topic_out(agenda.active_topic) if agenda.active_topic else None,
        queued_topics=[_agenda_topic_out(t) for t in agenda.queued_topics()],
        drafted_topics=[_agenda_topic_out(t) for t in agenda.drafted_topics()],
        session_settings=AgendaSessionSettings(
            max_turns_per_topic=agenda.max_turns_per_topic,
            rhythm=agenda.rhythm,
            response_length=agenda.response_length,
            safety_mode=agenda.safety_mode,
            profile_style=agenda.profile.get("style", ""),
        ),
        metrics=AgendaMetrics(
            total_rejections=metrics["total_rejections"],
            by_error_code=metrics["by_error_code"],
            by_guardrail=metrics["by_guardrail"],
            avg_similarity_overlap_pct=metrics["avg_similarity_overlap_pct"],
            current_state=metrics["current_state"],
            failure_count=metrics["failure_count"],
            response_length=metrics["response_length"],
            active_topic=metrics["active_topic"],
            topics_queued=metrics["topics_queued"],
            last_outputs_count=metrics["last_outputs_count"],
        ),
    )


def _music_track_status(track) -> str:
    if track.missing:
        return "faltante"
    if track.invalid:
        return "invalido"
    return "ok"


def _music_track_out(track) -> MusicTrackOut:
    return MusicTrackOut(
        id=track.id, label=track.label, mood=track.mood, status=_music_track_status(track)
    )


def _normalize_mood_strict(mood: str) -> "str | None":
    """Case/whitespace-normalize `mood`, then REQUIRE KNOWN_MOODS membership.

    Mirrors music_library.normalize_mood's regex normalization but returns
    None on an unknown mood instead of silently falling back to 'normal'. That
    silent fallback is safe for the CTK dropdown (it can't send garbage) but a
    footgun at an API trust boundary — a typo like 'hyep' would otherwise file
    the track under 'normal' with no signal (design D5)."""
    normalized = re.sub(r"[^a-z0-9_]+", "_", (mood or "").strip().lower()).strip("_")
    return normalized if normalized in KNOWN_MOODS else None


# POST /api/music/fade — a fade INTENT (client executes it, never backend audio).
_MUSIC_FADE_DIRECTIONS = frozenset({"in", "out"})
_MUSIC_FADE_DEFAULT_MS = 6000
_MUSIC_FADE_MAX_MS = 60000

# POST /api/music/import — bound the source path + copy size at the trust boundary.
_MUSIC_IMPORT_PATH_MAX_LENGTH = 1024
_MUSIC_IMPORT_MAX_BYTES = 200 * 1024 * 1024


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
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
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
        return StatusResponse(
            is_ready=is_ready,
            current_model=host.motor.current_model,
            is_speaking=is_speaking,
            is_processing=is_processing,
            active_profile=host.motor._current_profile_name,
            health=HealthState(**dataclasses.asdict(host.monitor.state)),
            state_version=request.app.state.dispatcher.state_version,
            ollama_warming=getattr(host, "ollama_warming", False),
            avatar_state=avatar_state,
        )

    @app.get("/api/perfiles", response_model=ProfilesListResponse)
    def list_perfiles() -> ProfilesListResponse:
        perfiles = cargar_perfiles()
        if not isinstance(perfiles, dict):
            return ProfilesListResponse(profiles=[])
        return ProfilesListResponse(profiles=list(perfiles.keys()))

    @app.get("/api/perfiles/{name}", response_model=ProfileDetailResponse)
    def get_perfil(name: str):
        # R8/D5: explicit field picks — never `**data` — so the stored `id`
        # (or any future field) can never leak. GET is lock-free like list.
        perfiles = cargar_perfiles()
        if not isinstance(perfiles, dict) or name not in perfiles or not isinstance(
            perfiles[name], dict
        ):
            return JSONResponse(status_code=404, content={"detail": "profile not found"})
        data = perfiles[name]
        return ProfileDetailResponse(
            name=name,
            prompt=str(data.get("prompt", "")),
            use_system=bool(data.get("use_system", False)),
        )

    @app.post("/api/perfiles", response_model=ProfileDetailResponse)
    def create_perfil(body: ProfileCreateRequest):
        name = body.name.strip()
        if not name or len(name) > _PROFILE_NAME_MAX_LENGTH:
            return JSONResponse(status_code=422, content={"detail": "invalid profile name"})
        if len(body.prompt) > _PROFILE_PROMPT_MAX_LENGTH:
            return JSONResponse(status_code=422, content={"detail": "prompt exceeds max length"})
        with _profiles_lock:
            perfiles = cargar_perfiles()
            if not isinstance(perfiles, dict):
                return JSONResponse(status_code=503, content={"detail": "profiles_unavailable"})
            if name in perfiles:
                return JSONResponse(status_code=409, content={"detail": "profile already exists"})
            # Stable id minted server-side (R12) — never accepted from the wire.
            perfiles[name] = {
                "id": str(uuid.uuid4()),
                "prompt": body.prompt,
                "use_system": body.use_system,
            }
            guardar_perfiles(perfiles)
        return ProfileDetailResponse(name=name, prompt=body.prompt, use_system=body.use_system)

    @app.put("/api/perfiles/{name}", response_model=ProfileDetailResponse)
    def update_perfil(name: str, body: ProfileUpdateRequest):
        new_name = body.new_name.strip() if body.new_name is not None else None
        if new_name is not None and (not new_name or len(new_name) > _PROFILE_NAME_MAX_LENGTH):
            return JSONResponse(status_code=422, content={"detail": "invalid profile name"})
        if body.prompt is not None and len(body.prompt) > _PROFILE_PROMPT_MAX_LENGTH:
            return JSONResponse(status_code=422, content={"detail": "prompt exceeds max length"})
        with _profiles_lock:
            perfiles = cargar_perfiles()
            if not isinstance(perfiles, dict) or name not in perfiles:
                return JSONResponse(status_code=404, content={"detail": "profile not found"})
            # Copy preserves the stable id (R12) across both edit and rename.
            data = dict(perfiles[name])
            if body.prompt is not None:
                data["prompt"] = body.prompt
            if body.use_system is not None:
                data["use_system"] = body.use_system
            target = name
            if new_name and new_name != name:
                if new_name in perfiles:
                    return JSONResponse(
                        status_code=409, content={"detail": "profile already exists"}
                    )
                del perfiles[name]
                target = new_name
            perfiles[target] = data
            guardar_perfiles(perfiles)
        return ProfileDetailResponse(
            name=target,
            prompt=str(data.get("prompt", "")),
            use_system=bool(data.get("use_system", False)),
        )

    @app.delete("/api/perfiles/{name}")
    def delete_perfil(name: str):
        with _profiles_lock:
            perfiles = cargar_perfiles()
            if not isinstance(perfiles, dict) or name not in perfiles:
                return JSONResponse(status_code=404, content={"detail": "profile not found"})
            # Last-profile guard ONLY (resolution 2905): deleting the active
            # profile is allowed — the engine keeps its in-memory copy until
            # the next switch (CTK parity, not an improvement).
            if len(perfiles) <= 1:
                return JSONResponse(
                    status_code=409, content={"detail": "cannot delete the last profile"}
                )
            del perfiles[name]
            guardar_perfiles(perfiles)
        return {"ok": True}

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

    @app.get("/api/memoria/list", response_model=MemoriaListResponse)
    def get_memoria_list(profile_id: str) -> MemoriaListResponse:
        if not MEMORIAS_ENABLED:
            return MemoriaListResponse(items=[])
        rows = _list_memoria_metadata(MEMORIAS_DB, profile_id)
        return MemoriaListResponse(
            items=[
                MemoriaListItem(
                    id=row["id"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                    revision=row["revision"],
                    pinned=bool(row["pinned"]),
                    private=bool(row["private"]),
                    inactive=bool(row["inactive"]),
                )
                for row in rows
            ]
        )

    @app.post("/api/memoria/purge", response_model=MemoriaPurgeResponse)
    def post_memoria_purge(body: MemoriaPurgeRequest) -> MemoriaPurgeResponse:
        if not MEMORIAS_ENABLED:
            return MemoriaPurgeResponse(deleted=0)
        deleted = _purge_memoria(MEMORIAS_DB, body.profile_id)
        return MemoriaPurgeResponse(deleted=deleted)

    @app.post("/api/memoria/flags", response_model=MemoriaMutationResponse)
    def post_memoria_flags(body: MemoriaFlagsRequest):
        if not MEMORIAS_ENABLED:
            return MemoriaMutationResponse(ok=False)  # benign no-op, mirrors purge deleted=0
        if body.pinned is None and body.private is None and body.inactive is None:
            return JSONResponse(status_code=422, content={"detail": "no flags provided"})
        store = _get_memoria_store()
        row = store.get(body.id)
        # Same 404 for a wrong-profile row as for a missing one (R7: no
        # cross-profile existence oracle — identical shape and detail).
        if row is None or row["profile_id"] != body.profile_id:
            return JSONResponse(status_code=404, content={"detail": "memoria not found"})
        # F5: set_flags enforces the freeze rule (pin/private promote curated,
        # un-pin never demotes). A False means the write genuinely failed — it
        # MUST surface as 503, never be swallowed, or the next auto-capture
        # could overwrite a row the operator believed frozen.
        if not store.set_flags(
            body.id, pinned=body.pinned, private=body.private, inactive=body.inactive
        ):
            return JSONResponse(status_code=503, content={"detail": "memoria_write_failed"})
        return MemoriaMutationResponse(ok=True)

    @app.post("/api/memoria/delete", response_model=MemoriaMutationResponse)
    def post_memoria_delete(body: MemoriaDeleteRequest):
        if not MEMORIAS_ENABLED:
            return MemoriaMutationResponse(ok=False)  # benign no-op, mirrors flags/purge
        store = _get_memoria_store()
        row = store.get(body.id)
        # Same 404 for a wrong-profile row as for a missing one (R7: no
        # cross-profile existence oracle). This pre-check owns the "already
        # gone" case, so delete_row's False below can only mean a real write
        # failure — never "the row wasn't there".
        if row is None or row["profile_id"] != body.profile_id:
            return JSONResponse(status_code=404, content={"detail": "memoria not found"})
        if not store.delete_row(body.id):
            return JSONResponse(status_code=503, content={"detail": "memoria_write_failed"})
        return MemoriaMutationResponse(ok=True)

    @app.post("/api/memoria/update", response_model=MemoriaMutationResponse)
    def post_memoria_update(body: MemoriaUpdateRequest):
        if not MEMORIAS_ENABLED:
            return MemoriaMutationResponse(ok=False)  # benign no-op, mirrors flags/delete
        # Empty-check is API-side: update_row whitespace-normalizes without
        # validating emptiness, so an all-whitespace body would write ''.
        title, content = body.title.strip(), body.content.strip()
        if not title or not content:
            return JSONResponse(
                status_code=422, content={"detail": "title and content must not be empty"}
            )
        if len(title) > _MEMORIA_TITLE_MAX_LENGTH or len(content) > _MEMORIA_CONTENT_MAX_LENGTH:
            return JSONResponse(
                status_code=422, content={"detail": "title or content exceeds max length"}
            )
        store = _get_memoria_store()
        row = store.get(body.id)
        # Same 404 for a wrong-profile row as for a missing one (R7: no
        # cross-profile existence oracle — identical shape and detail).
        if row is None or row["profile_id"] != body.profile_id:
            return JSONResponse(status_code=404, content={"detail": "memoria not found"})
        # F5: update_row promotes status='curated' in the same statement. A
        # False means the write genuinely failed — surface as 503, never swallow
        # (else the next auto-capture could overwrite a row believed frozen).
        if not store.update_row(body.id, title=title, content=content):
            return JSONResponse(status_code=503, content={"detail": "memoria_write_failed"})
        return MemoriaMutationResponse(ok=True)

    @app.post("/api/memoria/capture", response_model=MemoriaMutationResponse)
    def post_memoria_capture(request: Request, body: MemoriaCaptureRequest):
        if not MEMORIAS_ENABLED:
            return MemoriaMutationResponse(ok=False)  # gated no-op, mirrors flags/delete/update
        # Direct motor call (design resolution 2905/D3): the CTK toggles this on
        # the motor directly (inspector_memory.py), NOT via the command queue.
        # No llm_engine.py change, no _COMMAND_WHITELIST entry.
        request.app.state.host.motor.set_memorias_private(body.paused)
        return MemoriaMutationResponse(ok=True)

    @app.get("/api/music/library", response_model=MusicLibraryResponse)
    def get_music_library(request: Request):
        host = request.app.state.host
        library = getattr(host, "music_library", None)
        if library is None:
            return JSONResponse(status_code=503, content={"detail": "music_unavailable"})
        # D4: guard every library read/mutation on host.music_lock — the
        # library has no internal lock and all_tracks() iterates self.tracks,
        # which a concurrent import/delete could mutate mid-iteration.
        with host.music_lock:
            tracks = [_music_track_out(t) for t in library.all_tracks()]
        return MusicLibraryResponse(
            tracks=tracks, count=len(tracks), moods=sorted({t.mood for t in tracks})
        )

    @app.post("/api/music/mood", response_model=MusicMoodResponse)
    def post_music_mood(request: Request, body: MusicMoodRequest):
        host = request.app.state.host
        library = getattr(host, "music_library", None)
        if library is None:
            return JSONResponse(status_code=503, content={"detail": "music_unavailable"})
        mood = _normalize_mood_strict(body.mood)
        if mood is None:
            return JSONResponse(status_code=422, content={"detail": "unknown mood"})
        # State only — orchestration, NEVER backend audio (2911). The Tauri
        # client reads this mood and plays the corresponding track itself.
        host.music_state.set_mood(mood)
        with host.music_lock:
            valid = library.valid_tracks()
            suggested = library.select_for_mood(mood)
        return MusicMoodResponse(
            active_mood=mood,
            tracks=[_music_track_out(t) for t in valid if t.mood == mood],
            suggested_track_id=suggested.id if suggested else None,
        )

    @app.get("/api/music/state", response_model=MusicStateResponse)
    def get_music_state(request: Request):
        host = request.app.state.host
        if getattr(host, "music_library", None) is None:
            return JSONResponse(status_code=503, content={"detail": "music_unavailable"})
        return MusicStateResponse(**host.music_state.snapshot())

    @app.post("/api/music/fade", response_model=MusicStateResponse)
    def post_music_fade(request: Request, body: MusicFadeRequest):
        host = request.app.state.host
        if getattr(host, "music_library", None) is None:
            return JSONResponse(status_code=503, content={"detail": "music_unavailable"})
        if body.direction not in _MUSIC_FADE_DIRECTIONS:
            return JSONResponse(status_code=422, content={"detail": "unknown direction"})
        duration_ms = _MUSIC_FADE_DEFAULT_MS if body.duration_ms is None else body.duration_ms
        if not 0 < duration_ms <= _MUSIC_FADE_MAX_MS:
            return JSONResponse(status_code=422, content={"detail": "duration_ms out of range"})
        # Intent only — the Tauri client runs the fade; the API never touches
        # AudioBedEngine (2911, headless host has none).
        host.music_state.set_fade(body.direction, duration_ms)
        return MusicStateResponse(**host.music_state.snapshot())

    @app.post("/api/music/import", response_model=MusicImportResponse)
    def post_music_import(request: Request, body: MusicImportRequest):
        host = request.app.state.host
        library = getattr(host, "music_library", None)
        if library is None:
            return JSONResponse(status_code=503, content={"detail": "music_unavailable"})
        mood = _normalize_mood_strict(body.mood)
        if mood is None:
            return JSONResponse(status_code=422, content={"detail": "unknown mood"})
        raw = body.path.strip()
        if not raw or len(raw) > _MUSIC_IMPORT_PATH_MAX_LENGTH:
            return JSONResponse(status_code=422, content={"detail": "invalid path"})
        source = Path(raw)
        if not source.is_absolute():
            return JSONResponse(status_code=422, content={"detail": "path must be absolute"})
        if source.suffix.lower() not in ALLOWED_AUDIO_EXTENSIONS:
            return JSONResponse(status_code=422, content={"detail": "only .mp3/.wav files"})
        try:
            if not source.is_file():
                return JSONResponse(status_code=422, content={"detail": "file not found"})
            if source.stat().st_size > _MUSIC_IMPORT_MAX_BYTES:
                return JSONResponse(status_code=422, content={"detail": "file too large"})
        except OSError:
            return JSONResponse(status_code=422, content={"detail": "file not readable"})
        # add_file re-validates the audio signature, copies into the managed
        # dir under a uuid name (traversal-proof), and saves. No backend audio.
        try:
            with host.music_lock:
                track = library.add_file(source, mood)
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc)})
        except OSError:
            return JSONResponse(status_code=503, content={"detail": "music_write_failed"})
        return MusicImportResponse(track=_music_track_out(track))

    @app.delete("/api/music/track/{track_id}")
    def delete_music_track(request: Request, track_id: str):
        host = request.app.state.host
        library = getattr(host, "music_library", None)
        if library is None:
            return JSONResponse(status_code=503, content={"detail": "music_unavailable"})
        # remove(delete_file=True) unlinks only files inside library_dir
        # (_delete_managed_file's is_relative_to guard); an externally-pathed
        # entry is deregistered but its file survives. No backend audio.
        try:
            with host.music_lock:
                removed = library.remove(track_id, delete_file=True)
        except OSError:
            return JSONResponse(status_code=503, content={"detail": "music_write_failed"})
        if not removed:
            return JSONResponse(status_code=404, content={"detail": "track not found"})
        return {"ok": True}  # mirrors delete_perfil; repeat DELETE -> 404

    @app.get("/api/music/track/{track_id}/audio")
    def get_music_track_audio(request: Request, track_id: str):
        # Client-side-playback model (2911) + resolution 2914: the API is the
        # single point that mediates audio access. It never plays audio; it
        # streams the managed file and reports availability. Missing/moved/
        # corrupted/out-of-library files all surface as 404 instead of the
        # client hitting a phantom source unnoticed.
        host = request.app.state.host
        library = getattr(host, "music_library", None)
        if library is None:
            return JSONResponse(status_code=503, content={"detail": "music_unavailable"})
        with host.music_lock:
            track = library.tracks.get(track_id)
            if track is None:
                return JSONResponse(status_code=404, content={"detail": "track not found"})
            library_root = library.library_dir.resolve()
            track_path = Path(track.path).resolve()
        # Path-safety: serve ONLY files inside library_dir (mirrors
        # _delete_managed_file's is_relative_to guard). is_supported_audio_path
        # then covers both missing-on-disk and failed-signature in one check.
        if not track_path.is_relative_to(library_root):
            return JSONResponse(status_code=404, content={"detail": "track not found"})
        if not is_supported_audio_path(track_path):
            return JSONResponse(status_code=404, content={"detail": "track not found"})
        media_type = mimetypes.guess_type(str(track_path))[0] or "application/octet-stream"
        return FileResponse(track_path, media_type=media_type)

    @app.get("/api/agenda", response_model=AgendaResponse)
    def get_agenda(request: Request):
        agenda = getattr(request.app.state.host, "agenda", None)
        if agenda is None:
            return JSONResponse(status_code=503, content={"detail": "agenda_unavailable"})
        with request.app.state.host.agenda_lock:
            return _agenda_response(agenda)

    @app.post("/api/agenda/topic", response_model=AgendaResponse)
    def post_agenda_topic(request: Request, body: AgendaTopicRequest):
        host = request.app.state.host
        agenda = getattr(host, "agenda", None)
        if agenda is None:
            return JSONResponse(status_code=503, content={"detail": "agenda_unavailable"})
        # Raw-count guard BEFORE the lock/sanitization: bound the regex work the
        # controller does per constraint (truncation-to-12 stays its contract).
        if body.constraints is not None and len(body.constraints) > _AGENDA_CONSTRAINTS_RAW_MAX:
            return JSONResponse(status_code=422, content={"detail": "too many constraints"})
        with host.agenda_lock:
            try:
                agenda.add_topic(
                    body.title,
                    angle=body.angle or "",
                    constraints=body.constraints or [],
                    priority=body.priority or "normal",
                    response_length=body.response_length or "normal",
                )
            except ValueError as exc:
                return JSONResponse(status_code=422, content={"detail": str(exc)})
            return _agenda_response(agenda)

    @app.post("/api/agenda/topic/action", response_model=AgendaResponse)
    def post_agenda_topic_action(request: Request, body: AgendaTopicActionRequest):
        host = request.app.state.host
        agenda = getattr(host, "agenda", None)
        if agenda is None:
            return JSONResponse(status_code=503, content={"detail": "agenda_unavailable"})
        if body.action not in _AGENDA_ACTION_WHITELIST:
            return JSONResponse(status_code=422, content={"detail": "unknown action"})
        with host.agenda_lock:
            try:
                if body.action == "approve":
                    agenda.approve_topic(body.topic_id)
                elif body.action == "queue":
                    agenda.queue_topic(body.topic_id)
                elif body.action == "remove":
                    agenda.remove_queued_topic(body.topic_id)
                elif body.action == "move":
                    agenda.move_queued_topic(body.topic_id, body.direction or 1)
            except (ValueError, KeyError) as exc:
                return JSONResponse(status_code=422, content={"detail": str(exc)})
            return _agenda_response(agenda)

    @app.put("/api/agenda/session", response_model=AgendaResponse)
    def put_agenda_session(request: Request, body: AgendaSessionRequest):
        host = request.app.state.host
        agenda = getattr(host, "agenda", None)
        if agenda is None:
            return JSONResponse(status_code=503, content={"detail": "agenda_unavailable"})
        with host.agenda_lock:
            if body.profile is not None and body.profile.style is not None:
                try:
                    agenda.set_profile({"style": body.profile.style})
                except ValueError as exc:
                    return JSONResponse(status_code=422, content={"detail": str(exc)})
            agenda.set_session_settings(
                max_turns_per_topic=body.max_turns_per_topic,
                rhythm=body.rhythm,
                response_length=body.response_length,
                safety_mode=body.safety_mode,
            )
            return _agenda_response(agenda)

    @app.post("/api/agenda/session/action", response_model=AgendaResponse)
    def post_agenda_session_action(request: Request, body: AgendaSessionActionRequest):
        host = request.app.state.host
        agenda = getattr(host, "agenda", None)
        if agenda is None:
            return JSONResponse(status_code=503, content={"detail": "agenda_unavailable"})
        if body.action not in _AGENDA_SESSION_ACTION_WHITELIST:
            return JSONResponse(status_code=422, content={"detail": "unknown action"})
        with host.agenda_lock:
            if body.action == "enable":
                agenda.enable()
            elif body.action == "soft_stop":
                # Returned AgendaAction (closing line) deliberately ignored: the
                # headless host has no speech pipeline to play it (CTK does).
                agenda.soft_stop()
            elif body.action == "emergency_stop":
                agenda.emergency_stop()
            return _agenda_response(agenda)

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

    @app.get("/api/chat/last-reply", response_model=ChatLastReplyResponse)
    def get_chat_last_reply(request: Request) -> ChatLastReplyResponse:
        # R8-safe: surfaces Kira's OWN generated reply text only — see
        # ChatReplySink docstring (engine_host.py). Never the viewer/operator
        # text that triggered it.
        return ChatLastReplyResponse(**request.app.state.host.chat_sink.last())

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
