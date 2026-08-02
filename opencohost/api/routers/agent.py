"""/api/agent/* -- topics, status, cards, notices
(moved verbatim from main.py, refactor_core_api_20260802 B5).

Agent gateway (agent_context_gateway Phase 2). Token gating lives in
`auth_middleware` rule 1 (opencohost/api/auth.py, untouched by this move):
everything under `/api/agent/` requires the agent OR operator token (always
enforced), and mutating calls are rate limited per token (429 on breach) --
no per-route auth wiring needed here beyond the operator-only checks below.

`_parse_iso8601_utc` and `_agent_request_role` are used ONLY by this family
(confirmed by grep across the rest of main.py before the move), so they
relocate here wholesale. `_count_sql` and `_editorial_cards_by_status` are
ALSO used by main.py's own (still-inline) GET /api/memoria/stats, so they
live in `opencohost.api.shared` instead and both main.py and this router
import them. `EDITORIAL_CARDS_DB` IS monkeypatched directly on
`opencohost.api.main` (test_agent_cards_api.py, test_api_agent_gateway.py --
autouse fixtures isolating every /api/agent/cards|notices test's db in a tmp
file), so every use here goes through `deps.editorial_cards_db()`.
`TopicInboxStore`/`EditorialCardStore`/`AgentNoticeStore` and their
exceptions are never monkeypatched, so they import directly from their home
modules.

Endpoint count note: proposal.md's target layout estimated this family at
"(6)"; the actual inline surface in main.py was 8 decorators (cards has a
separate GET and POST on the same path, notices likewise) over 7 unique
paths (`tests/test_api_agent_gateway.py::TestD1AgendaIsolation::
test_agent_surface_exposes_exactly_the_designed_routes` pins the exact
7-path set as a structural trust-model guard) -- all 8 move here verbatim;
see the B5 batch handoff for the reconciled endpoint inventory.
"""

import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from opencohost.api import deps
from opencohost.api.auth import TokenFileError, _bearer_token, _resolve_role
from opencohost.api.models import (
    AgentCardArmResponse,
    AgentCardListItem,
    AgentCardRequest,
    AgentCardResponse,
    AgentCardsListResponse,
    AgentNoticeDismissResponse,
    AgentNoticeOut,
    AgentNoticeRequest,
    AgentNoticeResponse,
    AgentNoticesResponse,
    AgentStatusResponse,
    AgentTopicRequest,
    AgentTopicResponse,
)
from opencohost.api.shared import _count_sql, _editorial_cards_by_status
from opencohost.core.agent_notices import (
    AgentNoticeCapError,
    AgentNoticeStore,
    AgentNoticeValidationError,
)
from opencohost.core.editorial_cards import (
    EditorialCard,
    EditorialCardStatus,
    EditorialCardStore,
    EditorialCardValidationError,
)
from opencohost.core.topic_inbox import (
    TopicInboxCapError,
    TopicInboxStore,
    TopicInboxValidationError,
)

router = APIRouter()


def _parse_iso8601_utc(value: str | None) -> datetime | None:
    """Parse an optional ISO-8601 timestamp; naive values are treated as UTC
    (same normalization editorial_cards applies on storage). Raises
    ValueError on malformed input."""
    if not value:
        return None
    if value.endswith(("Z", "z")):
        # Python 3.10 fromisoformat rejects the Z suffix — the canonical
        # machine-emitted UTC form (Date.toISOString(), RFC3339 emitters).
        value = value[:-1] + "+00:00"
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _agent_request_role(request: Request) -> str | None:
    """Token role of an already-authenticated /api/agent/* caller.

    auth_middleware rule 1 guarantees a valid bearer token reached the
    handler; this only differentiates operator vs agent for the
    operator-only notice surface (agent token -> 403, design error
    contract). Fail-closed: any token-file hiccup reads as no role.
    """
    token = _bearer_token(request)
    if token is None:
        return None
    try:
        return _resolve_role(token)
    except TokenFileError:
        return None


