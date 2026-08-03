"""/api/agenda/* -- state, topic add/action, session settings/action,
cohost-profiles list/save/select (moved verbatim from main.py,
refactor_core_api_20260802 B6).

No name in this family is monkeypatched on `opencohost.api.main` (confirmed
by grep across tests/ for every helper and constant below), so every import
here binds directly at module load time -- no `deps` accessor is needed and
none is imported. `AgendaState`/`TopicStatus` (smart_aggregator.kira_agenda_controller),
`enqueue_agenda_action` (api.agenda_driver), and `load_cohost_profiles`/
`normalize_cohost_profile`/`sanitize_profile_name`/`save_cohost_profiles`
(core.cohost_profiles) move out of `main.py` entirely -- nothing else there
still calls them. `_agenda_topic_out`, `_agenda_response`,
`_cohost_profiles_response`, `_cohost_profiles_lock`, and the three
whitelist/bound constants below are used ONLY by this family, so they
relocate here wholesale.

No parameterized (`{...}`) path exists in this family -- every mutation is a
POST body verb (`action`, `direction`) against a fixed URL, never a URL
template -- so this router carries no path-ambiguity CAUTION block (contrast
routers/music.py, routers/perfiles.py, routers/agent.py, routers/memoria.py).
"""

import threading

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from opencohost.api.agenda_driver import enqueue_agenda_action
from opencohost.api.models import (
    AgendaMetrics,
    AgendaResponse,
    AgendaSessionActionRequest,
    AgendaSessionRequest,
    AgendaSessionSettings,
    AgendaTopicActionRequest,
    AgendaTopicOut,
    AgendaTopicRequest,
    CohostProfileOut,
    CohostProfileSaveRequest,
    CohostProfileSelectRequest,
    CohostProfileSelectResponse,
    CohostProfilesResponse,
)
from opencohost.core.profiles.cohost_profiles import (
    load_cohost_profiles,
    normalize_cohost_profile,
    sanitize_profile_name,
    save_cohost_profiles,
)
from opencohost.smart_aggregator.kira_agenda_controller import AgendaState, TopicStatus

router = APIRouter()

# POST /api/agenda/topic/action verb whitelist -- mirrors main.py's
# _COMMAND_WHITELIST idiom. "reject" acts on the pre-approval DRAFTED
# suggestion inbox (-> SKIPPED), distinct from "remove" which drops a
# post-approval QUEUED topic.
_AGENDA_ACTION_WHITELIST = frozenset({"approve", "queue", "remove", "move", "reject"})

# POST /api/agenda/session/action verb whitelist -- the three KiraAgendaController
# mode controls. None of them raise, so the handler needs no try/except.
_AGENDA_SESSION_ACTION_WHITELIST = frozenset({"enable", "soft_stop", "emergency_stop"})

# POST /api/agenda/topic raw-constraint DoS bound = 2x controller MAX_CONSTRAINTS
# (12). add_topic sanitizes EVERY submitted constraint (regex chain) BEFORE
# slicing to MAX_CONSTRAINTS, so an unbounded list is unbounded regex work at a
# trust boundary. Truncation-to-12 stays the controller's contract; this only
# caps the raw count before any sanitization runs.
_AGENDA_CONSTRAINTS_RAW_MAX = 24

# cohost_profiles.json write-lock (WU3): guards the read-modify-write of the
# cohost-profiles file (save). A separate file gets a separate lock (D4).
_cohost_profiles_lock = threading.Lock()


def _agenda_topic_out(topic) -> AgendaTopicOut:
    return AgendaTopicOut(
        id=topic.id,
        title=topic.title,
        angle=topic.angle,
        priority=topic.priority,
        response_length=topic.response_length,
        status=topic.status.value,
        turns_spoken=topic.turns_spoken,
        confidence=topic.confidence,
        source=topic.source,
    )


def _agenda_response(agenda, *, applied=None, reason=None) -> AgendaResponse:
    metrics = agenda.get_metrics()
    return AgendaResponse(
        state=agenda.state.value,
        active_topic=_agenda_topic_out(agenda.active_topic) if agenda.active_topic else None,
        queued_topics=[_agenda_topic_out(t) for t in agenda.queued_topics()],
        drafted_topics=[_agenda_topic_out(t) for t in agenda.drafted_topics()],
        session_settings=AgendaSessionSettings(
            max_turns_per_topic=agenda.max_turns_per_topic,
            rhythm=agenda.rhythm,
            response_length=agenda.response_length,
            safety_mode=agenda.safety_mode,
            profile_style=agenda.profile.get("style", ""),
        ),
        metrics=AgendaMetrics(
            total_rejections=metrics["total_rejections"],
            by_error_code=metrics["by_error_code"],
            by_guardrail=metrics["by_guardrail"],
            avg_similarity_overlap_pct=metrics["avg_similarity_overlap_pct"],
            current_state=metrics["current_state"],
            failure_count=metrics["failure_count"],
            response_length=metrics["response_length"],
            active_topic=metrics["active_topic"],
            topics_queued=metrics["topics_queued"],
            last_outputs_count=metrics["last_outputs_count"],
        ),
        # FIX-C: session/action outcome. Only POST /api/agenda/session/action
        # sets these; every other response leaves them null.
        applied=applied,
        reason=reason,
    )


