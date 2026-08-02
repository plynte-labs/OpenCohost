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


def test_sink_records_queue_wait_ms_when_given():
    # Unit 4.1 (runtime_findings_batch_20260731, F5): the seam 4.2 consumes —
    # a stamped direct/chat/ptt turn's queue wait rides through unmodified.
    sink = ChatReplySink()
    sink.record("hola, todo bien", "kira", queue_wait_ms=987)
    assert sink.last()["queue_wait_ms"] == 987


def test_sink_queue_wait_ms_defaults_to_none_never_a_fake_zero():
    # An unstamped turn (agenda, accumulated) must report null, not 0 —
    # 0 would falsely claim a measured instant dequeue.
    sink = ChatReplySink()
    sink.record("respuesta de agenda", "kira-agenda")
    assert sink.last()["queue_wait_ms"] is None


# ──────────────────────────────────────────────────────────────────────────
# Unit 4.2 (F12 closure): provider-disclosure fields
# ──────────────────────────────────────────────────────────────────────────


def test_sink_records_provider_disclosure_fields_when_given():
    sink = ChatReplySink()
    sink.record(
        "respuesta directa",
        "kira",
        queue_wait_ms=42000,
        answered_by_provider="local",
        answered_by_transport="local",
        submitted_under_provider="nvidia_nim",
        provider_changed_while_queued=True,
    )
    last = sink.last()
    assert last["answered_by_provider"] == "local"
    assert last["answered_by_transport"] == "local"
    assert last["submitted_under_provider"] == "nvidia_nim"
    assert last["provider_changed_while_queued"] is True


def test_sink_provider_fields_default_to_none_for_an_untagged_turn():
    # Agenda/accumulated turns (and any turn recorded before this unit) never
    # carry a submitted_under_provider tag — the new fields must be null, not
    # a fabricated value.
    sink = ChatReplySink()
    sink.record("bloque de agenda", "kira-agenda")
    last = sink.last()
    assert last["answered_by_provider"] is None
    assert last["answered_by_transport"] is None
    assert last["submitted_under_provider"] is None
    assert last["provider_changed_while_queued"] is None


def test_endpoint_surfaces_provider_disclosure_for_a_tagged_turn():
    app = _app()
    with TestClient(app) as client:
        app.state.host.chat_sink.record(
            "respuesta directa",
            "kira",
            queue_wait_ms=3000,
            answered_by_provider="local",
            answered_by_transport="local",
            submitted_under_provider="nvidia_nim",
            provider_changed_while_queued=True,
        )
        body = client.get("/api/chat/last-reply").json()
        assert body["answered_by_provider"] == "local"
        assert body["answered_by_transport"] == "local"
        assert body["submitted_under_provider"] == "nvidia_nim"
        assert body["provider_changed_while_queued"] is True


def test_endpoint_same_provider_carries_no_mismatch():
    app = _app()
    with TestClient(app) as client:
        app.state.host.chat_sink.record(
            "respuesta directa",
            "kira",
            queue_wait_ms=500,
            answered_by_provider="local",
            answered_by_transport="local",
            submitted_under_provider="local",
            provider_changed_while_queued=False,
        )
        body = client.get("/api/chat/last-reply").json()
        assert body["provider_changed_while_queued"] is False


# ──────────────────────────────────────────────────────────────────────────
# GET /api/chat/last-reply endpoint tests
# ──────────────────────────────────────────────────────────────────────────


def test_endpoint_empty_before_any_turn():
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/chat/last-reply")
        assert resp.status_code == 200
        # Unit 4.1 (runtime_findings_batch_20260731, F5): the response model
        # gained queue_wait_ms (null until a stamped direct/chat/ptt turn ships
        # a real wait) — the sink's own empty-state dict is unchanged, but the
        # pydantic model always serializes its declared fields.
        # Unit 4.2 (F12 closure): four more provider-disclosure fields, all
        # null before any turn, for the same reason.
        assert resp.json() == {
            "text": None,
            "source": None,
            "turn_id": 0,
            "ts": None,
            "queue_wait_ms": None,
            "answered_by_provider": None,
            "answered_by_transport": None,
            "submitted_under_provider": None,
            "provider_changed_while_queued": None,
        }


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


def test_endpoint_surfaces_queue_wait_ms_for_a_stamped_turn():
    # Unit 4.1 (runtime_findings_batch_20260731, F5): the exact seam 4.2 reads
    # from — a direct turn's queue_wait_ms reaches the HTTP response.
    app = _app()
    with TestClient(app) as client:
        app.state.host.chat_sink.record("respuesta directa", "kira", queue_wait_ms=125000)
        body = client.get("/api/chat/last-reply").json()
        assert body["queue_wait_ms"] == 125000
