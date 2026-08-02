"""/api/chat/turn, /api/chat/last-reply, POST /api/commands
(moved verbatim from main.py, refactor_core_api_20260802 B6).

No name in this family is monkeypatched on `opencohost.api.main` (confirmed
by grep across tests/), so every import here binds directly at module load
time -- no `deps` accessor is needed. `_COMMAND_WHITELIST`,
`_engine_command_payload`, `_validate_command_value`, `_CHAT_TEXT_MAX_LENGTH`,
and `_validate_chat_text` are used ONLY by this family, so they relocate here
wholesale. `i18n_active` (opencohost.i18n) is used only by
POST /api/chat/turn's history-wrapper call and moves with it.

No parameterized (`{...}`) path exists in this family -- no CAUTION block
needed (contrast routers/music.py, routers/perfiles.py, routers/agent.py,
routers/memoria.py).
"""

import time

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from opencohost.api.models import ChatLastReplyResponse, ChatTurnRequest, CommandRequest
from opencohost.i18n import active as i18n_active

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
        "set_motor_tts",
        "switch_model",
        "switch_llm_tier",
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
    if command in ("switch_model", "switch_llm_tier", "set_piper_voice", "set_motor_tts"):
        if not isinstance(value, str):
            return f"{command} requires a non-None string value"
        return None
    if command == "set_tts_local_only":
        if not isinstance(value, bool):
            return "set_tts_local_only requires a boolean value"
        return None
    return None


def _validate_chat_text(text: str) -> "str | None":
    """Returns None when `text` is acceptable, or a rejection reason string."""
    if not text.strip():
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


@router.post("/api/chat/turn")
def post_chat_turn(request: Request, body: ChatTurnRequest):
    # R8-CRITICAL: this handler must never return `body.text` (or any
    # derived dialogue) in the response, and must never log it. The ack
    # below carries only accepted/command_id/status/state_version.
    rejection = _validate_chat_text(body.text)
    if rejection is not None:
        return JSONResponse(status_code=422, content={"detail": rejection})
    key = request.headers.get("Idempotency-Key") or body.idempotency_key
    dispatcher = request.app.state.dispatcher
    host = request.app.state.host
    # B1 (turn provenance): a typed turn used to commit to historial BARE, so
    # the model could not tell WHO spoke and guessed (it referred to "the
    # viewer before" with no chat platform connected at all). history_text
    # gives it the same speaker frame PTT already had; the prompt payload
    # stays the raw text, so generation behavior is unchanged.
    # Unit 4.1 (runtime_findings_batch_20260731, F5): stamp submitted_at HERE
    # -- the earliest backend receipt of a typed direct question -- so
    # queue_wait_ms can later report the FULL wait (queue time was
    # invisible to TURN_LATENCY before this unit). source="direct" made
    # explicit alongside it (was previously left to _consume_command's
    # default).
    # Unit 4.2 (F12 closure): tag the item with the provider posture in
    # effect AT SUBMIT time (unit 1.3's provider_runtime_state -- the same
    # live truth /api/status reports), so a fallback/return that happens
    # while this item sits queued can be disclosed on the reply instead of
    # silently answering under a different provider with no note.
    result = dispatcher.dispatch(
        "process_context",
        body.text,
        key,
        history_text=i18n_active.typed_history_wrapper().format(text=body.text),
        source="direct",
        submitted_at=time.monotonic(),
        submitted_under_provider=host.motor.provider_runtime_state()["provider"],
    )
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


@router.get("/api/chat/last-reply", response_model=ChatLastReplyResponse)
def get_chat_last_reply(request: Request) -> ChatLastReplyResponse:
    # R8-safe: surfaces Kira's OWN generated reply text only -- see
    # ChatReplySink docstring (engine_host.py). Never the viewer/operator
    # text that triggered it.
    return ChatLastReplyResponse(**request.app.state.host.chat_sink.last())
