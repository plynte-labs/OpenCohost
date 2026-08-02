"""Standalone FastAPI app exposing Kira's engine control surface (Phase 1).

Run form (REQUIRED -- see design v2.1 B-SF2):

    uvicorn opencohost.api.main:app --host 127.0.0.1 --port 8765 --workers 1

WARNING: binding `--host 0.0.0.0` exposes the engine control surface to the
LAN. CORS only defends BROWSER callers -- it does nothing against curl or a
script hitting the port directly. Keep this process on loopback unless a
separate authenticating proxy sits in front of it. `--workers` MUST stay at
1: a second worker means a second `MotorVocalIA` (second Ollama load +
audio device grab) racing the first. `EngineHost`'s lockfile enforces this
across processes; the `WEB_CONCURRENCY`/`UVICORN_WORKERS` env check and the
in-process `_host_active` guard below are the best-effort in-process layers.

R8 (carried non-negotiable, binds Phase 2+): no raw viewer chat may ever be
exposed over HTTP. Any future `/api/memorias` or `/api/history` endpoint
MUST reuse the T1 provenance gate verbatim -- the `_DIGEST_CAPTURE_SOURCES`
allowlist (opencohost/core/llm_engine.py) and the `memory_inspector_snapshot`
policy are the ONLY provenance gates; never re-derived.

This package NEVER imports `opencohost.ui` or `customtkinter`. The engine
is constructed only inside `lifespan()` -- never at import time -- so
importing this module has zero side effects on hardware/VRAM/Ollama.
"""

import concurrent.futures
import os
import sqlite3
import threading
from contextlib import asynccontextmanager

import ollama
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from opencohost.api.auth import auth_middleware, ensure_tokens
from opencohost.api.dispatch import Dispatcher
from opencohost.api.engine_host import EngineHost, EventLogSink
from opencohost.api.observability import audit_middleware, setup_api_logging
from opencohost.api.ptt_session import PttController, probe_stt_ws
# `opencohost.api.routers.avatar`/`.obs` own VALID_STATES/AvatarConfigUnreadableError/
# load_avatar_config/save_avatar_config directly now (refactor_core_api_20260802 B4)
# -- this module has no remaining caller for them. `OBSClient` stays imported
# even though `_test_obs_connection_bounded` (its last direct caller) moved
# to `opencohost.api.shared` in B5 Part B: `opencohost.api.deps.
# obs_client_cls()` reads `main.OBSClient` at call time, and
# tests/test_api_obs.py monkeypatches it directly on THIS module across most
# of its POST /api/obs/test cases.
from opencohost.avatar.obs_client import OBSClient
# `save_provider_config` moved with PUT /api/llm/provider to
# `opencohost.api.routers.llm_provider`, which imports it directly.
# `load_provider_config` stays: `opencohost.api.deps.load_provider_config()`
# reads it off THIS module at call time (GET /api/models replaces the whole
# function in tests/test_api_reads.py, so the router can't bind it at its
# own import time -- see deps.py's module docstring).
from opencohost.config.llm_provider import load_provider_config
from opencohost.config.settings import (
    EDITORIAL_CARDS_DB,
    # EXPERIMENTAL_HEAVY_TTS_ENABLED/LLM_KEYS_FILE/load_piper_voice/
    # load_tts_local_only/load_tts_speed are read by opencohost.api.deps at
    # call time off THIS module (tests monkeypatch them here, not at their
    # settings.py source) -- kept even though no code in this file calls
    # them directly anymore.
    EXPERIMENTAL_HEAVY_TTS_ENABLED,
    LLM_KEYS_FILE,
    # MEMORIAS_DB/MEMORIAS_ENABLED/MEMORIAS_IMPORT_CAP/save_ptt_ws_uri are
    # ALSO monkeypatched on `main` (every memoria test; test_api_ptt.py's
    # write-failure case) -- deps.memorias_db()/.memorias_enabled()/
    # .memorias_import_cap()/.save_ptt_ws_uri() read them off THIS module at
    # call time for routers/memoria.py and routers/ptt.py.
    MEMORIAS_DB,
    MEMORIAS_ENABLED,
    MEMORIAS_IMPORT_CAP,
    _canonical_model_tag,
    load_piper_voice,
    # load_ptt_ws_uri: used directly below by lifespan() (never
    # monkeypatched); routers/ptt.py imports it separately from settings.
    load_ptt_ws_uri,
    load_tts_local_only,
    save_ptt_ws_uri,
    load_tts_speed,
)
# `build_signature`/`build_title`/`derive_import_key`/`is_capturable` moved
# with POST /api/memoria/import to `opencohost.api.routers.memoria`, which
# imports them directly (never monkeypatched). `MemoriaStore` stays: it is
# constructed by `_get_memoria_store()` below.
from opencohost.core.memoria_store import MemoriaStore
# `clear_personalization`/`save_personalization`/`cargar_perfiles` stay
# imported here (never called by this file's own code anymore) because all
# three ARE monkeypatched directly on `main` -- opencohost.api.deps'
# accessors read them off THIS module at call time for
# routers/personalization.py, routers/perfiles.py, and routers/memoria.py's
# `_legacy_profile_key` (refactor_core_api_20260802 B6).
from opencohost.core.personalization import clear_personalization, save_personalization
from opencohost.core.profiles import cargar_perfiles
# `logger`, the cross-family write locks, `_PROFILE_ID_RE`, `_count_sql`,
# `_editorial_cards_by_status`, `_MEMORIA_TITLE_MAX_LENGTH`/
# `_MEMORIA_CONTENT_MAX_LENGTH`, and the handful of plain response-builder
# helpers below live in `opencohost.api.shared` (B5 Part B / B6) so both
# this module and every router can import them without either importing the
# other -- see shared.py's docstring. Names with a live reader off
# `opencohost.api.main` (e.g. `main_mod._test_obs_connection_bounded`,
# `main_mod._MEMORIA_TITLE_MAX_LENGTH`) MUST stay; the rest of the block
# (locks, response builders, `_count_sql`, `logger`, ...) currently has no
# external reader and is kept only as inert back-compat surface -- safe to
# prune in a later cleanup if grep still finds no reader.
from opencohost.api.shared import (
    _MEMORIA_CONTENT_MAX_LENGTH,
    _MEMORIA_TITLE_MAX_LENGTH,
    _OBS_TEST_TIMEOUT_SECONDS,
    _PROFILE_ID_RE,
    _STATS_DB_READ_TIMEOUT_SECONDS,
    _apply_avatar_runtime,
    _avatar_config_response,
    _config_lock,
    _count_sql,
    _ctx_telemetry_out,
    _derive_session_mode,
    _display_model,
    _editorial_cards_by_status,
    _llm_provider_lock,
    _llm_provider_response,
    _obs_config_response,
    _personalization_lock,
    _profiles_lock,
    _test_obs_connection_bounded,
    logger,
)

