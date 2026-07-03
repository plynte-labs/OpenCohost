"""Tests for the headless Kira Agenda endpoints (WS3 slice 3): GET/POST/PUT
/api/agenda* — Tier-C direct KiraAgendaController calls under `agenda_lock`
(NOT the command_queue).

KiraAgendaController is a real, Tk-free, pure state machine (no Ollama/TTS/
OBS/UI calls) — tests use the REAL controller instead of a fake. R8: GET
must never surface `get_metrics().rejection_log` matched_text/matched_phrase
or any raw viewer phrase — counts/state only.
"""

import pytest
from fastapi.testclient import TestClient

from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController
from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


def _host_with_agenda(agenda):
    def factory():
        host = FakeHost()
        host.agenda = agenda
        return host

    return factory


def _app(host_factory):
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=host_factory, cors_origins=_DEFAULT_TEST_ORIGINS)


# ──────────────────────────────────────────────────────────────────────────
# GET /api/agenda — shape + 503 when unavailable
# ──────────────────────────────────────────────────────────────────────────


def test_get_agenda_shape_from_real_controller():
    controller = KiraAgendaController()
    app = _app(_host_with_agenda(controller))
    with TestClient(app) as client:
        resp = client.get("/api/agenda")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "OFF"
        assert body["active_topic"] is None
        assert body["queued_topics"] == []
        assert body["drafted_topics"] == []
        assert body["session_settings"]["max_turns_per_topic"] == 3
        assert body["session_settings"]["rhythm"] == "normal"
        assert body["session_settings"]["response_length"] == "normal"
        assert body["session_settings"]["safety_mode"] == "live_safe"
        assert body["metrics"]["total_rejections"] == 0
        assert body["metrics"]["topics_queued"] == 0


def test_get_agenda_503_when_agenda_none():
    app = _app(_host_with_agenda(None))
    with TestClient(app) as client:
        resp = client.get("/api/agenda")
        assert resp.status_code == 503
        assert resp.json()["detail"] == "agenda_unavailable"


# ──────────────────────────────────────────────────────────────────────────
# R8: GET must never surface raw viewer text from rejection_log
# ──────────────────────────────────────────────────────────────────────────


def test_get_agenda_never_leaks_rejection_log_matched_text():
    controller = KiraAgendaController()
    secret = "VIEWER_SAID_A_PROMPT_INJECTION_SECRET_XYZ"
    # Seed a rejection the way `_validate_output` would populate it —
    # `matched_text`/`matched_phrase` carry text derived from generation
    # output, exactly what R8 forbids from ever reaching the wire.
    controller.rejection_log.append(
        {
            "error": "ERR_GUARDRAIL_BREAKS_CHARACTER",
            "guardrail": "breaks_character",
            "reason": "Kira rompió contrato de personaje: prompt_leak",
            "length": "normal",
            "state": "GENERATING",
            "subtype": "prompt_leak",
            "matched_pattern": "prompt_leak",
            "matched_text": secret,
        }
    )
    controller.rejection_log.append(
        {
            "error": "ERR_GUARDRAIL_SIMILAR",
            "guardrail": "repeats_recent_line",
            "reason": "Repitió una frase de respuestas recientes",
            "length": "normal",
            "state": "GENERATING",
            "matched_phrase": secret,
        }
    )
    app = _app(_host_with_agenda(controller))
    with TestClient(app) as client:
        resp = client.get("/api/agenda")
        assert resp.status_code == 200
        # Non-vacuous: the rejection actually happened (counts reflect it) —
        # this proves the endpoint reads real data, not just an empty log.
        assert resp.json()["metrics"]["total_rejections"] == 2
        assert secret not in resp.text
        assert "matched_text" not in resp.text
        assert "matched_phrase" not in resp.text
        assert "rejection_log" not in resp.text


# ──────────────────────────────────────────────────────────────────────────
# POST /api/agenda/topic — add_topic
# ──────────────────────────────────────────────────────────────────────────


def test_post_agenda_topic_appends_drafted_topic():
    controller = KiraAgendaController()
    app = _app(_host_with_agenda(controller))
    with TestClient(app) as client:
        resp = client.post("/api/agenda/topic", json={"title": "Nueva IA local", "angle": "privacidad primero"})
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["drafted_topics"]) == 1
        assert body["drafted_topics"][0]["title"] == "Nueva IA local"
        assert body["drafted_topics"][0]["angle"] == "privacidad primero"
        assert body["drafted_topics"][0]["status"] == "drafted"
    assert len(controller.topics) == 1


def test_post_agenda_topic_422_on_invalid_title():
    controller = KiraAgendaController()
    app = _app(_host_with_agenda(controller))
    with TestClient(app) as client:
        resp = client.post("/api/agenda/topic", json={"title": ""})
        assert resp.status_code == 422


def test_post_agenda_topic_503_when_agenda_none():
    app = _app(_host_with_agenda(None))
    with TestClient(app) as client:
        resp = client.post("/api/agenda/topic", json={"title": "x"})
        assert resp.status_code == 503


# ──────────────────────────────────────────────────────────────────────────
# POST /api/agenda/topic/action — approve/queue/move/remove
# ──────────────────────────────────────────────────────────────────────────


