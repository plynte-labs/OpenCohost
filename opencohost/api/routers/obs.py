"""GET/PUT /api/obs/config, POST /api/obs/test
(moved verbatim from main.py, refactor_core_api_20260802 B4).

`OBSClient` goes through `deps.obs_client_cls()` -- test_api_obs.py replaces
it directly on `opencohost.api.main` with fakes across most of its POST
/api/obs/test cases. `_test_obs_connection_bounded`/`_OBS_TEST_TIMEOUT_SECONDS`
stay defined in main.py (test_api_obs_timeout.py calls
`main_mod._test_obs_connection_bounded` directly as a unit test of the
helper itself) and are imported here unchanged -- never monkeypatched, so a
plain top-level import is safe. `_config_lock` is the SAME lock instance
`avatar.py` guards `avatar.yaml` writes with (one file, one lock, D4).
"""

import dataclasses
from typing import Optional

from fastapi import APIRouter, Request

from opencohost.api import deps
from opencohost.api.main import _apply_avatar_runtime, _config_lock, _obs_config_response, _test_obs_connection_bounded
from opencohost.api.models import ObsConfigRequest, ObsConfigResponse, ObsTestResponse
from opencohost.avatar.avatar_config import AvatarConfigUnreadableError, load_avatar_config, save_avatar_config
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/api/obs/config", response_model=ObsConfigResponse)
def get_obs_config() -> ObsConfigResponse:
    return _obs_config_response(load_avatar_config())


@router.put("/api/obs/config", response_model=ObsConfigResponse)
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


@router.post("/api/obs/test", response_model=ObsTestResponse)
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
    obs_client_cls = deps.obs_client_cls()
    client = obs_client_cls(config=obs_cfg, assets_folder=cfg.assets_folder)
    ok, message = _test_obs_connection_bounded(client)
    return ObsTestResponse(ok=ok, error=None if ok else message)
