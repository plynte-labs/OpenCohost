"""GET /api/events (moved verbatim from main.py, refactor_core_api_20260802 B4)."""

from fastapi import APIRouter, Request

from opencohost.api.models import EventLogResponse

router = APIRouter()


@router.get("/api/events", response_model=EventLogResponse)
def get_events(request: Request, since: int = 0) -> EventLogResponse:
    # Item B: engine-thread event log, metadata-only (see EventLogSink /
    # EngineHost._record_motor_event's closed whitelist, engine_host.py).
    # GET stays open per auth.py rule 3 (non-mutating reads are never
    # gated), same tier as /api/status and /api/chat/last-reply above.
    return EventLogResponse(**request.app.state.host.event_log.since(since))
