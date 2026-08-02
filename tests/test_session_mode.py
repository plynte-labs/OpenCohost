"""Unit 2.5 (runtime_findings_batch_20260731 F13): `session_mode` /
`llm_generating` / `pending_commands_count` / `ctx_telemetry` on
`GET /api/status`, plus the unit 2.3 -> 2.5 handoff: `EventOut.detail` now
accepts a numeric dict (`ctx_pressure_high`), not just `str | None`.

FakeMotor/FakeHost pattern mirrors unit 1.3's tests/test_api_phase1.py.
Agenda state uses the REAL `KiraAgendaController` (mirrors
tests/test_api_agenda.py) -- a pure, Tk-free state machine with no reason to
fake it; `.state` is a plain mutable attribute (see kira_agenda_controller.py),
so tests set it directly instead of driving the full enable()/topic flow.
"""

import pytest
from fastapi.testclient import TestClient

from opencohost.smart_aggregator.kira_agenda_controller import AgendaState, KiraAgendaController
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


def _app(host_factory=FakeHost):
    import opencohost.api.main as main_mod

    return main_mod.create_app(host_factory=host_factory, cors_origins=_DEFAULT_TEST_ORIGINS)


# ──────────────────────────────────────────────────────────────────────────
# session_mode derivation (plan's Layer 1 verification table)
# ──────────────────────────────────────────────────────────────────────────


def test_session_mode_off_and_speaking_is_post_agenda():
    agenda = KiraAgendaController()
    assert agenda.state == AgendaState.OFF
    app = _app(_host_with_agenda(agenda))
    with TestClient(app) as client:
        app.state.host.motor._is_speaking = True
        resp = client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json()["session_mode"] == "post-agenda"


def test_session_mode_off_and_all_clear_is_inactiva():
    agenda = KiraAgendaController()
    app = _app(_host_with_agenda(agenda))
    with TestClient(app) as client:
        resp = client.get("/api/status")
        assert resp.status_code == 200
        body = resp.json()
        assert body["session_mode"] == "inactiva"
        assert body["llm_generating"] is False
        assert body["pending_commands_count"] == 0


def test_session_mode_agenda_active_wins_regardless_of_booleans():
    agenda = KiraAgendaController()
    agenda.state = AgendaState.OPEN_TOPIC
    app = _app(_host_with_agenda(agenda))
    with TestClient(app) as client:
        # Nothing else is busy -- agenda != OFF must still report "agenda".
        resp = client.get("/api/status")
        assert resp.json()["session_mode"] == "agenda"


def test_session_mode_off_and_only_pending_commands_is_post_agenda():
    agenda = KiraAgendaController()
    app = _app(_host_with_agenda(agenda))
    with TestClient(app) as client:
        app.state.host.motor.command_queue.put_nowait(("noop", {}))
        resp = client.get("/api/status")
        body = resp.json()
        assert body["session_mode"] == "post-agenda"
        assert body["pending_commands_count"] == 1


def test_session_mode_off_and_only_llm_generating_is_post_agenda():
    agenda = KiraAgendaController()
    app = _app(_host_with_agenda(agenda))
    with TestClient(app) as client:
        app.state.host.motor._llm_generating = True
        resp = client.get("/api/status")
        body = resp.json()
        assert body["session_mode"] == "post-agenda"
        assert body["llm_generating"] is True


def test_session_mode_no_agenda_controller_is_treated_as_off():
    """`host.agenda is None` (agenda unavailable, e.g. resilient-construction
    failure) is not "active" -- falls through to the post-agenda/inactiva
    read on the booleans, same as a real AgendaState.OFF."""
    app = _app(FakeHost)
    with TestClient(app) as client:
        resp = client.get("/api/status")
        assert resp.json()["session_mode"] == "inactiva"


# ──────────────────────────────────────────────────────────────────────────
# ctx_telemetry (unit 2.3's ring surfaced on StatusResponse)
# ──────────────────────────────────────────────────────────────────────────

_RING_ENTRY = {
    "request_id": "r1",
    "timestamp": 123.0,
    "source": "direct",
    "provider": "local",
    "model": "gemma4:e4b",
    "native_ctx": 8192,
    "effective_ctx": 4096,
    "ratio": 0.62,
    "prompt_eval_count": 2540,
    "prefill_ms": 120.0,
    "decode_ms": 900.0,
    "eval_count": 128,
    "evicted_pairs": 2,
}


def test_ctx_telemetry_populated_from_ring_latest():
    app = _app()
    with TestClient(app) as client:
        app.state.host.motor._ctx_ring = [_RING_ENTRY]
        resp = client.get("/api/status")
        assert resp.json()["ctx_telemetry"] == {
            "ratio": 0.62,
            "effective_ctx": 4096,
            "native_ctx": 8192,
            "evicted_pairs": 2,
            "source": "direct",
        }


def test_ctx_telemetry_absent_when_no_generation_yet():
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/status")
        assert resp.json()["ctx_telemetry"] is None


# ──────────────────────────────────────────────────────────────────────────
# EventOut.detail widening (2.3 -> 2.5 handoff): dict detail must round-trip
# through GET /api/events without a Pydantic validation error, and the
# existing str-detail path (e.g. memoria_captured's coalesced count) must
# stay valid.
# ──────────────────────────────────────────────────────────────────────────


def test_events_dict_detail_round_trips():
    app = _app()
    with TestClient(app) as client:
        app.state.host.event_log.record(
            "motor",
            "ctx_pressure_high",
            {"ratio": 0.83, "effective_ctx": 4096, "native_ctx": 8192, "evicted_pairs": 2},
        )
        resp = client.get("/api/events")
        assert resp.status_code == 200
        events = resp.json()["events"]
        assert events[-1]["action"] == "ctx_pressure_high"
        assert events[-1]["detail"] == {
            "ratio": 0.83,
            "effective_ctx": 4096,
            "native_ctx": 8192,
            "evicted_pairs": 2,
        }


def test_events_str_detail_still_valid():
    app = _app()
    with TestClient(app) as client:
        app.state.host.event_log.record("motor", "memoria_captured", "3")
        resp = client.get("/api/events")
        assert resp.status_code == 200
        events = resp.json()["events"]
        assert events[-1]["detail"] == "3"


def test_events_none_detail_still_valid():
    app = _app()
    with TestClient(app) as client:
        app.state.host.event_log.record("motor", "ready")
        resp = client.get("/api/events")
        assert resp.status_code == 200
        events = resp.json()["events"]
        assert events[-1]["detail"] is None
