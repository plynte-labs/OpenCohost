"""/api/ptt/* -- start, keepalive, stop, state, config, test
(moved verbatim from main.py, refactor_core_api_20260802 B6).

Push-to-Talk (liveaudio_ptt_tauri_20260710). PRIVACY (hard rule 2): NO
/api/ptt/* request or response body ever carries transcript text -- the
dictation travels WhisperLive WS -> PttSession buffer (RAM) -> the
process_context dispatch and never crosses HTTP, so the Tauri client
literally never receives it. buffered_chars is an int count; events are
fixed literals with detail=None (recorded by the controller on
host.event_log). POSTs are operator-token tier (auth.py rule 2) and NOT
rate-limited (RateLimiter counts only /api/agent/ mutations); GET stays open
per rule 3.

`save_ptt_ws_uri` IS monkeypatched directly on `opencohost.api.main`
(test_api_ptt.py, `_boom` write-failure case), so PUT /api/ptt/config goes
through `deps.save_ptt_ws_uri()`. `load_ptt_ws_uri`/`is_valid_stt_ws_uri`
(settings.py) are never monkeypatched, so they import straight from their
home module (settings.py is ALSO imported the same way by `main.py`'s own
`lifespan()`, which needs `load_ptt_ws_uri()` to seed the initial
`PttController` -- two independent bindings of the same never-patched name,
same reasoning as every other never-monkeypatched settings import already
duplicated across routers and main.py).

`_test_stt_connection_bounded` and its `_PTT_TEST_TIMEOUT_SECONDS` default
STAY in `opencohost.api.main` -- NOT here, and NOT in `opencohost.api.shared`
-- because the function calls `probe_stt_ws(...)` by its own bare (unqualified)
name, and `probe_stt_ws` IS monkeypatched directly on `main`
(test_api_ptt.py's `test_probe_is_bounded_and_never_blocks_on_a_hung_server`,
which ALSO calls `main_mod._test_stt_connection_bounded` directly). A bare
call only re-resolves through a patched module attribute when both the call
site and the patched name live in the SAME module's global namespace -- so
moving the function anywhere else would silently stop honoring the
`probe_stt_ws` patch (unlike `_test_obs_connection_bounded` in shared.py,
which never calls `OBSClient` by name -- it receives an already-built
client instance as a parameter). POST /api/ptt/test therefore calls it
through `deps.stt_connection_bounded_check()`, a variadic late-import
accessor mirroring `deps.discover_ollama_models()`.

`PttUnreachable`/`SessionActive` (ptt_session.py exceptions) and
`_apply_ptt_ws_uri`/`_ptt_config_lock` are used ONLY by this family and are
never monkeypatched, so they relocate here wholesale. `PttController` and
`probe_stt_ws` itself stay imported in `main.py` too (the former for
`lifespan()`'s controller construction, the latter as `_test_stt_connection_
bounded`'s own dependency, per the paragraph above).

LiveAudio auto-start (liveaudio-service-client track): `POST /api/ptt/test`
probes the effective URI first and, on a LOOPBACK miss, runs one
`ensure_local_service` cycle (spawn-or-scan, runtime-only repoint via
`set_ws_uri`, never persisted) through
`request.app.state.liveaudio_supervisor`. `POST /api/ptt/start` attempts the
hold directly — no pre-probe, so live holds and duplicate presses are never
disturbed — and only on `PttUnreachable` runs that same single ensure cycle
plus exactly one retry. Both consult an in-memory loading gate first
(supervisor alive but ASR starting/loading → bounded wait, then retryable 503
`stt_loading`, the one new contract literal). When the post-ensure outcome is
already `loading`, start answers `stt_loading` immediately with no second
wait and no retry — the model is known-not-ready, the client re-presses.
HTTP worst-case budget: one readiness wait (5 s) + one failed connect
(`ws_open_timeout`, default 2 s + margin) + one port wait (8 s) + bounded
per-port handshakes — never chained waits. Non-loopback (dual-PC remote)
URIs skip everything and behave exactly as before. `GET /api/ptt/state`
additionally reports the additive `stt_service` summary (passive — no
spawn/probe/scan).
"""

