"""remote-vrm-avatar T9: phone messages recorded in the desktop conversation.

An in-memory, bounded feed of accepted phone turns, read by an operator-only
endpoint on the MAIN API. The text lives only in memory; it is never logged.
"""

import logging

import pytest
from fastapi.testclient import TestClient

from opencohost.config import settings
from tests.test_remote_gateway_turns import TURN_TEXT, FakeDispatcher, Rig


class Clock:
    def __init__(self, now=1000.0):
        self.now = now

    def __call__(self):
        return self.now


def _feed(clock=None):
    from opencohost.gateway.feed import RemoteTurnFeed

    return RemoteTurnFeed(clock=clock or Clock())


def _rig_with_feed(dispatcher=None):
    from opencohost.gateway.app import create_gateway_app

    rig = Rig(dispatcher)
    rig.feed = _feed(rig.clock)
    rig.client = TestClient(
        create_gateway_app(rig.host, rig.dispatcher, rig.registry, clock=rig.clock, feed=rig.feed)
    )
    return rig


# --- feed unit ---------------------------------------------------------------


def test_feed_assigns_increasing_seq_and_filters_by_after():
    feed = _feed()
    a = feed.add("uno", "dev-1", "Pixel")
    b = feed.add("dos", "dev-1", "Pixel")
    assert (a, b) == (1, 2)
    snap = feed.since(0)
    assert [i["seq"] for i in snap["items"]] == [1, 2]
    assert snap["latest_seq"] == 2
    assert [i["text"] for i in feed.since(1)["items"]] == ["dos"]
    assert feed.since(2) == {"items": [], "latest_seq": 2}
    item = feed.since(0)["items"][0]
    assert item["device_label"] == "Pixel" and item["device_id"] == "dev-1"
    assert item["accepted_at"] == 1000.0


def test_feed_is_bounded_to_50_entries():
    feed = _feed()
    for n in range(60):
        feed.add(f"m{n}", "d", "P")
    items = feed.since(0)["items"]
    assert len(items) == 50
    assert items[0]["seq"] == 11 and items[-1]["seq"] == 60
    assert feed.since(0)["latest_seq"] == 60


def test_feed_drops_entries_older_than_30_minutes():
    clock = Clock()
    feed = _feed(clock)
    feed.add("viejo", "d", "P")
    clock.now += 31 * 60
    feed.add("nuevo", "d", "P")
    snap = feed.since(0)
    assert [i["text"] for i in snap["items"]] == ["nuevo"]
    assert snap["latest_seq"] == 2


# --- recorded by /turn -------------------------------------------------------


def test_accepted_turn_is_recorded_with_its_text():
    rig = _rig_with_feed()
    device_id, headers = rig.pair("Pixel")
    assert rig.turn(headers).status_code == 200
    [item] = rig.feed.since(0)["items"]
    assert item["text"] == TURN_TEXT
    assert item["device_id"] == device_id and item["device_label"] == "Pixel"


@pytest.mark.parametrize("state", ["queue_full", "conflict", "unavailable"])
def test_refused_turn_is_not_recorded(state):
    rig = _rig_with_feed(FakeDispatcher(state))
    _, headers = rig.pair()
    assert rig.turn(headers).status_code in (409, 429)
    assert rig.feed.since(0)["items"] == []


def test_rate_limited_turn_is_not_recorded():
    rig = _rig_with_feed()
    _, headers = rig.pair()
    assert rig.turn(headers).status_code == 200
    assert rig.turn(headers).status_code == 429  # previous turn still pending
    assert len(rig.feed.since(0)["items"]) == 1


def test_invalid_text_is_not_recorded():
    rig = _rig_with_feed()
    _, headers = rig.pair()
    rig.client.post("/turn", json={"text": ""}, headers=headers)
    assert rig.feed.since(0)["items"] == []


def test_feed_text_never_reaches_logs(caplog):
    rig = _rig_with_feed()
    _, headers = rig.pair()
    with caplog.at_level(logging.DEBUG):
        rig.turn(headers)
    assert TURN_TEXT not in "\n".join(r.getMessage() for r in caplog.records)


def test_gateway_does_not_expose_the_messages_path():
    rig = _rig_with_feed()
    _, headers = rig.pair()
    rig.turn(headers)
    assert rig.client.get("/api/remote/messages", headers=headers).status_code == 404
    assert rig.client.get("/messages", headers=headers).status_code == 404


# --- operator endpoint on the main API ---------------------------------------


def _client_ctx(monkeypatch):
    import opencohost.gateway.server as server_mod

    captured = {}

    class FakeServer:
        def __init__(self, app, port):
            captured["app"] = app

        def start(self):
            return True

        def stop(self):
            pass

    monkeypatch.setattr(server_mod, "GatewayServer", FakeServer)
    monkeypatch.setattr(settings, "REMOTE_GATEWAY_ENABLED", True)
    monkeypatch.setattr(settings, "REMOTE_GATEWAY_PORT", 8794)
    monkeypatch.setattr(settings, "API_AUTH_ENFORCED", False)
    return captured


def test_messages_endpoint_needs_the_operator_token(monkeypatch):
    from tests.test_remote_gateway_pairing import _main_app, _role_headers

    _client_ctx(monkeypatch)
    with TestClient(_main_app()) as client:
        assert client.get("/api/remote/messages").status_code == 401
        assert client.get("/api/remote/messages", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert client.get("/api/remote/messages", headers=_role_headers("agent")).status_code == 403


def test_messages_endpoint_returns_gateway_turns_for_the_operator(monkeypatch):
    from tests.test_remote_gateway_pairing import _main_app, _role_headers

    captured = _client_ctx(monkeypatch)
    app = _main_app()
    with TestClient(app) as client:
        op = _role_headers("operator")
        code = client.post("/api/remote/pair-code", headers=op).json()["code"]
        gw = TestClient(captured["app"])
        paired = gw.post("/pair", json={"code": code, "name": "Pixel"}).json()
        auth = {"Authorization": f"Bearer {paired['token']}"}
        assert client.get("/api/remote/messages", headers=op).json() == {"items": [], "latest_seq": 0}
        assert gw.post("/turn", json={"text": "hola desde el movil"}, headers=auth).status_code == 200
        body = client.get("/api/remote/messages", headers=op).json()
        assert body["items"][0]["text"] == "hola desde el movil"
        assert body["latest_seq"] == body["items"][-1]["seq"]
        assert client.get(f"/api/remote/messages?after={body['latest_seq']}", headers=op).json()["items"] == []
        assert gw.get("/api/remote/messages", headers=auth).status_code == 404


def test_messages_endpoint_409_when_gateway_off(monkeypatch):
    from tests.test_remote_gateway_pairing import _main_app, _role_headers

    monkeypatch.setattr(settings, "REMOTE_GATEWAY_ENABLED", False)
    with TestClient(_main_app()) as client:
        resp = client.get("/api/remote/messages", headers=_role_headers("operator"))
        assert resp.status_code == 409 and resp.json() == {"error": "gateway_disabled"}
