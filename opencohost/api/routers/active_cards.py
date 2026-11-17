"""/api/editorial/cards/* -- active cards (operator surface).

Deliberately NOT under ``/api/agent/``: that prefix accepts the agent token,
and activate / deactivate / disable / delete / field edits are operator decisions
(``tests/test_api_agent_gateway.py`` also pins the agent surface to an exact
route set). Outside that prefix the generic mutating-route middleware already
requires the operator token once ``API_AUTH_ENFORCED`` is on; every mutating
handler here additionally refuses an agent token unconditionally, so an
agent can never activate, whatever the enforcement flag says.

Error shapes follow ``routers/agent.py``: ``{"detail": "<code>"}`` with 404
``card_not_found``, 409 for a state refusal, 503 ``editorial_cards_unavailable``
on store errors, and the dataclass's message as the 422 detail.

Every state change re-syncs the forbidden-claim output guard from the store,
so an activation or deactivation is enforced from the next reply without waiting for a turn.
Route order: the literal ``/active`` is registered before the ``{card_id}``
template that shares its segment count.
"""

import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from opencohost.api import deps
from opencohost.api.models import (
    ActiveCardOut,
    ActiveCardRequest,
    ActiveCardDeleteResponse,
    ActiveCardDisableResponse,
    ActiveCardFieldsUpdate,
    ActiveCardListResponse,
    ActiveCardStateResponse,
)
from opencohost.api.routers.agent import _agent_request_role
from opencohost.core.editorial.editorial_cards import (
    EditorialCard,
    EditorialCardStatus,
    EditorialCardStore,
    EditorialCardValidationError,
)
from opencohost.core.editorial.active_cards import sync_guard_from_store
from opencohost.core.editorial.card_embeddings import refresh_embedding

router = APIRouter()

_CARD_FIELDS = ("must_say", "forbidden_claims", "discount_code", "link")


def _agent_token_refused(request: Request) -> JSONResponse | None:
    """403 when the caller presented the agent token; None otherwise."""
    if _agent_request_role(request) == "agent":
        return JSONResponse(status_code=403, content={"detail": "operator token required"})
    return None


def _unavailable() -> JSONResponse:
    return JSONResponse(status_code=503, content={"detail": "editorial_cards_unavailable"})


def _not_found() -> JSONResponse:
    return JSONResponse(status_code=404, content={"detail": "card_not_found"})


def _out(card: EditorialCard) -> ActiveCardOut:
    return ActiveCardOut(
        id=card.id,
        topic=card.topic,
        topic_slug=card.topic_slug,
        status=card.status.value,
        origin=card.origin,
        summary=card.summary,
        streamer_take=card.streamer_take,
        active=card.active,
        single_use=card.single_use,
        must_say=card.must_say,
        forbidden_claims=card.forbidden_claims,
        discount_code=card.discount_code,
        link=card.link,
        updated_at=card.updated_at.isoformat(),
    )


@router.post("/api/editorial/cards", response_model=ActiveCardOut)
def post_card(body: ActiveCardRequest, request: Request):
    refused = _agent_token_refused(request)
    if refused:
        return refused
    try:
        card = EditorialCard(**body.model_dump())
    except EditorialCardValidationError as exc:
        return JSONResponse(status_code=422, content={"detail": str(exc)})
    try:
        store = EditorialCardStore(deps.editorial_cards_db())
        stored = store.upsert(card)
        sync_guard_from_store(store)  # upsert over an active card changes its phrases
        refresh_embedding(store, stored)  # best effort, never raises
    except (sqlite3.Error, OSError):
        return _unavailable()
    return _out(stored)


@router.get("/api/editorial/cards/active", response_model=ActiveCardListResponse)
def get_active_cards():
    try:
        cards = EditorialCardStore(deps.editorial_cards_db()).list_active()
    except (sqlite3.Error, OSError):
        return _unavailable()
    return ActiveCardListResponse(cards=[_out(card) for card in cards])


@router.get("/api/editorial/cards/{card_id}", response_model=ActiveCardOut)
def get_card(card_id: str):
    try:
        card = EditorialCardStore(deps.editorial_cards_db()).get(card_id)
    except (sqlite3.Error, OSError):
        return _unavailable()
    return _out(card) if card else _not_found()


