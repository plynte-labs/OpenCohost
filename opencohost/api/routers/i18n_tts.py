"""GET/PUT /api/i18n, GET /api/tts/config
(moved verbatim from main.py, refactor_core_api_20260802 B4).

`_i18n_state_response` was a closure nested inside `create_app()` in
main.py (its only two callers, `get_i18n`/`set_i18n`, both move here) -- it
touches no create_app-local state, only module-level imports, so it is
relocated here as a plain private helper rather than left orphaned in
main.py. `load_piper_voice`/`load_tts_local_only`/`load_tts_speed`/
`EXPERIMENTAL_HEAVY_TTS_ENABLED` go through `deps` because
test_tts_config_shape_from_accessors (tests/test_api_reads.py) replaces
all four directly on `opencohost.api.main`.
"""

import logging
import os
import threading
from typing import Optional

from fastapi import APIRouter, Body, Request
from pydantic import BaseModel

from opencohost.api import deps
from opencohost.core.speech import pronunciation
from opencohost.api.models import I18nSetLocaleRequest, I18nStateResponse, TTSConfigResponse
from opencohost.config import settings as app_settings
from opencohost.config.settings import default_piper_voice_for_locale, piper_voice_path
from opencohost.core.speech import edge_voices, piper_catalog
from opencohost.i18n import active as i18n_active
from opencohost.i18n import state as i18n_state
from opencohost.i18n import tags as i18n_tags
from opencohost.i18n.coherence import check_coherence
from opencohost.i18n.startup import load_registry
from fastapi.responses import JSONResponse

router = APIRouter()
logger = logging.getLogger("opencohost.api.i18n_tts")

_pronunciation_lock = threading.Lock()


def _pronunciation_payload(locale: Optional[str] = None) -> dict:
    return {
        "entries": pronunciation.get_entries(locale=locale),
        "limits": dict(pronunciation.LIMITS),
    }


@router.get("/api/tts/pronunciation")
def get_tts_pronunciation(locale: Optional[str] = None) -> dict:
    """Owner pronunciation dictionary: ``{entries:[{match, replace}], limits}``."""
    return _pronunciation_payload(locale=locale)


@router.put("/api/tts/pronunciation")
def put_tts_pronunciation(body: dict = Body(...), locale: Optional[str] = None):
    """Full replace. 422 ``{detail, errors:[{index, field, code}]}`` on invalid
    input, 503 when the file cannot be written. Operator tier is enforced by the
    generic mutating-route middleware (same as PUT /api/personalization)."""
    target_locale = locale if locale is not None else body.get("locale")
    with _pronunciation_lock:
        result = pronunciation.save_pronunciation(body.get("entries"), locale=target_locale)
    if result is None:
        return JSONResponse(status_code=503, content={"detail": "pronunciation_write_failed"})
    if result:
        return JSONResponse(
            status_code=422, content={"detail": "invalid_pronunciation", "errors": result}
        )
    return _pronunciation_payload(locale=target_locale)


def _i18n_state_response() -> I18nStateResponse:
    # Reuses load_registry() (official + community discovery, official-wins
    # anti-shadowing merge) and get_active_bundle() — zero new state
    # (design §7.1). `available` reads each bundle's own `meta.display`/
    # `meta.status` (already authored per-locale), not a re-derived check.
    registry = load_registry()
    active_bundle = i18n_active.get_active_bundle()
    persisted_locale = i18n_state.get_locale()
    pending_restart = i18n_tags.normalize(persisted_locale) != active_bundle.code
    available = [
        {
            "code": code,
            "display": str((bundle.data.get("meta") or {}).get("display", code)),
            "tier": bundle.tier,
            "status": str((bundle.data.get("meta") or {}).get("status", "unknown")),
        }
        for code, bundle in sorted(registry.items())
    ]
    # Bundle-level only (design §7.1) — no profile/piper args, so only
    # BUNDLE_VOICE_MISMATCH can ever fire here.
    warnings = [
        {"code": w.code, "message": w.message} for w in check_coherence(active_bundle)
    ]
    return I18nStateResponse(
        active_locale=active_bundle.code,
        persisted_locale=persisted_locale,
        pending_restart=pending_restart,
        available=available,
        warnings=warnings,
    )


@router.get("/api/i18n", response_model=I18nStateResponse)
def get_i18n() -> I18nStateResponse:
    return _i18n_state_response()


@router.put("/api/i18n", response_model=I18nStateResponse)
def set_i18n(body: I18nSetLocaleRequest):
    registry = load_registry()
    matched = i18n_tags.match(body.locale, list(registry.keys()))
    if not matched:
        return JSONResponse(status_code=422, content={"detail": "unknown locale"})
    # D6: next-boot only — persists the choice, never hot-swaps the
    # running process's active bundle.
    i18n_state.set_locale(matched)
    return _i18n_state_response()


def _piper_unavailable_reason(voice_key: str) -> str | None:
    """Fixed code explaining why Piper is down; never carries a filesystem path."""
    from opencohost.core.speech.backends import tts_piper

    if not tts_piper._PIPER_AVAILABLE:
        return "package_missing"
    if not os.path.isfile(piper_voice_path(voice_key)):
        return "voice_file_missing"
    return None


