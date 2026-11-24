"""/api/chat/turn, /api/chat/last-reply, POST /api/commands
(moved verbatim from main.py, refactor_core_api_20260802 B6).

No name in this family is monkeypatched on `opencohost.api.main` (confirmed
by grep across tests/), so every import here binds directly at module load
time -- no `deps` accessor is needed. `_COMMAND_WHITELIST`,
`_engine_command_payload`, `_validate_command_value`, `_CHAT_TEXT_MAX_LENGTH`,
and `_validate_chat_text` are used ONLY by this family, so they relocate here
wholesale. The POST /api/chat/turn submission itself lives in
`opencohost.api.direct_turn`, shared with the remote gateway.

No parameterized (`{...}`) path exists in this family -- no CAUTION block
needed (contrast routers/music.py, routers/perfiles.py, routers/agent.py,
routers/memoria.py).
"""

from typing import Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from opencohost.api.models import (
    ChatLastReplyResponse,
    ChatSpeechProgressResponse,
    ChatTurnRequest,
    CommandRequest,
)
from opencohost.api import chat_image
from opencohost.api.direct_turn import submit_direct_turn
from opencohost.core import turn_origin
from opencohost.core.vision import direct_cloud
from opencohost.api.shared import logger
from opencohost.config.settings import PIPER_SPEAKER_ID_MAX

router = APIRouter()

# Server-side verb whitelist for POST /api/commands -- exactly the verbs
# MotorVocalIA._dispatch_command (opencohost/core/llm_engine.py) handles.
# A command NOT in this set is rejected before it ever reaches the queue.
_COMMAND_WHITELIST = frozenset(
    {
        "clear_history",
        "set_tts_local_only",
        "set_tts_speed",
        "set_piper_voice",
        "set_edge_voice",
        "set_motor_tts",
        "switch_model",
        "switch_llm_tier",
        "set_discard_speech_residue",
        "set_interrupt_on_direct",
    }
)

# POST /api/chat/turn value guard -- mirrors `_validate_command_value`'s
# trust-boundary philosophy: reject empty/whitespace or unbounded text
# BEFORE it ever reaches the engine command_queue. 4000 chars is a sane cap
# for a single chat turn (process_context builds the full prompt from this).
_CHAT_TEXT_MAX_LENGTH = 4000


def _engine_command_payload(command: str, payload: dict):
    """Translate the wire-level `{payload: dict}` body into the raw scalar
    (or None) each engine verb actually consumes -- `_dispatch_command`
    reads `payload` directly as a str/float/bool/None per verb, never a
    dict. `"value"` is the one wire key used until a verb needs more than
    one field.
    """
    if command == "clear_history":
        return None
    if command == "set_piper_voice" and payload.get("speaker_id") is not None:
        # Catalog voice + speaker: the engine takes a dict only when a speaker is present, so the
        # legacy scalar shape (registry key / catalog id alone) is unchanged.
        return {"value": payload.get("value"), "speaker_id": payload.get("speaker_id")}
    return payload.get("value")


def _validate_command_value(command: str, value) -> "str | None":
    """Validate `value` against the per-verb contract `_dispatch_command`
    (opencohost/core/llm_engine.py) actually expects. Returns None when the
    value is acceptable, or a rejection reason string when it is not.

    This is the trust-boundary fix for the DoS where an uncaught `TypeError`
    (e.g. `float(None)` for `set_tts_speed`) inside `_dispatch_command`
    kills the engine command-loop thread -- that call sits outside the
    `queue.Empty` try in `run()`. Rejecting bad values here means they are
    never enqueued in the first place.
    """
    if command == "clear_history":
        return None
    if command == "set_tts_speed":
        try:
            float(value)
        except (TypeError, ValueError):
            return "set_tts_speed requires a numeric value"
        return None
    if command == "set_piper_voice" and isinstance(value, dict):
        speaker = value.get("speaker_id")
        if not isinstance(value.get("value"), str):
            return "set_piper_voice requires a non-None string value"
        if isinstance(speaker, bool) or not isinstance(speaker, int) or speaker < 0 or speaker > PIPER_SPEAKER_ID_MAX:
            return "set_piper_voice speaker_id must be a non-negative integer"
        return None
    if command == "set_edge_voice":
        if not isinstance(value, str):
            return "set_edge_voice requires a non-None string value"
        if len(value) > 80:
            return "set_edge_voice value is too long"
        return None
    if command in ("switch_model", "switch_llm_tier", "set_piper_voice", "set_motor_tts"):
        if not isinstance(value, str):
            return f"{command} requires a non-None string value"
        return None
    if command == "set_tts_local_only":
        if not isinstance(value, bool):
            return "set_tts_local_only requires a boolean value"
        return None
    if command == "set_discard_speech_residue":
        if not isinstance(value, bool):
            return "set_discard_speech_residue requires a boolean value"
        return None
    if command == "set_interrupt_on_direct":
        if not isinstance(value, bool):
            return "set_interrupt_on_direct requires a boolean value"
        return None
    return None


def _validate_chat_text(text: str, *, allow_empty: bool = False) -> "str | None":
    """Returns None when `text` is acceptable, or a rejection reason string.

    `allow_empty` is for an image-only turn: the pasted image supplies the
    content. The cap applies to the operator's text only."""
    if not text.strip() and not allow_empty:
        return "text must not be empty or whitespace-only"
    if len(text) > _CHAT_TEXT_MAX_LENGTH:
        return f"text exceeds max length of {_CHAT_TEXT_MAX_LENGTH} characters"
    return None