def _cohost_profiles_response(profiles: dict) -> CohostProfilesResponse:
    return CohostProfilesResponse(
        profiles=[
            CohostProfileOut(
                name=name,
                style=str(p.get("style", "")),
                default_priority=str(p.get("default_priority", "normal")),
                default_response_length=str(p.get("default_response_length", "normal")),
            )
            for name, p in profiles.items()
        ]
    )


@router.get("/api/agenda", response_model=AgendaResponse)
def get_agenda(request: Request):
    agenda = getattr(request.app.state.host, "agenda", None)
    if agenda is None:
        return JSONResponse(status_code=503, content={"detail": "agenda_unavailable"})
    with request.app.state.host.agenda_lock:
        return _agenda_response(agenda)


@router.post("/api/agenda/topic", response_model=AgendaResponse)
def post_agenda_topic(request: Request, body: AgendaTopicRequest):
    host = request.app.state.host
    agenda = getattr(host, "agenda", None)
    if agenda is None:
        return JSONResponse(status_code=503, content={"detail": "agenda_unavailable"})
    # Raw-count guard BEFORE the lock/sanitization: bound the regex work the
    # controller does per constraint (truncation-to-12 stays its contract).
    if body.constraints is not None and len(body.constraints) > _AGENDA_CONSTRAINTS_RAW_MAX:
        return JSONResponse(status_code=422, content={"detail": "too many constraints"})
    with host.agenda_lock:
        try:
            # CTK parity decision 2026-07-05 (Option A): a POSTed topic is
            # operator-approved and goes straight to the queue, matching the
            # UI label "Agregar a cola" and app_shell.py:1317-1318
            # (add_topic(approved=True) + queue_topic). Only QUEUED topics
            # are selectable by the driver (_select_next_topic).
            topic = agenda.add_topic(
                body.title,
                angle=body.angle or "",
                constraints=body.constraints or [],
                priority=body.priority or "normal",
                response_length=body.response_length or "normal",
                approved=True,
            )
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc)})
        # Idempotent: a dedup that returns an already-QUEUED (or otherwise
        # non-APPROVED) topic must not re-queue -- queue_topic only accepts
        # APPROVED and would raise on a repeat POST (WU4 idempotency test).
        if topic.status == TopicStatus.APPROVED:
            agenda.queue_topic(topic.id)
        return _agenda_response(agenda)


@router.post("/api/agenda/topic/action", response_model=AgendaResponse)
def post_agenda_topic_action(request: Request, body: AgendaTopicActionRequest):
    host = request.app.state.host
    agenda = getattr(host, "agenda", None)
    if agenda is None:
        return JSONResponse(status_code=503, content={"detail": "agenda_unavailable"})
    if body.action not in _AGENDA_ACTION_WHITELIST:
        return JSONResponse(status_code=422, content={"detail": "unknown action"})
    with host.agenda_lock:
        try:
            if body.action == "approve":
                # CTK parity decision 2026-07-05 (Option A): approving a
                # drafted suggestion also queues it in one step, mirroring
                # topic_inbox_bridge.py:138-139. approve_topic raises on a
                # non-DRAFTED topic (surfaced as 422 below), so a repeat
                # never double-queues.
                agenda.approve_topic(body.topic_id)
                agenda.queue_topic(body.topic_id)
            elif body.action == "queue":
                agenda.queue_topic(body.topic_id)
            elif body.action == "remove":
                agenda.remove_queued_topic(body.topic_id)
            elif body.action == "move":
                agenda.move_queued_topic(body.topic_id, body.direction or 1)
            elif body.action == "reject":
                agenda.reject_topic(body.topic_id)
        except (ValueError, KeyError) as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc)})
        return _agenda_response(agenda)


@router.put("/api/agenda/session", response_model=AgendaResponse)
def put_agenda_session(request: Request, body: AgendaSessionRequest):
    host = request.app.state.host
    agenda = getattr(host, "agenda", None)
    if agenda is None:
        return JSONResponse(status_code=503, content={"detail": "agenda_unavailable"})
    with host.agenda_lock:
        if body.profile is not None and body.profile.style is not None:
            try:
                agenda.set_profile({"style": body.profile.style})
            except ValueError as exc:
                return JSONResponse(status_code=422, content={"detail": str(exc)})
        agenda.set_session_settings(
            max_turns_per_topic=body.max_turns_per_topic,
            rhythm=body.rhythm,
            response_length=body.response_length,
            safety_mode=body.safety_mode,
        )
        return _agenda_response(agenda)


