"""T1 of remote-voice-gateway: settings, gateway skeleton app, server thread, lifespan wiring."""

import json
import socket
import urllib.error
import urllib.request

import pytest
from fastapi.testclient import TestClient

from tests.test_api_phase1 import FakeHost


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


# --- (a) settings resolvers -------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [(None, False), ("1", True), ("0", False), ("true", False), (" 1", False), ("", False)],
)
def test_gateway_enabled_resolver(monkeypatch, value, expected):
    from opencohost.config import settings

    if value is None:
        monkeypatch.delenv("OPENCOHOST_REMOTE_GATEWAY", raising=False)
    else:
        monkeypatch.setenv("OPENCOHOST_REMOTE_GATEWAY", value)
    assert settings._resolve_remote_gateway_enabled() is expected


@pytest.mark.parametrize(
    "value,expected",
    [(None, 8780), ("9000", 9000), ("abc", 8780), ("0", 8780), ("70000", 8780), ("-5", 8780), ("", 8780)],
)
def test_gateway_port_resolver(monkeypatch, value, expected):
    from opencohost.config import settings

    if value is None:
        monkeypatch.delenv("OPENCOHOST_REMOTE_GATEWAY_PORT", raising=False)
    else:
        monkeypatch.setenv("OPENCOHOST_REMOTE_GATEWAY_PORT", value)
    assert settings._resolve_remote_gateway_port() == expected


# --- (b) gateway app --------------------------------------------------------


def _gateway_client():
    from opencohost.gateway.app import create_gateway_app

    return TestClient(create_gateway_app(FakeHost(), object()))


def test_status_is_minimal():
    resp = _gateway_client().get("/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert body["gateway"] == "v0"
    assert isinstance(body["engine_ready"], bool)
    blob = json.dumps(body).lower()
    for forbidden in ("text", "token", "setting", "key", "secret"):
        assert forbidden not in blob


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json", "/api/status", "/nope", "/animations/evil.vrma", "/avatar/anything", "/avatar/model/../x", "/api/remote/messages"])
def test_other_paths_are_404(path):
    assert _gateway_client().get(path).status_code == 404


def test_no_cors_headers():
    resp = _gateway_client().get("/status", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in {k.lower() for k in resp.headers}


# --- (c)/(d) lifespan wiring ------------------------------------------------


def _install_fakes(monkeypatch, order):
    import opencohost.gateway.app as app_mod
    import opencohost.gateway.server as server_mod

    class RecordingHost(FakeHost):
        def stop(self):
            order.append("host.stop")
            super().stop()

    class FakeServer:
        instances = []

        def __init__(self, app, port):
            self.app = app
            self.port = port
            FakeServer.instances.append(self)
            order.append("server.init")

        def start(self):
            order.append("server.start")
            return True

        def stop(self):
            order.append("server.stop")

    def fake_create(host, dispatcher, registry=None, feed=None):
        order.append("create_app")
        assert dispatcher is not None
        return object()

    monkeypatch.setattr(server_mod, "GatewayServer", FakeServer)
    monkeypatch.setattr(app_mod, "create_gateway_app", fake_create)
    return RecordingHost, FakeServer


def test_lifespan_flag_off_constructs_nothing(monkeypatch):
    import opencohost.api.main as main_mod
    from opencohost.config import settings

    order = []
    host_cls, fake_server = _install_fakes(monkeypatch, order)
    monkeypatch.setattr(settings, "REMOTE_GATEWAY_ENABLED", False)
    with TestClient(main_mod.create_app(host_factory=host_cls)):
        pass
    assert fake_server.instances == []
    assert order == ["host.stop"]


def test_lifespan_flag_on_starts_after_dispatcher_and_stops_before_host(monkeypatch):
    import opencohost.api.main as main_mod
    from opencohost.config import settings

    order = []
    host_cls, fake_server = _install_fakes(monkeypatch, order)
    monkeypatch.setattr(settings, "REMOTE_GATEWAY_ENABLED", True)
    monkeypatch.setattr(settings, "REMOTE_GATEWAY_PORT", 8791)
    app = main_mod.create_app(host_factory=host_cls)
    with TestClient(app):
        assert app.state.dispatcher is not None
        assert order == ["create_app", "server.init", "server.start"]
        assert fake_server.instances[0].port == 8791
    assert order == ["create_app", "server.init", "server.start", "server.stop", "host.stop"]


def test_lifespan_gateway_failure_does_not_block_main_api(monkeypatch, caplog):
    import opencohost.api.main as main_mod
    import opencohost.gateway.server as server_mod
    from opencohost.config import settings

    order = []
    host_cls, _ = _install_fakes(monkeypatch, order)

    class Boom:
        def __init__(self, app, port):
            raise OSError("secret-detail-should-not-be-logged")

    monkeypatch.setattr(server_mod, "GatewayServer", Boom)
    monkeypatch.setattr(settings, "REMOTE_GATEWAY_ENABLED", True)
    monkeypatch.setattr(settings, "REMOTE_GATEWAY_PORT", 8792)
    with caplog.at_level("WARNING"):
        with TestClient(main_mod.create_app(host_factory=host_cls)) as client:
            assert client.get("/api/status").status_code == 200
    assert order[-1] == "host.stop"
    msgs = [r.getMessage() for r in caplog.records if "gateway" in r.getMessage().lower()]
    assert any("OSError" in m and "8792" in m for m in msgs)
    assert not any("secret-detail" in m for m in msgs)


# --- (e) real socket --------------------------------------------------------


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_gateway_server_real_socket_start_stop():
    from opencohost.gateway.app import create_gateway_app
    from opencohost.gateway.server import GatewayServer

    port = _free_port()
    server = GatewayServer(create_gateway_app(FakeHost(), object()), port)
    assert server.start() is True
    assert server.start() is True  # idempotent
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/status", timeout=5) as r:
            assert r.status == 200
            assert json.loads(r.read())["gateway"] == "v0"
        with pytest.raises(urllib.error.HTTPError) as exc:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/docs", timeout=5)
        assert exc.value.code == 404
    finally:
        server.stop()
    server.stop()  # idempotent
    assert server._thread is None
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
