"""/api/vision/* -- owner vision controls (vision-on-demand V1.6).

- ``GET  /api/vision``       -> settings + live status
- ``PUT  /api/vision``       partial update of the owner settings
- ``POST /api/vision/look``  arm the one-shot "look at this" for the next owner turn
- ``GET  /api/vision/looks/{id}/thumbnails``  V1.11: the ~320 px previews of the
  frames a look sent, as JPEG data URLs, from an in-memory ring of the last 10
  looks. 404 ``look_not_found`` when unknown, expired or thumbnails are off.
  Also carries ``description`` (the look's text, same in-memory expiry as the
  frames). The whole route requires the operator token (403 without it, 503 on a
  broken token file); the frames are the bytes SENT to the model.
- ``GET /api/vision/models``  V1.17: the current vision model, the installed
  Ollama tags with their vision capability, the suggested catalog and whether
  Ollama answered. Selecting a model reuses ``PUT /api/vision {model}``.

PRIVACY: nothing here logs an image or a description. The only image and
description payload is the thumbnails route, served from memory to the owner's
own UI, and only to the operator token. Auth is inherited from the
middleware (every PUT/POST is operator tier, GET stays open).
"""

import logging
import threading
from dataclasses import replace

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from opencohost.api import chat_image
from opencohost.api.auth import TokenFileError, _bearer_token, _resolve_role
from opencohost.api.models import VisionModelsResponse
from opencohost.api import deps
from opencohost.config import vision_settings
from opencohost.core.vision.cloud_describer import resolve_cloud_key, vision_key_id
from opencohost.core.vision.gate import normalize_mode
from opencohost.stream_admin.oauth_store import OAuthStore

logger = logging.getLogger("OpenCohost")

router = APIRouter()

_write_lock = threading.Lock()
# V1.8: `state` is off|starting|warming|ready|failed (+ `last_error` code);
# `last_result` is {id, outcome, capture_ms, describe_ms, at, frames_captured,
# frames_sent, self_capture_skipped} or null before the first look (V1.11 added
# id and the frame counters). Codes, counts and latencies only, never content.
_STATUS_KEYS = (
    "state",
    "last_error",
    "daemon_state",
    "model_installed",
    "model_loaded",
    "capture_available",
    "armed",
    "looking",
    "last_result",
)


def _host(request: Request):
    return getattr(getattr(getattr(request, "app", None), "state", None), "host", None)


def _unavailable() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "detail": {
                "error": "ENGINE_UNAVAILABLE",
                "message": "The engine host is not available.",
            }
        },
    )


def _key_flags(values) -> dict:
    """Whether a key exists for the cloud vision model (booleans only; the key is
    write-only and never leaves the key store). The store is not touched until a
    cloud preset is chosen."""
    own = chat = False
    preset = values.vision_cloud_preset
    if preset:
        try:
            store = OAuthStore(deps.llm_keys_file())
            own = bool((store.load(vision_key_id(preset)) or {}).get("api_key"))
            chat = bool(resolve_cloud_key(
                replace(values, vision_cloud_use_chat_key=True),
                deps.load_provider_config(),
                _ChatKeysOnly(store),
            ))
        except Exception:
            logger.warning("[VISION] key flags unavailable")
    return {"vision_cloud_key_set": own, "vision_cloud_chat_key_set": chat}


class _ChatKeysOnly:
    """Key store view that hides the preset's own vision key, so the lookup
    answers "does the chat provider have a key for this preset"."""

    def __init__(self, store) -> None:
        self._store = store

    def load(self, profile_id):
        if str(profile_id).startswith("vision_"):
            return None
        return self._store.load(profile_id)


def _payload(host, values=None) -> dict:
    values = values if values is not None else vision_settings.current()
    status = host.vision_status()
    body = values.to_dict()
    body.update(_key_flags(values))
    body["status"] = {key: status.get(key) for key in _STATUS_KEYS}
    # runtime-fixes-20261110 T7: how many images one chat message may carry.
    body["max_images"] = chat_image.MAX_IMAGES
    return body


@router.get("/api/vision")
def get_vision(request: Request):
    host = _host(request)
    if host is None:
        return _unavailable()
    return _payload(host)


