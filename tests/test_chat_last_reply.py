"""Tests for GET /api/chat/last-reply and the bounded ChatReplySink (S2, P3).

R8-safe: this endpoint surfaces Kira's OWN generated reply text only — the
sink is fed exclusively via `MotorVocalIA.dialogue_callback`
(`_emit_dialogue` in llm_engine.py), never raw viewer/operator chat.
"""

import pytest
from fastapi.testclient import TestClient

from opencohost.api.engine_host import ChatReplySink
from tests.test_api_phase1 import FakeHost

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


# ──────────────────────────────────────────────────────────────────────────
# ChatReplySink unit tests
# ──────────────────────────────────────────────────────────────────────────


def test_sink_empty_before_any_turn():
    sink = ChatReplySink()
    assert sink.last() == {"text": None, "source": None, "turn_id": 0, "ts": None}


def test_sink_records_exact_text_with_monotonic_turn_id():
    sink = ChatReplySink()
    sink.record("hola, todo bien", "kira")
    first = sink.last()
    assert first["text"] == "hola, todo bien"
    assert first["source"] == "kira"
    assert first["turn_id"] == 1
    assert first["ts"] is not None

    sink.record("segunda respuesta", "kira-agenda")
    second = sink.last()
    assert second["text"] == "segunda respuesta"
    assert second["turn_id"] == 2


def test_sink_deque_never_exceeds_maxlen():
    sink = ChatReplySink(maxlen=16)
    for i in range(50):
        sink.record(f"reply-{i}", "kira")
    assert len(sink._replies) == 16
    last = sink.last()
    assert last["turn_id"] == 50
    assert last["text"] == "reply-49"


# ──────────────────────────────────────────────────────────────────────────
# GET /api/chat/last-reply endpoint tests
# ──────────────────────────────────────────────────────────────────────────


def test_endpoint_empty_before_any_turn():
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/chat/last-reply")
        assert resp.status_code == 200
        assert resp.json() == {"text": None, "source": None, "turn_id": 0, "ts": None}


def test_endpoint_returns_exact_text_and_increasing_turn_id():
    app = _app()
    with TestClient(app) as client:
        app.state.host.chat_sink.record("primera", "kira")
        first = client.get("/api/chat/last-reply").json()
        assert first["text"] == "primera"
        assert first["source"] == "kira"
        assert first["turn_id"] == 1

        app.state.host.chat_sink.record("segunda", "kira")
        second = client.get("/api/chat/last-reply").json()
        assert second["text"] == "segunda"
        assert second["turn_id"] == 2