import threading
from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from opencohost.api import deps
from opencohost.api.models import (
    PttConfigRequest,
    PttConfigResponse,
    PttKeepaliveRequest,
    PttKeepaliveResponse,
    PttStartResponse,
    PttStateResponse,
    PttStopRequest,
    PttStopResponse,
    PttTestRequest,
    PttTestResponse,
)
from opencohost.api.ptt_session import PttUnreachable, SessionActive
from opencohost.config.settings import is_valid_stt_ws_uri, load_ptt_ws_uri
from opencohost.stt.discovery import ensure_local_service, is_loopback_host, is_loopback_uri

router = APIRouter()

# Serializes writes to ptt_settings.json (shared with the legacy CTK
# PTTManager hotkey store), mirroring _config_lock / _llm_provider_lock.
_ptt_config_lock = threading.Lock()


def _bounded_probe(uri: str):
    """(ok, detail) via the existing bounded probe; never raises."""
    try:
        return deps.stt_connection_bounded_check(uri)
    except Exception:
        return False, "unreachable"


def _ensure_local_stt(request: Request, uri: str):
    """Best-effort auto-start/discovery for LOOPBACK uris (liveaudio-service-
    client track). Returns the EnsureOutcome, or None when the legacy path
    must run untouched (no supervisor attached, non-loopback/remote URI, or
    a controller that cannot be repointed). Never raises, never persists —
    the ephemeral fallback port stays runtime-only by design.
    """
    if not isinstance(uri, str) or not is_loopback_uri(uri):
        return None
    supervisor = getattr(request.app.state, "liveaudio_supervisor", None)
    if supervisor is None:
        return None
    controller = getattr(request.app.state, "ptt_controller", None)
    if getattr(controller, "set_ws_uri", None) is None:
        return None
    try:
        return ensure_local_service(
            configured_uri=uri,
            controller=controller,
            supervisor=supervisor,
            probe=_bounded_probe,
        )
    except Exception:
        return None


def _supervisor_is_loading(request: Request, uri: str) -> bool:
    """In-memory loading gate (no socket, no spawn): True when a supervised
    local service is alive but ASR is still starting/loading. Pure snapshot
    reads, so duplicate presses and live holds are never disturbed."""
    if not isinstance(uri, str) or not is_loopback_uri(uri):
        return False
    supervisor = getattr(request.app.state, "liveaudio_supervisor", None)
    is_loading = getattr(supervisor, "is_loading", None)
    if not callable(is_loading):
        return False
    try:
        return bool(is_loading())
    except Exception:
        return False


def _wait_service_ready(request: Request) -> bool:
    """Bounded wait for ASR ``ready``; False when there is nothing to wait on."""
    supervisor = getattr(request.app.state, "liveaudio_supervisor", None)
    waiter = getattr(supervisor, "wait_for_ready", None)
    if not callable(waiter):
        return False
    try:
        return bool(waiter())
    except Exception:
        return False


def _service_summary(request: Request, stt_ws_url) -> Optional[dict]:
    """Additive ``stt_service`` payload for GET /api/ptt/state (coerced to
    :class:`SttServiceStatus` by the response model). Passive: no spawn, no
    probe, no scan — purely the supervisor snapshot, or the honest mode when
    there is nothing supervised. ``wss://`` loopback reports ``local`` (a
    configured/manual connection, never auto-spawned), like ``remote`` it
    never triggers ensure."""
    if not isinstance(stt_ws_url, str) or not is_loopback_host(stt_ws_url):
        return {"mode": "remote"}
    if not is_loopback_uri(stt_ws_url):
        return {"mode": "local"}
    supervisor = getattr(request.app.state, "liveaudio_supervisor", None)
    status = getattr(supervisor, "status", None)
    if not callable(status):
        return {"mode": "local"}
    try:
        summary = dict(status())
    except Exception:
        return {"mode": "local"}
    summary["mode"] = "auto"
    return summary