@router.put("/api/vision")
def put_vision(request: Request, body=Body(...)):
    host = _host(request)
    if host is None:
        return _unavailable()
    if not isinstance(body, dict):
        return JSONResponse(
            status_code=422,
            content={
                "detail": "invalid_vision_settings",
                "errors": [{"field": "body", "code": "invalid_type"}],
            },
        )
    updates = {k: v for k, v in body.items() if k in vision_settings._ATTRS}
    errors = [
        {"field": key, "code": "invalid_value"}
        for key, value in updates.items()
        if vision_settings._validated(key, value) is None
    ]
    # P3: the cloud vision key is write-only ("" clears it); never a setting.
    new_key = body.get("vision_cloud_api_key")
    if "vision_cloud_api_key" in body and not isinstance(new_key, str):
        errors.append({"field": "vision_cloud_api_key", "code": "invalid_value"})
    if errors:
        return JSONResponse(
            status_code=422, content={"detail": "invalid_vision_settings", "errors": errors}
        )
    with _write_lock:
        merged = vision_settings.with_updates(vision_settings.current(), updates)
        errors = _cloud_errors(merged, new_key if isinstance(new_key, str) else None)
        if errors:
            return JSONResponse(
                status_code=422, content={"detail": "invalid_vision_settings", "errors": errors}
            )
        key_error = _write_key(merged, new_key if isinstance(new_key, str) else None)
        if key_error is not None:
            return key_error
        new = host.vision_apply_settings(updates, persist=True)
    if new is None:
        return JSONResponse(status_code=503, content={"detail": "vision_write_failed"})
    return _payload(host, new)


def _cloud_errors(merged, new_key) -> list:
    """Cross-field rules for ``cloud_model``: provider, model and a usable key."""
    if merged.vision_source != "cloud_model":
        return []
    errors = []
    if not merged.vision_cloud_preset:
        errors.append({"field": "vision_cloud_preset", "code": "required"})
    if not merged.vision_cloud_model:
        errors.append({"field": "vision_cloud_model", "code": "required"})
    if merged.vision_cloud_preset and not new_key:
        try:
            have = resolve_cloud_key(
                merged, deps.load_provider_config(), OAuthStore(deps.llm_keys_file())
            )
        except Exception:
            have = None
        if new_key == "" or not have:
            errors.append({"field": "vision_cloud_api_key", "code": "required"})
    return errors


def _write_key(merged, new_key):
    """Store or clear the preset's own vision key. A JSONResponse on failure."""
    if new_key is None or not merged.vision_cloud_preset:
        return None
    try:
        store = OAuthStore(deps.llm_keys_file())
        if new_key == "":
            store.delete(vision_key_id(merged.vision_cloud_preset))
        else:
            store.save(vision_key_id(merged.vision_cloud_preset), {"api_key": new_key})
    except OSError:
        logger.warning("[VISION] key store write failed")
        return JSONResponse(status_code=503, content={"detail": "key_store_write_failed"})
    return None


@router.post("/api/vision/look")
def post_vision_look(request: Request, body=Body(None)):
    """Arm one look. Optional body ``{"mode": "clip" | "text"}`` (``texto`` is
    accepted) is the backend half of ``/mira clip`` / ``/mira texto`` (V1.26/27);
    without it the utterance decides the mode."""
    host = _host(request)
    if host is None:
        return _unavailable()
    mode = None
    if body is not None:
        raw = body.get("mode") if isinstance(body, dict) else body
        mode = normalize_mode(raw) if raw is not None else None
        if raw is not None and mode is None:
            return JSONResponse(
                status_code=422,
                content={
                    "detail": "invalid_vision_look",
                    "errors": [{"field": "mode", "code": "invalid_value"}],
                },
            )
    if not host.vision_status().get("enabled"):
        return JSONResponse(status_code=409, content={"detail": "vision_disabled"})
    armed = host.vision_arm_look(mode) if mode else host.vision_arm_look()
    if not armed:
        return JSONResponse(status_code=409, content={"detail": "vision_disabled"})
    return {"armed": True, "mode": mode} if mode else {"armed": True}


@router.get("/api/vision/models", response_model=VisionModelsResponse)
def get_vision_models(request: Request):
    host = _host(request)
    lister = getattr(host, "vision_models", None) if host is not None else None
    if not callable(lister):
        return _unavailable()
    return lister()


@router.get("/api/vision/looks/{look_id}/thumbnails")
def get_vision_look_thumbnails(look_id: str, request: Request):
    # The frames are the full-resolution screenshots sent to the model and the
    # description is what it read off them: screen content. auth_middleware
    # leaves plain GETs open (auth.py rule 3), so this route gates itself on the
    # operator token, unconditionally, like chat-live. A broken token file is a
    # server fault (503), not a bad credential.
    try:
        token = _bearer_token(request)
        role = _resolve_role(token) if token is not None else None
    except TokenFileError:
        return JSONResponse(status_code=503, content={"detail": "auth_unavailable"})
    if role != "operator":
        return JSONResponse(status_code=403, content={"detail": "operator token required"})
    host = _host(request)
    if host is None:
        return _unavailable()
    getter = getattr(host, "vision_thumbnails", None)
    thumbnails = getter(look_id) if callable(getter) else None
    if thumbnails is None:
        return JSONResponse(status_code=404, content={"detail": "look_not_found"})
    body = {"id": look_id, "thumbnails": list(thumbnails)}
    describer = getattr(host, "vision_description", None)
    description = describer(look_id) if callable(describer) else None
    if description is not None:
        body["description"] = description
    return body
