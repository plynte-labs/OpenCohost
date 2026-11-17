"""POST /api/system/shutdown -- graceful engine stop for the Tauri shell.

The shell used to hard-kill the backend on exit (TerminateProcess), so the
lifespan finally and ``EngineHost.stop()`` -- which unloads the Ollama model
(``keep_alive=0``) and tears down the vision daemon -- never ran, leaving
models resident for the whole keep-alive window. The shell now calls this
first, then kills the process; the server does not exit here.

Auth: operator bearer token, enforced UNCONDITIONALLY (same self-gate as
``GET /api/stream/chat-live/messages``), not only behind API_AUTH_ENFORCED:
an unauthenticated local page must not be able to tear the engine down.
"""

import asyncio
import logging
import time
from functools import partial

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from opencohost.api.auth import TokenFileError, _bearer_token, _resolve_role

router = APIRouter()
logger = logging.getLogger("OpenCohost")

# Hard bound on the stop; the shell's own bound is slightly larger.
SHUTDOWN_BOUND_SECONDS = 3.0


async def _wants_unload(request: Request) -> bool:
    """Optional JSON body ``{"unload_models": false}`` opts out of the unload.

    Absent, empty, malformed or any other value means unload (the default).
    """
    try:
        body = await request.json()
    except Exception:
        return True
    return not (isinstance(body, dict) and body.get("unload_models") is False)


@router.post("/api/system/shutdown")
async def post_system_shutdown(request: Request):
    token = _bearer_token(request)
    try:
        role = _resolve_role(token) if token is not None else None
    except TokenFileError:
        return JSONResponse(status_code=503, content={"detail": "auth_unavailable"})
    if role != "operator":
        return JSONResponse(status_code=403, content={"detail": "operator token required"})

    unload_models = await _wants_unload(request)
    host = request.app.state.host
    t0 = time.monotonic()
    ok = True
    timed_out = False
    try:
        await asyncio.wait_for(
            run_in_threadpool(
                host.stop if unload_models else partial(host.stop, unload_models=False)
            ),
            timeout=SHUTDOWN_BOUND_SECONDS
        )
    except asyncio.TimeoutError:
        timed_out = True
    except Exception as exc:  # stop() is best-effort; never 500 the shell
        ok = False
        logger.warning("[SHUTDOWN] host.stop raised %s", type(exc).__name__)
    unloaded = getattr(host, "last_unloaded_model", None)
    logger.info(
        "[SHUTDOWN] graceful stop requested ok=%s timed_out=%s elapsed_ms=%d unloaded=%s",
        ok,
        timed_out,
        int((time.monotonic() - t0) * 1000),
        unloaded,
    )
    return {"ok": ok, "unloaded": unloaded, "timed_out": timed_out}
