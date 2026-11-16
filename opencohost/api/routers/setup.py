"""/api/setup/* -- setup assistant backend (setup-assistant T1/T2).

- ``GET/PUT /api/setup/state``: persisted status plus a live ``engine_ready`` /
  ``engine`` derived from the active provider (cloud profile with a stored key) or
  the installed Ollama models. The API key itself is never read into a response.
- ``PUT/DELETE /api/setup/checkpoint``: where an interrupted setup resumes (S10).
  Non-secret answers only; GET state returns it. Completing the setup clears it.
- ``POST /api/setup/inference-test``: cold + warm streamed generations against the
  active engine (see ``opencohost.core.engine.setup_inference``).

Auth is inherited from the middleware (mutating routes are operator tier).
"""

import logging
import threading

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from opencohost.api.models import (
    SetupCheckpointRequest,
    SetupCheckpointResponse,
    SetupInferenceTestRequest,
    SetupInferenceTestResponse,
    SetupStateRequest,
    SetupStateResponse,
)
from opencohost.api.shared import engine_settling
from opencohost.config import setup_checkpoint, setup_state
from opencohost.core.engine import llm_readiness as llm_readiness_mod
from opencohost.core.engine import setup_inference

logger = logging.getLogger("opencohost.api.routers.setup")

router = APIRouter()

_write_lock = threading.Lock()


def _host(request: Request):
    return getattr(getattr(getattr(request, "app", None), "state", None), "host", None)


def _active_provider(host) -> str:
    cfg = getattr(getattr(host, "motor", None), "_provider_config", None)
    if not cfg:
        try:
            from opencohost.api import deps

            cfg = deps.load_provider_config()
        except Exception:
            cfg = {}
    return str((cfg or {}).get("active_provider") or "local")


def _resolve_engine(host):
    """``(engine_ready, engine)``. Never raises: an unreachable Ollama just means
    no local models."""
    active = _active_provider(host)
    if active != "local" and llm_readiness_mod._check_cloud_key_configured(active):
        return True, "cloud"
    try:
        _reachable, _version, models = llm_readiness_mod._ping_ollama()
    except Exception:
        models = []
    if models:
        return True, "local"
    return False, None


def _state_payload(host, status: str) -> dict:
    engine_ready, engine = _resolve_engine(host)
    loaded = setup_checkpoint.load_checkpoint()
    return {
        "checkpoint": loaded.checkpoint,
        "checkpoint_reset": loaded.reset,
        "status": status,
        "engine_ready": engine_ready,
        "engine": engine,
        # An earlier version is installed: the welcome says this one starts fresh.
        "legacy_install": setup_state.detect_legacy_install(),
        # A completed setup stays closed while the engine is still starting:
        # an unreachable Ollama then means "not up yet", not "missing".
        "show_on_start": status == "pending"
        or (status == "completed" and not engine_ready and not engine_settling(host)),
    }


@router.get("/api/setup/state", response_model=SetupStateResponse)
def get_setup_state(request: Request):
    return _state_payload(_host(request), setup_state.load_status())


@router.put("/api/setup/state", response_model=SetupStateResponse)
def put_setup_state(body: SetupStateRequest, request: Request):
    with _write_lock:
        try:
            setup_state.save_status(body.status)
        except OSError:
            logger.warning("setup state write failed")
            return JSONResponse(status_code=503, content={"detail": "setup_state_write_failed"})
        if body.status == "completed":
            # A finished setup has nothing to resume. Best effort: a stale
            # checkpoint is only offered again for a pending/skipped setup.
            try:
                setup_checkpoint.clear_checkpoint()
            except OSError:
                logger.warning("setup checkpoint clear failed")
    return _state_payload(_host(request), body.status)


@router.put("/api/setup/checkpoint", response_model=SetupCheckpointResponse)
def put_setup_checkpoint(body: SetupCheckpointRequest):
    with _write_lock:
        try:
            stored = setup_checkpoint.save_checkpoint(body.model_dump())
        except OSError:
            logger.warning("setup checkpoint write failed")
            return JSONResponse(status_code=503, content={"detail": "setup_checkpoint_write_failed"})
    return {"checkpoint": stored}


@router.delete("/api/setup/checkpoint", response_model=SetupCheckpointResponse)
def delete_setup_checkpoint():
    with _write_lock:
        try:
            setup_checkpoint.clear_checkpoint()
        except OSError:
            logger.warning("setup checkpoint clear failed")
            return JSONResponse(status_code=503, content={"detail": "setup_checkpoint_clear_failed"})
    return {"checkpoint": None}


@router.post("/api/setup/inference-test", response_model=SetupInferenceTestResponse)
def post_setup_inference_test(body: SetupInferenceTestRequest, request: Request):
    host = _host(request)
    if host is None or getattr(host, "motor", None) is None:
        return JSONResponse(
            status_code=503,
            content={
                "detail": {
                    "error": "ENGINE_UNAVAILABLE",
                    "message": "The engine host is not available.",
                }
            },
        )
    return setup_inference.run_inference_test(host, body.runs)