@router.post("/api/commands")
def post_command(request: Request, body: CommandRequest):
    if body.command not in _COMMAND_WHITELIST:
        return JSONResponse(status_code=422, content={"detail": "unknown command"})
    engine_payload = _engine_command_payload(body.command, body.payload)
    rejection = _validate_command_value(body.command, engine_payload)
    if rejection is not None:
        return JSONResponse(status_code=422, content={"detail": rejection})
    key = request.headers.get("Idempotency-Key") or body.idempotency_key
    dispatcher = request.app.state.dispatcher
    result = dispatcher.dispatch(body.command, engine_payload, key)
    if result.state in ("accepted", "replay"):
        return {
            "accepted": True,
            "command_id": result.command_id,
            "status": "queued",
            "state_version": dispatcher.state_version,
        }
    if result.state == "conflict":
        return JSONResponse(status_code=409, content={"accepted": False, "reason": "conflict"})
    return JSONResponse(status_code=429, content={"accepted": False, "reason": "queue_full"})


def _engine_unavailable(host):
    """409 ENGINE_UNAVAILABLE response when the engine cannot chat, else None.
    Checked before a pasted image is described, so no vision time is wasted."""
    from opencohost.core.engine import llm_readiness as llm_readiness_mod

    readiness = llm_readiness_mod.resolve_llm_readiness(host)
    if readiness.can_chat:
        return None
    return JSONResponse(
        status_code=409,
        content={
            "accepted": False,
            "detail": {
                "error": "ENGINE_UNAVAILABLE",
                "state": readiness.state,
                "message": "LLM engine is not ready for inference.",
            },
        },
    )


@router.post("/api/chat/turn")
@router.post("/api/chat/message")
def post_chat_turn(request: Request, body: ChatTurnRequest):
    # R8-CRITICAL: this handler must never return `body.text` (or any
    # derived dialogue) in the response, and must never log it. The ack
    # below carries only accepted/command_id/status/state_version.
    try:
        items = chat_image.request_images(body.image, body.images)
    except chat_image.ChatImageError as exc:
        return JSONResponse(status_code=exc.status_code, content=exc.body())
    rejection = _validate_chat_text(body.text, allow_empty=bool(items))
    if rejection is not None:
        return JSONResponse(status_code=422, content={"detail": rejection})

    key = request.headers.get("Idempotency-Key") or body.idempotency_key
    dispatcher = request.app.state.dispatcher
    host = getattr(getattr(request, "app", None), "state", None)
    host = getattr(host, "host", None)
    if host is None:
        host = request.app.state.host  # preserved: raises like the pre-extraction handler
    text = body.text
    if items:
        # Route A. This is a sync route, so FastAPI runs it in the worker
        # threadpool: the describe call (seconds) never blocks the event loop.
        try:
            decoded = chat_image.decode_images(items)
            direct = chat_image.direct_route(host)
            if not direct:
                chat_image.check_vision_ready(host)
            unavailable = _engine_unavailable(host)
            if unavailable is not None:
                return unavailable
            if not direct:
                # Per-image size and mime are logged inside describe_images, nothing else.
                descriptions = chat_image.describe_images(host, decoded, body.text)
        except chat_image.ChatImageError as exc:
            return JSONResponse(status_code=exc.status_code, content=exc.body())
        if direct:
            # P2: the cloud chat model sees the images itself, in the same call.
            text = chat_image.compose_direct_turn_text(body.text, len(decoded))
            host.vision_stash_images(
                text, [direct_cloud.shrink_for_cloud(data, mime) for data, mime in decoded]
            )
        else:
            text = chat_image.compose_turn_text_multi(body.text, descriptions)
    # runtime-fixes-20261110 T2: this route IS the desktop door; the origin is
    # fixed here and never taken from the request.
    outcome = submit_direct_turn(host, dispatcher, text, key, origin=turn_origin.DESKTOP)
    if outcome.kind == "unavailable":
        return JSONResponse(
            status_code=409,
            content={
                "accepted": False,
                "detail": {
                    "error": "ENGINE_UNAVAILABLE",
                    "state": outcome.state,
                    "message": "LLM engine is not ready for inference.",
                },
            },
        )
    if outcome.kind == "accepted":
        return {
            "accepted": True,
            "command_id": outcome.result.command_id,
            "status": "queued",
            "state_version": dispatcher.state_version,
        }
    if outcome.kind == "conflict":
        return JSONResponse(status_code=409, content={"accepted": False, "reason": "conflict"})
    return JSONResponse(status_code=429, content={"accepted": False, "reason": "queue_full"})


@router.get("/api/chat/last-reply", response_model=ChatLastReplyResponse)
def get_chat_last_reply(request: Request, turn_id: Optional[int] = None):
    # R8-safe: surfaces Kira's OWN generated reply text only -- see
    # ChatReplySink docstring (engine_host.py). Never the viewer/operator
    # text that triggered it.
    sink = request.app.state.host.chat_sink
    if turn_id is None:
        return ChatLastReplyResponse(**sink.last())
    # `?turn_id=N`: an older recent turn (follow-reading of a queued reply).
    # 404 once it aged out of the sink window.
    reply = sink.get(turn_id)
    if reply is None:
        return JSONResponse(status_code=404, content={"detail": "turn_not_found"})
    return ChatLastReplyResponse(**reply)


@router.get("/api/chat/speech-progress", response_model=ChatSpeechProgressResponse)
def get_chat_speech_progress(request: Request) -> ChatSpeechProgressResponse:
    """Real-time, in-memory speech playback progress for UI sentence tracking."""
    host = getattr(getattr(request, "app", None), "state", None)
    host = getattr(host, "host", None)
    motor = getattr(host, "motor", None)
    getter = getattr(motor, "speech_progress", None)
    progress = getter() if callable(getter) else None
    if not progress:
        return ChatSpeechProgressResponse(speaking=False, turn_id=None, current_idx=None, played=0, total=0)
    return ChatSpeechProgressResponse(**progress)