@router.post("/api/agent/topics", response_model=AgentTopicResponse)
def post_agent_topic(body: AgentTopicRequest):
    # Owner decision D1: this is the ONLY topic path agents get. The
    # proposal lands in the human-gated topic_inbox as 'proposed' and
    # the operator approves it in the app UI. POST /api/agenda/topic
    # (auto-approve + queue) stays operator-token-only and is never
    # called from here.
    try:
        store = TopicInboxStore(deps.editorial_cards_db())
        # ponytail: read-before-write dedupe detection instead of a store
        # API change; single-process local app, the race window is moot.
        prior_ids = {row["id"] for row in store.list_all(status_filter="proposed")}
        row = store.propose(body.title, body.angle, body.tags, source=body.agent)
    except TopicInboxValidationError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except TopicInboxCapError as exc:
        # Cap semantics for agents are "back off, tell your human" —
        # retry-later 429 fits better than 409 (design, endpoint
        # contracts).
        return JSONResponse(status_code=429, content={"detail": str(exc)})
    except (sqlite3.Error, OSError):
        return JSONResponse(
            status_code=503, content={"detail": "topic_inbox_unavailable"}
        )
    return AgentTopicResponse(
        id=row["id"], status=row["status"], deduped=row["id"] in prior_ids
    )


@router.get("/api/agent/status", response_model=AgentStatusResponse)
def get_agent_status():
    # Read-only with any valid token; counts only (no topic/card text).
    # Both reads are bounded + fail-open (0 / {}) like the memoria stats
    # helpers they reuse.
    return AgentStatusResponse(
        topics_pending=_count_sql(
            deps.editorial_cards_db(),
            "SELECT COUNT(*) FROM topic_inbox WHERE status='proposed'",
        ),
        cards=_editorial_cards_by_status(deps.editorial_cards_db()),
        notices_undismissed=_count_sql(
            deps.editorial_cards_db(),
            "SELECT COUNT(*) FROM agent_notices WHERE status='proposed'",
        ),
    )


@router.post("/api/agent/cards", response_model=AgentCardResponse)
def post_agent_card(body: AgentCardRequest):
    # Card content validation is owned by the EditorialCard dataclass
    # (same rules as the CLI); its error string becomes the 422 detail.
    try:
        expires = _parse_iso8601_utc(body.expires_at)
    except ValueError:
        return JSONResponse(
            status_code=422,
            content={"detail": "expires_at is not a valid ISO-8601 timestamp"},
        )
    try:
        card = EditorialCard(
            topic=body.topic,
            summary=body.summary,
            streamer_take=body.streamer_take,
            counterpoints=body.counterpoints,
            discussion_hooks=body.discussion_hooks,
            triggers=body.triggers,
            expires_at=expires,
            origin=body.agent,
            single_use=body.single_use,
        )
    except EditorialCardValidationError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    try:
        store = EditorialCardStore(deps.editorial_cards_db())
        stored = store.upsert(card)
        # Demotion rule (design 'POST /api/agent/cards'): upsert
        # preserves existing status, so an agent rewriting an ARMED/
        # ACTIVE card would silently keep it auto-firing with
        # unreviewed content. Force it back to DRAFT — the operator
        # re-arms via CLI/UI. New cards are already DRAFT (no-op).
        demoted = store.demote_to_draft(stored.id)
    except (sqlite3.Error, OSError):
        return JSONResponse(
            status_code=503, content={"detail": "editorial_cards_unavailable"}
        )
    return AgentCardResponse(
        id=stored.id,
        topic_slug=stored.topic_slug,
        status=EditorialCardStatus.DRAFT.value if demoted else stored.status.value,
        demoted=demoted,
    )


