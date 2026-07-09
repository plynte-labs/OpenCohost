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
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import ollama
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from starlette.background import BackgroundTask

from opencohost.api.agenda_driver import enqueue_agenda_action

# _bearer_token/_resolve_role are auth-internal, imported here ONLY to
# differentiate operator vs agent on the operator-only notice routes —
# auth_middleware rule 1 already guaranteed the token is valid.
from opencohost.api.auth import (
    TokenFileError,
    _bearer_token,
    _resolve_role,
    auth_middleware,
    ensure_tokens,
)
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
    AgentCardRequest,
    AgentCardResponse,
    AgentNoticeDismissResponse,
    AgentNoticeOut,
    AgentNoticeRequest,
    AgentNoticeResponse,
    AgentNoticesResponse,
    AgentStatusResponse,
    AgentTopicRequest,
    AgentTopicResponse,
    AvatarConfigRequest,
    AvatarConfigResponse,
    ChatLastReplyResponse,
    ChatTurnRequest,
    CohostProfileOut,
    CohostProfileSaveRequest,
    CohostProfileSelectRequest,
    CohostProfileSelectResponse,
    CohostProfilesResponse,
    CommandRequest,
    HealthResponse,
    HealthState,
    I18nSetLocaleRequest,
    I18nStateResponse,
    MemoriaDeleteRequest,
    MemoriaFlagsRequest,
    MemoriaListResponse,
    MemoriaListItem,
    MemoriaCaptureRequest,
    MemoriaMutationResponse,
    MemoriaNoticeRequest,
    MemoriaNoticeResponse,
    MemoriaPurgeRequest,
    MemoriaPurgeResponse,
    MemoriaRowResponse,
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
    PersonalizationResponse,
    PersonalizationUpdateRequest,
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
from opencohost.avatar.avatar_config import (
    VALID_STATES,
    AvatarConfigUnreadableError,
    load_avatar_config,
    save_avatar_config,
)
from opencohost.avatar.obs_client import OBSClient
from opencohost.config.settings import (
    EDITORIAL_CARDS_DB,
    EXPERIMENTAL_HEAVY_TTS_ENABLED,
    MEMORIAS_DB,
    MEMORIAS_ENABLED,
    MODELS_CATALOG,
    PERSONALIZATION_INSTRUCTIONS_MAX,
    PERSONALIZATION_INTERESTS_MAX,
    PERSONALIZATION_NICKNAME_MAX,
    PERSONALIZATION_OCCUPATION_MAX,
    _canonical_model_tag,
    default_piper_voice_for_locale,
    load_memorias_notice_dismissed,
    load_piper_voice,
    load_tts_local_only,
    save_last_profile,
    save_memorias_notice_dismissed,
    load_tts_speed,
    resolve_llm_tiers,
)
from opencohost.core.agent_notices import (
    AgentNoticeCapError,
    AgentNoticeStore,
    AgentNoticeValidationError,
)
from opencohost.core.editorial_cards import (
    EditorialCard,
    EditorialCardStatus,
    EditorialCardStore,
    EditorialCardValidationError,
)
from opencohost.core.memoria_store import MemoriaStore
from opencohost.core.personalization import (
    clear_personalization,
    load_personalization,
    save_personalization,
)
from opencohost.core.music_library import (
    ALLOWED_AUDIO_EXTENSIONS,
    KNOWN_MOODS,
    is_supported_audio_path,
)
from opencohost.core.cohost_profiles import (
    load_cohost_profiles,
    normalize_cohost_profile,
    sanitize_profile_name,
    save_cohost_profiles,
)
from opencohost.core.profiles import cargar_perfiles, guardar_perfiles
from opencohost.i18n import active as i18n_active
from opencohost.i18n import state as i18n_state
from opencohost.i18n import tags as i18n_tags
from opencohost.i18n.coherence import check_coherence
from opencohost.i18n.startup import load_registry
from opencohost.core.topic_inbox import (
    TopicInboxCapError,
    TopicInboxStore,
    TopicInboxValidationError,
)
from opencohost.smart_aggregator.kira_agenda_controller import AgendaState, TopicStatus
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


def _count_sql(db_path: str, sql: str, params: tuple = ()) -> int:
    """Bounded, fail-open COUNT(*) read. Never creates db_path as a side
    effect: a missing file (e.g. a standalone API process that never
    touched this store) returns 0 without connecting.

    `params` are bound positionally for parameterized WHERE clauses (e.g. the
    per-profile split in GET /api/memoria/stats) — never string-interpolated."""
    if not db_path or not os.path.exists(db_path):
        return 0
    try:
        with closing(sqlite3.connect(db_path, timeout=_STATS_DB_READ_TIMEOUT_SECONDS)) as conn:
            row = conn.execute(sql, params).fetchone()
            return int(row[0]) if row else 0
    except sqlite3.Error:
        return 0


