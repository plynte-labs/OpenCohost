"""/api/speech/* -- read-aloud controls (readaloud-speech T2).

POST /api/speech/say {text, mode} -> {job_id}; POST /stop, /pause, /resume;
GET /state -> {paused, readaloud_active, readaloud_pending}.

PRIVACY (same contract as /api/chat/turn): no response body and no log line
ever carries the submitted text; a guard block answers with the rule id only.
"""
import logging

import pytest
from fastapi.testclient import TestClient

from opencohost.api.routers.chat import _CHAT_TEXT_MAX_LENGTH
from opencohost.config import settings
from tests.test_api_phase1 import FakeHost

_ORIGINS = ["http://localhost:5173"]


class _Job:
    def __init__(self, job_id):
        self.job_id = job_id


class _SpeechMotor:
    """The speech surface of MotorVocalIA the router needs, recorded."""

    def __init__(self, enabled=True):
        self._speech_router_enabled = enabled
        self.calls = []
        self.paused = False
        self.stop_result = 2

    def speak_readaloud(self, text, mode="queue"):
        self.calls.append(("say", text, mode))
        return _Job(41)

    def stop_readaloud(self):
        self.calls.append(("stop",))
        return self.stop_result

    def user_pause_speech(self):
        self.calls.append(("pause",))
        self.paused = True
        return True

    def user_resume_speech(self):
        self.calls.append(("resume",))
        self.paused = False
        return True

    def readaloud_state(self):
        return {"paused": self.paused, "readaloud_active": True, "readaloud_pending": 3}


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


def _client(enabled=True):
    import opencohost.api.main as main_mod

    app = main_mod.create_app(host_factory=FakeHost, cors_origins=_ORIGINS)
    client = TestClient(app)
    client.__enter__()
    motor = _SpeechMotor(enabled=enabled)
    app.state.host.motor = motor
    return app, client, motor


def test_say_queue_returns_job_id_and_submits_with_mode():
    app, client, motor = _client()
    try:
        resp = client.post("/api/speech/say", json={"text": "Hola mundo.", "mode": "queue"})
        assert resp.status_code == 200
        assert resp.json() == {"job_id": 41}
        assert motor.calls == [("say", "Hola mundo.", "queue")]
    finally:
        client.__exit__(None, None, None)


def test_say_now_mode_is_forwarded():
    app, client, motor = _client()
    try:
        resp = client.post("/api/speech/say", json={"text": "Ahora.", "mode": "now"})
        assert resp.status_code == 200
        assert motor.calls == [("say", "Ahora.", "now")]
    finally:
        client.__exit__(None, None, None)


def test_say_mode_defaults_to_queue_and_rejects_unknown_modes():
    app, client, motor = _client()
    try:
        assert client.post("/api/speech/say", json={"text": "Hola."}).status_code == 200
        assert motor.calls[-1] == ("say", "Hola.", "queue")
        assert client.post(
            "/api/speech/say", json={"text": "Hola.", "mode": "interrupt"}
        ).status_code == 422
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("text", ["", "   \n\t", "x" * (_CHAT_TEXT_MAX_LENGTH + 1)])
def test_say_rejects_empty_and_oversize_text_with_422(text):
    app, client, motor = _client()
    try:
        resp = client.post("/api/speech/say", json={"text": text, "mode": "queue"})
        assert resp.status_code == 422
        assert motor.calls == []
    finally:
        client.__exit__(None, None, None)


def test_say_accepts_text_at_the_cap():
    app, client, motor = _client()
    try:
        resp = client.post("/api/speech/say", json={"text": "x" * _CHAT_TEXT_MAX_LENGTH})
        assert resp.status_code == 200
    finally:
        client.__exit__(None, None, None)


