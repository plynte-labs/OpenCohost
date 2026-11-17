"""T3 of remote-voice-gateway: the turn relay (POST /turn, GET /reply).

A paired phone sends text as a ``direct`` turn and short-polls for the final reply.
The FakeHost carries a REAL ChatReplySink and EventLogSink; the dispatcher is a fake
that records calls. Time is injected, so the rate limits and the reply timeout are
deterministic.
"""

import logging
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from tests.test_api_phase1 import FakeHost
from tests.test_remote_gateway_pairing import FakeClock, _registry

TURN_TEXT = "cuentame algo secreto-del-turno"
REPLY_TEXT = "respuesta-privada-de-kira"


class FakeDispatcher:
    state_version = 7

    def __init__(self, state="accepted"):
        self.state = state
        self.calls = []

    def dispatch(self, command, payload, key=None, **kwargs):
        self.calls.append((command, payload, key, kwargs))
        return SimpleNamespace(state=self.state, command_id=f"cmd-{len(self.calls)}")


class Rig:
    def __init__(self, dispatcher=None):
        self.clock = FakeClock()
        self.registry = _registry(self.clock)
        self.host = FakeHost()
        self.dispatcher = dispatcher or FakeDispatcher()
        from opencohost.gateway.app import create_gateway_app

        self.client = TestClient(create_gateway_app(self.host, self.dispatcher, self.registry, clock=self.clock))

    def pair(self, name="Pixel"):
        code, _ = self.registry.mint_code()
        body = self.client.post("/pair", json={"code": code, "name": name}).json()
        return body["device_id"], {"Authorization": f"Bearer {body['token']}"}

    def turn(self, headers, text=TURN_TEXT):
        return self.client.post("/turn", json={"text": text}, headers=headers)

    def reply(self, headers, after):
        return self.client.get("/reply", params={"after": after}, headers=headers)

    def final(self, text=REPLY_TEXT, source="direct"):
        return self.host.chat_sink.record(text, source)


@pytest.fixture
def rig():
    return Rig()


# --- auth ---------------------------------------------------------------------


def test_turn_and_reply_require_a_device_token(rig):
    assert rig.client.post("/turn", json={"text": "hola"}).status_code == 401
    assert rig.client.get("/reply", params={"after": 0}).status_code == 401
    bad = {"Authorization": "Bearer nope"}
    assert rig.client.post("/turn", json={"text": "hola"}, headers=bad).status_code == 401
    assert rig.dispatcher.calls == []


# --- /turn --------------------------------------------------------------------


def test_turn_dispatches_a_direct_turn_and_returns_after(rig):
    _, headers = rig.pair()
    rig.final("old reply")  # turn_id 1
    resp = rig.turn(headers)
    assert resp.status_code == 200
    assert resp.json() == {"accepted": True, "after": 1}
    (command, payload, _key, kwargs), = rig.dispatcher.calls
    assert command == "process_context"
    assert payload == TURN_TEXT
    assert kwargs["source"] == "direct"
    assert TURN_TEXT in kwargs["history_text"]
    assert "submitted_at" in kwargs and "submitted_under_provider" in kwargs


def test_turn_after_is_zero_on_an_empty_sink(rig):
    _, headers = rig.pair()
    assert rig.turn(headers).json()["after"] == 0


@pytest.mark.parametrize("body", [{"text": ""}, {"text": "   \n"}, {"text": 5}, {}, [], "x"])
def test_bad_or_empty_text_is_rejected_without_dispatch(rig, body):
    _, headers = rig.pair()
    resp = rig.client.post("/turn", json=body, headers=headers)
    assert resp.status_code in (400, 422)
    assert set(resp.json()) == {"error"}
    assert rig.dispatcher.calls == []


def test_invalid_json_is_rejected_without_dispatch(rig):
    _, headers = rig.pair()
    resp = rig.client.post("/turn", content=b"{not json", headers=headers)
    assert resp.status_code == 400
    assert rig.dispatcher.calls == []


