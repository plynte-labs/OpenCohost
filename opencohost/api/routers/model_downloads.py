"""In-app model downloads (model-download-manager T3, ADR-063).

- ``POST /api/models/downloads`` ``{tag}`` -> 202 job | 422 invalid_tag |
  409 download_active (one active job at a time; body names the active job) |
  503 ollama_unreachable. A tag with a paused/failed job resumes that job.
- ``GET  /api/models/downloads`` -> every kept job, newest first, plus the
  active job id: the UI's progress poll target.
- ``POST /api/models/downloads/{job_id}/pause|resume|cancel`` -> 200 job |
  404 job_not_found | 409 invalid_transition (resume also 409
  download_active and 503 ollama_unreachable).
- ``GET  /api/models/hf/{owner}/{repo}/quants`` -> GGUF quants with sizes,
  ready-to-pull ``hf.co/...`` tags and a VRAM-fit hint | 422
  invalid_hf_repo | 404 hf_repo_not_found | 502 hf_unavailable.

The manager lives on ``request.app.state.download_manager`` (built in
``create_app()``'s lifespan) and runs its pull on its own thread -- nothing
here touches the engine command queue. Parameterized paths
(``/api/models/downloads/{job_id}/<action>``,
``/api/models/hf/{owner}/{repo}/quants``) share no literal prefix + segment
count with any other route (``/api/models``, ``/api/models/reasoning``).
"""

from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from opencohost.api.models import (
    HfQuantsResponse,
    ModelDownloadJob,
    ModelDownloadRequest,
    ModelDownloadsResponse,
)
from opencohost.core.engine import llm_readiness as llm_readiness_mod
from opencohost.core.model_downloads import hf_quants
from opencohost.core.model_downloads.manager import (
    MAX_RETRIES,
    ActiveDownloadError,
    InvalidTransitionError,
    JobNotFoundError,
)
from opencohost.core.model_downloads.tags import is_valid_model_tag

router = APIRouter()

_OLLAMA_PROBE_TIMEOUT_SECONDS = 1.5


def _manager(request: Request):
    return getattr(request.app.state, "download_manager", None)


def _error(status: int, detail: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, **extra})


def _ollama_reachable() -> bool:
    # Module attribute lookup at call time so tests can monkeypatch it.
    try:
        return bool(llm_readiness_mod._ping_ollama(timeout=_OLLAMA_PROBE_TIMEOUT_SECONDS)[0])
    except Exception:
        return False


def _vram_mb() -> Optional[int]:
    try:
        return int(llm_readiness_mod._detect_gpu_hardware()[1]) or None
    except Exception:
        return None


@router.get("/api/models/downloads", response_model=ModelDownloadsResponse)
def list_model_downloads(request: Request):
    manager = _manager(request)
    if manager is None:
        return _error(503, "download_manager_unavailable")
    return ModelDownloadsResponse(
        jobs=[ModelDownloadJob(**job) for job in manager.list_jobs()],
        active_job_id=manager.active_job_id(),
        max_retries=MAX_RETRIES,
    )


@router.post("/api/models/downloads", status_code=202, response_model=ModelDownloadJob)
def start_model_download(body: ModelDownloadRequest, request: Request):
    manager = _manager(request)
    if manager is None:
        return _error(503, "download_manager_unavailable")
    if not is_valid_model_tag(body.tag):
        return _error(422, "invalid_tag")
    active = manager.active_job_id()
    if active is not None:
        return _error(409, "download_active", active_job_id=active)
    if not _ollama_reachable():
        return _error(503, "ollama_unreachable")
    try:
        return ModelDownloadJob(**manager.start(body.tag))
    except ActiveDownloadError as exc:
        return _error(409, "download_active", active_job_id=exc.active_job_id)
    except InvalidTransitionError:
        return _error(409, "invalid_transition")
    except ValueError:
        return _error(422, "invalid_tag")


def _job_action(request: Request, job_id: str, action: str):
    manager = _manager(request)
    if manager is None:
        return _error(503, "download_manager_unavailable")
    try:
        if action == "resume":
            job = manager.get_job(job_id)
            if job["state"] in ("paused", "failed") and not _ollama_reachable():
                return _error(503, "ollama_unreachable")
        return ModelDownloadJob(**getattr(manager, action)(job_id))
    except JobNotFoundError:
        return _error(404, "job_not_found")
    except ActiveDownloadError as exc:
        return _error(409, "download_active", active_job_id=exc.active_job_id)
    except InvalidTransitionError:
        return _error(409, "invalid_transition")


@router.post("/api/models/downloads/{job_id}/pause", response_model=ModelDownloadJob)
def pause_model_download(job_id: str, request: Request):
    return _job_action(request, job_id, "pause")


@router.post("/api/models/downloads/{job_id}/resume", response_model=ModelDownloadJob)
def resume_model_download(job_id: str, request: Request):
    return _job_action(request, job_id, "resume")


@router.post("/api/models/downloads/{job_id}/cancel", response_model=ModelDownloadJob)
def cancel_model_download(job_id: str, request: Request):
    return _job_action(request, job_id, "cancel")


@router.get("/api/models/hf/{owner}/{repo}/quants", response_model=HfQuantsResponse)
def list_hf_quants(owner: str, repo: str):
    try:
        return HfQuantsResponse(**hf_quants.list_quants(owner, repo, vram_mb=_vram_mb()))
    except ValueError:
        return _error(422, "invalid_hf_repo")
    except hf_quants.HfRepoNotFoundError:
        return _error(404, "hf_repo_not_found")
    except hf_quants.HfUpstreamError:
        return _error(502, "hf_unavailable")