def test_say_guard_block_is_422_with_rule_id_only_and_never_echoes_text(caplog):
    caplog.set_level(logging.DEBUG)
    app, client, motor = _client()
    blocked = "As an AI language model, I am an artificial intelligence."
    try:
        resp = client.post("/api/speech/say", json={"text": blocked, "mode": "queue"})
        assert resp.status_code == 422
        body = resp.json()
        assert body["detail"]["error"] == "GUARD_BLOCKED"
        assert body["detail"]["rule"] == "no_ai_self_identification"
        assert "artificial intelligence" not in resp.text
        assert motor.calls == [], "a blocked text must never reach the router"
        assert not any("artificial intelligence" in r.getMessage() for r in caplog.records)
    finally:
        client.__exit__(None, None, None)


def test_say_503_when_the_router_is_disabled():
    app, client, motor = _client(enabled=False)
    try:
        resp = client.post("/api/speech/say", json={"text": "Hola.", "mode": "queue"})
        assert resp.status_code == 503
        assert resp.json()["detail"]["error"] == "SPEECH_UNAVAILABLE"
        assert motor.calls == []
    finally:
        client.__exit__(None, None, None)


def test_say_503_when_submit_returns_no_job():
    app, client, motor = _client()
    motor.speak_readaloud = lambda text, mode="queue": None
    try:
        resp = client.post("/api/speech/say", json={"text": "Hola."})
        assert resp.status_code == 503
    finally:
        client.__exit__(None, None, None)


def test_say_response_and_logs_never_carry_the_text(caplog):
    caplog.set_level(logging.DEBUG)
    app, client, motor = _client()
    try:
        resp = client.post("/api/speech/say", json={"text": "SECRETPHRASE uno."})
        assert "SECRETPHRASE" not in resp.text
        assert not any("SECRETPHRASE" in r.getMessage() for r in caplog.records)
    finally:
        client.__exit__(None, None, None)


def test_stop_pause_resume_and_state_contract():
    app, client, motor = _client()
    try:
        stop = client.post("/api/speech/stop")
        assert stop.status_code == 200 and stop.json() == {"discarded": 2}

        pause = client.post("/api/speech/pause")
        assert pause.status_code == 200 and pause.json() == {"paused": True}

        state = client.get("/api/speech/state")
        assert state.status_code == 200
        assert state.json() == {"paused": True, "readaloud_active": True, "readaloud_pending": 3}

        resume = client.post("/api/speech/resume")
        assert resume.status_code == 200 and resume.json() == {"paused": False}
        assert [c[0] for c in motor.calls] == ["stop", "pause", "resume"]
    finally:
        client.__exit__(None, None, None)


@pytest.mark.parametrize("path", ["/api/speech/stop", "/api/speech/pause", "/api/speech/resume"])
def test_controls_503_when_the_router_is_disabled(path):
    app, client, motor = _client(enabled=False)
    try:
        resp = client.post(path)
        assert resp.status_code == 503
        assert motor.calls == []
    finally:
        client.__exit__(None, None, None)


def test_state_is_safe_defaults_when_the_router_is_disabled():
    app, client, motor = _client(enabled=False)
    try:
        resp = client.get("/api/speech/state")
        assert resp.status_code == 200
        assert resp.json() == {"paused": False, "readaloud_active": False, "readaloud_pending": 0}
    finally:
        client.__exit__(None, None, None)


def test_posts_require_operator_token_when_enforced_and_get_stays_open(monkeypatch):
    monkeypatch.setattr(settings, "API_AUTH_ENFORCED", True)
    app, client, motor = _client()
    try:
        for path, body in (
            ("/api/speech/say", {"text": "Hola."}),
            ("/api/speech/stop", None),
            ("/api/speech/pause", None),
            ("/api/speech/resume", None),
        ):
            resp = client.post(path, json=body)
            assert resp.status_code == 401, path
        assert motor.calls == [], "no motor call may happen before auth"
        assert client.get("/api/speech/state").status_code == 200
    finally:
        client.__exit__(None, None, None)
