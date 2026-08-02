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
import time

import pytest
from fastapi.testclient import TestClient

from opencohost.i18n import active as i18n_active
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
        before = time.monotonic()
        resp = client.post("/api/chat/turn", json={"text": "hola Kira, como estas?"})
        after = time.monotonic()
        assert resp.status_code == 200
        body = resp.json()
        assert body["accepted"] is True
        assert COMMAND_ID_RE.match(body["command_id"])
        assert body["state_version"] == 1

        queued = app.state.host.motor.command_queue.get_nowait()
        # B1 (turn provenance): the prompt payload is still the raw text; the
        # honest speaker frame rides as the 3rd element (history_text).
        # Unit 4.1 (runtime_findings_batch_20260731, F5): source (4th) and the
        # monotonic submitted_at stamp (5th) now ride alongside it — the
        # earliest backend receipt point for a typed direct question.
        assert queued[:4] == (
            "process_context",
            "hola Kira, como estas?",
            i18n_active.typed_history_wrapper().format(text="hola Kira, como estas?"),
            "direct",
        )
        assert before <= queued[4] <= after
        assert app.state.dispatcher.state_version == 1


def test_typed_turn_commits_history_text_naming_the_speaker():
    # B1 (turn provenance batch 1): a typed operator turn used to commit BARE,
    # so in the history the model reads it was indistinguishable from any other
    # voice — with no chat platform connected at all Kira still referred to "the
    # viewer before". PTT already commits wrapped (ptt_history_wrapper); typed
    # now mirrors that shape and locale handling with its OWN slot (a typed turn
    # must never claim it arrived through PTT).
    app = _app()
    with TestClient(app) as client:
        text = "que opinas del parche nuevo"
        resp = client.post("/api/chat/turn", json={"text": text})
        assert resp.status_code == 200

        # Unit 4.1: the queue tuple grew a 4th (source) and 5th (submitted_at)
        # element — see test_valid_text_accepted_and_enqueued_as_process_context.
        # Unit 4.2 (F12 closure): a 6th (submitted_under_provider) rides
        # alongside it now — the provider posture at submit time.
        command, payload, history_text, source, submitted_at, submitted_under_provider = (
            app.state.host.motor.command_queue.get_nowait()
        )
        assert command == "process_context"
        assert payload == text  # prompt unchanged — generation behavior untouched
        assert history_text == i18n_active.typed_history_wrapper().format(text=text)
        assert text in history_text
        assert source == "direct"
        assert isinstance(submitted_at, float)
        assert history_text != i18n_active.ptt_history_wrapper().format(text=text)
        assert submitted_under_provider == "local"


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