def test_over_max_length_text_is_rejected_without_echo(rig):
    from opencohost.api.routers.chat import _CHAT_TEXT_MAX_LENGTH

    _, headers = rig.pair()
    text = "z" * (_CHAT_TEXT_MAX_LENGTH + 1)
    resp = rig.turn(headers, text)
    assert resp.status_code == 422
    assert "zzzz" not in resp.text
    assert rig.dispatcher.calls == []


def test_oversized_body_is_rejected_without_dispatch(rig):
    _, headers = rig.pair()
    resp = rig.client.post("/turn", content=b'{"text":"' + b"a" * 40_000 + b'"}', headers=headers)
    assert resp.status_code == 413
    assert rig.dispatcher.calls == []


def test_readiness_failure_is_a_409_with_a_code_only(rig, monkeypatch):
    from opencohost.core.engine import llm_readiness as mod

    monkeypatch.setattr(
        mod, "resolve_llm_readiness", lambda host: SimpleNamespace(can_chat=False, state="local_ollama_offline")
    )
    _, headers = rig.pair()
    resp = rig.turn(headers)
    assert resp.status_code == 409
    assert resp.json()["error"] == "ENGINE_UNAVAILABLE"
    assert TURN_TEXT not in resp.text
    assert rig.dispatcher.calls == []


def test_queue_full_is_a_429_and_does_not_lock_the_device(rig):
    rig.dispatcher.state = "queue_full"
    _, headers = rig.pair()
    resp = rig.turn(headers)
    assert resp.status_code == 429
    assert resp.json() == {"error": "queue_full"}
    rig.dispatcher.state = "accepted"
    assert rig.turn(headers).status_code == 200  # not rate limited by the failed attempt


def test_conflict_passes_through_as_409(rig):
    rig.dispatcher.state = "conflict"
    _, headers = rig.pair()
    resp = rig.turn(headers)
    assert resp.status_code == 409
    assert resp.json() == {"error": "conflict"}


# --- rate limits --------------------------------------------------------------


def test_second_turn_while_one_is_in_flight_is_rate_limited(rig):
    _, headers = rig.pair()
    assert rig.turn(headers).status_code == 200
    resp = rig.turn(headers)
    assert resp.status_code == 429
    assert resp.json() == {"error": "rate_limited"}
    assert len(rig.dispatcher.calls) == 1


def test_a_new_turn_is_allowed_once_the_reply_arrived(rig):
    _, headers = rig.pair()
    after = rig.turn(headers).json()["after"]
    rig.final()
    assert rig.reply(headers, after).json()["status"] == "ready"
    assert rig.turn(headers).status_code == 200


def test_a_new_turn_is_allowed_after_the_reply_timeout(rig):
    from opencohost.gateway import turns

    _, headers = rig.pair()
    assert rig.turn(headers).status_code == 200
    rig.clock.now += turns.REPLY_TIMEOUT_S + 1
    assert rig.turn(headers).status_code == 200


def test_per_minute_limit_then_recovery(rig):
    from opencohost.gateway import turns

    _, headers = rig.pair()
    for _ in range(turns.MAX_TURNS_PER_MINUTE):
        after = rig.turn(headers).json()["after"]
        rig.final()
        assert rig.reply(headers, after).json()["status"] == "ready"
    assert rig.turn(headers).status_code == 429
    rig.clock.now += 61
    assert rig.turn(headers).status_code == 200


def test_rate_limits_are_per_device(rig):
    _, first = rig.pair("A")
    _, second = rig.pair("B")
    assert rig.turn(first).status_code == 200
    assert rig.turn(second).status_code == 200


# --- /reply -------------------------------------------------------------------


def test_reply_without_a_submission_is_idle(rig):
    _, headers = rig.pair()
    assert rig.reply(headers, 0).json() == {"status": "idle"}


def test_reply_stays_pending_while_only_streaming_chunks_exist(rig):
    _, headers = rig.pair()
    after = rig.turn(headers).json()["after"]
    rig.host.chat_sink.record_stream_chunk("s1", "partial", ["partial"], "direct")
    assert rig.reply(headers, after).json() == {"status": "pending"}


def test_reply_is_ready_on_the_final_item(rig):
    _, headers = rig.pair()
    after = rig.turn(headers).json()["after"]
    rig.host.chat_sink.record_stream_chunk("s1", "partial", ["partial"], "direct")
    turn_id = rig.final()
    resp = rig.reply(headers, after)
    assert resp.json() == {"status": "ready", "text": REPLY_TEXT, "turn_id": turn_id}
    assert "no-store" in resp.headers.get("cache-control", "")


