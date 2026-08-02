"""/api/llm/provider GET/PUT + /api/llm/provider/probe
(moved verbatim from main.py, refactor_core_api_20260802 B4).

`load_provider_config` goes through `deps.load_provider_config()`: the
suite's established idiom patches `main.load_provider_config` directly in
several places, and a direct from-import here would silently bypass such a
patch. `save_provider_config` is patched nowhere, so it stays a direct
import. `LLM_KEYS_FILE` IS monkeypatched directly on `main` (by these tests
AND by tests/conftest.py's autouse key-file isolation fixture), so it goes
through `deps.llm_keys_file()`.

`_PROFILE_ID_RE`, `_llm_provider_lock`, `_llm_provider_response`, and
`logger` live in `opencohost.api.shared` (refactor_core_api_20260802 B5 Part
B -- moved out of main.py to break the routers<->main module-level import
cycle; none are ever monkeypatched, confirmed by grep, so a plain import is
safe). `logger` is `logging.getLogger("opencohost.api.main")` regardless of
which module holds the name -- `logging.getLogger` caches by name, so the
two `logger.exception(...)` calls below keep emitting under the exact
`"opencohost.api.main"` logger name -- test_llm_provider_config.py's
`caplog.at_level(..., logger="opencohost.api.main")` scopes to that name
specifically.
"""

from fastapi import APIRouter, Request

from opencohost.api import deps
from opencohost.api.shared import _PROFILE_ID_RE, _llm_provider_lock, _llm_provider_response, logger
from opencohost.api.models import (
    LlmProviderProbeResponse,
    LlmProviderRequest,
    LlmProviderResponse,
)
from opencohost.config.llm_provider import save_provider_config
from opencohost.config.settings import LLM_PROVIDER_PRESETS
from opencohost.stream_admin.oauth_store import OAuthStore
from fastapi.responses import JSONResponse

router = APIRouter()


@router.get("/api/llm/provider", response_model=LlmProviderResponse)
def get_llm_provider() -> LlmProviderResponse:
    return _llm_provider_response(deps.load_provider_config(), OAuthStore(deps.llm_keys_file()))


