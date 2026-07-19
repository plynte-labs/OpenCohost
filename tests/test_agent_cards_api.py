"""Strict-TDD tests for the two new operator-UI card routes (design
'knowledge_cards_upload_20260718', D2 + D4): ``GET /api/agent/cards`` and
``POST /api/agent/cards/{card_id}/arm``.

Pre-check finding (tasks.md WU1): ``tests/test_api_agent_gateway.py::
TestAgentCardUpsert`` already fully covers ``POST /api/agent/cards`` — this
file covers only the two actually-new routes.

Fixture/helper pattern copied verbatim from ``tests/test_api_agent_gateway.py``
so this file stays runnable standalone (pytest fixtures do not cross modules
without a shared conftest.py).
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import opencohost.api.auth as auth
import opencohost.api.main as main_mod
from opencohost.core.editorial_cards import (
    EditorialCard,
    EditorialCardStatus,
    EditorialCardStore,
)
from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    main_mod._host_active = False
    yield
    main_mod._host_active = False


@pytest.fixture(autouse=True)
def _isolate_cards_db(tmp_path, monkeypatch):
    """Redirect the handlers' cards DB to a per-test temp file (same pattern
    as ``tests/test_api_agent_gateway.py``'s ``_isolate_cards_db``)."""
    monkeypatch.setattr(main_mod, "EDITORIAL_CARDS_DB", str(tmp_path / "cards.db"))


@pytest.fixture(autouse=True)
def _fresh_rate_limiter(monkeypatch):
    """Per-test limiter so one test's window never bleeds into the next."""
    monkeypatch.setattr(auth, "_rate_limiter", auth.RateLimiter())


@pytest.fixture
def agent_headers():
    auth.ensure_tokens()
    return {"Authorization": f"Bearer {auth.load_tokens()['agent']}"}


@pytest.fixture
def operator_headers():
    auth.ensure_tokens()
    return {"Authorization": f"Bearer {auth.load_tokens()['operator']}"}


def _app(host_factory=FakeHost):
    return main_mod.create_app(
        host_factory=host_factory, cors_origins=_DEFAULT_TEST_ORIGINS
    )


def _cards_store() -> EditorialCardStore:
    return EditorialCardStore(main_mod.EDITORIAL_CARDS_DB)


def _new_card(topic: str, **overrides) -> EditorialCard:
    defaults = dict(
        topic=topic,
        summary="A structured summary of the topic.",
        streamer_take="An honest streamer take on it.",
    )
    defaults.update(overrides)
    return EditorialCard(**defaults)


# ──────────────────────────────────────────────────────────────────────────
# GET /api/agent/cards — light projection, newest first
# ──────────────────────────────────────────────────────────────────────────


class TestAgentCardsList:
    def test_list_empty_returns_empty_cards_array(self, agent_headers):
        app = _app()
        with TestClient(app) as client:
            resp = client.get("/api/agent/cards", headers=agent_headers)
        assert resp.status_code == 200
        assert resp.json() == {"cards": []}

    def test_list_returns_projection_shape(self, agent_headers):
        store = _cards_store()
        store.upsert(_new_card("Projection shape topic"))
        app = _app()
        with TestClient(app) as client:
            resp = client.get("/api/agent/cards", headers=agent_headers)
        assert resp.status_code == 200
        cards = resp.json()["cards"]
        assert len(cards) == 1
        assert set(cards[0].keys()) == {
            "id",
            "topic",
            "topic_slug",
            "status",
            "origin",
            "expires_at",
            "updated_at",
            "single_use",
        }
        assert cards[0]["topic"] == "Projection shape topic"
        assert cards[0]["status"] == "draft"

    def test_list_mixed_statuses_all_returned(self, agent_headers):
        store = _cards_store()
        draft = store.upsert(_new_card("Draft topic"))
        armed = store.upsert(_new_card("Armed topic"))
        store.arm(armed.id)
        used = store.upsert(_new_card("Used topic"))
        store.mark_used(used.id)
        app = _app()
        with TestClient(app) as client:
            resp = client.get("/api/agent/cards", headers=agent_headers)
        statuses = {c["id"]: c["status"] for c in resp.json()["cards"]}
        assert statuses == {
            draft.id: "draft",
            armed.id: "armed",
            used.id: "used",
        }

    def test_list_is_newest_first(self, agent_headers):
        store = _cards_store()
        older = store.upsert(_new_card("Older topic"))
        store.upsert(_new_card("Newer topic"))
        # Bump older's updated_at past newer's by re-upserting it.
        bumped = store.upsert(_new_card("Older topic", summary="Updated summary."))
        assert bumped.id == older.id
        app = _app()
        with TestClient(app) as client:
            resp = client.get("/api/agent/cards", headers=agent_headers)
        cards = resp.json()["cards"]
        assert cards[0]["id"] == older.id

    def test_list_requires_bearer_token(self):
        app = _app()
        with TestClient(app) as client:
            resp = client.get("/api/agent/cards")
        assert resp.status_code == 401

    def test_list_accepts_operator_token_too(self, operator_headers):
        app = _app()
        with TestClient(app) as client:
            resp = client.get("/api/agent/cards", headers=operator_headers)
        assert resp.status_code == 200

    def test_list_is_never_rate_limited(self, agent_headers, monkeypatch):
        monkeypatch.setattr(auth, "_rate_limiter", auth.RateLimiter(limit=0))
        app = _app()
        with TestClient(app) as client:
            resp = client.get("/api/agent/cards", headers=agent_headers)
        assert resp.status_code == 200

    def test_list_503_on_corrupt_db(self, agent_headers, tmp_path, monkeypatch):
        bad_db = tmp_path / "corrupt.db"
        bad_db.write_bytes(b"this is not sqlite\x00\x01\x02")
        monkeypatch.setattr(main_mod, "EDITORIAL_CARDS_DB", str(bad_db))
        app = _app()
        with TestClient(app) as client:
            resp = client.get("/api/agent/cards", headers=agent_headers)
        assert resp.status_code == 503
        assert resp.json()["detail"] == "editorial_cards_unavailable"