def _parse_iso8601_utc(value: str | None) -> datetime | None:
    """Parse an optional ISO-8601 timestamp; naive values are treated as UTC
    (same normalization editorial_cards applies on storage). Raises
    ValueError on malformed input."""
    if not value:
        return None
    if value.endswith(("Z", "z")):
        # Python 3.10 fromisoformat rejects the Z suffix — the canonical
        # machine-emitted UTC form (Date.toISOString(), RFC3339 emitters).
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _agent_request_role(request: Request) -> str | None:
    """Token role of an already-authenticated /api/agent/* caller.

    auth_middleware rule 1 guarantees a valid bearer token reached the
    handler; this only differentiates operator vs agent for the
    operator-only notice surface (agent token -> 403, design error
    contract). Fail-closed: any token-file hiccup reads as no role.
    """
    token = _bearer_token(request)
    if token is None:
        return None
    try:
        return _resolve_role(token)
    except TokenFileError:
        return None


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
    """Bounded, fail-open metadata read for GET /api/memoria/list.

    WU-H (operator viewing decision, 2026-07-05): the SELECT now ALSO reads
    `title` — a deliberate, scoped relaxation of the prior metadata-only
    rule so the operator can recognize a row before deciding to load it.
    `content` still stays off this SELECT entirely; it is only readable one
    row at a time via GET /api/memoria/row/{id} (R8 unaffected — memoria
    title/content is Kira's curated/derived memory, not raw viewer chat)."""
    if not db_path or not os.path.exists(db_path):
        return []
    try:
        with closing(sqlite3.connect(db_path, timeout=_STATS_DB_READ_TIMEOUT_SECONDS)) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT id, title, created_at, updated_at, revision, pinned, private, inactive "
                "FROM memorias WHERE profile_id = ? ORDER BY updated_at DESC, id ASC",
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


def _memoria_store_or_none() -> "MemoriaStore | None":
    """Construct/return the shared store, or None if it can't be built.

    MemoriaStore.__init__ runs CREATE TABLE / PRAGMA writes, so a locked or
    corrupt db makes construction raise. Callers map None -> 503
    memoria_unavailable instead of letting an opaque 500 escape.
    """
    try:
        return _get_memoria_store()
    except (sqlite3.Error, OSError):
        return None

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

# cohost_profiles.json write-lock (WU3): guards the read-modify-write of the
# cohost-profiles file (save). A separate file gets a separate lock (D4).
_cohost_profiles_lock = threading.Lock()

# personalization.json write-lock (Phase 2, kira_personalization_onboarding):
# guards every read-modify-write of the global personalization store. A
# separate file gets a separate lock (D4).
_personalization_lock = threading.Lock()


def _cohost_profiles_response(profiles: dict) -> CohostProfilesResponse:
    return CohostProfilesResponse(
        profiles=[
            CohostProfileOut(
                name=name,
                style=str(p.get("style", "")),
                default_priority=str(p.get("default_priority", "normal")),
                default_response_length=str(p.get("default_response_length", "normal")),
            )
            for name, p in profiles.items()
        ]
    )


# POST/PUT /api/perfiles bounds — profiles.json is loaded whole per request,
# so cap the write to keep a single profile from ballooning the file.
_PROFILE_NAME_MAX_LENGTH = 100
_PROFILE_PROMPT_MAX_LENGTH = 20000

# POST /api/obs/test bound: the socket timeout is now threaded into
# OBSClient.test_connection (obsws ws.connect self-bounds), so the worker
# returns on its own. This executor is only a backstop for anything the socket
# timeout can't cover — and it must NOT join a stuck worker: a `with` block's
# shutdown(wait=True) would re-block on the very thread we timed out on, so we
# manage the executor manually and shutdown(wait=False, cancel_futures=True).
_OBS_TEST_TIMEOUT_SECONDS = 5.0


def _test_obs_connection_bounded(client: OBSClient, timeout: float = _OBS_TEST_TIMEOUT_SECONDS):
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(client.test_connection, timeout=timeout)
        return future.result(timeout=timeout + 1.0)  # socket self-bounds; +1s backstop
    except concurrent.futures.TimeoutError:
        return False, "OBS connection test timed out"
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _obs_config_response(cfg) -> ObsConfigResponse:
    return ObsConfigResponse(
        enabled=cfg.obs.enabled,
        host=cfg.obs.host,
        port=cfg.obs.port,
        source=cfg.obs.source_name,
        password_set=bool(cfg.obs.password),
    )


def _apply_avatar_runtime(request: Request) -> None:
    """FIX-B: push the just-saved avatar.yaml to the live OBS runtime.

    Best-effort and doubly guarded (getattr for the missing attribute, try/except
    for a runtime error) so a successful config write never turns into a 500:
    FakeHost test doubles carry no ``obs_runtime``, and a failed/absent runtime
    must not fail the request. The ObsRuntime rebuilds its OBSClient (state_images
    are snapshotted at construction — see obs_client.py) and reconnects on the
    new host/port. Called ONLY after a successful save (never on the 503 paths).
    """
    host = getattr(request.app.state, "host", None)
    runtime = getattr(host, "obs_runtime", None)
    if runtime is None:
        return
    try:
        runtime.apply_config()
    except Exception:
        # OBS is best-effort; a runtime rebuild failure never fails the write.
        pass


# POST /api/agenda/topic/action verb whitelist — mirrors _COMMAND_WHITELIST.
# "reject" acts on the pre-approval DRAFTED suggestion inbox (-> SKIPPED),
# distinct from "remove" which drops a post-approval QUEUED topic.
_AGENDA_ACTION_WHITELIST = frozenset({"approve", "queue", "remove", "move", "reject"})

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


