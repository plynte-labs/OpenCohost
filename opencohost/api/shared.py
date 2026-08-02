"""Shared state and pure helpers for `main.py` and every router
(refactor_core_api_20260802, B5 Part B).

Breaks the routers<->main module-level import cycle: wave-1 routers
(avatar/obs/llm_provider/status) previously did `from opencohost.api.main
import <name>` at THEIR module level. That only works when `main.py` is the
entry point of the import graph -- its own `app = create_app()` at the
bottom lazily imports the routers package AFTER main's own top-level code
has already populated these names. Importing a router FIRST on a fresh
interpreter (e.g. `from opencohost.api.routers import agent`) instead
triggers `opencohost.api.routers/__init__.py`, which imports EVERY router
module in turn -- including one whose `from opencohost.api.main import X`
then re-enters `opencohost.api.main`'s own top-level execution, which itself
tries to import `opencohost.api.routers` (for `ALL_ROUTERS`) while THAT
package is still mid-import -- ImportError, name not yet defined
(`tests/test_routers_import_order.py` pins this on a fresh subprocess).

Every name below is a PLAIN object: never monkeypatched on `opencohost.api.
main` by any test (confirmed by grepping tests/ for `setattr(main_mod,
"<name>", ...)` before moving each one here), so both `main.py` and every
router can bind it directly at module load time with no read-through seam
required. `main.py` re-exports each name via a plain import so any existing
code or test that reads it off `opencohost.api.main` still resolves it
exactly as before the move (e.g. `test_api_obs_timeout.py` calls
`main_mod._test_obs_connection_bounded` directly as a unit test of the
helper itself, and `main.py`'s own memoria/stream code still calls the
module-level `logger`).

A name that IS monkeypatched on `main` (e.g. `cargar_perfiles`,
`EDITORIAL_CARDS_DB`, `_MUSIC_IMPORT_MAX_BYTES`) stays OUT of this module --
see `opencohost.api.deps`'s late-import accessor pattern for those; a plain
import here would freeze the pre-patch value at router import time.
"""

from __future__ import annotations

import concurrent.futures
import logging
import os
import re
import sqlite3
import threading
from contextlib import closing
from typing import Optional

from fastapi import Request

from opencohost.api.models import (
    AvatarConfigResponse,
    CtxTelemetryOut,
    LlmProviderProfileOut,
    LlmProviderResponse,
    ObsConfigResponse,
)
from opencohost.avatar.obs_client import OBSClient
from opencohost.stream_admin.oauth_store import OAuthStore

# Child of the "opencohost.api" tree that setup_api_logging() attaches
# handlers to and raises to INFO -- no separate wiring needed here.
# `logging.getLogger` caches loggers by name, so this is the SAME logger
# object `logging.getLogger("opencohost.api.main")` would return from
# anywhere else -- moving the call site never changes identity, and every
# caller (routers, main.py, caplog scoping in tests) still logs/matches
# under the exact "opencohost.api.main" name.
logger = logging.getLogger("opencohost.api.main")

# Tier-C config write-lock (WS3 slice 2): guards every PUT to avatar.yaml
# (OBS + avatar config share the one file) so a read-modify-write is atomic
# across concurrent requests. Reads do not need it -- only read-modify-write.
_config_lock = threading.Lock()

# profiles.json write-lock (WU5): guards every read-modify-write of the
# profiles file (create/update/delete). Deliberately NOT `_config_lock`,
# which is avatar.yaml-specific -- a separate file gets a separate lock (D4).
_profiles_lock = threading.Lock()

# personalization.json write-lock (Phase 2, kira_personalization_onboarding):
# guards every read-modify-write of the global personalization store. A
# separate file gets a separate lock (D4).
_personalization_lock = threading.Lock()

# llm_provider.json write-lock (multi_provider_llm_20260723 Phase 1): guards
# every read-modify-write of the provider/profiles config. A separate file
# gets a separate lock (D4).
_llm_provider_lock = threading.Lock()

# PUT /api/llm/provider profile ids (multi_provider_llm_20260723 design
# 'Provider Config Surface'). "local" is reserved for the local-only selector.
_PROFILE_ID_RE = re.compile(r"^[a-z0-9_]+$")

# GET /api/memoria/stats (main.py) + GET /api/agent/status (routers/agent.py)
# sqlite reads -- short, mirrors memoria_store.py's READ_TIMEOUT_SECONDS
# (bounded, fail-open to a zero count/empty dict).
_STATS_DB_READ_TIMEOUT_SECONDS = 0.5

# POST /api/obs/test bound: the socket timeout is now threaded into
# OBSClient.test_connection (obsws ws.connect self-bounds), so the worker
# returns on its own. This executor is only a backstop for anything the socket
# timeout can't cover -- and it must NOT join a stuck worker: a `with` block's
# shutdown(wait=True) would re-block on the very thread we timed out on, so we
# manage the executor manually and shutdown(wait=False, cancel_futures=True).
_OBS_TEST_TIMEOUT_SECONDS = 5.0


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