def _apply_ptt_ws_uri(request: Request, uri: str) -> None:
    """Push the just-saved WhisperLive URL to the live PttController.

    Same best-effort, doubly-guarded contract as `_apply_avatar_runtime`
    (opencohost.api.shared): called ONLY after a successful save, OUTSIDE the
    config lock, and guarded twice (getattr for the missing attribute,
    try/except for a runtime error) so a controller that cannot be
    repointed -- a minimal test double, or app state without one -- never
    turns a good write into a 500.

    Only the NEXT hold picks the new socket up; an in-flight session keeps
    the URI it was built with (see `PttController.set_ws_uri`).
    """
    controller = getattr(request.app.state, "ptt_controller", None)
    setter = getattr(controller, "set_ws_uri", None)
    if setter is None:
        return
    try:
        setter(uri)
    except Exception:
        pass


@router.post("/api/ptt/start")
def post_ptt_start(request: Request):
    controller = request.app.state.ptt_controller
    state = controller.state() if hasattr(controller, "state") else {}
    uri = state.get("stt_ws_url") if isinstance(state, dict) else None
    # Loading gate FIRST, from memory only (no probe, no spawn): when OUR
    # supervised service is alive but ASR is still loading, wait bounded and
    # answer retryable stt_loading instead of opening a hold whose first
    # seconds would starve. Never disturbs a live hold or a duplicate press.
    if isinstance(uri, str) and _supervisor_is_loading(request, uri):
        if not _wait_service_ready(request):
            return JSONResponse(status_code=503, content={"detail": "stt_loading"})
    try:
        session_id = controller.start()
    except SessionActive:
        return JSONResponse(status_code=409, content={"detail": "session_active"})
    except PttUnreachable:
        pass
    else:
        return PttStartResponse(session_id=session_id, state="listening")
    # Connect failed against the effective URI. For loopback URIs, run ONE
    # ensure cycle (spawn-or-scan, runtime-only repoint) and retry the start
    # exactly once; remote URIs and missing supervisors fall straight through
    # to the honest legacy 503 with the slot freed, as before.
    if isinstance(uri, str):
        outcome = _ensure_local_stt(request, uri)
        if outcome is not None and outcome.available:
            if outcome.loading:
                # Freshly spawned (or discovered) but ASR still loading: fail
                # fast with the retryable literal — no second bounded wait and
                # no start retry here, the model simply is not ready yet and
                # the client re-presses. Keeps the worst case inside budget.
                return JSONResponse(status_code=503, content={"detail": "stt_loading"})
            try:
                session_id = controller.start()
            except SessionActive:
                return JSONResponse(status_code=409, content={"detail": "session_active"})
            except PttUnreachable:
                pass
            else:
                return PttStartResponse(session_id=session_id, state="listening")
        elif outcome is not None and outcome.loading:
            return JSONResponse(status_code=503, content={"detail": "stt_loading"})
    return JSONResponse(status_code=503, content={"detail": "stt_unreachable"})


@router.post("/api/ptt/keepalive")
def post_ptt_keepalive(request: Request, body: PttKeepaliveRequest):
    controller = request.app.state.ptt_controller
    result = controller.keepalive(body.session_id)
    if result is None:
        # Server guillotined this session (watchdog) -- client drops to idle.
        return JSONResponse(
            status_code=409, content={"state": "idle", "detail": "session_not_active"}
        )
    return PttKeepaliveResponse(**result)


@router.post("/api/ptt/stop")
def post_ptt_stop(request: Request, body: Optional[PttStopRequest] = None):
    # ALWAYS 200, fully idempotent: returns immediately, the grace + flush
    # happen in the background watcher. A stop on an unknown/absent session
    # returns state=idle, so client retries and watchdog races never error.
    controller = request.app.state.ptt_controller
    session_id = body.session_id if body is not None else None
    return PttStopResponse(**controller.stop(session_id))


