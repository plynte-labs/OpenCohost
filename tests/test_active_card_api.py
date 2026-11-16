"""Active cards: operator routes and agent draft boundary (R1).

Fixtures mirror tests/test_agent_cards_api.py so the file runs standalone.
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

import opencohost.api.auth as auth
import opencohost.api.main as main_mod
from opencohost.config import active_card_guard
from opencohost.config.validation import output_guard
from opencohost.core.editorial.editorial_cards import (
    EditorialCard,
    EditorialCardStatus,
    EditorialCardStore,
)
from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]

BASE = "/api/editorial/cards"


@pytest.fixture(autouse=True)
def _reset_host_active():
    main_mod._host_active = False
    yield
    main_mod._host_active = False


@pytest.fixture(autouse=True)
def _isolate_cards_db(tmp_path, monkeypatch):
    monkeypatch.setattr(main_mod, "EDITORIAL_CARDS_DB", str(tmp_path / "cards.db"))


@pytest.fixture(autouse=True)
def _fresh_rate_limiter(monkeypatch):
    monkeypatch.setattr(auth, "_rate_limiter", auth.RateLimiter())


@pytest.fixture(autouse=True)
def _clean_guard():
    active_card_guard.clear()
    yield
    active_card_guard.clear()


@pytest.fixture
def agent_headers():
    auth.ensure_tokens()
    return {"Authorization": f"Bearer {auth.load_tokens()['agent']}"}


@pytest.fixture
def operator_headers():
    auth.ensure_tokens()
    return {"Authorization": f"Bearer {auth.load_tokens()['operator']}"}


def _app():
    return main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)


def _store() -> EditorialCardStore:
    return EditorialCardStore(main_mod.EDITORIAL_CARDS_DB)


def _card(topic="Acme Mic", **kw) -> EditorialCard:
    return EditorialCard(topic=topic, summary="A USB microphone.", streamer_take="I like it.", **kw)


SPONSOR_BODY = {
    "topic": "Acme Mic",
    "summary": "A USB microphone.",
    "streamer_take": "I like it.",
    "must_say": ["49 USD"],
    "forbidden_claims": ["cura la ansiedad"],
    "discount_code": "KIRA10",
    "link": "https://acme.example/kira",
}


class TestCreateAndUpdate:
    def test_operator_creates_a_sponsor_draft(self, operator_headers):
        with TestClient(_app()) as client:
            resp = client.post(BASE, json=SPONSOR_BODY, headers=operator_headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "draft" and data["active"] is False and data["origin"] == ""
        assert "product" not in data and data["must_say"] == ["49 USD"]
        assert data["forbidden_claims"] == ["cura la ansiedad"]
        assert data["discount_code"] == "KIRA10" and data["link"] == "https://acme.example/kira"
        assert _store().get(data["id"]).must_say == ["49 USD"]

    def test_validation_error_is_422_with_detail(self, operator_headers):
        with TestClient(_app()) as client:
            resp = client.post(BASE, json={**SPONSOR_BODY, "link": "x" * 300}, headers=operator_headers)
        assert resp.status_code == 422
        assert "link exceeds" in resp.json()["detail"]

    def test_agent_token_cannot_create_through_the_operator_route(self, agent_headers):
        with TestClient(_app()) as client:
            resp = client.post(BASE, json=SPONSOR_BODY, headers=agent_headers)
        assert resp.status_code == 403
        assert resp.json() == {"detail": "operator token required"}

    def test_update_sponsor_fields_is_partial_and_keeps_the_rest(self, operator_headers):
        card = _store().upsert(_card(discount_code="OLD", must_say=["a"]))
        with TestClient(_app()) as client:
            resp = client.put(
                f"{BASE}/{card.id}/fields", json={"discount_code": "NEW"}, headers=operator_headers
            )
        assert resp.status_code == 200
        stored = _store().get(card.id)
        assert stored.discount_code == "NEW" and stored.must_say == ["a"]

    def test_update_unknown_card_is_404(self, operator_headers):
        with TestClient(_app()) as client:
            resp = client.put(f"{BASE}/ec_missing/fields", json={"link": "a.b"}, headers=operator_headers)
        assert resp.status_code == 404
        assert resp.json() == {"detail": "card_not_found"}

    def test_update_of_active_card_refreshes_the_guard(self, operator_headers):
        store = _store()
        card = store.upsert(_card(forbidden_claims=["uno"]))
        with TestClient(_app()) as client:
            client.post(f"{BASE}/{card.id}/activate", headers=operator_headers)
            assert output_guard("dos", source="chat")[0] is True
            client.put(f"{BASE}/{card.id}/fields", json={"forbidden_claims": ["dos"]}, headers=operator_headers)
        assert output_guard("dos", source="chat")[0] is False
        assert output_guard("uno", source="chat")[0] is True


class TestActivateDeactivate:
    def test_activate_several_and_list_active(self, operator_headers):
        store = _store()
        a, b = store.upsert(_card("A")), store.upsert(_card("B"))
        with TestClient(_app()) as client:
            assert client.get(f"{BASE}/active", headers=operator_headers).json() == {"cards": []}
            resp = client.post(f"{BASE}/{a.id}/activate", headers=operator_headers)
            assert resp.status_code == 200 and resp.json() == {"id": a.id, "active": True}
            client.post(f"{BASE}/{b.id}/activate", headers=operator_headers)
            listed = client.get(f"{BASE}/active", headers=operator_headers).json()["cards"]
        assert {c["id"] for c in listed} == {a.id, b.id}
        assert all(c["active"] is True for c in listed)
        assert store.get(a.id).active is True  # activating B did not deactivate A

    def test_activate_unknown_404_and_expired_409(self, operator_headers):
        store = _store()
        card = store.upsert(_card())
        store.disable(card.id)
        with TestClient(_app()) as client:
            miss = client.post(f"{BASE}/ec_missing/activate", headers=operator_headers)
            gone = client.post(f"{BASE}/{card.id}/activate", headers=operator_headers)
        assert miss.status_code == 404 and miss.json() == {"detail": "card_not_found"}
        assert gone.status_code == 409 and gone.json() == {"detail": "card_not_activatable"}

    def test_deactivate_is_idempotent_and_404_for_unknown(self, operator_headers):
        card = _store().upsert(_card())
        with TestClient(_app()) as client:
            client.post(f"{BASE}/{card.id}/activate", headers=operator_headers)
            first = client.post(f"{BASE}/{card.id}/deactivate", headers=operator_headers)
            second = client.post(f"{BASE}/{card.id}/deactivate", headers=operator_headers)
            miss = client.post(f"{BASE}/ec_missing/deactivate", headers=operator_headers)
        assert first.json() == {"id": card.id, "active": False}
        assert second.status_code == 200 and second.json() == {"id": card.id, "active": False}
        assert miss.status_code == 404

    def test_activate_and_deactivate_drive_the_forbidden_claim_guard(self, operator_headers):
        card = _store().upsert(_card(forbidden_claims=["cura la ansiedad"]))
        with TestClient(_app()) as client:
            assert output_guard("cura la ansiedad", source="chat")[0] is True
            client.post(f"{BASE}/{card.id}/activate", headers=operator_headers)
            assert output_guard("cura la ansiedad", source="chat")[0] is False
            client.post(f"{BASE}/{card.id}/deactivate", headers=operator_headers)
            assert output_guard("cura la ansiedad", source="chat")[0] is True

    @pytest.mark.parametrize(
        "method, suffix",
        [("post", "/activate"), ("post", "/deactivate"), ("post", "/disable"), ("delete", ""), ("put", "/fields")],
    )
    def test_agent_token_never_activates_deactivates_disables_or_deletes(self, agent_headers, method, suffix):
        store = _store()
        card = store.upsert(_card(forbidden_claims=["x"]))
        store.activate(card.id)
        with TestClient(_app()) as client:
            resp = getattr(client, method)(
                f"{BASE}/{card.id}{suffix}", headers=agent_headers, **({"json": {}} if method == "put" else {})
            )
        assert resp.status_code == 403
        assert resp.json() == {"detail": "operator token required"}
        assert store.get(card.id).active is True


class TestDisableDelete:
    def test_disable_expires_deactivates_and_clears_guard(self, operator_headers):
        card = _store().upsert(_card(forbidden_claims=["cura la ansiedad"]))
        with TestClient(_app()) as client:
            client.post(f"{BASE}/{card.id}/activate", headers=operator_headers)
            resp = client.post(f"{BASE}/{card.id}/disable", headers=operator_headers)
        assert resp.status_code == 200 and resp.json() == {"id": card.id, "status": "expired"}
        assert _store().get(card.id).active is False
        assert output_guard("cura la ansiedad", source="chat")[0] is True

    def test_disable_used_card_is_409_and_unknown_404(self, operator_headers):
        store = _store()
        card = store.upsert(_card())
        store.mark_used(card.id)
        with TestClient(_app()) as client:
            used = client.post(f"{BASE}/{card.id}/disable", headers=operator_headers)
            miss = client.post(f"{BASE}/ec_missing/disable", headers=operator_headers)
        assert used.status_code == 409 and used.json() == {"detail": "card_not_disableable"}
        assert miss.status_code == 404

    def test_delete_removes_card_and_active_flag(self, operator_headers):
        card = _store().upsert(_card(forbidden_claims=["cura la ansiedad"]))
        with TestClient(_app()) as client:
            client.post(f"{BASE}/{card.id}/activate", headers=operator_headers)
            resp = client.delete(f"{BASE}/{card.id}", headers=operator_headers)
            again = client.delete(f"{BASE}/{card.id}", headers=operator_headers)
        assert resp.status_code == 200 and resp.json() == {"id": card.id, "deleted": True}
        assert again.status_code == 404
        assert _store().get(card.id) is None
        assert output_guard("cura la ansiedad", source="chat")[0] is True

    def test_store_failure_is_503(self, operator_headers, monkeypatch):
        card = _store().upsert(_card())

        def boom(self, card_id):
            raise sqlite3.OperationalError("boom")

        monkeypatch.setattr(EditorialCardStore, "get", boom)
        with TestClient(_app()) as client:
            resp = client.post(f"{BASE}/{card.id}/activate", headers=operator_headers)
        assert resp.status_code == 503
        assert resp.json() == {"detail": "editorial_cards_unavailable"}


class TestAgentDraftBoundary:
    def test_agent_may_draft_card_fields_but_never_active(self, agent_headers):
        with TestClient(_app()) as client:
            resp = client.post(
                "/api/agent/cards",
                json={"agent": "claude-code", **SPONSOR_BODY},
                headers=agent_headers,
            )
        assert resp.status_code == 200
        stored = _store().get(resp.json()["id"])
        assert stored.status is EditorialCardStatus.DRAFT
        assert stored.active is False and stored.must_say == ["49 USD"] and stored.origin == "claude-code"

    def test_agent_rewrite_of_an_active_card_deactivates_it(self, agent_headers):
        store = _store()
        card = store.upsert(_card(forbidden_claims=["cura la ansiedad"]))
        store.activate(card.id)
        active_card_guard.set_cards([(card.id, ["cura la ansiedad"])])
        with TestClient(_app()) as client:
            resp = client.post(
                "/api/agent/cards",
                json={"agent": "claude-code", **SPONSOR_BODY, "discount_code": "HACKED"},
                headers=agent_headers,
            )
        assert resp.status_code == 200
        assert resp.json()["deactivated"] is True
        assert store.get(card.id).active is False
        assert output_guard("cura la ansiedad", source="chat")[0] is True

    def test_agent_list_projection_exposes_active(self, agent_headers):
        store = _store()
        card = store.upsert(_card())
        store.activate(card.id)
        with TestClient(_app()) as client:
            resp = client.get("/api/agent/cards", headers=agent_headers)
        assert resp.json()["cards"][0]["active"] is True
