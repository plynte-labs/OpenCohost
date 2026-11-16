"""Turn relay for the remote gateway: ``POST /turn`` and ``GET /reply``.

A paired phone sends text, which enters the engine as an owner-typed ``direct``
turn (the shared ``submit_direct_turn`` path), then short-polls ``/reply``. The
reply is read from the host's ``ChatReplySink``; a ``turn_dropped`` or
``reasoning_budget_exhausted`` motor event, or a timeout, ends the wait with
``no_reply``.

KNOWN v0 RACE: nothing correlates a turn to the command that produced a reply.
A ``direct`` reply is matched only by ``turn_id > after`` and ``origin ==
"direct"``, so a desktop-typed direct turn answered between this device's submit
and its own reply could be returned to the phone. Accepted for v0 (single owner).

PRIVACY: logs carry device ids and counts only. Neither the turn text nor the
reply text is ever logged or echoed in an error.
"""

import collections
import json
import logging
import threading
import time

from fastapi import HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

logger = logging.getLogger("OpenCohost.gateway")

TURN_BODY_MAX_BYTES = 16 * 1024  # 4000 chars of non-ASCII text is up to ~8 KB of UTF-8
REPLY_TIMEOUT_S = 90.0
MAX_TURNS_PER_MINUTE = 20
_WINDOW_S = 60.0
_NO_REPLY_EVENTS = frozenset({"turn_dropped", "reasoning_budget_exhausted"})
_NO_STORE = {"Cache-Control": "no-store"}


class _Submission:
    def __init__(self, submitted_at: float, after: int, event_cursor: int):
        self.submitted_at = submitted_at
        self.after = after
        self.event_cursor = event_cursor
        # Terminal result, kept so a repeated poll (a lost response) answers the same.
        self.result = None


class TurnRelay:
    def __init__(self, host, dispatcher, clock=time.monotonic):
        self._host = host
        self._dispatcher = dispatcher
        self._clock = clock
        self._lock = threading.Lock()
        self._submissions: dict = {}
        self._attempts: dict = {}

    # --- /turn -----------------------------------------------------------

    def reserve(self, device_id: str):
        """Claim the device's turn slot, or return None when it is rate limited.
        Returns ``(submission, previous)``; ``previous`` restores the slot on failure."""
        now = self._clock()
        with self._lock:
            current = self._submissions.get(device_id)
            if current is not None and current.result is None and now - current.submitted_at < REPLY_TIMEOUT_S:
                return None
            attempts = self._attempts.setdefault(device_id, collections.deque())
            while attempts and now - attempts[0] >= _WINDOW_S:
                attempts.popleft()
            if len(attempts) >= MAX_TURNS_PER_MINUTE:
                return None
            attempts.append(now)
            sink = getattr(self._host, "chat_sink", None)
            after = int((sink.last().get("turn_id") if sink is not None else 0) or 0)
            submission = _Submission(now, after, self._event_cursor())
            self._submissions[device_id] = submission
            return submission, current

    def release(self, device_id: str, submission, previous) -> None:
        with self._lock:
            if self._submissions.get(device_id) is submission:
                if previous is None:
                    del self._submissions[device_id]
                else:
                    self._submissions[device_id] = previous
            attempts = self._attempts.get(device_id)
            if attempts:
                attempts.pop()  # a refused submit does not count against the minute budget

    def _event_cursor(self) -> int:
        log = getattr(self._host, "event_log", None)
        return int(log.since(10**12)["cursor"]) if log is not None else 0

    def submit(self, text: str):
        from opencohost.api.direct_turn import submit_direct_turn
        from opencohost.core import turn_origin

        # Everything that enters through the gateway is remote, whatever the
        # request says: the origin is fixed here, never read from the client.
        return submit_direct_turn(self._host, self._dispatcher, text, origin=turn_origin.REMOTE)

    # --- /reply ----------------------------------------------------------

    def poll(self, device_id: str, after: int) -> dict:
        with self._lock:
            sub = self._submissions.get(device_id)
            if sub is None:
                return {"status": "idle"}
            if sub.result is not None:
                return dict(sub.result)
            result = self._resolve(sub, after)
            if result is not None:
                sub.result = result
                return dict(result)
            return {"status": "pending"}

    def _resolve(self, sub, after: int):
        # The server's own snapshot wins over the client-supplied ``after``: a
        # device cannot read replies from before its own submission.
        floor = max(sub.after, after)
        sink = getattr(self._host, "chat_sink", None)
        if sink is not None:
            for item in sink.since(floor):
                if item.get("origin") == "direct" and not item.get("is_streaming"):
                    return {"status": "ready", "text": item.get("text") or "", "turn_id": item["turn_id"]}
        log = getattr(self._host, "event_log", None)
        if log is not None:
            for event in log.since(sub.event_cursor)["events"]:
                if event.get("action") in _NO_REPLY_EVENTS:
                    return {"status": "no_reply"}
        if self._clock() - sub.submitted_at >= REPLY_TIMEOUT_S:
            return {"status": "no_reply"}
        return None


