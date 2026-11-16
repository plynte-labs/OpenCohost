"""T4: /api/usage/* (usage-activity-ledger-20261109)."""
import csv
import io
import json
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

import opencohost.api.auth as auth
from opencohost.config import settings
from opencohost.core.usage import ledger
from tests.test_api_phase1 import FakeHost

SECRET = "SECRET-CHAT-TEXT-QUOKKA"


@pytest.fixture(autouse=True)
def _reset():
    ledger._reset_for_tests()
    yield
    ledger._reset_for_tests()


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=["http://localhost:5173"])


@pytest.fixture
def client():
    with TestClient(_app()) as c:
        yield c


def _seed():
    ledger.record_call(model="gemma", is_local=True, kind="turn", source="direct",
                       profile_id="kira", prompt_tokens=100, completion_tokens=10, compute_ms=900)
    ledger.record_call(model="cloudy", is_local=False, kind="vision", profile_id="kira",
                       prompt_tokens=5, completion_tokens=1, compute_ms=200)
    ledger.record_decision("vision", "on", "laya", "ok")


def test_router_is_registered():
    from opencohost.api.routers import ALL_ROUTERS, usage

    assert usage.router in ALL_ROUTERS


def test_summary_empty_defaults(client):
    r = client.get("/api/usage/summary")
    assert r.status_code == 200
    body = r.json()
    today = date.today()
    assert body["to"] == today.isoformat()
    assert body["from"] == (today - timedelta(days=365)).isoformat()
    assert body["enabled"] is True and body["first_recorded"] is None
    assert body["days"] == [] and body["decisions"] == []
    assert body["cache"] == {"estimated_reused_tokens": 0, "real_available": False,
                             "real_prompt_tokens": None, "real_reused_tokens": None}


def test_summary_with_data_matches_contract(client):
    _seed()
    body = client.get("/api/usage/summary").json()
    assert set(body) == {"from", "to", "enabled", "first_recorded", "days", "by_model", "by_kind",
                         "by_source", "by_profile", "cache", "decisions"}
    assert body["days"][0] == {"date": date.today().isoformat(), "calls": 2, "prompt_tokens": 105,
                               "completion_tokens": 11, "compute_ms": 1100}
    assert {(m["model"], m["provider"]) for m in body["by_model"]} == {("gemma", "local"), ("cloudy", "cloud")}
    assert body["by_profile"][0]["profile_id"] == "kira"
    assert body["decisions"][0]["decision_id"] == "vision"


def test_summary_range_params(client):
    _seed()
    today = date.today().isoformat()
    body = client.get(f"/api/usage/summary?from={today}&to={today}").json()
    assert body["from"] == today and len(body["days"]) == 1
    old = (date.today() - timedelta(days=400)).isoformat()
    older = (date.today() - timedelta(days=399)).isoformat()
    assert client.get(f"/api/usage/summary?from={old}&to={older}").json()["days"] == []


@pytest.mark.parametrize("query", [
    "from=2026-02-01&to=2026-01-01",   # from after to
    "from=2020-01-01&to=2026-01-01",   # over 1100 days
    "from=nope",                       # unparsable
    "to=2026-13-40",
])
def test_summary_invalid_range_is_422(client, query):
    r = client.get(f"/api/usage/summary?{query}")
    assert r.status_code == 422 and r.json() == {"detail": "invalid_range"}


def test_export_json_is_attachment(client):
    _seed()
    r = client.get("/api/usage/export?format=json")
    assert r.status_code == 200
    assert "attachment" in r.headers["content-disposition"]
    assert r.headers["content-disposition"].endswith('.json"')
    rows = r.json()["rows"]
    assert {x["table"] for x in rows} == {"usage_daily", "decision_daily"}
    assert SECRET not in r.text


def test_export_csv_is_attachment_with_header(client):
    _seed()
    r = client.get("/api/usage/export?format=csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"] and ".csv" in r.headers["content-disposition"]
    parsed = list(csv.DictReader(io.StringIO(r.text)))
    assert {x["table"] for x in parsed} == {"usage_daily", "decision_daily"}
    assert any(x["model"] == "gemma" and x["calls"] == "1" for x in parsed)


def test_export_default_is_json_and_empty_ok(client):
    r = client.get("/api/usage/export")
    assert r.status_code == 200 and r.json()["rows"] == []


@pytest.mark.parametrize("fmt", ["xml", "", "JSON "])
def test_export_bad_format_is_422(client, fmt):
    r = client.get("/api/usage/export", params={"format": fmt})
    assert r.status_code == 422 and r.json() == {"detail": "invalid_format"}


@pytest.mark.parametrize("body", [{}, {"confirm": "yes"}, {"confirm": "delete_usage"}, {"confirm": 5}])
def test_purge_needs_exact_confirm(client, body):
    _seed()
    r = client.post("/api/usage/purge", json=body)
    assert r.status_code == 422 and r.json() == {"detail": "confirm_required"}
    assert client.get("/api/usage/summary").json()["days"] != []


def test_purge_without_body_is_confirm_required(client):
    _seed()
    r = client.post("/api/usage/purge")
    assert r.status_code == 422 and r.json() == {"detail": "confirm_required"}


def test_purge_with_confirm_deletes_everything(client):
    _seed()
    r = client.post("/api/usage/purge", json={"confirm": "DELETE_USAGE"})
    assert r.status_code == 200 and r.json() == {"deleted": True}
    body = client.get("/api/usage/summary").json()
    assert body["days"] == [] and body["decisions"] == [] and body["first_recorded"] is None


def test_sensitive_prefix_registered():
    assert "/api/usage/" in auth._SENSITIVE_PREFIXES
    assert auth._is_sensitive_path("/api/usage/summary")


class TestAuthEnforced:
    @pytest.fixture
    def enforced(self, monkeypatch):
        monkeypatch.delenv("OPENCOHOST_API_AUTH", raising=False)
        monkeypatch.setattr(settings, "API_AUTH_ENFORCED", settings._resolve_api_auth_enforced())
        auth.ensure_tokens()
        return auth.load_tokens()

    def test_get_requires_operator_token(self, enforced):
        _seed()
        with TestClient(_app()) as c:
            for path in ("/api/usage/summary", "/api/usage/export?format=csv"):
                assert c.get(path).status_code == 401
                ok = c.get(path, headers={"Authorization": f"Bearer {enforced['operator']}"})
                assert ok.status_code == 200
                bad = c.get(path, headers={"Authorization": f"Bearer {enforced['agent']}"})
                assert bad.status_code == 403

    def test_purge_requires_operator_token(self, enforced):
        _seed()
        with TestClient(_app()) as c:
            assert c.post("/api/usage/purge", json={"confirm": "DELETE_USAGE"}).status_code == 401
            ok = c.post("/api/usage/purge", json={"confirm": "DELETE_USAGE"},
                        headers={"Authorization": f"Bearer {enforced['operator']}"})
            assert ok.status_code == 200