def _agenda_response(agenda, *, applied=None, reason=None) -> AgendaResponse:
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
        # FIX-C: session/action outcome. Only POST /api/agenda/session/action
        # sets these; every other response leaves them null.
        applied=applied,
        reason=reason,
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
        # Mint the API bearer tokens once per install (agent_context_gateway
        # Phase 1). Best-effort: a failed mint degrades to 503s on protected
        # surfaces (auth.py), never blocks engine startup.
        ensure_tokens()
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
    # Auth gate (agent_context_gateway ADR-4): registered BEFORE CORSMiddleware
    # so CORS — added last, therefore outermost in Starlette's stack — still
    # decorates 401/403/503 auth responses and handles OPTIONS preflight.
    app.middleware("http")(auth_middleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins if cors_origins is not None else _DEFAULT_CORS_ORIGINS,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Idempotency-Key", "Authorization"],
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
        return StatusResponse(
            is_ready=is_ready,
            current_model=host.motor.current_model,
            is_speaking=is_speaking,
            is_processing=is_processing,
            active_profile=host.motor._current_profile_name,
            active_profile_id=getattr(host.motor, "_current_profile_id", None),
            health=HealthState(**dataclasses.asdict(host.monitor.state)),
            state_version=request.app.state.dispatcher.state_version,
            ollama_warming=getattr(host, "ollama_warming", False),
            avatar_state=avatar_state,
            obs_connected=obs_connected,
        )

    @app.get("/api/perfiles", response_model=ProfilesListResponse)
    def list_perfiles() -> ProfilesListResponse:
        perfiles = cargar_perfiles()
        if not isinstance(perfiles, dict):
            return ProfilesListResponse(profiles=[])
        return ProfilesListResponse(profiles=list(perfiles.keys()))

    @app.get("/api/perfiles/{name}", response_model=ProfileDetailResponse)
    def get_perfil(name: str):
        # R8/D5: explicit field picks — never `**data` — so only name/id/
        # prompt/use_system are ever returned; any future persisted field
        # (or chat content) can never leak through. `id` IS returned so the
        # FE can target this profile's memoria rows (stored keyed by uuid).
        # GET is lock-free like list.
        perfiles = cargar_perfiles()
        if not isinstance(perfiles, dict) or name not in perfiles or not isinstance(
            perfiles[name], dict
        ):
            return JSONResponse(status_code=404, content={"detail": "profile not found"})
        data = perfiles[name]
        return ProfileDetailResponse(
            name=name,
            id=str(data.get("id", "")),
            prompt=str(data.get("prompt", "")),
            use_system=bool(data.get("use_system", False)),
            locale=data.get("locale"),
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
            new_id = str(uuid.uuid4())
            perfiles[name] = {
                "id": new_id,
                "prompt": body.prompt,
                "use_system": body.use_system,
                "locale": body.locale,
            }
            if not guardar_perfiles(perfiles):
                return JSONResponse(status_code=503, content={"detail": "profiles_write_failed"})
        return ProfileDetailResponse(
            name=name,
            id=new_id,
            prompt=body.prompt,
            use_system=body.use_system,
            locale=body.locale,
        )

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
            if body.locale is not None:
                data["locale"] = body.locale
            target = name
            if new_name and new_name != name:
                if new_name in perfiles:
                    return JSONResponse(
                        status_code=409, content={"detail": "profile already exists"}
                    )
                del perfiles[name]
                target = new_name
            perfiles[target] = data
            if not guardar_perfiles(perfiles):
                return JSONResponse(status_code=503, content={"detail": "profiles_write_failed"})
        return ProfileDetailResponse(
            name=target,
            id=str(data.get("id", "")),
            prompt=str(data.get("prompt", "")),
            use_system=bool(data.get("use_system", False)),
            locale=data.get("locale"),
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
            if not guardar_perfiles(perfiles):
                return JSONResponse(status_code=503, content={"detail": "profiles_write_failed"})
        return {"ok": True}

    def _i18n_state_response() -> I18nStateResponse:
        # Reuses load_registry() (official + community discovery, official-wins
        # anti-shadowing merge) and get_active_bundle() — zero new state
        # (design §7.1). `available` reads each bundle's own `meta.display`/
        # `meta.status` (already authored per-locale), not a re-derived check.
        registry = load_registry()
        active_bundle = i18n_active.get_active_bundle()
        persisted_locale = i18n_state.get_locale()
        pending_restart = i18n_tags.normalize(persisted_locale) != active_bundle.code
        available = [
            {
                "code": code,
                "display": str((bundle.data.get("meta") or {}).get("display", code)),
                "tier": bundle.tier,
                "status": str((bundle.data.get("meta") or {}).get("status", "unknown")),
            }
            for code, bundle in sorted(registry.items())
        ]
        # Bundle-level only (design §7.1) — no profile/piper args, so only
        # BUNDLE_VOICE_MISMATCH can ever fire here.
        warnings = [
            {"code": w.code, "message": w.message} for w in check_coherence(active_bundle)
        ]
        return I18nStateResponse(
            active_locale=active_bundle.code,
            persisted_locale=persisted_locale,
            pending_restart=pending_restart,
            available=available,
            warnings=warnings,
        )

    @app.get("/api/i18n", response_model=I18nStateResponse)
    def get_i18n() -> I18nStateResponse:
        return _i18n_state_response()

    @app.put("/api/i18n", response_model=I18nStateResponse)
    def set_i18n(body: I18nSetLocaleRequest):
        registry = load_registry()
        matched = i18n_tags.match(body.locale, list(registry.keys()))
        if not matched:
            return JSONResponse(status_code=422, content={"detail": "unknown locale"})
        # D6: next-boot only — persists the choice, never hot-swaps the
        # running process's active bundle.
        i18n_state.set_locale(matched)
        return _i18n_state_response()

    def _personalization_response(data: dict) -> PersonalizationResponse:
        # Explicit field picks only — never `**data` — so the internal
        # `version` key (or any future field) can never leak through.
        return PersonalizationResponse(
            enabled=bool(data.get("enabled", True)),
            nickname=str(data.get("nickname", "")),
            occupation=str(data.get("occupation", "")),
            interests=str(data.get("interests", "")),
            custom_instructions=str(data.get("custom_instructions", "")),
            updated_at=data.get("updated_at"),
        )

    @app.get("/api/personalization", response_model=PersonalizationResponse)
    def get_personalization() -> PersonalizationResponse:
        # Lock-free like list_perfiles/get_perfil — GET never mutates and
        # load_personalization() already fails open to defaults.
        return _personalization_response(load_personalization())

    @app.put("/api/personalization", response_model=PersonalizationResponse)
    def update_personalization(body: PersonalizationUpdateRequest):
        if body.nickname is not None and len(body.nickname) > PERSONALIZATION_NICKNAME_MAX:
            return JSONResponse(status_code=422, content={"detail": "nickname exceeds max length"})
        if body.occupation is not None and len(body.occupation) > PERSONALIZATION_OCCUPATION_MAX:
            return JSONResponse(
                status_code=422, content={"detail": "occupation exceeds max length"}
            )
        if body.interests is not None and len(body.interests) > PERSONALIZATION_INTERESTS_MAX:
            return JSONResponse(status_code=422, content={"detail": "interests exceeds max length"})
        if (
            body.custom_instructions is not None
            and len(body.custom_instructions) > PERSONALIZATION_INSTRUCTIONS_MAX
        ):
            return JSONResponse(
                status_code=422, content={"detail": "custom_instructions exceeds max length"}
            )
        with _personalization_lock:
            data = load_personalization()
            if body.enabled is not None:
                data["enabled"] = body.enabled
            if body.nickname is not None:
                data["nickname"] = body.nickname
            if body.occupation is not None:
                data["occupation"] = body.occupation
            if body.interests is not None:
                data["interests"] = body.interests
            if body.custom_instructions is not None:
                data["custom_instructions"] = body.custom_instructions
            if not save_personalization(data):
                return JSONResponse(
                    status_code=503, content={"detail": "personalization_write_failed"}
                )
            # Re-read: save_personalization stamps its own `updated_at` and
            # re-clamps, so the response must reflect the persisted values,
            # not the pre-save `data` dict.
            data = load_personalization()
        return _personalization_response(data)

    @app.delete("/api/personalization")
    def delete_personalization():
        with _personalization_lock:
            if not clear_personalization():
                return JSONResponse(
                    status_code=503, content={"detail": "personalization_write_failed"}
                )
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
            piper_voice=load_piper_voice(
                default=default_piper_voice_for_locale(i18n_active.get_active_bundle().code)
            ),
            local_only=load_tts_local_only(),
            speed=load_tts_speed(),
            engine=host.motor.motor_tts,
            heavy_available=EXPERIMENTAL_HEAVY_TTS_ENABLED,
        )

    @app.get("/api/memoria/stats", response_model=MemoriaStatsResponse)
    def get_memoria_stats(request: Request, profile_id: Optional[str] = None) -> MemoriaStatsResponse:
        host = request.app.state.host
        # R8: reuse the ONLY provenance gate (memory_inspector_snapshot
        # already applies _DIGEST_CAPTURE_SOURCES) — take counts only, never
        # touch entry["content"] / digest line text.
        snapshot = host.motor.memory_inspector_snapshot()
        session_turns = len(snapshot["entries"])
        digest_entries = snapshot["digest"]["line_count"]

        # FIX-A: when `profile_id` is present, `saved_memorias`/`pinned` filter
        # to that profile (semantics parity with MemoriaStore.count_all /
        # count_all_pinned) so the headline count agrees with the per-profile
        # GET /api/memoria/list. `saved_memorias_total`/`pinned_total` keep the
        # global figures as separate fields. Without `profile_id`, the two
        # halves coincide (both global) — preserves the pre-FIX-A contract for
        # any old caller.
        saved_total = 0
        pinned_total = 0
        saved_memorias = 0
        pinned = 0
        if MEMORIAS_ENABLED:
            saved_total = _count_sql(MEMORIAS_DB, "SELECT COUNT(*) FROM memorias")
            pinned_total = _count_sql(MEMORIAS_DB, "SELECT COUNT(*) FROM memorias WHERE pinned = 1")
            if profile_id:
                saved_memorias = _count_sql(
                    MEMORIAS_DB, "SELECT COUNT(*) FROM memorias WHERE profile_id = ?", (profile_id,)
                )
                pinned = _count_sql(
                    MEMORIAS_DB,
                    "SELECT COUNT(*) FROM memorias WHERE profile_id = ? AND pinned = 1",
                    (profile_id,),
                )
            else:
                saved_memorias = saved_total
                pinned = pinned_total

        return MemoriaStatsResponse(
            session_turns=session_turns,
            digest_entries=digest_entries,
            saved_memorias=saved_memorias,
            pinned=pinned,
            saved_memorias_total=saved_total,
            pinned_total=pinned_total,
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
                    title=row["title"],
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

    @app.get("/api/memoria/row/{row_id}", response_model=MemoriaRowResponse)
    def get_memoria_row(row_id: str, profile_id: str):
        """One row's full content, on-demand (WU-H, operator viewing decision).

        Mirrors the ownership-guard pattern from POST /api/memoria/{delete,
        update}: store.get(raising=True) pre-check, then the SAME 404 body
        for a missing row and a wrong-profile row (R7: no cross-profile
        existence oracle). Disabled feature also maps to 404 — there is no
        "empty" analog for a single-row GET the way list/stats have.
        """
        if not MEMORIAS_ENABLED:
            return JSONResponse(status_code=404, content={"detail": "memoria not found"})
        store = _memoria_store_or_none()
        if store is None:
            return JSONResponse(status_code=503, content={"detail": "memoria_unavailable"})
        try:
            row = store.get(row_id, raising=True)
        except sqlite3.Error:
            return JSONResponse(status_code=503, content={"detail": "memoria_unavailable"})
        if row is None or row["profile_id"] != profile_id:
            return JSONResponse(status_code=404, content={"detail": "memoria not found"})
        return MemoriaRowResponse(
            id=row["id"],
            title=row["title"],
            content=row["content"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            pinned=bool(row["pinned"]),
            private=bool(row["private"]),
            inactive=bool(row["inactive"]),
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
        store = _memoria_store_or_none()
        if store is None:
            return JSONResponse(status_code=503, content={"detail": "memoria_unavailable"})
        try:
            row = store.get(body.id, raising=True)
        except sqlite3.Error:
            # Transient lock/db error on the pre-check must NOT masquerade as a
            # missing row (404) — surface it as unavailable (503).
            return JSONResponse(status_code=503, content={"detail": "memoria_unavailable"})
        # Same 404 for a wrong-profile row as for a missing one (R7: no
        # cross-profile existence oracle — identical shape and detail).
        if row is None or row["profile_id"] != body.profile_id:
            return JSONResponse(status_code=404, content={"detail": "memoria not found"})
        # F5: set_flags enforces the freeze rule (pin/private promote curated,
        # un-pin never demotes). A genuine write failure raises (raising=True) —
        # it MUST surface as 503, never be swallowed, or the next auto-capture
        # could overwrite a row the operator believed frozen.
        try:
            applied = store.set_flags(
                body.id, pinned=body.pinned, private=body.private, inactive=body.inactive, raising=True
            )
        except sqlite3.Error:
            return JSONResponse(status_code=503, content={"detail": "memoria_write_failed"})
        if not applied:
            # rowcount==0 on a pre-found row: it vanished in the check-then-act
            # race window — that is not-found, not a write failure.
            return JSONResponse(status_code=404, content={"detail": "memoria not found"})
        return MemoriaMutationResponse(ok=True)

    @app.post("/api/memoria/delete", response_model=MemoriaMutationResponse)
    def post_memoria_delete(body: MemoriaDeleteRequest):
        if not MEMORIAS_ENABLED:
            return MemoriaMutationResponse(ok=False)  # benign no-op, mirrors flags/purge
        store = _memoria_store_or_none()
        if store is None:
            return JSONResponse(status_code=503, content={"detail": "memoria_unavailable"})
        try:
            row = store.get(body.id, raising=True)
        except sqlite3.Error:
            return JSONResponse(status_code=503, content={"detail": "memoria_unavailable"})
        # Same 404 for a wrong-profile row as for a missing one (R7: no
        # cross-profile existence oracle). This pre-check owns the "already
        # gone" case, so delete_row's False below can only mean a real write
        # failure — never "the row wasn't there" (delete_row is idempotent).
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
        store = _memoria_store_or_none()
        if store is None:
            return JSONResponse(status_code=503, content={"detail": "memoria_unavailable"})
        try:
            row = store.get(body.id, raising=True)
        except sqlite3.Error:
            return JSONResponse(status_code=503, content={"detail": "memoria_unavailable"})
        # Same 404 for a wrong-profile row as for a missing one (R7: no
        # cross-profile existence oracle — identical shape and detail).
        if row is None or row["profile_id"] != body.profile_id:
            return JSONResponse(status_code=404, content={"detail": "memoria not found"})
        # F5: update_row promotes status='curated' in the same statement. A
        # genuine write failure raises (raising=True) -> 503; a plain False now
        # means only rowcount==0 (the row vanished in the check-then-act race)
        # -> 404, never a misleading write-failed.
        try:
            applied = store.update_row(body.id, title=title, content=content, raising=True)
        except sqlite3.Error:
            return JSONResponse(status_code=503, content={"detail": "memoria_write_failed"})
        if not applied:
            return JSONResponse(status_code=404, content={"detail": "memoria not found"})
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

    @app.get("/api/memoria/notice", response_model=MemoriaNoticeResponse)
    def get_memoria_notice() -> MemoriaNoticeResponse:
        # Fail-open to False (banner shows) when the flag file is absent — no
        # host state, works even before host init. No lock: single-flag read.
        return MemoriaNoticeResponse(dismissed=load_memorias_notice_dismissed())

    @app.post("/api/memoria/notice", response_model=MemoriaNoticeResponse)
    def post_memoria_notice(body: MemoriaNoticeRequest) -> MemoriaNoticeResponse:
        # Atomic single-flag write (os.replace), no read-modify-write, so no
        # lock. Re-read from disk so the response reflects actual persisted
        # state (save swallows write errors by design — CTK fail-open parity).
        save_memorias_notice_dismissed(body.dismissed)
        return MemoriaNoticeResponse(dismissed=load_memorias_notice_dismissed())

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
            matching = [t for t in valid if t.mood == mood]
            suggested = library.select_for_mood(mood)
            fallback = not matching
            if fallback:
                # Mirror select_for_mood's mood->normal->any fallback chain so
                # `tracks` and `suggested_track_id` stay consistent — an empty
                # bucket must never leave the client rotating over nothing.
                normal_pool = [t for t in valid if t.mood == "normal"]
                pool = normal_pool if normal_pool else valid
            else:
                pool = matching
        return MusicMoodResponse(
            active_mood=mood,
            tracks=[_music_track_out(t) for t in pool],
            suggested_track_id=suggested.id if suggested else None,
            fallback=fallback,
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
        # WU5: dedup pre-check under the lock, then do the (up-to-200MB) copy
        # OUTSIDE the lock so it never blocks concurrent control-plane reads,
        # then re-take the lock only to register the finished track. stage_copy
        # re-validates the audio signature and copies into the managed dir under
        # a uuid name (traversal-proof); register_staged rechecks dedup and rolls
        # back (pop + unlink) on save failure. No backend audio.
        with host.music_lock:
            existing = library.find_existing(source, mood)
        if existing is not None:
            return MusicImportResponse(track=_music_track_out(existing))
        try:
            staged = library.stage_copy(source)
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc)})
        except OSError:
            return JSONResponse(status_code=503, content={"detail": "music_write_failed"})
        try:
            with host.music_lock:
                track = library.register_staged(staged, source, mood)
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
                removed = library.remove(track_id, delete_file=True, raising=True)
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
        # WU5/D8 TOCTOU: resolve, validate, AND open the file handle all under
        # the lock, then hand the handle to FileResponse via BackgroundTask so
        # it is closed only when the stream finishes. On Windows the open handle
        # blocks a concurrent DELETE's unlink -> that DELETE gets a retryable
        # 503 instead of pulling the file out from under a live stream (500).
        # ponytail: handle-as-delete-guard is Windows semantics; POSIX would
        # need fd-based serving to get the same interlock.
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
            try:
                fh = track_path.open("rb")
            except OSError:
                return JSONResponse(status_code=404, content={"detail": "track not found"})
        media_type = mimetypes.guess_type(str(track_path))[0] or "application/octet-stream"
        return FileResponse(
            track_path, media_type=media_type, background=BackgroundTask(fh.close)
        )

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
                # CTK parity decision 2026-07-05 (Option A): a POSTed topic is
                # operator-approved and goes straight to the queue, matching the
                # UI label "Agregar a cola" and app_shell.py:1317-1318
                # (add_topic(approved=True) + queue_topic). Only QUEUED topics
                # are selectable by the driver (_select_next_topic).
                topic = agenda.add_topic(
                    body.title,
                    angle=body.angle or "",
                    constraints=body.constraints or [],
                    priority=body.priority or "normal",
                    response_length=body.response_length or "normal",
                    approved=True,
                )
            except ValueError as exc:
                return JSONResponse(status_code=422, content={"detail": str(exc)})
            # Idempotent: a dedup that returns an already-QUEUED (or otherwise
            # non-APPROVED) topic must not re-queue — queue_topic only accepts
            # APPROVED and would raise on a repeat POST (WU4 idempotency test).
            if topic.status == TopicStatus.APPROVED:
                agenda.queue_topic(topic.id)
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
                    # CTK parity decision 2026-07-05 (Option A): approving a
                    # drafted suggestion also queues it in one step, mirroring
                    # topic_inbox_bridge.py:138-139. approve_topic raises on a
                    # non-DRAFTED topic (surfaced as 422 below), so a repeat
                    # never double-queues.
                    agenda.approve_topic(body.topic_id)
                    agenda.queue_topic(body.topic_id)
                elif body.action == "queue":
                    agenda.queue_topic(body.topic_id)
                elif body.action == "remove":
                    agenda.remove_queued_topic(body.topic_id)
                elif body.action == "move":
                    agenda.move_queued_topic(body.topic_id, body.direction or 1)
                elif body.action == "reject":
                    agenda.reject_topic(body.topic_id)
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

    @app.get("/api/agenda/cohost-profiles", response_model=CohostProfilesResponse)
    def get_cohost_profiles() -> CohostProfilesResponse:
        # Disk-only read; no host needed. Falls back to the 3 defaults when the
        # file is absent. No `selected` field — selection is stateless (RAM).
        with _cohost_profiles_lock:
            return _cohost_profiles_response(load_cohost_profiles())

    @app.post("/api/agenda/cohost-profiles", response_model=CohostProfilesResponse)
    def save_cohost_profile(body: CohostProfileSaveRequest):
        clean = sanitize_profile_name(body.name)
        if not clean:
            return JSONResponse(status_code=422, content={"detail": "empty profile name"})
        with _cohost_profiles_lock:
            profiles = load_cohost_profiles()
            profiles[clean] = normalize_cohost_profile(
                {
                    "style": body.style,
                    "default_priority": body.priority or "normal",
                    "default_response_length": body.length or "normal",
                }
            )
            if not save_cohost_profiles(profiles):
                return JSONResponse(status_code=503, content={"detail": "cohost_write_failed"})
            return _cohost_profiles_response(profiles)

    @app.post("/api/agenda/cohost-profiles/select", response_model=CohostProfileSelectResponse)
    def select_cohost_profile(request: Request, body: CohostProfileSelectRequest):
        host = request.app.state.host
        agenda = getattr(host, "agenda", None)
        if agenda is None:
            return JSONResponse(status_code=503, content={"detail": "agenda_unavailable"})
        # Load under the disk lock but NEVER write — selection is stateless.
        with _cohost_profiles_lock:
            profiles = load_cohost_profiles()
        if body.name not in profiles:
            return JSONResponse(status_code=404, content={"detail": "profile not found"})
        with host.agenda_lock:
            try:
                agenda.set_profile({"style": profiles[body.name]["style"]})
            except ValueError as exc:
                return JSONResponse(status_code=422, content={"detail": str(exc)})
        return CohostProfileSelectResponse(selected=body.name)

    @app.post("/api/agenda/session/action", response_model=AgendaResponse)
    def post_agenda_session_action(request: Request, body: AgendaSessionActionRequest):
        host = request.app.state.host
        agenda = getattr(host, "agenda", None)
        if agenda is None:
            return JSONResponse(status_code=503, content={"detail": "agenda_unavailable"})
        if body.action not in _AGENDA_SESSION_ACTION_WHITELIST:
            return JSONResponse(status_code=422, content={"detail": "unknown action"})
        motor = getattr(host, "motor", None)
        with host.agenda_lock:
            if body.action == "enable":
                # CTK parity (app_shell.py:1415-1419): never start a session with
                # an empty queue and no active topic. Return 200 with an explicit
                # not-applied outcome so the UI can explain, instead of silently
                # flipping to IDLE with nothing to say.
                if not agenda.queued_topics() and not agenda.active_topic:
                    return _agenda_response(agenda, applied=False, reason="empty_queue")
                agenda.enable()
                if agenda.state == AgendaState.OFF:
                    # Fail-closed gate refused (GUARDRAILS_MISSING) — mirror the
                    # empty_queue branch above: honest applied=False instead of
                    # claiming success while the state never left OFF.
                    return _agenda_response(agenda, applied=False, reason="guardrails_missing")
                # CTK parity (app_shell.py:1432): tick immediately on enable so
                # Kira opens the first topic without waiting out the driver
                # cadence. nudge() just sets an Event — safe under the lock.
                driver = getattr(host, "_agenda_driver", None)
                if driver is not None:
                    driver.nudge()
                return _agenda_response(agenda, applied=True)
            if body.action == "soft_stop":
                # FIX-C: the headless host now HAS a speech pipeline (chat works),
                # so enqueue the closing line the controller returns instead of
                # dropping it (mirror app_shell.py:1435). A none() action (agenda
                # already idle) is a no-op inside enqueue_agenda_action.
                enqueue_agenda_action(motor, agenda.soft_stop())
                return _agenda_response(agenda, applied=True)
            # emergency_stop
            agenda.emergency_stop()
            # Mirror app_shell.py:1494-1497: interrupt in-flight speech and drop
            # any pending agenda turns from the motor queue.
            if motor is not None:
                if hasattr(motor, "interrupt_speaking"):
                    motor.interrupt_speaking()
                if hasattr(motor, "drop_pending_sources"):
                    motor.drop_pending_sources(("kira-agenda",))
            return _agenda_response(agenda, applied=True)

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
            # FIX-A: remember this as the last-used profile so the next engine
            # boot seeds it (best-effort — persistence must never fail the
            # switch; save_last_profile already swallows, the guard is defense
            # in depth).
            try:
                save_last_profile(body.name)
            except Exception:
                pass
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
    def put_obs_config(request: Request, body: ObsConfigRequest) -> ObsConfigResponse:
        with _config_lock:
            try:
                cfg = load_avatar_config(strict=True)
            except AvatarConfigUnreadableError:
                return JSONResponse(status_code=503, content={"detail": "config_unreadable"})
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
            try:
                save_avatar_config(cfg)
            except (OSError, RuntimeError):
                return JSONResponse(status_code=503, content={"detail": "config_write_failed"})
            response = _obs_config_response(cfg)
        # FIX-B: push the saved config to the live OBS runtime (outside the
        # write-lock so an OBS reconnect never holds the shared-yaml lock).
        _apply_avatar_runtime(request)
        return response

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
    def put_avatar_config(request: Request, body: AvatarConfigRequest):
        if body.state_images is not None:
            unknown = sorted(set(body.state_images) - VALID_STATES)
            if unknown:
                return JSONResponse(
                    status_code=422, content={"detail": f"unknown avatar state(s): {unknown}"}
                )
        with _config_lock:
            try:
                cfg = load_avatar_config(strict=True)
            except AvatarConfigUnreadableError:
                return JSONResponse(status_code=503, content={"detail": "config_unreadable"})
            if body.enabled is not None:
                cfg.enabled = body.enabled
            if body.mode is not None:
                cfg.mode = body.mode
            if body.state_images is not None:
                new_state_images = dict(cfg.state_images)
                for state, path in body.state_images.items():
                    new_state_images[state] = Path(path)
                cfg.state_images = new_state_images
            try:
                save_avatar_config(cfg)
            except (OSError, RuntimeError):
                return JSONResponse(status_code=503, content={"detail": "config_write_failed"})
            response = _avatar_config_response(cfg)
        # FIX-B: a live OBSClient snapshots state_images at construction, so an
        # avatar-card change needs a full runtime rebuild, not just a reconnect.
        _apply_avatar_runtime(request)
        return response

    # ── Agent gateway (agent_context_gateway Phase 2) ────────────────────
    # Token gating lives in auth_middleware rule 1: everything under
    # /api/agent/ requires the agent OR operator token (always enforced),
    # and mutating calls are rate limited per token (429 on breach).

    @app.post("/api/agent/topics", response_model=AgentTopicResponse)
    def post_agent_topic(body: AgentTopicRequest):
        # Owner decision D1: this is the ONLY topic path agents get. The
        # proposal lands in the human-gated topic_inbox as 'proposed' and
        # the operator approves it in the app UI. POST /api/agenda/topic
        # (auto-approve + queue) stays operator-token-only and is never
        # called from here.
        try:
            store = TopicInboxStore(EDITORIAL_CARDS_DB)
            # ponytail: read-before-write dedupe detection instead of a store
            # API change; single-process local app, the race window is moot.
            prior_ids = {row["id"] for row in store.list_all(status_filter="proposed")}
            row = store.propose(body.title, body.angle, body.tags, source=body.agent)
        except TopicInboxValidationError as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc)})
        except TopicInboxCapError as exc:
            # Cap semantics for agents are "back off, tell your human" —
            # retry-later 429 fits better than 409 (design, endpoint
            # contracts).
            return JSONResponse(status_code=429, content={"detail": str(exc)})
        except (sqlite3.Error, OSError):
            return JSONResponse(
                status_code=503, content={"detail": "topic_inbox_unavailable"}
            )
        return AgentTopicResponse(
            id=row["id"], status=row["status"], deduped=row["id"] in prior_ids
        )

    @app.get("/api/agent/status", response_model=AgentStatusResponse)
    def get_agent_status():
        # Read-only with any valid token; counts only (no topic/card text).
        # Both reads are bounded + fail-open (0 / {}) like the memoria stats
        # helpers they reuse.
        return AgentStatusResponse(
            topics_pending=_count_sql(
                EDITORIAL_CARDS_DB,
                "SELECT COUNT(*) FROM topic_inbox WHERE status='proposed'",
            ),
            cards=_editorial_cards_by_status(EDITORIAL_CARDS_DB),
            notices_undismissed=_count_sql(
                EDITORIAL_CARDS_DB,
                "SELECT COUNT(*) FROM agent_notices WHERE status='proposed'",
            ),
        )

    @app.post("/api/agent/cards", response_model=AgentCardResponse)
    def post_agent_card(body: AgentCardRequest):
        # Card content validation is owned by the EditorialCard dataclass
        # (same rules as the CLI); its error string becomes the 422 detail.
        try:
            expires = _parse_iso8601_utc(body.expires_at)
        except ValueError:
            return JSONResponse(
                status_code=422,
                content={"detail": "expires_at is not a valid ISO-8601 timestamp"},
            )
        try:
            card = EditorialCard(
                topic=body.topic,
                summary=body.summary,
                streamer_take=body.streamer_take,
                counterpoints=body.counterpoints,
                discussion_hooks=body.discussion_hooks,
                triggers=body.triggers,
                expires_at=expires,
                origin=body.agent,
            )
        except EditorialCardValidationError as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc)})
        try:
            store = EditorialCardStore(EDITORIAL_CARDS_DB)
            stored = store.upsert(card)
            # Demotion rule (design 'POST /api/agent/cards'): upsert
            # preserves existing status, so an agent rewriting an ARMED/
            # ACTIVE card would silently keep it auto-firing with
            # unreviewed content. Force it back to DRAFT — the operator
            # re-arms via CLI/UI. New cards are already DRAFT (no-op).
            demoted = store.demote_to_draft(stored.id)
        except (sqlite3.Error, OSError):
            return JSONResponse(
                status_code=503, content={"detail": "editorial_cards_unavailable"}
            )
        return AgentCardResponse(
            id=stored.id,
            topic_slug=stored.topic_slug,
            status=EditorialCardStatus.DRAFT.value if demoted else stored.status.value,
            demoted=demoted,
        )

    @app.post("/api/agent/notice", response_model=AgentNoticeResponse)
    def post_agent_notice(body: AgentNoticeRequest):
        try:
            store = AgentNoticeStore(EDITORIAL_CARDS_DB)
            # ponytail: read-before-write dedupe detection, same pattern as
            # POST /api/agent/topics — single-process local app.
            prior_ids = {row["id"] for row in store.list_all(status_filter="proposed")}
            row = store.propose(body.text, source=body.agent)
        except AgentNoticeValidationError as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc)})
        except AgentNoticeCapError as exc:
            return JSONResponse(status_code=429, content={"detail": str(exc)})
        except (sqlite3.Error, OSError):
            return JSONResponse(
                status_code=503, content={"detail": "agent_notices_unavailable"}
            )
        return AgentNoticeResponse(id=row["id"], deduped=row["id"] in prior_ids)

    @app.get("/api/agent/notices", response_model=AgentNoticesResponse)
    def get_agent_notices(request: Request):
        # Operator surface: the agent token can CREATE notices but never
        # read the board back (other agents' notices are not its business).
        if _agent_request_role(request) != "operator":
            return JSONResponse(
                status_code=403, content={"detail": "operator token required"}
            )
        pending = AgentNoticeStore(EDITORIAL_CARDS_DB).list_pending()
        return AgentNoticesResponse(
            notices=[
                AgentNoticeOut(
                    id=row["id"],
                    text=row["text"],
                    source=row["source"],
                    created_at=row["created_at"],
                )
                for row in pending["valid"]
            ]
        )

    @app.post(
        "/api/agent/notices/{notice_id}/dismiss",
        response_model=AgentNoticeDismissResponse,
    )
    def dismiss_agent_notice(notice_id: str, request: Request):
        # Operator surface: a self-dismissing agent could hide its own
        # messages from the human — only the operator token may dismiss.
        if _agent_request_role(request) != "operator":
            return JSONResponse(
                status_code=403, content={"detail": "operator token required"}
            )
        try:
            dismissed = AgentNoticeStore(EDITORIAL_CARDS_DB).dismiss(notice_id)
        except (sqlite3.Error, OSError):
            return JSONResponse(
                status_code=503, content={"detail": "agent_notices_unavailable"}
            )
        if not dismissed:
            return JSONResponse(
                status_code=404, content={"detail": "notice_not_found"}
            )
        return AgentNoticeDismissResponse(dismissed=True)

    return app


app = create_app()