# ──────────────────────────────────────────────────────────────────────────
# single_use (D6): POST accepts the flag; GET projection exposes it
# ──────────────────────────────────────────────────────────────────────────


class TestAgentCardSingleUse:
    _POST_PAYLOAD = {
        "agent": "claude-code",
        "topic": "Single use flag topic",
        "summary": "A structured summary of the topic.",
        "streamer_take": "An honest streamer take on it.",
    }

    def test_post_defaults_single_use_false_when_omitted(self, agent_headers):
        app = _app()
        with TestClient(app) as client:
            resp = client.post(
                "/api/agent/cards", json=self._POST_PAYLOAD, headers=agent_headers
            )
        assert resp.status_code == 200
        card = _cards_store().get(resp.json()["id"])
        assert card is not None
        assert card.single_use is False

    def test_post_single_use_true_is_stored(self, agent_headers):
        payload = dict(self._POST_PAYLOAD, topic="One shot flag topic", single_use=True)
        app = _app()
        with TestClient(app) as client:
            resp = client.post("/api/agent/cards", json=payload, headers=agent_headers)
        assert resp.status_code == 200
        card = _cards_store().get(resp.json()["id"])
        assert card is not None
        assert card.single_use is True

    def test_get_list_items_include_single_use_matching_stored_value(self, agent_headers):
        store = _cards_store()
        reusable = store.upsert(_new_card("Reusable projection topic", single_use=False))
        one_shot = store.upsert(_new_card("One shot projection topic", single_use=True))
        app = _app()
        with TestClient(app) as client:
            resp = client.get("/api/agent/cards", headers=agent_headers)
        assert resp.status_code == 200
        by_id = {c["id"]: c for c in resp.json()["cards"]}
        assert by_id[reusable.id]["single_use"] is False
        assert by_id[one_shot.id]["single_use"] is True


# ──────────────────────────────────────────────────────────────────────────
# POST /api/agent/cards/{card_id}/arm — mirrors CLI semantics (store.arm)
# ──────────────────────────────────────────────────────────────────────────


