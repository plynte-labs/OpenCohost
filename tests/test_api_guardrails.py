"""GET/PUT /api/guardrails (guardrail-toggles T2).

``guardrails.json`` is redirected per test by the autouse
``_isolate_guardrail_policy`` fixture in tests/conftest.py.
"""

import pytest
from fastapi.testclient import TestClient

from opencohost.api import auth
from opencohost.config import guardrail_policy as gp
from opencohost.config import settings
from tests.test_api_phase1 import FakeHost

_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_ORIGINS)


def test_get_lists_all_rules_default_enabled():
    with TestClient(_app()) as client:
        resp = client.get("/api/guardrails")
    assert resp.status_code == 200
    rules = resp.json()["rules"]
    assert len(rules) == 15  # 11 non-negotiables + 4 always-enforced locked rows (S4)
    assert all(r["enabled"] for r in rules)
    assert {"id", "tier", "category", "enabled"} == set(rules[0])
    by_id = {r["id"]: r for r in rules}
    assert by_id["no_code_output"] == {
        "id": "no_code_output", "tier": "locked", "category": "safety", "enabled": True
    }


def test_put_roundtrip_and_live_apply():
    from opencohost.config.validation import output_guard

    with TestClient(_app()) as client:
        resp = client.put("/api/guardrails", json={"disabled": ["no_ai_self_identification"]})
        assert resp.status_code == 200
        by_id = {r["id"]: r for r in resp.json()["rules"]}
        assert by_id["no_ai_self_identification"]["enabled"] is False
        assert by_id["no_meta_commentary"]["enabled"] is True
        assert client.get("/api/guardrails").json() == resp.json()
        assert output_guard("As an AI language model, I cannot.", "chat")[0] is True
        client.put("/api/guardrails", json={"disabled": []})
        assert output_guard("As an AI language model, I cannot.", "chat")[0] is False


def test_put_persists_and_startup_loads_it():
    with TestClient(_app()) as client:
        client.put("/api/guardrails", json={"disabled": ["no_meta_commentary"]})
    gp.reset_guardrails()
    with TestClient(_app()) as client:  # lifespan calls load_guardrails()
        by_id = {r["id"]: r for r in client.get("/api/guardrails").json()["rules"]}
    assert by_id["no_meta_commentary"]["enabled"] is False


def test_put_422_locked_and_unknown():
    with TestClient(_app()) as client:
        resp = client.put("/api/guardrails", json={"disabled": ["no_hate_speech", "bogus"]})
    assert resp.status_code == 422
    body = resp.json()
    assert body["detail"] == "invalid_guardrails"
    assert {"id": "no_hate_speech", "code": "locked"} in body["errors"]
    assert {"id": "bogus", "code": "unknown"} in body["errors"]
    assert gp.get_disabled() == frozenset()


def test_put_422_when_disabled_key_missing_or_wrong_type():
    with TestClient(_app()) as client:
        assert client.put("/api/guardrails", json={"nope": 1}).status_code == 422
        assert client.put("/api/guardrails", json={"disabled": "x"}).status_code == 422


def test_put_503_on_write_failure(monkeypatch):
    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(gp.os, "replace", boom)
    with TestClient(_app()) as client:
        resp = client.put("/api/guardrails", json={"disabled": ["no_meta_commentary"]})
    assert resp.status_code == 503
    assert resp.json() == {"detail": "guardrails_write_failed"}
    assert gp.get_disabled() == frozenset()


class TestOperatorProtection:
    @pytest.fixture(autouse=True)
    def _flag_on(self, monkeypatch):
        monkeypatch.setattr(settings, "API_AUTH_ENFORCED", True)

    def test_missing_token_is_401(self):
        with TestClient(_app()) as client:
            assert client.put("/api/guardrails", json={"disabled": []}).status_code == 401

    def test_agent_token_is_403(self):
        auth.ensure_tokens()
        tokens = auth.load_tokens()
        with TestClient(_app()) as client:
            resp = client.put(
                "/api/guardrails",
                json={"disabled": []},
                headers={"Authorization": f"Bearer {tokens['agent']}"},
            )
        assert resp.status_code == 403

    def test_operator_token_is_200_and_get_stays_open(self):
        auth.ensure_tokens()
        tokens = auth.load_tokens()
        with TestClient(_app()) as client:
            resp = client.put(
                "/api/guardrails",
                json={"disabled": []},
                headers={"Authorization": f"Bearer {tokens['operator']}"},
            )
            assert resp.status_code == 200
            assert client.get("/api/guardrails").status_code == 200


def test_put_422_when_disabling_no_code_output():
    with TestClient(_app()) as client:
        resp = client.put("/api/guardrails", json={"disabled": ["no_code_output"]})
    assert resp.status_code == 422
    assert {"id": "no_code_output", "code": "locked"} in resp.json()["errors"]
    assert gp.get_disabled() == frozenset()
