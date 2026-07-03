"""Tests for POST /api/commands (Phase 2, B2) — the server-side whitelisted
generic engine-command endpoint.

Engine verb + payload shapes were verified against
`opencohost/core/llm_engine.py MotorVocalIA._dispatch_command` (read-only —
never edited from here). All whitelisted verbs consume a raw scalar payload
(or none, for `clear_history`) — never a dict — so the endpoint extracts the
single wire-level `payload["value"]` key before calling `dispatcher.dispatch`.
"""

import re

import pytest
from fastapi.testclient import TestClient

from tests.test_api_phase1 import FakeHost

COMMAND_ID_RE = re.compile(r"^cmd_[0-9a-f]{32}$")

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]

WHITELISTED_VERBS = [
    ("clear_history", {}, None),
    ("set_tts_local_only", {"value": True}, True),
    ("set_tts_speed", {"value": 1.5}, 1.5),
    ("set_piper_voice", {"value": "es_kira"}, "es_kira"),
    ("set_motor_tts", {"value": "ligero"}, "ligero"),
    ("switch_model", {"value": "qwen3:8b"}, "qwen3:8b"),
    ("switch_llm_tier", {"value": "heavy"}, "heavy"),
]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)


@pytest.mark.parametrize("command,wire_payload,engine_payload", WHITELISTED_VERBS)
def test_whitelisted_verb_accepted_and_enqueued(command, wire_payload, engine_payload):
    app = _app()
    with TestClient(app) as client:
        resp = client.post("/api/commands", json={"command": command, "payload": wire_payload})
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] is True
        assert COMMAND_ID_RE.match(body["command_id"])
        assert body["state_version"] == 1

        queued = app.state.host.motor.command_queue.get_nowait()
        assert queued == (command, engine_payload)
        assert app.state.dispatcher.state_version == 1


def test_unknown_command_rejected_and_not_enqueued():
    app = _app()
    with TestClient(app) as client:
        resp = client.post("/api/commands", json={"command": "delete_everything", "payload": {}})
        assert resp.status_code in (400, 422)
        assert app.state.host.motor.command_queue.qsize() == 0
        assert app.state.dispatcher.state_version == 0


def test_process_context_not_whitelisted():
    # R8/scope: process_context/chat is a later typed endpoint, never this one.
    app = _app()
    with TestClient(app) as client:
        resp = client.post("/api/commands", json={"command": "process_context", "payload": {}})
        assert resp.status_code in (400, 422)
        assert app.state.host.motor.command_queue.qsize() == 0


def test_idempotency_replay_same_key_same_command_and_payload():
    app = _app()
    with TestClient(app) as client:
        headers = {"Idempotency-Key": "req-1"}
        first = client.post(
            "/api/commands", json={"command": "clear_history", "payload": {}}, headers=headers
        )
        second = client.post(
            "/api/commands", json={"command": "clear_history", "payload": {}}, headers=headers
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["command_id"] == second.json()["command_id"]
        assert app.state.host.motor.command_queue.qsize() == 1
        assert app.state.dispatcher.state_version == 1


def test_idempotency_conflict_same_key_different_verb():
    app = _app()
    with TestClient(app) as client:
        headers = {"Idempotency-Key": "req-1"}
        first = client.post(
            "/api/commands",
            json={"command": "switch_model", "payload": {"value": "qwen3:8b"}},
            headers=headers,
        )
        assert first.status_code == 200

        second = client.post(
            "/api/commands",
            json={"command": "switch_llm_tier", "payload": {"value": "qwen3:8b"}},
            headers=headers,
        )
        assert second.status_code == 409
        assert app.state.host.motor.command_queue.qsize() == 1


def test_queue_full_returns_429():
    app = _app()
    with TestClient(app) as client:
        for _ in range(16):
            app.state.host.motor.command_queue.put_nowait(("noop", {}))

        resp = client.post("/api/commands", json={"command": "clear_history", "payload": {}})
        assert resp.status_code == 429
        body = resp.json()
        assert body["accepted"] is False
        assert body["reason"] == "queue_full"
        assert app.state.dispatcher.state_version == 0


INVALID_VALUE_CASES = [
    ("set_tts_speed", {}),  # missing value entirely
    ("set_tts_speed", {"value": None}),
    ("set_tts_speed", {"value": "fast"}),  # not float-able
    ("switch_model", {}),
    ("switch_model", {"value": None}),
    ("switch_llm_tier", {}),
    ("switch_llm_tier", {"value": None}),
    ("set_piper_voice", {}),
    ("set_piper_voice", {"value": None}),
    ("set_motor_tts", {}),
    ("set_motor_tts", {"value": None}),
    ("set_tts_local_only", {}),  # missing -> None -> not bool
    ("set_tts_local_only", {"value": None}),
    ("set_tts_local_only", {"value": "yes"}),  # non-bool string
    ("set_tts_local_only", {"value": 1}),  # non-bool int
]


@pytest.mark.parametrize("command,wire_payload", INVALID_VALUE_CASES)
def test_invalid_payload_value_rejected_and_not_enqueued(command, wire_payload):
    # DoS regression guard: a missing/None/wrong-type value for a verb that
    # requires one must be rejected at the trust boundary (422) and never
    # reach the engine command queue — see `_dispatch_command` in
    # llm_engine.py, where e.g. `float(None)` would otherwise raise
    # uncaught and kill the engine command-loop thread.
    app = _app()
    with TestClient(app) as client:
        resp = client.post("/api/commands", json={"command": command, "payload": wire_payload})
        assert resp.status_code == 422
        assert app.state.host.motor.command_queue.qsize() == 0
        assert app.state.dispatcher.state_version == 0


def test_clear_history_response_is_ack_only():
    app = _app()
    with TestClient(app) as client:
        resp = client.post(
            "/api/commands",
            json={"command": "clear_history", "payload": {"anything": "should not echo"}},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"accepted", "command_id", "status", "state_version"}
        assert "count" not in body
        assert "preview" not in body
        assert "should not echo" not in resp.text