@router.get("/api/tts/config", response_model=TTSConfigResponse)
def get_tts_config(request: Request) -> TTSConfigResponse:
    host = request.app.state.host
    piper = getattr(host.motor, "_piper", None)
    if piper is not None and not piper.is_available() and hasattr(piper, "load"):
        piper.load()
    piper_available = piper.is_available() if piper is not None else False
    edge_tts_offline = bool(getattr(host.motor, "_edge_tts_offline", False))
    breaker = getattr(host.motor, "_edge_breaker", None)
    edge_tts_degraded = bool(breaker is not None and breaker.degraded)
    edge_tts_last_error = breaker.last_error if edge_tts_degraded else None
    if edge_tts_offline:
        # A latched offline failure outranks any stale transient breaker code.
        edge_tts_last_error = "offline"
    piper_voice = deps.load_piper_voice(
        default=default_piper_voice_for_locale(i18n_active.get_active_bundle().code)
    )
    # The engine's REAL active voice and speaker win over the persisted value (a catalog voice has no
    # registry key); hosts without engine voice state keep the persisted-key behaviour.
    piper_voice_id = (app_settings.PIPER_VOICES.get(piper_voice) or {}).get("file")
    piper_speaker_id = None
    active_key = getattr(host.motor, "_piper_voice_key", None)
    if isinstance(active_key, str) and active_key:
        piper_voice = active_key
        piper_voice_id = (app_settings.PIPER_VOICES.get(active_key) or {}).get("file") or active_key
        speaker = getattr(piper, "speaker_id", None)
        if isinstance(speaker, int) and not isinstance(speaker, bool):
            piper_speaker_id = speaker
    failure = getattr(host.motor, "_last_piper_switch_failure", None)
    reason = failure.get("reason") if isinstance(failure, dict) else None
    piper_switch_failure_reason = reason if isinstance(reason, str) else None
    effective_edge = getattr(host.motor, "effective_edge_voice", None)
    edge_voice = effective_edge() if callable(effective_edge) else None
    if not isinstance(edge_voice, str) or not edge_voice:
        edge_voice = i18n_active.edge_voice()
    edge_failure = getattr(host.motor, "_last_edge_switch_failure", None)
    edge_reason = edge_failure.get("reason") if isinstance(edge_failure, dict) else None
    heavy_available = deps.experimental_heavy_tts_enabled()
    raw_engine = str(getattr(host.motor, "motor_tts", "ligero") or "ligero")
    engine = deps.load_motor_tts(
        default=raw_engine if raw_engine in ("ligero", "pesado") else "ligero",
        heavy_enabled=heavy_available,
    )
    if engine == "pesado" and not heavy_available:
        engine = "ligero"
    return TTSConfigResponse(
        piper_voice=piper_voice,
        local_only=deps.load_tts_local_only(),
        speed=deps.load_tts_speed(),
        engine=engine,
        heavy_available=heavy_available,
        piper_available=piper_available,
        piper_unavailable_reason=None if piper_available else _piper_unavailable_reason(piper_voice),
        edge_tts_offline=edge_tts_offline,
        edge_tts_degraded=edge_tts_degraded,
        edge_tts_last_error=edge_tts_last_error,
        discard_speech_residue=deps.load_discard_speech_residue(),
        interrupt_on_direct=deps.load_interrupt_on_direct(),
        piper_voice_id=piper_voice_id,
        piper_speaker_id=piper_speaker_id,
        piper_switch_failure_reason=piper_switch_failure_reason,
        edge_voice=edge_voice,
        edge_voice_failure_reason=edge_reason if isinstance(edge_reason, str) else None,
    )


# --- Voice catalogs (voice-catalog-selection-20261118) -----------------------


class PiperFolderRequest(BaseModel):
    path: str


def _piper_folder_response() -> dict:
    folder, source = piper_catalog.resolve_voice_dir()
    return {
        "path": str(folder),
        "source": source,
        "exists": folder.is_dir(),
        "voice_count": piper_catalog.count_voices(folder),
    }


def _log_piper_folder(outcome: str, voices: int = 0) -> None:
    # Enum outcome and a count only: the path can contain the Windows user name.
    logger.info("[PIPER_FOLDER] outcome=%s voices=%d", outcome, voices)


@router.get("/api/tts/piper/folder")
def get_piper_folder():
    return _piper_folder_response()


@router.put("/api/tts/piper/folder")
def put_piper_folder(body: PiperFolderRequest):
    try:
        piper_catalog.save_voice_dir(body.path)
    except piper_catalog.PiperFolderError as exc:
        _log_piper_folder(exc.code)
        return JSONResponse(status_code=422, content={"detail": exc.code})
    except OSError:
        _log_piper_folder("config_write_failed")
        return JSONResponse(status_code=503, content={"detail": "config_write_failed"})
    response = _piper_folder_response()
    _log_piper_folder("set", response["voice_count"])
    return response


@router.delete("/api/tts/piper/folder")
def reset_piper_folder():
    try:
        piper_catalog.clear_voice_dir()
    except OSError:
        _log_piper_folder("config_write_failed")
        return JSONResponse(status_code=503, content={"detail": "config_write_failed"})
    response = _piper_folder_response()
    _log_piper_folder("reset", response["voice_count"])
    return response


@router.get("/api/tts/piper/voices")
def list_piper_voices(lang: Optional[str] = None) -> list[dict]:
    """Bundled voices plus the chosen folder's, re-scanned on every request."""
    return [v.to_dict() for v in piper_catalog.list_voices(lang)]


@router.get("/api/tts/edge/voices")
def list_edge_voices(lang: Optional[str] = None) -> dict:
    """Edge-TTS voices (cached ~24h); the bundle default voices when offline."""
    voices, source = edge_voices.DEFAULT_CATALOG.get(lang)
    return {"voices": voices, "source": source}
