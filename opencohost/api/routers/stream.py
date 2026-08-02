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
"""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from opencohost.api.models import StreamChatLiveResponse, StreamConnectRequest, StreamLimitsRequest
from opencohost.api.shared import logger
from opencohost.smart_aggregator.url_parser import parse_chat_url

router = APIRouter()


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
    return _stream_state(agg)