def _display_model(host, provider_state: Optional[dict] = None) -> Optional[str]:
    """Resolve the model name to REPORT to clients (display bug fix).

    Mirrors the active provider so `/api/status` and `/api/models` can never
    drift apart again: cloud active -> the active profile's OWN `model`
    (never another profile's, never the engine's stale local bookkeeping
    attr); local active (or no provider config) -> the engine's own
    `current_model`. Degrades to None if the active cloud profile is
    missing or its `model` is blank — same graceful degradation
    `/api/models` already had.

    F4 fix (runtime_findings_batch_20260731 1.3): reads the engine's LIVE
    effective posture via `MotorVocalIA.provider_runtime_state()` — never the
    persisted `llm_provider.json`. That disk read is exactly what kept this
    function reporting the stale cloud model name through an active
    `_cloud_fallback_active` fallback. `provider_state` lets a caller that
    already computed it (get_status) pass it through instead of a second call.
    """
    state = provider_state if provider_state is not None else host.motor.provider_runtime_state()
    return state["generation_model"]


def _derive_session_mode(
    agenda_off: bool,
    is_speaking: bool,
    is_processing: bool,
    llm_generating: bool,
    pending_commands_count: int,
) -> str:
    """Unit 2.5 (runtime_findings_batch_20260731 F13): a session-level mode
    ABOVE `AgendaState.OFF`. Owner ruling: "agenda finalizada" (OFF) already
    means exactly what it says and stays; what's missing is the verdict on
    whether KIRA is still doing anything after it. DERIVED here, every call —
    never a stored field, or it would drift from the booleans it summarises
    (the exact failure class F4's `_display_model` bug already was).

    ponytail: accepted risks (plan's 2.5 table), not fixed here:
      - Queue.qsize() is documented racy under concurrency; a display
        refreshed every 1.5-2s tolerates one stale tick, it is not a scheduler.
      - agenda state (1500ms poll) and this status (2000ms poll) are read on
        the front from two different cadences, so they can briefly disagree —
        at most a one-tick post-agenda/inactiva flicker.
      - background pregen workers never touch command_queue — llm_generating
        is what covers that gap, which is why it is required below, not optional.
      - a queued-but-never-executed item (drained/cancelled/epoch-invalidated)
        still reads as busy — failing toward "not idle" is the safe direction:
        a late `inactiva` is only slow, a false one is a lie.
    """
    if not agenda_off:
        return "agenda"
    if is_speaking or is_processing or llm_generating or pending_commands_count > 0:
        return "post-agenda"
    return "inactiva"


def _ctx_telemetry_out(motor) -> Optional[CtxTelemetryOut]:
    """Unit 2.5: `motor.ctx_telemetry_snapshot()["latest"]` (unit 2.3), narrowed
    to the wire shape. Defensively typed (not a bare `**latest`) so a test
    double / older motor without a real ring degrades to None instead of a 500.
    """
    snapshot_fn = getattr(motor, "ctx_telemetry_snapshot", None)
    if not callable(snapshot_fn):
        return None
    snapshot = snapshot_fn()
    latest = snapshot.get("latest") if isinstance(snapshot, dict) else None
    if not isinstance(latest, dict):
        return None
    try:
        return CtxTelemetryOut(
            ratio=latest["ratio"],
            effective_ctx=latest["effective_ctx"],
            native_ctx=latest["native_ctx"],
            evicted_pairs=latest["evicted_pairs"],
            source=latest["source"],
        )
    except (KeyError, TypeError, ValueError):
        return None


def _llm_provider_response(cfg: dict, key_store: OAuthStore) -> LlmProviderResponse:
    """Build the never-echoes-a-key response (multi_provider_llm_20260723).

    `api_key_set` is derived per profile from the SEPARATE key store — the
    provider config dict itself never carries a key, mirroring
    `_obs_config_response`'s `password_set` derivation below.
    """
    profiles_out = {
        pid: LlmProviderProfileOut(
            base_url=str(prof.get("base_url", "")),
            model=str(prof.get("model", "")),
            preset=prof.get("preset"),
            api_key_set=key_store.has_token(pid),
        )
        for pid, prof in cfg.get("profiles", {}).items()
    }
    return LlmProviderResponse(
        active_provider=cfg.get("active_provider", "local"),
        fallback_mode=cfg.get("fallback_mode", "auto"),
        pregen_enabled=bool(cfg.get("pregen_enabled", False)),
        profiles=profiles_out,
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


def _avatar_config_response(cfg) -> AvatarConfigResponse:
    return AvatarConfigResponse(
        enabled=cfg.enabled,
        mode=cfg.mode,
        assets_folder=str(cfg.assets_folder),
        state_images={state: str(path) for state, path in cfg.state_images.items()},
    )
