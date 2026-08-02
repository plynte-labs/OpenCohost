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
through `deps.test_stt_connection_bounded()`, a variadic late-import
accessor mirroring `deps.discover_ollama_models()`.

`PttUnreachable`/`SessionActive` (ptt_session.py exceptions) and
`_apply_ptt_ws_uri`/`_ptt_config_lock` are used ONLY by this family and are
never monkeypatched, so they relocate here wholesale. `PttController` and
`probe_stt_ws` itself stay imported in `main.py` too (the former for
`lifespan()`'s controller construction, the latter as `_test_stt_connection_
bounded`'s own dependency, per the paragraph above).
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

router = APIRouter()

# Serializes writes to ptt_settings.json (shared with the legacy CTK
# PTTManager hotkey store), mirroring _config_lock / _llm_provider_lock.
_ptt_config_lock = threading.Lock()


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
    try:
        session_id = controller.start()
    except SessionActive:
        return JSONResponse(status_code=409, content={"detail": "session_active"})
    except PttUnreachable:
        return JSONResponse(status_code=503, content={"detail": "stt_unreachable"})
    return PttStartResponse(session_id=session_id, state="listening")


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
    return PttStateResponse(**request.app.state.ptt_controller.state())


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
    ok, detail = deps.test_stt_connection_bounded(uri)
    return PttTestResponse(ok=ok, detail=detail)