@router.get("/api/agent/cards", response_model=AgentCardsListResponse)
def get_agent_cards():
    # Light projection (design D4): no summary/streamer_take/list
    # fields — the operator UI only needs enough to show status and
    # arm a draft, and terminal statuses are counts-only elsewhere.
    try:
        cards = EditorialCardStore(deps.editorial_cards_db()).list_all()
    except (sqlite3.Error, OSError):
        return JSONResponse(
            status_code=503, content={"detail": "editorial_cards_unavailable"}
        )
    return AgentCardsListResponse(
        cards=[
            AgentCardListItem(
                id=card.id,
                topic=card.topic,
                topic_slug=card.topic_slug,
                status=card.status.value,
                origin=card.origin,
                expires_at=card.expires_at.isoformat() if card.expires_at else None,
                updated_at=card.updated_at.isoformat(),
                single_use=card.single_use,
            )
            for card in cards
        ]
    )


@router.post("/api/agent/cards/{card_id}/arm", response_model=AgentCardArmResponse)
def post_agent_card_arm(card_id: str):
    # Mirrors editorial_cli.py's arm semantics (store.arm): refuses
    # expired/USED/ACTIVE cards, idempotent on an already-ARMED card.
    try:
        store = EditorialCardStore(deps.editorial_cards_db())
        card = store.get(card_id)
        if card is None:
            return JSONResponse(status_code=404, content={"detail": "card_not_found"})
        armed = store.arm(card_id)
        if not armed:
            # arm() already flipped an expired card to EXPIRED as a side
            # effect; re-fetch to tell that apart from USED/ACTIVE.
            current = store.get(card_id)
    except (sqlite3.Error, OSError):
        return JSONResponse(
            status_code=503, content={"detail": "editorial_cards_unavailable"}
        )
    if not armed:
        detail = (
            "card_expired"
            if current is not None and current.status is EditorialCardStatus.EXPIRED
            else "card_not_armable"
        )
        return JSONResponse(status_code=409, content={"detail": detail})
    return AgentCardArmResponse(id=card_id, status=EditorialCardStatus.ARMED.value)


@router.post("/api/agent/notice", response_model=AgentNoticeResponse)
def post_agent_notice(body: AgentNoticeRequest):
    try:
        store = AgentNoticeStore(deps.editorial_cards_db())
        # ponytail: read-before-write dedupe detection, same pattern as
        # POST /api/agent/topics — single-process local app.
        prior_ids = {row["id"] for row in store.list_all(status_filter="proposed")}
        row = store.propose(body.text, source=body.agent)
    except AgentNoticeValidationError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    except AgentNoticeCapError as exc:
        return JSONResponse(status_code=429, content={"detail": str(exc)})
    except (sqlite3.Error, OSError):
        return JSONResponse(
            status_code=503, content={"detail": "agent_notices_unavailable"}
        )
    return AgentNoticeResponse(id=row["id"], deduped=row["id"] in prior_ids)


@router.get("/api/agent/notices", response_model=AgentNoticesResponse)
def get_agent_notices(request: Request):
    # Operator surface: the agent token can CREATE notices but never
    # read the board back (other agents' notices are not its business).
    if _agent_request_role(request) != "operator":
        return JSONResponse(
            status_code=403, content={"detail": "operator token required"}
        )
    pending = AgentNoticeStore(deps.editorial_cards_db()).list_pending()
    return AgentNoticesResponse(
        notices=[
            AgentNoticeOut(
                id=row["id"],
                text=row["text"],
                source=row["source"],
                created_at=row["created_at"],
            )
            for row in pending["valid"]
        ]
    )


@router.post(
    "/api/agent/notices/{notice_id}/dismiss",
    response_model=AgentNoticeDismissResponse,
)
def dismiss_agent_notice(notice_id: str, request: Request):
    # Operator surface: a self-dismissing agent could hide its own
    # messages from the human — only the operator token may dismiss.
    if _agent_request_role(request) != "operator":
        return JSONResponse(
            status_code=403, content={"detail": "operator token required"}
        )
    try:
        dismissed = AgentNoticeStore(deps.editorial_cards_db()).dismiss(notice_id)
    except (sqlite3.Error, OSError):
        return JSONResponse(
            status_code=503, content={"detail": "agent_notices_unavailable"}
        )
    if not dismissed:
        return JSONResponse(
            status_code=404, content={"detail": "notice_not_found"}
        )
    return AgentNoticeDismissResponse(dismissed=True)