def _device_label(registry, device_id: str) -> str:
    """The paired device's sanitized name, for the operator's own conversation view."""
    try:
        for record in registry.list_devices() if registry is not None else ():
            if record.get("device_id") == device_id:
                return str(record.get("name") or "")
    except Exception:
        pass
    return ""


def register_turn_routes(router, relay: TurnRelay, read_capped_body, feed=None, registry=None) -> None:
    from opencohost.api.routers.chat import _CHAT_TEXT_MAX_LENGTH, _validate_chat_text
    from opencohost.gateway.app import require_device
    from fastapi import Depends

    @router.post("/turn")
    async def post_turn(request: Request, device_id: str = Depends(require_device)):
        body = await read_capped_body(request, TURN_BODY_MAX_BYTES)
        if body is None:
            return JSONResponse(status_code=413, content={"error": "too_large"})
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if not isinstance(data, dict) or not isinstance(data.get("text"), str):
            return JSONResponse(status_code=400, content={"error": "bad_request"})
        text = data["text"]
        if _validate_chat_text(text) is not None:
            code = "text_too_long" if len(text) > _CHAT_TEXT_MAX_LENGTH else "empty_text"
            return JSONResponse(status_code=422, content={"error": code})

        claim = relay.reserve(device_id)
        if claim is None:
            logger.info("gateway turn rate_limited device=%s", device_id)
            return JSONResponse(status_code=429, content={"error": "rate_limited"}, headers={"Retry-After": "5"})
        submission, previous = claim
        try:
            outcome = await run_in_threadpool(relay.submit, text)
        except BaseException:
            relay.release(device_id, submission, previous)
            raise
        if outcome.kind != "accepted":
            relay.release(device_id, submission, previous)
            logger.info("gateway turn refused device=%s reason=%s", device_id, outcome.kind)
            if outcome.kind == "unavailable":
                return JSONResponse(status_code=409, content={"error": "ENGINE_UNAVAILABLE", "state": outcome.state})
            if outcome.kind == "conflict":
                return JSONResponse(status_code=409, content={"error": "conflict"})
            return JSONResponse(status_code=429, content={"error": "queue_full"})
        if feed is not None:
            feed.add(text, device_id, _device_label(registry, device_id))
        logger.info("gateway turn accepted device=%s", device_id)
        return JSONResponse(content={"accepted": True, "after": submission.after}, headers=_NO_STORE)

    @router.get("/reply")
    def get_reply(after: int, device_id: str = Depends(require_device)):
        if after < 0:
            raise HTTPException(status_code=400, detail="bad_request")
        result = relay.poll(device_id, after)
        if result["status"] != "pending":
            logger.info("gateway reply device=%s status=%s", device_id, result["status"])
        return JSONResponse(content=result, headers=_NO_STORE)
