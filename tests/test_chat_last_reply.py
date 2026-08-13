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
        # null before any turn, for the same reason. `origin` joins them on the
        # same terms — the sink's empty dict never grew a key.
        assert resp.json() == {
            "text": None,
            "source": None,
            "origin": None,
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


# ──────────────────────────────────────────────────────────────────────────
# `origin` — the TRUE generating source (chat_reply_origin_20260813)
#
# `source` collapses every non-agenda turn to "kira" so on-screen attribution
# stays uniform, which made a viewer reply and a streamer reply byte-identical
# by the time they reached the API. `origin` is the field that tells them
# apart. It is a fixed internal surface label, never a word of the text that
# triggered the reply, so R8 is untouched.
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("origin", ["chat", "direct", "ptt", "accumulated"])
def test_sink_keeps_true_origin_while_source_stays_collapsed(origin):
    sink = ChatReplySink()
    sink.record("respuesta", origin)
    last = sink.last()
    # The published attribution is unchanged for every one of these...
    assert last["source"] == "kira"
    # ...and the thing that used to be destroyed upstream survives.
    assert last["origin"] == origin


@pytest.mark.parametrize("origin", ["kira-agenda", "kira-agenda-stop"])
def test_sink_agenda_origin_survives_in_both_fields(origin):
    # Agenda was never collapsed, so `source` keeps carrying it — `origin` must
    # agree rather than reporting null for the one family that always worked.
    sink = ChatReplySink()
    sink.record("bloque", origin)
    last = sink.last()
    assert last["source"] == origin
    assert last["origin"] == origin


def test_sink_reports_null_origin_for_an_already_collapsed_source():
    # A caller handing over the literal "kira" has already destroyed the origin.
    # Reporting "kira" would launder that placeholder into a fake origin, so the
    # contract says null: genuinely unknown.
    sink = ChatReplySink()
    sink.record("respuesta", "kira")
    assert sink.last()["origin"] is None


def test_endpoint_surfaces_origin_for_a_viewer_chat_reply():
    app = _app()
    with TestClient(app) as client:
        app.state.host.chat_sink.record("respuesta a un viewer", "chat")
        body = client.get("/api/chat/last-reply").json()
        assert body["origin"] == "chat"
        assert body["source"] == "kira"


def test_endpoint_origin_distinguishes_consecutive_chat_and_direct_replies():
    # The regression this whole field exists for: two turns that were previously
    # indistinguishable at the API must now differ.
    app = _app()
    with TestClient(app) as client:
        app.state.host.chat_sink.record("para el chat", "chat")
        first = client.get("/api/chat/last-reply").json()
        app.state.host.chat_sink.record("para el streamer", "direct")
        second = client.get("/api/chat/last-reply").json()

    assert first["source"] == second["source"] == "kira"
    assert (first["origin"], second["origin"]) == ("chat", "direct")


def test_origin_never_carries_the_text_that_triggered_the_reply():
    # R8 guard with teeth: whatever a caller passes as the source, `origin` is
    # only ever the label — the endpoint must never become a back door for the
    # viewer text itself.
    sink = ChatReplySink()
    sink.record("respuesta de Kira", "chat")
    last = sink.last()
    assert last["origin"] == "chat"
    assert last["text"] == "respuesta de Kira"
    assert "respuesta de Kira" not in (last["origin"] or "")


def test_engine_to_sink_wire_delivers_the_true_origin_end_to_end():
    """The regression guard for the whole seam: a REAL MotorVocalIA turn wired
    to a REAL ChatReplySink, exactly as EngineHost wires it.

    Every unit test above can pass while the engine still collapses the source
    before the sink ever sees it — that is precisely the bug this field exists
    to fix, and only running the real emit path can catch its return.
    """
    import queue as _queue
    from unittest.mock import MagicMock

    from opencohost.core.llm_engine import MotorVocalIA

    sink = ChatReplySink()
    motor = MotorVocalIA(_queue.Queue(), lambda event: None, dialogue_callback=sink.record)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor.current_model = "llama3"
    motor._reasoning_model_cache["llama3"] = False
    motor._hablar = MagicMock()
    motor._ollama_chat = MagicMock(return_value={"message": {"content": "Che, buena esa."}})

    motor._ejecutar_inferencia("hola", source="chat")
    chat_turn = sink.last()
    assert chat_turn["text"] == "Che, buena esa."
    assert chat_turn["source"] == "kira"
    assert chat_turn["origin"] == "chat"

    motor._ejecutar_inferencia("hola", source="direct")
    direct_turn = sink.last()
    assert direct_turn["source"] == "kira"
    assert direct_turn["origin"] == "direct"

    # Same collapsed attribution, different origin — the two turns are no
    # longer indistinguishable by the time they reach the API consumer.
    assert chat_turn["source"] == direct_turn["source"]
    assert chat_turn["origin"] != direct_turn["origin"]