@router.put("/api/editorial/cards/{card_id}/fields", response_model=ActiveCardOut)
def put_card_fields(card_id: str, body: ActiveCardFieldsUpdate, request: Request):
    refused = _agent_token_refused(request)
    if refused:
        return refused
    try:
        store = EditorialCardStore(deps.editorial_cards_db())
        card = store.get(card_id)
        if card is None:
            return _not_found()
        changes = body.model_dump(exclude_none=True)
        try:
            updated = EditorialCard(
                **{
                    **{name: getattr(card, name) for name in _CARD_FIELDS},
                    **changes,
                    "topic": card.topic,
                    "summary": card.summary,
                    "streamer_take": card.streamer_take,
                    "counterpoints": card.counterpoints,
                    "discussion_hooks": card.discussion_hooks,
                    "triggers": card.triggers,
                    "single_use": card.single_use,
                    "origin": card.origin,
                    "expires_at": card.expires_at,
                }
            )
        except EditorialCardValidationError as exc:
            return JSONResponse(status_code=422, content={"detail": str(exc)})
        # upsert preserves status, usage history and the active flag.
        stored = store.upsert(updated)
        sync_guard_from_store(store)
        refresh_embedding(store, stored)  # must_say feeds the embedding
    except (sqlite3.Error, OSError):
        return _unavailable()
    return _out(stored)


@router.post("/api/editorial/cards/{card_id}/activate", response_model=ActiveCardStateResponse)
def post_activate_card(card_id: str, request: Request):
    refused = _agent_token_refused(request)
    if refused:
        return refused
    try:
        store = EditorialCardStore(deps.editorial_cards_db())
        if store.get(card_id) is None:
            return _not_found()
        activated = store.activate(card_id)
        sync_guard_from_store(store)
    except (sqlite3.Error, OSError):
        return _unavailable()
    if not activated:
        # activate() refuses only an expired/disabled card once it is known to exist.
        return JSONResponse(status_code=409, content={"detail": "card_not_activatable"})
    return ActiveCardStateResponse(id=card_id, active=True)


@router.post("/api/editorial/cards/{card_id}/deactivate", response_model=ActiveCardStateResponse)
def post_deactivate_card(card_id: str, request: Request):
    refused = _agent_token_refused(request)
    if refused:
        return refused
    try:
        store = EditorialCardStore(deps.editorial_cards_db())
        if store.get(card_id) is None:
            return _not_found()
        store.deactivate(card_id)  # idempotent: an inactive card stays inactive
        sync_guard_from_store(store)
    except (sqlite3.Error, OSError):
        return _unavailable()
    return ActiveCardStateResponse(id=card_id, active=False)


@router.post("/api/editorial/cards/{card_id}/disable", response_model=ActiveCardDisableResponse)
def post_disable_card(card_id: str, request: Request):
    refused = _agent_token_refused(request)
    if refused:
        return refused
    try:
        store = EditorialCardStore(deps.editorial_cards_db())
        if store.get(card_id) is None:
            return _not_found()
        disabled = store.disable(card_id)  # also deactivates it
        sync_guard_from_store(store)
    except (sqlite3.Error, OSError):
        return _unavailable()
    if not disabled:
        # Only a USED card refuses: its history must be preserved.
        return JSONResponse(status_code=409, content={"detail": "card_not_disableable"})
    return ActiveCardDisableResponse(id=card_id, status=EditorialCardStatus.EXPIRED.value)


@router.delete("/api/editorial/cards/{card_id}", response_model=ActiveCardDeleteResponse)
def delete_card(card_id: str, request: Request):
    refused = _agent_token_refused(request)
    if refused:
        return refused
    try:
        store = EditorialCardStore(deps.editorial_cards_db())
        deleted = store.delete(card_id)
        sync_guard_from_store(store)
    except (sqlite3.Error, OSError):
        return _unavailable()
    if not deleted:
        return _not_found()
    return ActiveCardDeleteResponse(id=card_id, deleted=True)
