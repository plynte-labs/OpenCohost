"""D1 (release-050 audit): the main API rejects non-loopback Host headers.

A DNS-rebinding page resolves its own name to 127.0.0.1 and then sends
same-origin requests to the local API with that name in ``Host``. CORS does
not stop this, so the main app refuses any Host that is not a loopback name
(``127.0.0.1``, ``localhost``, ``[::1]``, with or without a port).

The remote voice gateway is a separate ASGI app on loopback port 8780 and does
not install this guard, so its Cloudflare Tunnel hostname keeps working. The
gateway test below pins that.

tests/conftest.py adds ``testserver`` (the TestClient default Host) to the
allowlist for every test. The tests here restore the production allowlist.
"""

import asyncio

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from opencohost.config import settings
from tests.test_api_phase1 import FakeHost

_PRODUCTION_HOSTS = settings.LOOPBACK_API_HOST_NAMES


@pytest.fixture
def production_host_allowlist(monkeypatch):
    monkeypatch.setattr(settings, "API_ALLOWED_HOST_NAMES", _PRODUCTION_HOSTS)


def test_production_allowlist_is_loopback_names_only():
    assert settings.LOOPBACK_API_HOST_NAMES == frozenset({"127.0.0.1", "localhost", "[::1]"})


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "127.0.0.1:8765",
        "127.0.0.1:8770",
        "localhost",
        "localhost:8765",
        "LOCALHOST:8765",
        "[::1]",
        "[::1]:8765",
    ],
)
def test_loopback_host_headers_are_accepted(host):
    from opencohost.api.host_guard import is_loopback_host

    assert is_loopback_host(host, _PRODUCTION_HOSTS) is True


@pytest.mark.parametrize(
    "host",
    [
        None,
        "",
        "evil.example",
        "evil.example:8765",
        "localhost.evil.example",
        "localhost.evil.example:8765",
        "127.0.0.1.evil.example",
        "localhost.",
        "0.0.0.0",
        "0.0.0.0:8765",
        "192.168.1.20:8765",
        "testserver",
        "[::2]:8765",
        "[::1]junk",
        "[::1",
        "::1",
        "127.0.0.1:",
        "127.0.0.1:0",
        "127.0.0.1:65536",
        "127.0.0.1:8765:1",
        "127.0.0.1:87x65",
        "localhost:²",
    ],
)
def test_non_loopback_host_headers_are_rejected(host):
    from opencohost.api.host_guard import is_loopback_host

    assert is_loopback_host(host, _PRODUCTION_HOSTS) is False


def _rebinding_app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=["http://localhost:5173"])


def test_main_app_rejects_rebinding_host(production_host_allowlist):
    with TestClient(_rebinding_app(), base_url="http://evil.example:8765") as client:
        resp = client.get("/api/health")
    assert resp.status_code == 400
    assert resp.json() == {"error": "invalid_host"}


def test_main_app_rejects_rebinding_host_before_auth(production_host_allowlist, monkeypatch):
    # The Host check is outermost: a rebinding POST gets the fixed 400, not a 401 from auth.
    monkeypatch.setattr(settings, "API_AUTH_ENFORCED", True)
    with TestClient(_rebinding_app(), base_url="http://evil.example") as client:
        resp = client.post("/api/chat/turn", json={})
    assert resp.status_code == 400
    assert resp.json() == {"error": "invalid_host"}


def test_main_app_accepts_loopback_host(production_host_allowlist):
    with TestClient(_rebinding_app(), base_url="http://127.0.0.1:8765") as client:
        resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_gateway_app_accepts_its_public_tunnel_host(production_host_allowlist):
    from opencohost.gateway.app import create_gateway_app

    client = TestClient(create_gateway_app(FakeHost(), object()), base_url="https://phone.example.test")
    resp = client.get("/status")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


def test_websocket_with_rebinding_host_is_refused(production_host_allowlist):
    from opencohost.api.host_guard import HostGuardMiddleware

    app = FastAPI()
    app.add_middleware(HostGuardMiddleware)

    @app.websocket("/ws")
    async def ws_route(websocket: WebSocket):
        await websocket.accept()
        await websocket.send_text("reached")
        await websocket.close()

    # Starlette's websocket_connect ignores base_url for Host, so pass the header explicitly.
    client = TestClient(app)
    with pytest.raises(WebSocketDisconnect) as refused:
        with client.websocket_connect("/ws", headers={"host": "evil.example:8765"}):
            pass
    assert refused.value.code == 1008

    with client.websocket_connect("/ws", headers={"host": "127.0.0.1:8765"}) as ws:
        assert ws.receive_text() == "reached"


def _run_asgi(scope):
    """Drive HostGuardMiddleware with a raw ASGI scope; return (status, reached_inner_app)."""
    from opencohost.api.host_guard import HostGuardMiddleware

    reached = {"inner": False}
    sent = []

    async def inner(scope_, receive, send):
        reached["inner"] = True
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(HostGuardMiddleware(inner)(scope, receive, send))
    status = next((m["status"] for m in sent if m["type"] == "http.response.start"), None)
    return status, reached["inner"]


def _http_scope(headers):
    return {"type": "http", "method": "GET", "path": "/api/health", "headers": headers}


def test_duplicate_host_headers_are_refused(production_host_allowlist):
    scope = _http_scope([(b"host", b"127.0.0.1:8765"), (b"host", b"127.0.0.1:8765")])
    assert _run_asgi(scope) == (400, False)


def test_missing_host_header_is_refused(production_host_allowlist):
    assert _run_asgi(_http_scope([])) == (400, False)


def test_lifespan_scope_passes_through(production_host_allowlist):
    assert _run_asgi({"type": "lifespan"})[1] is True