@router.put("/api/llm/provider", response_model=LlmProviderResponse)
def put_llm_provider(body: LlmProviderRequest, request: Request):
    scoped_given = (
        body.base_url is not None
        or body.model is not None
        or body.preset is not None
        or body.api_key is not None
    )
    # delete_profile is a standalone action field -- never combines with a
    # profile edit in the same PUT (design 'Provider Config Surface').
    if body.delete_profile is not None and scoped_given:
        return JSONResponse(
            status_code=422,
            content={"detail": "delete_profile cannot be combined with profile edits"},
        )
    if scoped_given and body.profile_id is None:
        return JSONResponse(status_code=422, content={"detail": "profile_id required"})
    if body.profile_id is not None and (
        body.profile_id == "local" or not _PROFILE_ID_RE.fullmatch(body.profile_id)
    ):
        return JSONResponse(status_code=422, content={"detail": "invalid profile_id"})
    if body.preset is not None and body.preset not in LLM_PROVIDER_PRESETS:
        return JSONResponse(status_code=422, content={"detail": "unknown preset"})

    with _llm_provider_lock:
        cfg = deps.load_provider_config()
        profiles = cfg.setdefault("profiles", {})
        key_store = OAuthStore(deps.llm_keys_file())

        if body.delete_profile is not None and body.delete_profile not in profiles:
            return JSONResponse(status_code=422, content={"detail": "unknown profile"})

        if body.active_provider is not None:
            known = set(profiles) | ({body.profile_id} if body.profile_id else set())
            if body.active_provider != "local" and body.active_provider not in known:
                return JSONResponse(status_code=422, content={"detail": "unknown provider"})

        if body.delete_profile is not None:
            # Target must not be the RESOLVED active provider -- i.e. after
            # this same PUT's active_provider change (if any) is applied.
            # This is what makes switch-then-delete work in one call.
            resolved_active = (
                body.active_provider
                if body.active_provider is not None
                else cfg.get("active_provider", "local")
            )
            if body.delete_profile == resolved_active:
                return JSONResponse(
                    status_code=422, content={"detail": "cannot delete active profile"}
                )
            # Delete the key FIRST: if the key store write fails, the
            # profile removal below must never run, so a 503 here leaves
            # the profile (and its key) exactly as they were -- no config
            # change is persisted without the key delete succeeding.
            try:
                key_store.delete(body.delete_profile)
            except OSError:
                return JSONResponse(
                    status_code=503, content={"detail": "key_store_write_failed"}
                )
            profiles.pop(body.delete_profile, None)

        if body.profile_id is not None and (
            body.base_url is not None or body.model is not None or body.preset is not None
        ):
            profile = dict(profiles.get(body.profile_id, {}))
            if body.preset is not None:
                preset_cfg = LLM_PROVIDER_PRESETS[body.preset]
                profile["preset"] = body.preset
                if not profile.get("base_url"):
                    profile["base_url"] = preset_cfg["base_url"]
                if not profile.get("model") and preset_cfg.get("models"):
                    profile["model"] = preset_cfg["models"][0]
            if body.base_url is not None:
                profile["base_url"] = body.base_url
            if body.model is not None:
                profile["model"] = body.model
            profiles[body.profile_id] = profile
        elif (
            body.profile_id is not None
            and body.api_key is not None
            and body.profile_id not in profiles
        ):
            # F3: an api_key-only PUT for a brand-new profile id must
            # still create the (empty) profile entry -- otherwise the
            # stored key is invisible forever (no GET ever reports it).
            profiles[body.profile_id] = {}

        if body.active_provider is not None:
            cfg["active_provider"] = body.active_provider
        if body.fallback_mode is not None:
            cfg["fallback_mode"] = body.fallback_mode
        if body.pregen_enabled is not None:
            cfg["pregen_enabled"] = body.pregen_enabled

        # Activation-time completeness (design 'Provider Config Surface'
        # rule 5): fires whether this PUT switched the selector OR edited
        # the currently-active profile into an invalid state. Saving an
        # incomplete INACTIVE profile (draft) is never blocked here.
        active = cfg.get("active_provider", "local")
        if active != "local":
            active_profile = profiles.get(active, {})
            base_url = str(active_profile.get("base_url") or "")
            if not base_url.startswith(("http://", "https://")):
                return JSONResponse(
                    status_code=422,
                    content={"detail": "base_url required for active cloud profile"},
                )
            if not str(active_profile.get("model") or ""):
                return JSONResponse(
                    status_code=422,
                    content={"detail": "model required for active cloud profile"},
                )
            # Spec 'Cloud selected': base_url + model + a STORED KEY. An
            # api_key supplied in this SAME PUT counts; clearing the
            # active profile's key (api_key: "") never leaves it active.
            is_same_put = body.profile_id == active
            key_provided_now = is_same_put and bool(body.api_key)
            key_cleared_now = is_same_put and body.api_key == ""
            has_key = key_provided_now or (
                not key_cleared_now and key_store.has_token(active)
            )
            if not has_key:
                return JSONResponse(
                    status_code=422,
                    content={"detail": "api_key required for active cloud profile"},
                )

        # F1: persist the config FIRST. A config-write failure must leave
        # nothing persisted (no orphan key committed while the response
        # claims total failure). Only once the config is safely on disk
        # do we touch the key store, so a key-write failure afterward
        # leaves a visible, keyless draft profile a retry can converge --
        # never an invisible orphan secret.
        try:
            save_provider_config(cfg)
        except OSError:
            # Log the traceback so a persistent 503 is diagnosable. The
            # provider config carries no secret, and standard logging never
            # renders frame locals -- only source lines -- so no key leaks.
            logger.exception("llm provider config write failed")
            return JSONResponse(
                status_code=503, content={"detail": "provider_config_write_failed"}
            )

        if body.api_key is not None:
            try:
                if body.api_key == "":
                    key_store.delete(body.profile_id)
                else:
                    key_store.save(body.profile_id, {"api_key": body.api_key})
            except OSError:
                # Log the traceback so a persistent 503 is diagnosable. The
                # OAuthStore error (e.g. PermissionError on os.replace)
                # never carries the key value, and standard logging renders
                # only source lines, not frame locals -- so no key leaks.
                logger.exception("llm key store write failed")
                return JSONResponse(
                    status_code=503, content={"detail": "key_store_write_failed"}
                )

        # Phase 3: live-push the freshly-persisted config to the running
        # engine (no restart) — the piece Phase 1 deferred (design Doubts #2).
        # getattr-guarded: a fresh process / no feeder has no motor yet.
        motor = getattr(getattr(request.app.state, "host", None), "motor", None)
        if motor is not None and hasattr(motor, "set_provider_config"):
            motor.set_provider_config(cfg)

        return _llm_provider_response(cfg, key_store)


@router.post("/api/llm/provider/probe", response_model=LlmProviderProbeResponse)
def post_llm_provider_probe(request: Request) -> LlmProviderProbeResponse:
    """WU2 (cloud_rearm_20260801): manual cloud re-arm trigger. Same tier
    as PUT /api/llm/provider right above -- synchronous direct call onto
    the running motor, no dispatcher, no config-file write. Auth is
    inherited automatically (auth.py rule 2, mutating /api/* path
    prefix) -- no allowlist entry needed. `armed:false` is a benign
    no-op (not_in_fallback / no_cloud_profile), so it is still a 200;
    only a motor build predating trigger_cloud_probe_now is a 503.
    """
    motor = getattr(getattr(request.app.state, "host", None), "motor", None)
    if motor is None or not hasattr(motor, "trigger_cloud_probe_now"):
        return JSONResponse(status_code=503, content={"detail": "motor_unavailable"})
    result = motor.trigger_cloud_probe_now()
    return LlmProviderProbeResponse(**result)
