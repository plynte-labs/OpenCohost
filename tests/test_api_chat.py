"""Tests for POST /api/chat/turn — the typed chat-turn endpoint (design v2.1
build-order step 4, R8-CRITICAL).

The engine verb + payload shape were verified against
`opencohost/core/llm_engine.py MotorVocalIA._dispatch_command` (read-only —
never edited from here): the `process_context` branch treats its payload as
the raw user text and builds the full prompt internally, exactly like
`app_shell.py`'s `_enviar_contexto_manual` (`command_queue.put(("process_context",
texto))`). This endpoint is the HTTP twin — it dispatches the plain text str,
never a dict, via the existing `Dispatcher.dispatch("process_context", text, key)`.

R8: the response body is the ACK ONLY. The submitted text and any generated
dialogue must NEVER cross HTTP here — Kira's reply is observed via GET
/api/status (is_processing/is_speaking), never returned by this endpoint.
"""

import re

import pytest
from fastapi.testclient import TestClient

from tests.test_api_phase1 import FakeHost

COMMAND_ID_RE = re.compile(r"^cmd_[0-9a-f]{32}$")

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


def _app():
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=FakeHost, cors_origins=_DEFAULT_TEST_ORIGINS)


def test_valid_text_accepted_and_enqueued_as_process_context():
    app = _app()
    with TestClient(app) as client:
        resp = client.post("/api/chat/turn", json={"text": "hola Kira, como estas?"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] is True
        assert COMMAND_ID_RE.match(body["command_id"])
        assert body["state_version"] == 1

        queued = app.state.host.motor.command_queue.get_nowait()
        assert queued == ("process_context", "hola Kira, como estas?")
        assert app.state.dispatcher.state_version == 1


@pytest.mark.parametrize("text", ["", "   ", "\n\t  \n"])
def test_empty_or_whitespace_text_rejected_and_not_enqueued(text):
    app = _app()
    with TestClient(app) as client:
        resp = client.post("/api/chat/turn", json={"text": text})
        assert resp.status_code == 422
        assert app.state.host.motor.command_queue.qsize() == 0
        assert app.state.dispatcher.state_version == 0


def test_oversize_text_rejected_and_not_enqueued():
    app = _app()
    with TestClient(app) as client:
        resp = client.post("/api/chat/turn", json={"text": "a" * 4001})
        assert resp.status_code == 422
        assert app.state.host.motor.command_queue.qsize() == 0
        assert app.state.dispatcher.state_version == 0


def test_max_length_text_is_accepted():
    app = _app()
    with TestClient(app) as client:
        resp = client.post("/api/chat/turn", json={"text": "a" * 4000})
        assert resp.status_code == 200


def test_accepted_response_never_echoes_submitted_text():
    app = _app()
    with TestClient(app) as client:
        distinctive = "R8-CANARY-do-not-echo-me-3f9c"
        resp = client.post("/api/chat/turn", json={"text": distinctive})
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"accepted", "command_id", "status", "state_version"}
        assert distinctive not in resp.text


def test_idempotency_replay_same_key_same_text():
    app = _app()
    with TestClient(app) as client:
        headers = {"Idempotency-Key": "chat-1"}
        first = client.post("/api/chat/turn", json={"text": "hola"}, headers=headers)
        second = client.post("/api/chat/turn", json={"text": "hola"}, headers=headers)
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["command_id"] == second.json()["command_id"]
        assert app.state.host.motor.command_queue.qsize() == 1
        assert app.state.dispatcher.state_version == 1


def test_idempotency_conflict_same_key_different_text():
    app = _app()
    with TestClient(app) as client:
        headers = {"Idempotency-Key": "chat-1"}
        first = client.post("/api/chat/turn", json={"text": "hola"}, headers=headers)
        assert first.status_code == 200

        second = client.post("/api/chat/turn", json={"text": "chau"}, headers=headers)
        assert second.status_code == 409
        assert app.state.host.motor.command_queue.qsize() == 1


def test_queue_full_returns_429():
    app = _app()
    with TestClient(app) as client:
        for _ in range(16):
            app.state.host.motor.command_queue.put_nowait(("noop", {}))

        resp = client.post("/api/chat/turn", json={"text": "hola"})
        assert resp.status_code == 429
        body = resp.json()
        assert body["accepted"] is False
        assert body["reason"] == "queue_full"
        assert app.state.dispatcher.state_version == 0