@router.get("/api/agenda/cohost-profiles", response_model=CohostProfilesResponse)
def get_cohost_profiles() -> CohostProfilesResponse:
    # Disk-only read; no host needed. Falls back to the 3 defaults when the
    # file is absent. No `selected` field -- selection is stateless (RAM).
    with _cohost_profiles_lock:
        return _cohost_profiles_response(load_cohost_profiles())


@router.post("/api/agenda/cohost-profiles", response_model=CohostProfilesResponse)
def save_cohost_profile(body: CohostProfileSaveRequest):
    clean = sanitize_profile_name(body.name)
    if not clean:
        return JSONResponse(status_code=422, content={"detail": "empty profile name"})
    with _cohost_profiles_lock:
        profiles = load_cohost_profiles()
        profiles[clean] = normalize_cohost_profile(
            {
                "style": body.style,
                "default_priority": body.priority or "normal",
                "default_response_length": body.length or "normal",
            }
        )
        if not save_cohost_profiles(profiles):
            return JSONResponse(status_code=503, content={"detail": "cohost_write_failed"})
        return _cohost_profiles_response(profiles)


@router.post("/api/agenda/cohost-profiles/select", response_model=CohostProfileSelectResponse)
def select_cohost_profile(request: Request, body: CohostProfileSelectRequest):
    host = request.app.state.host
    agenda = getattr(host, "agenda", None)
    if agenda is None:
        return JSONResponse(status_code=503, content={"detail": "agenda_unavailable"})
    # Load under the disk lock but NEVER write -- selection is stateless.
    with _cohost_profiles_lock:
        profiles = load_cohost_profiles()
    if body.name not in profiles:
        return JSONResponse(status_code=404, content={"detail": "profile not found"})
    with host.agenda_lock:
        try:
            agenda.set_profile({"style": profiles[body.name]["style"]})
        except ValueError as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc)})
    return CohostProfileSelectResponse(selected=body.name)


@router.post("/api/agenda/session/action", response_model=AgendaResponse)
def post_agenda_session_action(request: Request, body: AgendaSessionActionRequest):
    host = request.app.state.host
    agenda = getattr(host, "agenda", None)
    if agenda is None:
        return JSONResponse(status_code=503, content={"detail": "agenda_unavailable"})
    if body.action not in _AGENDA_SESSION_ACTION_WHITELIST:
        return JSONResponse(status_code=422, content={"detail": "unknown action"})
    motor = getattr(host, "motor", None)
    with host.agenda_lock:
        if body.action == "enable":
            # CTK parity (app_shell.py:1415-1419): never start a session with
            # an empty queue and no active topic. Return 200 with an explicit
            # not-applied outcome so the UI can explain, instead of silently
            # flipping to IDLE with nothing to say.
            if not agenda.queued_topics() and not agenda.active_topic:
                return _agenda_response(agenda, applied=False, reason="empty_queue")
            agenda.enable()
            if agenda.state == AgendaState.OFF:
                # Fail-closed gate refused (GUARDRAILS_MISSING) -- mirror the
                # empty_queue branch above: honest applied=False instead of
                # claiming success while the state never left OFF.
                return _agenda_response(agenda, applied=False, reason="guardrails_missing")
            # Bug B fix: clear any speech-cancellation token left by a prior
            # emergency stop so this legitimate agenda run can speak.
            if motor is not None and hasattr(motor, "clear_speech_cancel"):
                motor.clear_speech_cancel()
            # CTK parity (app_shell.py:1432): tick immediately on enable so
            # Kira opens the first topic without waiting out the driver
            # cadence. nudge() just sets an Event -- safe under the lock.
            driver = getattr(host, "_agenda_driver", None)
            if driver is not None:
                driver.nudge()
            return _agenda_response(agenda, applied=True)
        if body.action == "soft_stop":
            # FIX-C: the headless host now HAS a speech pipeline (chat works),
            # so enqueue the closing line the controller returns instead of
            # dropping it (mirror app_shell.py:1435). A none() action (agenda
            # already idle) is a no-op inside enqueue_agenda_action.
            enqueue_agenda_action(motor, agenda.soft_stop())
            return _agenda_response(agenda, applied=True)
        # emergency_stop
        agenda.emergency_stop()
        # Mirror app_shell.py:1494-1497: interrupt in-flight speech and drop
        # any pending agenda turns from the motor queue.
        if motor is not None:
            # Bug B fix: set the speech-cancellation token BEFORE interrupt
            # so a turn already popped from the priority queue during its
            # generation phase (the straggler) is refused at _hablar entry
            # instead of playing after the stop.
            if hasattr(motor, "cancel_speech_for_sources"):
                motor.cancel_speech_for_sources(("kira-agenda",))
            if hasattr(motor, "interrupt_speaking"):
                motor.interrupt_speaking()
            if hasattr(motor, "drop_pending_sources"):
                motor.drop_pending_sources(("kira-agenda",))
        return _agenda_response(agenda, applied=True)
