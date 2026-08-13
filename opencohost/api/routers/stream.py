"""/api/stream/chat-live* -- state, connect, disconnect, limits (moved
VERBATIM from main.py, refactor_core_api_20260802 B6). Viewer surface --
out-of-scope for any rework, per proposal.md; this move is purely mechanical.

No name in this family is monkeypatched on `opencohost.api.main` (confirmed
by grep across tests/), so every import here binds directly at module load
time -- no `deps` accessor is needed. `parse_chat_url`
(smart_aggregator.url_parser) and `_stream_state` are used ONLY by this
family and move here wholesale. `logger` is never monkeypatched and already
lives in `opencohost.api.shared` (imported by both `main.py` and every
router that needs it); reused here for the one warning log in
`post_stream_connect`.

No parameterized (`{...}`) path exists in this family -- no CAUTION block
needed (contrast routers/music.py, routers/perfiles.py, routers/agent.py,
routers/memoria.py).

`GET .../chat-live/messages` (RF3, tauri_stream_chat_20260812 B3) is new, not
part of the B6 move -- it self-gates on the operator token (`_operator_role`
below) since auth_middleware's rule 3 leaves plain GETs open, and this is the
one GET in the whole API allowed to carry raw viewer chat text.
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from opencohost.api.auth import TokenFileError, _bearer_token, _resolve_role
from opencohost.api.models import (
    ChatLiveMessageOut,
    ChatLiveMessagesResponse,
    StreamChatLiveResponse,
    StreamConnectRequest,
    StreamLimitsRequest,
)
from opencohost.api.shared import logger
# Module import, not `from ... import USE_INPUT_CONTRACT_PROMPT`: a from-import
# would freeze the boot value, and both live readers (the CTK toggle in
# stream_admin_ui and chat_reaction's RF3 gate) go through the module
# attribute so a flip is seen everywhere.
import opencohost.smart_aggregator.chat_input_contract as chat_input_contract
# Same module-attribute discipline for the tier-split settings: the engine's
# TTL sweep and the agenda controller's mint sites read these live.
from opencohost.core import turn_priority
from opencohost.smart_aggregator.url_parser import parse_chat_url

router = APIRouter()

# RF3: GET /api/stream/chat-live/messages caps a single poll response so a
# backlog can't blow up json.dumps cost (measured: 200 msgs = 291us/31KB,
# 50 = 7.8KB) -- see execution-plan.md 2 "Transport is settled".
_CHAT_LIVE_PAGE_SIZE = 50


def _operator_role(request: Request) -> "str | None":
    """Fail-closed-to-None token role, mirroring agent.py's
    `_agent_request_role`. Needed because auth_middleware only ever gates
    `/api/agent/*` (any method) and mutating verbs elsewhere (auth.py rules
    1-2) -- a bare GET like this one stays open under rule 3, and this is
    the one GET that must NOT: it's the sole place raw viewer chat text and
    usernames are allowed onto HTTP at all (R8 scoping, owner-approved
    2026-08-12), so it gates itself instead of relying on the middleware.

    TokenFileError propagates rather than collapsing to None: an unreadable
    token file is a broken server, not a bad credential. Swallowing it would
    tell an operator with a corrupt token file that their token is wrong,
    which sends them looking in the wrong place. This is where it diverges
    from `_agent_request_role`, whose fail-closed-to-None is fine because
    auth_middleware rule 1 already gated the request before it runs.
    `get_stream_chat_live_messages` -- the only caller -- catches it and
    returns the same 503 `auth_unavailable` auth_middleware returns.
    """
    token = _bearer_token(request)
    if token is None:
        return None
    return _resolve_role(token)


def _stream_state(agg) -> StreamChatLiveResponse:
    # R8: STATE + LIMITS ONLY -- never read anything but the accessors
    # below (no message/user/text field exists on Aggregator's public
    # surface for this to leak from).
    source = agg._source
    connected = source is not None and source.is_connected()
    return StreamChatLiveResponse(
        connected=connected,
        platform=source.platform if source is not None else None,
        source_id=source._source_id if source is not None else None,
        threshold_per_second=agg.activity.threshold_per_second,
        cooldown_seconds=agg.activity.cooldown_seconds,
        max_messages_per_user=agg._spam_max_messages,
        filter_policy=agg.get_filter_policy(),
        # NOT read off `agg`: this is process-global module state (the same
        # flag the CTK Input Contract switch toggles), see the write site in
        # put_stream_limits below.
        input_contract=chat_input_contract.USE_INPUT_CONTRACT_PROMPT,
        # Also process-global module state (turn_priority), not `agg` state.
        stream_over_agenda=turn_priority.STREAM_OVER_AGENDA,
        stream_ttl_seconds=turn_priority.STREAM_TTL_SECONDS,
        # Derived, read-only: agenda-first floors the window (THE TRAP guard,
        # see turn_priority.effective_stream_ttl) — surfaced so the UI can
        # show the streamer the window actually in force.
        effective_stream_ttl_seconds=turn_priority.effective_stream_ttl(),
    )


@router.get("/api/stream/chat-live", response_model=StreamChatLiveResponse)
def get_stream_chat_live(request: Request):
    agg = getattr(request.app.state.host, "aggregator", None)
    if agg is None:
        return JSONResponse(status_code=503, content={"detail": "stream_unavailable"})
    return _stream_state(agg)


@router.post("/api/stream/chat-live/connect", response_model=StreamChatLiveResponse)
def post_stream_connect(request: Request, body: StreamConnectRequest):
    host = request.app.state.host
    agg = getattr(host, "aggregator", None)
    if agg is None:
        return JSONResponse(status_code=503, content={"detail": "stream_unavailable"})
    try:
        parsed = parse_chat_url(body.url)
    except ValueError:
        return JSONResponse(status_code=422, content={"detail": "invalid_url"})
    if not host.aggregator_lock.acquire(blocking=False):
        return JSONResponse(status_code=409, content={"detail": "busy"})
    try:
        agg.connect(parsed["source_id"], platform=parsed["platform"])
    except ValueError:
        # Aggregator rejected the platform (e.g. "Plataforma no soportada").
        return JSONResponse(status_code=422, content={"detail": "unsupported_platform"})
    except RuntimeError:
        # Chat connector unavailable (e.g. pytchat not installed).
        return JSONResponse(status_code=503, content={"detail": "chat_source_unavailable"})
    except Exception:
        # Unexpected failure: degrade to 503 without leaking internals.
        # R8: log id/platform only -- never chat content or the raw message.
        logger.warning(
            "chat-live connect failed platform=%s source_id=%s",
            parsed["platform"],
            parsed["source_id"],
        )
        return JSONResponse(status_code=503, content={"detail": "stream_unavailable"})
    finally:
        host.aggregator_lock.release()
    return _stream_state(agg)


@router.post("/api/stream/chat-live/disconnect", response_model=StreamChatLiveResponse)
def post_stream_disconnect(request: Request):
    host = request.app.state.host
    agg = getattr(host, "aggregator", None)
    if agg is None:
        return JSONResponse(status_code=503, content={"detail": "stream_unavailable"})
    # Single-flight with connect: both mutate agg._source, so serialize
    # them on the same lock (mirrors post_stream_connect) to avoid a
    # disconnect racing an in-flight connect on _source.
    if not host.aggregator_lock.acquire(blocking=False):
        return JSONResponse(status_code=409, content={"detail": "busy"})
    try:
        agg.disconnect()
    finally:
        host.aggregator_lock.release()
    return _stream_state(agg)


@router.get("/api/stream/chat-live/messages", response_model=ChatLiveMessagesResponse)
def get_stream_chat_live_messages(request: Request, since: int = 0):
    # R8-CRITICAL: operator token required, unconditionally -- unlike the
    # other mutating-only operator gate (auth.py rule 2, opt-in behind
    # API_AUTH_ENFORCED for back-compat with the token-less Tauri app), this
    # is a brand new surface exposing raw chat, so it is never left open.
    try:
        role = _operator_role(request)
    except TokenFileError:
        # An unreadable/corrupt token file is a broken server, not a bad
        # credential: same 503 + detail auth_middleware returns (auth.py
        # rules 1-2), never a 500 traceback and never a 403 that would send
        # the operator hunting for a token that is fine. There is no
        # exception handler registered on this app, so without this catch it
        # would escape as a 500 -- and 500 is not fail-closed by accident,
        # it just happens not to serve messages.
        return JSONResponse(status_code=503, content={"detail": "auth_unavailable"})
    if role != "operator":
        return JSONResponse(status_code=403, content={"detail": "operator token required"})
    # chat_feed is always constructed in EngineHost.__init__ (unlike
    # aggregator, which is None until start() succeeds) -- no 503 branch
    # needed; a disconnected/not-yet-started chat just reads as empty.
    sink_result = request.app.state.host.chat_feed.since(since)
    pending = sink_result["messages"]
    more_pending = len(pending) > _CHAT_LIVE_PAGE_SIZE
    page = pending[:_CHAT_LIVE_PAGE_SIZE] if more_pending else pending
    # Cursor advances only to the last message actually returned when
    # capped, so the next poll resumes right after it instead of skipping
    # the rest of the backlog; uncapped, it mirrors the sink's own cursor
    # (still correct on an empty page -- never regresses).
    cursor = page[-1]["seq"] if more_pending else sink_result["cursor"]
    return ChatLiveMessagesResponse(
        messages=[ChatLiveMessageOut(**m) for m in page],
        cursor=cursor,
        boot=sink_result["boot"],
        session=sink_result["session"],
        more_pending=more_pending,
    )


@router.put("/api/stream/chat-live/limits", response_model=StreamChatLiveResponse)
def put_stream_limits(request: Request, body: StreamLimitsRequest):
    agg = getattr(request.app.state.host, "aggregator", None)
    if agg is None:
        return JSONResponse(status_code=503, content={"detail": "stream_unavailable"})
    if body.threshold_per_second is not None or body.cooldown_seconds is not None:
        agg.set_activity_limits(
            threshold_per_second=body.threshold_per_second,
            cooldown_seconds=body.cooldown_seconds,
        )
    if body.max_messages_per_user is not None:
        agg.set_spam_limits(max_messages_per_user=body.max_messages_per_user)
    if body.filter_policy is not None:
        try:
            agg.set_filter_policy(body.filter_policy)
        except ValueError:
            return JSONResponse(status_code=422, content={"detail": "invalid_filter_policy"})
    if body.input_contract is not None:
        # PROCESS-GLOBAL module flag, not aggregator state — a future reader
        # will look for it on `agg` and it is not there. Same mechanism as the
        # CTK toggle (stream_admin_ui.py _toggle_input_contract): rebind the
        # module attribute so the RF3 gate (chat_reaction, running on the chat
        # source reader thread) sees the flip on its next live read. A bare
        # bool rebind is atomic under the GIL; no lock needed, matching the
        # lockless CTK writer.
        chat_input_contract.USE_INPUT_CONTRACT_PROMPT = bool(body.input_contract)
    # Tier-split settings (tauri_stream_chat_20260812 §3.2). PROCESS-GLOBAL
    # turn_priority module attributes, same rebind idiom as input_contract
    # above (atomic under the GIL, read live by the engine's TTL sweep, the
    # agenda controller's mint sites and enqueue()'s per-source default).
    # Validate BEFORE writing either so a bad TTL never half-applies a body
    # that also flips the order.
    if body.stream_ttl_seconds is not None and not (
        turn_priority.STREAM_TTL_MIN_SECONDS
        <= body.stream_ttl_seconds
        <= turn_priority.STREAM_TTL_MAX_SECONDS
    ):
        # A ~0s window would expire every stream item before it can be
        # popped — the mute-co-host failure from the other direction.
        return JSONResponse(status_code=422, content={"detail": "invalid_stream_ttl"})
    if body.stream_over_agenda is not None:
        turn_priority.STREAM_OVER_AGENDA = bool(body.stream_over_agenda)
    if body.stream_ttl_seconds is not None:
        turn_priority.STREAM_TTL_SECONDS = float(body.stream_ttl_seconds)
    return _stream_state(agg)