def test_topic_action_approve_then_queue_then_remove():
    controller = KiraAgendaController()
    topic = controller.add_topic("Tema uno")
    app = _app(_host_with_agenda(controller))
    with TestClient(app) as client:
        resp = client.post("/api/agenda/topic/action", json={"action": "approve", "topic_id": topic.id})
        assert resp.status_code == 200
        assert controller._topic(topic.id).status.value == "approved"

        resp = client.post("/api/agenda/topic/action", json={"action": "queue", "topic_id": topic.id})
        assert resp.status_code == 200
        assert len(resp.json()["queued_topics"]) == 1

        resp = client.post("/api/agenda/topic/action", json={"action": "remove", "topic_id": topic.id})
        assert resp.status_code == 200
        assert resp.json()["queued_topics"] == []


def test_topic_action_move_reorders_queue():
    controller = KiraAgendaController()
    first = controller.add_topic("Tema A", approved=True)
    second = controller.add_topic("Tema B", approved=True)
    controller.queue_topic(first.id)
    controller.queue_topic(second.id)
    app = _app(_host_with_agenda(controller))
    with TestClient(app) as client:
        resp = client.post(
            "/api/agenda/topic/action",
            json={"action": "move", "topic_id": second.id, "direction": -1},
        )
        assert resp.status_code == 200
        titles = [t["title"] for t in resp.json()["queued_topics"]]
        assert titles == ["Tema B", "Tema A"]


def test_topic_action_unknown_action_422():
    controller = KiraAgendaController()
    topic = controller.add_topic("Tema uno")
    app = _app(_host_with_agenda(controller))
    with TestClient(app) as client:
        resp = client.post("/api/agenda/topic/action", json={"action": "reject", "topic_id": topic.id})
        assert resp.status_code == 422


def test_topic_action_unknown_topic_id_422():
    controller = KiraAgendaController()
    app = _app(_host_with_agenda(controller))
    with TestClient(app) as client:
        resp = client.post("/api/agenda/topic/action", json={"action": "approve", "topic_id": "does-not-exist"})
        assert resp.status_code == 422


def test_topic_action_invalid_transition_422():
    """approve on an already-approved topic raises ValueError in the
    controller — must surface as 422, not 500."""
    controller = KiraAgendaController()
    topic = controller.add_topic("Tema uno", approved=True)
    app = _app(_host_with_agenda(controller))
    with TestClient(app) as client:
        resp = client.post("/api/agenda/topic/action", json={"action": "approve", "topic_id": topic.id})
        assert resp.status_code == 422


def test_topic_action_503_when_agenda_none():
    app = _app(_host_with_agenda(None))
    with TestClient(app) as client:
        resp = client.post("/api/agenda/topic/action", json={"action": "approve", "topic_id": "x"})
        assert resp.status_code == 503


# ──────────────────────────────────────────────────────────────────────────
# PUT /api/agenda/session — set_session_settings / set_profile
# ──────────────────────────────────────────────────────────────────────────


def test_put_agenda_session_updates_settings_and_profile():
    controller = KiraAgendaController()
    app = _app(_host_with_agenda(controller))
    with TestClient(app) as client:
        resp = client.put(
            "/api/agenda/session",
            json={
                "max_turns_per_topic": 7,
                "rhythm": "dinamico",
                "response_length": "corta",
                "safety_mode": "monologue",
                "profile": {"style": "co-host irónica y directa"},
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_settings"]["max_turns_per_topic"] == 7
        assert body["session_settings"]["rhythm"] == "dinamico"
        assert body["session_settings"]["response_length"] == "corta"
        assert body["session_settings"]["safety_mode"] == "monologue"
        assert body["session_settings"]["profile_style"] == "co-host irónica y directa"
    assert controller.max_turns_per_topic == 7
    assert controller.profile["style"] == "co-host irónica y directa"


def test_put_agenda_session_partial_update_leaves_rest_unchanged():
    controller = KiraAgendaController()
    app = _app(_host_with_agenda(controller))
    with TestClient(app) as client:
        resp = client.put("/api/agenda/session", json={"rhythm": "calmo"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_settings"]["rhythm"] == "calmo"
        assert body["session_settings"]["max_turns_per_topic"] == 3  # unchanged default


def test_put_agenda_session_503_when_agenda_none():
    app = _app(_host_with_agenda(None))
    with TestClient(app) as client:
        resp = client.put("/api/agenda/session", json={"rhythm": "calmo"})
        assert resp.status_code == 503


# ──────────────────────────────────────────────────────────────────────────
# EngineHost resilient agenda construction (mirrors the Aggregator pattern)
# ──────────────────────────────────────────────────────────────────────────


def test_engine_host_start_survives_agenda_construction_failure(tmp_path, monkeypatch):
    """A host whose KiraAgendaController ctor raises still start()s with
    agenda=None — engine/profiles/commands must not be bricked by an
    agenda-controller failure."""
    import opencohost.api.engine_host as engine_host_mod
    from queue import Queue
    from unittest.mock import MagicMock

    fake_motor = MagicMock()
    fake_motor.command_queue = Queue()
    fake_motor.current_model = None
    fake_monitor = MagicMock()

    monkeypatch.setattr(engine_host_mod, "MotorVocalIA", lambda *a, **kw: fake_motor)
    monkeypatch.setattr(engine_host_mod, "HealthMonitor", lambda: fake_monitor)
    monkeypatch.setattr(engine_host_mod, "ollama", MagicMock())
    monkeypatch.setattr(
        engine_host_mod,
        "KiraAgendaController",
        lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("agenda ctor boom")),
    )

    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    try:
        host.start()  # must not raise
        assert host.agenda is None
        assert host.motor is fake_motor  # engine still came up
    finally:
        host.stop()