@router.get("/api/ptt/state", response_model=PttStateResponse)
def get_ptt_state(request: Request) -> PttStateResponse:
    # buffered_chars is an int count, NEVER text. GET stays open (rule 3).
    # This is ALSO the read surface for the configured stt_ws_url -- no
    # dedicated GET /api/ptt/config exists, deliberately.
    payload = dict(request.app.state.ptt_controller.state())
    payload["stt_service"] = _service_summary(request, payload.get("stt_ws_url"))
    return PttStateResponse(**payload)


@router.put("/api/ptt/config", response_model=PttConfigResponse)
def put_ptt_config(request: Request, body: PttConfigRequest):
    # Repoint the PTT bridge at a different LiveAudio/WhisperLive server
    # WITHOUT restarting the backend: the operator regularly launches
    # OpenCohost before LiveAudio, and killing the process to recover is
    # the pain this closes (liveaudio_ws_uri_config_20260724).
    #
    # SECURITY: WhisperLive is recv-only and its text flows straight into
    # `process_context` -- the SAME path as a real operator turn (privacy
    # header, opencohost/api/ptt_session.py:1-21). A URL pointed at an
    # arbitrary WS server is therefore a prompt-injection channel
    # impersonating the operator. Scheme validation (mirroring the
    # base_url gate on /api/llm/provider) is the guard we ship; the read
    # side in settings.load_ptt_ws_uri enforces the SAME predicate so a
    # hand-edited file cannot bypass it. A loopback-only restriction was
    # deliberately NOT applied (owner decision): LiveAudio on a second
    # capture PC is a legitimate dual-PC setup, exactly like OBS accepting
    # any host.
    if body.stt_ws_uri is None:
        return PttConfigResponse(stt_ws_uri=load_ptt_ws_uri())
    if not is_valid_stt_ws_uri(body.stt_ws_uri):
        return JSONResponse(
            status_code=422,
            content={"detail": "stt_ws_uri must start with ws:// or wss://"},
        )
    with _ptt_config_lock:
        try:
            deps.save_ptt_ws_uri(body.stt_ws_uri)
        except (OSError, RuntimeError, ValueError):
            return JSONResponse(
                status_code=503, content={"detail": "config_write_failed"}
            )
    # Live-apply AFTER a successful save and OUTSIDE the write lock (same
    # ordering as _apply_avatar_runtime on PUT /api/obs/config), so a URL
    # is never applied to the runtime unless it also survives a restart.
    _apply_ptt_ws_uri(request, body.stt_ws_uri)
    return PttConfigResponse(stt_ws_uri=body.stt_ws_uri)


@router.post("/api/ptt/test", response_model=PttTestResponse)
def post_ptt_test(request: Request, body: Optional[PttTestRequest] = None):
    # Bare connect + immediate close. Builds NO PttSession, never claims
    # the single slot, never dispatches, never logs a lifecycle event -- so
    # it is safe to run mid-hold and cannot be used to inject a turn.
    # ALWAYS 200: a failed probe is a result the operator asked for, not
    # an HTTP error.
    uri = body.stt_ws_uri if body is not None else None
    if uri is None:
        controller = getattr(request.app.state, "ptt_controller", None)
        state = controller.state() if controller is not None else {}
        uri = state.get("stt_ws_url") or load_ptt_ws_uri()
    if not is_valid_stt_ws_uri(uri):
        return PttTestResponse(ok=False, detail="invalid_scheme")
    ok, detail = deps.stt_connection_bounded_check(uri)
    if not ok:
        # Loopback miss: try auto-start/discovery once (spawn or manual
        # scan), then report the post-ensure result. Remote URIs skip this
        # entirely — a failed remote probe stays a plain result.
        outcome = _ensure_local_stt(request, uri)
        if outcome is not None and outcome.available:
            ok, detail = True, "connected"
    return PttTestResponse(ok=ok, detail=detail)
