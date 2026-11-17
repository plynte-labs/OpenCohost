"""/api/speech/* -- read-aloud controls (readaloud-speech T2).

The owner selects text in a Kira reply and has it spoken straight through the
speech router, bypassing the LLM:

- ``POST /api/speech/say``    {text, mode: "queue"|"now"} -> {job_id}
- ``POST /api/speech/stop``   discard every read-aloud job -> {discarded}
- ``POST /api/speech/pause``  engage the user hold -> {paused: true}
- ``POST /api/speech/resume`` release the user hold -> {paused: false}
- ``GET  /api/speech/state``  -> {paused, readaloud_active, readaloud_pending}

Auth is inherited from the middleware (auth.py rule 2): every POST is
operator-token tier, GET stays open. No path here is parameterized.

PRIVACY (same contract as /api/chat/turn, R8): the submitted text is never
logged, echoed, persisted, added to historial or emitted as dialogue. A guard
block answers with the rule id only. Logs carry job ids, mode and counts.

The router is REQUIRED: with it disabled (it is armed only by EngineHost)
every POST answers 503 and nothing falls back to a blocking `_hablar` on the
HTTP thread. GET /state then reports the idle defaults.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from opencohost.api.models import (
    SpeechDiscardResponse,
    SpeechPauseResponse,
    SpeechSayRequest,
    SpeechSayResponse,
    SpeechStateResponse,
)
from opencohost.api.routers.chat import _validate_chat_text
from opencohost.api.shared import logger
from opencohost.config.validation import redact_block_preview
from opencohost.core.context.generation_evaluator import (
    guard_rule_id,
    output_guard_with_tts_check,
)

router = APIRouter()

# The owner is the streamer and the text is owner-initiated, so it is judged
# like a direct (streamer-facing) turn: identity/safety rules apply, the
# audience-engagement rules (R10/R11) do not. Judging it as "readaloud" would
# fall into the strictest default and could block the owner re-reading a
# `direct` reply that already passed.
_GUARD_SOURCE = "direct"

_IDLE_STATE = {"paused": False, "readaloud_active": False, "readaloud_pending": 0}


def _speech_motor(request: Request):
    """The motor when the router-backed speech path is available, else None."""
    host = getattr(getattr(request, "app", None), "state", None)
    host = getattr(host, "host", None)
    motor = getattr(host, "motor", None)
    if motor is None or not getattr(motor, "_speech_router_enabled", False):
        return None
    return motor


def _unavailable() -> JSONResponse:
    return JSONResponse(
        status_code=503,
        content={
            "detail": {
                "error": "SPEECH_UNAVAILABLE",
                "message": "The speech router is not available.",
            }
        },
    )


@router.post("/api/speech/say", response_model=SpeechSayResponse)
def post_speech_say(request: Request, body: SpeechSayRequest):
    # R8: never return or log `body.text`.
    rejection = _validate_chat_text(body.text)
    if rejection is not None:
        return JSONResponse(status_code=422, content={"detail": rejection})
    # The guard logs a ~120-char preview of what it blocks; redact it here.
    with redact_block_preview():
        allowed, reason = output_guard_with_tts_check(body.text, source=_GUARD_SOURCE)
    if not allowed:
        rule = guard_rule_id(reason)
        logger.info("[READALOUD] blocked rule=%s chars=%d", rule, len(body.text))
        return JSONResponse(
            status_code=422,
            content={"detail": {"error": "GUARD_BLOCKED", "rule": rule}},
        )
    motor = _speech_motor(request)
    if motor is None:
        return _unavailable()
    job = motor.speak_readaloud(body.text, body.mode)
    if job is None:
        return _unavailable()
    return {"job_id": job.job_id}


@router.post("/api/speech/stop", response_model=SpeechDiscardResponse)
def post_speech_stop(request: Request):
    motor = _speech_motor(request)
    if motor is None:
        return _unavailable()
    return {"discarded": motor.stop_readaloud()}


@router.post("/api/speech/pause", response_model=SpeechPauseResponse)
def post_speech_pause(request: Request):
    motor = _speech_motor(request)
    if motor is None or not motor.user_pause_speech():
        return _unavailable()
    return {"paused": True}


@router.post("/api/speech/resume", response_model=SpeechPauseResponse)
def post_speech_resume(request: Request):
    motor = _speech_motor(request)
    if motor is None or not motor.user_resume_speech():
        return _unavailable()
    return {"paused": False}


@router.get("/api/speech/state", response_model=SpeechStateResponse)
def get_speech_state(request: Request):
    motor = _speech_motor(request)
    if motor is None:
        return dict(_IDLE_STATE)
    return motor.readaloud_state()