# GET /api/models external I/O bound -- short client timeout so a stalled/
# unreachable Ollama daemon degrades the response instead of pinning a
# threadpool thread (design v2.1 Tier B resilience).
_OLLAMA_DISCOVERY_TIMEOUT_SECONDS = 2.5


def _discover_ollama_models(timeout: float = _OLLAMA_DISCOVERY_TIMEOUT_SECONDS) -> list[str]:
    """Best-effort live Ollama discovery, bounded by a short client timeout.

    Mirrors the `ollama.Client(timeout=...)` pattern MotorVocalIA already
    uses for chat calls (llm_engine.py `_create_ollama_chat_client`).
    Degrades to `[]` on ANY failure (timeout, connection error, malformed
    response) -- never raises, never hangs the request thread.
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


# Lazy module-level MemoriaStore singleton (design D1). MemoriaStore.__init__
# mkdirs + runs CREATE TABLE / PRAGMA writes, so it must NEVER be constructed
# at import time (module contract: importing has zero side effects). One shared
# instance is thread-safe across FastAPI's threadpool -- the store opens a fresh
# connection per operation and guards its warn-once state with its own lock --
# and preserves that warn-once episode state (a per-request instance would
# reset it every call). Stays HERE, not moved with the rest of the memoria
# family to routers/memoria.py in B6: tests reset it with a bare
# `main_mod._memoria_store = None` fixture and monkeypatch
# `_get_memoria_store`/`_memoria_store_or_none` directly on `main` -- both
# only work if the global and the functions closing over it stay put.
# `deps.memoria_store_or_none()` is the router-facing accessor.
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

# POST /api/ptt/test bound -- same shape and the same reasoning as
# _OBS_TEST_TIMEOUT_SECONDS above. `probe_stt_ws` self-bounds via the WS
# open_timeout, so this executor is only a backstop for what that cannot cover
# (a server that completes the TCP/WS handshake and then stalls). It must NOT
# join a stuck worker: shutdown(wait=False, cancel_futures=True), never a
# `with` block, whose shutdown(wait=True) would re-block on the very thread we
# just timed out on.
_PTT_TEST_TIMEOUT_SECONDS = 5.0


def _test_stt_connection_bounded(uri: str, timeout: float = _PTT_TEST_TIMEOUT_SECONDS):
    """Stays HERE, not moved to routers/ptt.py or opencohost.api.shared:
    calls `probe_stt_ws(...)` by bare name, and `probe_stt_ws` IS
    monkeypatched directly on `main` (test_api_ptt.py's hung-server test,
    which also calls `main_mod._test_stt_connection_bounded` directly) --
    unlike `_test_obs_connection_bounded` (shared.py), which receives an
    already-built client instead of resolving a patchable name itself.
    `deps.test_stt_connection_bounded()` is the router-facing accessor.
    """
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(probe_stt_ws, uri, open_timeout=timeout)
        future.result(timeout=timeout + 1.0)  # WS self-bounds; +1s backstop
        return True, "connected"
    except concurrent.futures.TimeoutError:
        return False, "timeout"
    except Exception:
        # Fixed literal, never the raw exception text: a probe failure message
        # can carry bytes echoed from an untrusted remote straight into the
        # operator's UI.
        return False, "unreachable"
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


# POST /api/music/import size cap. `opencohost.api.routers.music` reads this
# through `deps.music_import_max_bytes()` -- test_api_music_library_mutations.py
# monkeypatches it directly on this module (down to 4 bytes so a 12-byte wav
# fixture deterministically exceeds the cap) so it stays defined here rather
# than moving with the rest of the music family.
_MUSIC_IMPORT_MAX_BYTES = 200 * 1024 * 1024


def _check_single_worker() -> None:
    for var in ("WEB_CONCURRENCY", "UVICORN_WORKERS"):
        value = os.environ.get(var, "").strip()
        if value and value not in ("0", "1"):
            raise RuntimeError(f"opencohost.api requires a single worker (found {var}={value!r})")


def create_app(host_factory=EngineHost, cors_origins=None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        global _host_active
        # Persist + redact the opencohost.api logger tree (WU-C) before
        # anything else in startup can log -- idempotent, safe across
        # repeated create_app()/TestClient cycles in one process.
        setup_api_logging()
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
            # PTT single-slot controller: recv-only WhisperLive bridge. ws_uri
            # comes from the operator's persisted choice, falling back to
            # settings.WS_URI when nothing was ever saved (nothing hardcodes
            # 8765/8770 here -- the API's own uvicorn port is separate). Reading
            # it here is what makes PUT /api/ptt/config survive a restart; the
            # same PUT also live-applies it to this instance, so a reconnect
            # never needs a process kill. Dispatches flushes through the SAME
            # process_context path as /api/chat/turn; records lifecycle events
            # (detail=None) on the host event log.
            # getattr fallback mirrors the obs_runtime resiliency in get_status:
            # a real EngineHost always has event_log, but minimal host doubles
            # (health/status test fakes) may not -- never break app startup.
            app.state.ptt_controller = PttController(
                load_ptt_ws_uri(),
                app.state.dispatcher,
                getattr(host, "event_log", None) or EventLogSink(),
                # Recovery hook (2026-07-15 PTT voice-death fix): smallest
                # possible surface into the engine -- a bound method, never
                # the whole motor. getattr fallback mirrors event_log above:
                # a real EngineHost always has motor.mark_audio_suspect, but
                # minimal host doubles in tests may not.
                on_audio_suspect=getattr(
                    getattr(host, "motor", None), "mark_audio_suspect", None
                ),
                # Listening cue (ptt_cue_20260717): same smallest-surface,
                # getattr-guarded pattern -- a bound motor.play_ptt_cue fired
                # when the hold reaches listening, so the operator hears the
                # mic go live even with the app window unfocused.
                on_listening=getattr(
                    getattr(host, "motor", None), "play_ptt_cue", None
                ),
                # WU5 (agenda interruption, ADR-037): position-aware PTT cut.
                # Fires SYNCHRONOUSLY at flush so it can interrupt agenda
                # speech mid-turn (the queued command path can't -- the engine
                # thread is inside _hablar). Same guarded bound-method idiom.
                on_flush_precheck=getattr(
                    getattr(host, "motor", None),
                    "ptt_interrupt_if_agenda_speaking",
                    None,
                ),
            )
            yield
        finally:
            host.stop()
            _host_active = False

    app = FastAPI(lifespan=lifespan, debug=False)
    # Auth gate (agent_context_gateway ADR-4): registered BEFORE CORSMiddleware
    # so CORS -- added last, therefore outermost in Starlette's stack -- still
    # decorates 401/403/503 auth responses and handles OPTIONS preflight.
    app.middleware("http")(auth_middleware)
    # Audit trail (WU-B, api_observability): registered AFTER auth_middleware
    # and BEFORE CORSMiddleware -- later registration = outer in Starlette's
    # stack -- so it wraps auth and audits its 401/403/429/503 rejections too.
    app.middleware("http")(audit_middleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins if cors_origins is not None else _DEFAULT_CORS_ORIGINS,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Idempotency-Key", "Authorization"],
        allow_credentials=False,
    )

    # B4-B6 (refactor_core_api_20260802): every resource family -- all
    # routers in ALL_ROUTERS (opencohost/api/routers/__init__.py) -- mounts
    # here; main.py no longer carries a single inline @app. decorator (B6
    # moved the last five: memoria, ptt, agenda, chat, stream). Routers
    # NEVER import this module at module level: cross-cutting objects come
    # from opencohost.api.shared, and every monkeypatched name goes through
    # a call-time accessor in opencohost.api.deps -- reintroducing a
    # module-level `from opencohost.api.main import ...` in any router
    # recreates the import-order landmine tests/test_routers_import_order.py
    # pins dead. The import below is late only to keep main.py's own import
    # order stable. Parameterized paths exist in several routers
    # (perfiles/{name}, music/track/{track_id}[/audio], agent card/notice
    # actions, memoria/row/{row_id}); none overlaps another route on segment
    # count + literal prefix -- see each router's own docstring.
    from opencohost.api.routers import ALL_ROUTERS

    for _router in ALL_ROUTERS:
        app.include_router(_router)

    return app


app = create_app()
