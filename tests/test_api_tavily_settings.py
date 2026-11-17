"""Tavily settings are operator-only, redacted and never a provider probe."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opencohost.api import auth
from opencohost.api.routers.web_fetch import router
from opencohost.config import settings
from tests.test_search_keys import CANARY, secure_search


@pytest.fixture
def client(secure_search, monkeypatch):
    monkeypatch.setattr(settings, "API_AUTH_ENFORCED", False)
    monkeypatch.setattr(auth, "load_tokens", lambda: {"operator": "op", "agent": "agent"})
    app = FastAPI()
    app.middleware("http")(auth.auth_middleware)
    app.include_router(router)
    return TestClient(app)


@pytest.mark.parametrize("method", ["get", "put", "delete", "post", "patch", "options", "head"])
@pytest.mark.parametrize("token,code", [(None, 401), ("bad", 401), ("agent", 403)])
def test_tavily_routes_always_require_operator_even_warn_only(client, method, token, code):
    headers = {"Authorization": "Bearer " + token} if token else {}
    assert client.request(method.upper(), "/api/web-fetch/tavily", headers=headers).status_code == code


@pytest.mark.parametrize("body", [{}, {"api_key": None}, {"api_key": 42}, {"api_key": CANARY, "extra": CANARY}, {"api_key": CANARY * 200}])
def test_invalid_body_never_echoes_secret(client, body, caplog):
    response = client.put("/api/web-fetch/tavily", headers={"Authorization": "Bearer op"}, json=body)
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_tavily_setting"}
    assert CANARY not in response.text + caplog.text


def test_save_and_get_never_dispatch_search(client, monkeypatch, caplog):
    import socket
    monkeypatch.setattr(socket, "create_connection", lambda *a, **kw: pytest.fail("network forbidden"))
    headers = {"Authorization": "Bearer op"}
    assert client.get("/api/web-fetch/tavily", headers=headers).json()["source"] == "none"
    saved = client.put("/api/web-fetch/tavily", headers=headers, json={"api_key": CANARY})
    assert saved.status_code == 200
    assert saved.json() == {"api_key_set": True, "stored_key_set": True, "source": "keyring", "storage_available": True}
    assert client.get("/api/web-fetch/tavily", headers=headers).json() == saved.json()
    assert CANARY not in saved.text + caplog.text
    removed = client.put("/api/web-fetch/tavily", headers=headers, json={"api_key": ""})
    assert removed.json()["stored_key_set"] is False
    schema = client.app.openapi()
    assert CANARY not in str(schema)
    assert schema["components"]["schemas"]["TavilyKeyRequest"]["properties"]["api_key"]["writeOnly"] is True


def test_unavailable_keyring_returns_safe_503(client, secure_search, caplog):
    _, backend = secure_search
    backend.failure = True
    response = client.put("/api/web-fetch/tavily", headers={"Authorization": "Bearer op"}, json={"api_key": CANARY})
    assert response.status_code == 503
    assert response.json() == {"detail": "tavily_key_store_unavailable"}
    assert CANARY not in response.text + caplog.text
    status = client.get("/api/web-fetch/tavily", headers={"Authorization": "Bearer op"})
    assert status.status_code == 200
    assert status.json() == {
        "api_key_set": False, "stored_key_set": False,
        "source": "none", "storage_available": False
    }


def test_token_store_failure_is_closed(client, monkeypatch):
    def unavailable():
        raise auth.TokenFileError(CANARY)
    monkeypatch.setattr(auth, "load_tokens", unavailable)
    response = client.get("/api/web-fetch/tavily", headers={"Authorization": "Bearer op"})
    assert response.status_code == 503
    assert response.json() == {"detail": "auth_unavailable"}


def test_tavily_snapshot_fragments_match_in_process_schema(client):
    import json
    from pathlib import Path

    snapshot = json.loads((Path(__file__).parents[1] / "OpenCohost_UI/openapi.snapshot.json").read_text(encoding="utf-8"))
    actual = client.app.openapi()
    assert snapshot["paths"]["/api/web-fetch/tavily"] == actual["paths"]["/api/web-fetch/tavily"]
    for name in ("TavilyKeyRequest", "TavilyKeyResponse"):
        assert snapshot["components"]["schemas"][name] == actual["components"]["schemas"][name]