def test_reply_ready_survives_a_repeated_poll(rig):
    _, headers = rig.pair()
    after = rig.turn(headers).json()["after"]
    rig.final()
    assert rig.reply(headers, after).json()["status"] == "ready"
    assert rig.reply(headers, after).json()["status"] == "ready"


def test_older_turn_ids_and_other_origins_are_ignored(rig):
    _, headers = rig.pair()
    rig.final("older")  # turn 1, before the snapshot
    after = rig.turn(headers).json()["after"]
    assert after == 1
    rig.final("agenda line", source="kira-agenda")
    rig.final("ptt line", source="ptt")
    assert rig.reply(headers, after).json() == {"status": "pending"}
    assert rig.reply(headers, 5).json() == {"status": "pending"}  # nothing above 5


def test_turn_dropped_event_gives_no_reply(rig):
    _, headers = rig.pair()
    rig.host.event_log.record("motor", "turn_dropped")  # before the turn: ignored
    after = rig.turn(headers).json()["after"]
    assert rig.reply(headers, after).json() == {"status": "pending"}
    rig.host.event_log.record("motor", "turn_dropped")
    assert rig.reply(headers, after).json() == {"status": "no_reply"}


def test_reasoning_budget_exhausted_gives_no_reply(rig):
    _, headers = rig.pair()
    after = rig.turn(headers).json()["after"]
    rig.host.event_log.record("motor", "reasoning_budget_exhausted")
    assert rig.reply(headers, after).json() == {"status": "no_reply"}


def test_unrelated_events_do_not_end_the_wait(rig):
    _, headers = rig.pair()
    after = rig.turn(headers).json()["after"]
    rig.host.event_log.record("motor", "cloud_restored")
    assert rig.reply(headers, after).json() == {"status": "pending"}


def test_timeout_gives_no_reply(rig):
    from opencohost.gateway import turns

    _, headers = rig.pair()
    after = rig.turn(headers).json()["after"]
    rig.clock.now += turns.REPLY_TIMEOUT_S - 1
    assert rig.reply(headers, after).json() == {"status": "pending"}
    rig.clock.now += 2
    assert rig.reply(headers, after).json() == {"status": "no_reply"}


def test_another_device_cannot_read_the_reply(rig):
    _, owner = rig.pair("A")
    _, other = rig.pair("B")
    after = rig.turn(owner).json()["after"]
    rig.final()
    assert rig.reply(other, after).json() == {"status": "idle"}
    assert rig.reply(owner, after).json()["status"] == "ready"


def test_a_missing_or_bad_after_is_a_400(rig):
    _, headers = rig.pair()
    assert rig.client.get("/reply", headers=headers).status_code in (400, 422)
    resp = rig.client.get("/reply", params={"after": "abc"}, headers=headers)
    assert resp.status_code in (400, 422)


# --- privacy ------------------------------------------------------------------


def test_logs_carry_no_turn_or_reply_text(rig, caplog):
    with caplog.at_level(logging.DEBUG):
        _, headers = rig.pair()
        rig.dispatcher.state = "queue_full"
        rig.turn(headers)
        rig.dispatcher.state = "accepted"
        after = rig.turn(headers).json()["after"]
        rig.turn(headers)  # rate limited
        rig.final()
        rig.reply(headers, after)
        rig.client.post("/turn", json={"text": ""}, headers=headers)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert TURN_TEXT not in text and REPLY_TEXT not in text
    assert headers["Authorization"].split()[1] not in text


def test_gateway_logs_reach_the_opencohost_logger():
    import opencohost.gateway.devices as devices_mod
    import opencohost.gateway.server as server_mod
    import opencohost.gateway.turns as turns_mod

    for module in (devices_mod, server_mod, turns_mod):
        assert module.logger.name.startswith("OpenCohost.")
        parent = module.logger
        names = []
        while parent is not None and parent.name != "root":
            names.append(parent.name)
            parent = parent.parent
        assert "OpenCohost" in names