class TestAgentCardArm:
    def test_arm_draft_card_becomes_armed(self, agent_headers):
        store = _cards_store()
        card = store.upsert(_new_card("Arm me"))
        app = _app()
        with TestClient(app) as client:
            resp = client.post(f"/api/agent/cards/{card.id}/arm", headers=agent_headers)
        assert resp.status_code == 200
        assert resp.json() == {"id": card.id, "status": "armed"}
        assert store.get(card.id).status is EditorialCardStatus.ARMED

    def test_arm_is_idempotent_on_already_armed_card(self, agent_headers):
        store = _cards_store()
        card = store.upsert(_new_card("Arm twice"))
        app = _app()
        with TestClient(app) as client:
            first = client.post(f"/api/agent/cards/{card.id}/arm", headers=agent_headers)
            second = client.post(f"/api/agent/cards/{card.id}/arm", headers=agent_headers)
        assert first.status_code == 200
        assert second.status_code == 200
        assert store.get(card.id).status is EditorialCardStatus.ARMED

    def test_arm_unknown_card_id_is_404(self, agent_headers):
        app = _app()
        with TestClient(app) as client:
            resp = client.post("/api/agent/cards/ec_unknown/arm", headers=agent_headers)
        assert resp.status_code == 404
        assert resp.json() == {"detail": "card_not_found"}

    def test_arm_expired_card_is_409_and_flips_to_expired(self, agent_headers):
        from datetime import datetime, timedelta, timezone

        store = _cards_store()
        card = store.upsert(
            _new_card(
                "Expired topic",
                expires_at=datetime.now(timezone.utc) - timedelta(days=1),
            )
        )
        app = _app()
        with TestClient(app) as client:
            resp = client.post(f"/api/agent/cards/{card.id}/arm", headers=agent_headers)
        assert resp.status_code == 409
        assert resp.json() == {"detail": "card_expired"}
        assert store.get(card.id).status is EditorialCardStatus.EXPIRED

    def test_arm_used_card_is_409(self, agent_headers):
        store = _cards_store()
        card = store.upsert(_new_card("Used topic"))
        store.mark_used(card.id)
        app = _app()
        with TestClient(app) as client:
            resp = client.post(f"/api/agent/cards/{card.id}/arm", headers=agent_headers)
        assert resp.status_code == 409
        assert resp.json() == {"detail": "card_not_armable"}

    def test_arm_active_card_is_409(self, agent_headers):
        store = _cards_store()
        card = store.upsert(_new_card("Active topic"))
        store.arm(card.id)
        store.activate_for_topic(card.topic_slug)
        app = _app()
        with TestClient(app) as client:
            resp = client.post(f"/api/agent/cards/{card.id}/arm", headers=agent_headers)
        assert resp.status_code == 409
        assert resp.json() == {"detail": "card_not_armable"}

    def test_arm_requires_bearer_token(self):
        store = _cards_store()
        card = store.upsert(_new_card("No token"))
        app = _app()
        with TestClient(app) as client:
            resp = client.post(f"/api/agent/cards/{card.id}/arm")
        assert resp.status_code == 401

    def test_arm_shares_mutation_rate_limit_with_other_agent_routes(self, agent_headers):
        store = _cards_store()
        card = store.upsert(_new_card("Rate limit probe"))
        app = _app()
        payload = {"agent": "claude-code", "title": "Rate limit probe topic"}
        with TestClient(app) as client:
            for _ in range(60):
                ok = client.post("/api/agent/topics", json=payload, headers=agent_headers)
                assert ok.status_code == 200
            blocked = client.post(
                f"/api/agent/cards/{card.id}/arm", headers=agent_headers
            )
        assert blocked.status_code == 429

    def test_arm_503_on_corrupt_db(self, agent_headers, tmp_path, monkeypatch):
        bad_db = tmp_path / "corrupt.db"
        bad_db.write_bytes(b"this is not sqlite\x00\x01\x02")
        monkeypatch.setattr(main_mod, "EDITORIAL_CARDS_DB", str(bad_db))
        app = _app()
        with TestClient(app) as client:
            resp = client.post("/api/agent/cards/ec_whatever/arm", headers=agent_headers)
        assert resp.status_code == 503
        assert resp.json()["detail"] == "editorial_cards_unavailable"

    def test_arm_503_when_post_arm_refetch_raises(self, agent_headers, monkeypatch):
        """A not-armable card (arm() itself returns False cleanly) whose
        post-arm re-fetch (main.py's `current = store.get(card_id)`) hits a
        sqlite error must still map to 503, not an unhandled 500."""
        store = _cards_store()
        card = store.upsert(_new_card("Used topic for refetch failure"))
        store.mark_used(card.id)
        app = _app()

        original_get = EditorialCardStore.get
        calls = {"n": 0}

        def flaky_get(self, card_id):
            calls["n"] += 1
            if calls["n"] >= 3:
                raise sqlite3.OperationalError("boom")
            return original_get(self, card_id)

        monkeypatch.setattr(EditorialCardStore, "get", flaky_get)

        with TestClient(app) as client:
            resp = client.post(f"/api/agent/cards/{card.id}/arm", headers=agent_headers)
        assert resp.status_code == 503
        assert resp.json() == {"detail": "editorial_cards_unavailable"}
