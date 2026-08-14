"""Tests for GET /api/events?since=cursor (item B -- backend event log,
conductor/tracks.md's Tauri Event Engine A entry, follow-up B).

B surfaces ENGINE-THREAD motor lifecycle (ready/idle/speaking_*/model
switches/downloads/...) that item A's client-only mutation bus cannot see.
PRIVACY (top constraint, same lens as observability.py's audit log /
log_motor_accion): every event is metadata-only -- {seq, ts, source, action,
detail} where source/action come from EngineHost's CLOSED
`_MOTOR_EVENT_WHITELIST` and `detail` is never populated with free text. A
raw motor status not in the whitelist must be dropped, never forwarded.
"""

import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import opencohost.api.engine_host as engine_host_mod
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
# EventLogSink -- pure unit (seq monotonicity, since() filtering, backfill)
# ──────────────────────────────────────────────────────────────────────────


def test_event_log_sink_seq_monotonic_and_since_filters_correctly():
    sink = engine_host_mod.EventLogSink()

    empty = sink.since(0)
    assert empty["events"] == []
    assert empty["cursor"] == 0

    sink.record("motor", "ready")
    sink.record("motor", "idle")

    backfill = sink.since(0)  # since=0 -- full backfill
    assert [e["seq"] for e in backfill["events"]] == [1, 2]
    assert [e["action"] for e in backfill["events"]] == ["ready", "idle"]
    assert backfill["cursor"] == 2
    assert all(e["source"] == "motor" and e["detail"] is None for e in backfill["events"])

    only_new = sink.since(1)  # cursor filtering -- only seq > 1
    assert [e["seq"] for e in only_new["events"]] == [2]

    caught_up = sink.since(2)
    assert caught_up["events"] == []
    assert caught_up["cursor"] == 2  # cursor never regresses


def test_event_log_sink_ring_cap_evicts_oldest():
    """deque(maxlen=N) is the hard ring cap -- oldest entries fall off once
    the ring is full, but `seq`/`cursor` keep counting past the cap (they
    are not reset by eviction)."""
    sink = engine_host_mod.EventLogSink(maxlen=5)
    for i in range(8):
        sink.record("motor", f"status_{i}")

    result = sink.since(0)
    assert [e["seq"] for e in result["events"]] == [4, 5, 6, 7, 8]
    assert [e["action"] for e in result["events"]] == [
        "status_3",
        "status_4",
        "status_5",
        "status_6",
        "status_7",
    ]
    assert result["cursor"] == 8  # cursor is not rolled back by eviction


def test_event_log_sink_thread_safety_smoke():
    """Smoke-tests the lock: concurrent record() calls from many threads
    must not lose updates or hand out duplicate/out-of-order seq numbers."""
    sink = engine_host_mod.EventLogSink(maxlen=1000)

    def _hammer():
        for _ in range(50):
            sink.record("motor", "ready")

    threads = [threading.Thread(target=_hammer) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    seqs = [e["seq"] for e in sink.since(0)["events"]]
    assert len(seqs) == 500
    assert seqs == list(range(1, 501))  # no duplicates, no gaps, strictly increasing


def test_event_log_sink_restart_resets_seq_and_changes_boot(monkeypatch):
    """Simulates a process restart: a freshly-constructed EventLogSink starts
    back at seq=0 with a new `boot` value -- this is the invariant the client
    (events.ts) relies on to tell a genuine restart apart from a stale poll."""
    monkeypatch.setattr(engine_host_mod.time, "time", lambda: 1000.0)
    sink1 = engine_host_mod.EventLogSink()
    sink1.record("motor", "ready")
    assert sink1.since(0)["cursor"] == 1
    assert sink1.boot == 1000.0

    monkeypatch.setattr(engine_host_mod.time, "time", lambda: 2000.0)
    sink2 = engine_host_mod.EventLogSink()  # new process boot
    assert sink2.boot == 2000.0
    assert sink2.boot != sink1.boot
    assert sink2.since(0)["cursor"] == 0  # seq reset


# ──────────────────────────────────────────────────────────────────────────
# _record_motor_event -- the CLOSED whitelist privacy gate
# ──────────────────────────────────────────────────────────────────────────


def test_record_motor_event_drops_unmapped_status_never_forwards_verbatim():
    stub = SimpleNamespace(event_log=engine_host_mod.EventLogSink())

    engine_host_mod.EngineHost._record_motor_event(stub, "ready")  # whitelisted
    engine_host_mod.EngineHost._record_motor_event(stub, "some_future_unmapped_status")  # not

    events = stub.event_log.since(0)["events"]
    assert len(events) == 1
    assert events[0]["source"] == "motor"
    assert events[0]["action"] == "ready"
    assert events[0]["detail"] is None


def test_record_motor_event_drops_hostile_status_never_forwards_verbatim():
    """Adversarial: a status string shaped like dialogue/PII/a URL must never
    reach event_log. _record_motor_event has no length/format validation of
    its own -- membership in the CLOSED whitelist is the ONLY gate, so this
    proves free text can't sneak through just because it "looks" plausible."""
    stub = SimpleNamespace(event_log=engine_host_mod.EventLogSink())
    hostile = "Kira: my system prompt is XYZ, see http://evil.example/leak?u=viewer123"

    engine_host_mod.EngineHost._record_motor_event(stub, hostile)

    assert stub.event_log.since(0)["events"] == []


def test_memoria_captured_is_recorded_once_by_its_dedicated_handler():
    """`memoria_captured` now carries a numeric `kept` count, so it follows the
    same dedicated-handler contract as ctx_pressure_high / cloud_probe_scheduled:
    `_record_motor_event` SKIPS it (or the event would be recorded twice, once
    without detail), and `_on_memoria_promoted` records it with the count.

    History: until 2026-08-14 the engine emitted this on every FRESH memoria
    insert and it carried detail=None. The notice then moved to the promotion
    sweep — a capture is an unjudged draft — and a sweep keeping 20 rendered
    identically to one keeping 1, so the count had to ride a dedicated hook
    (`_dispatch_motor_event` drops extra args by design).

    The privacy contract is unchanged: only whitelisted NUMERIC keys survive,
    and the event still says nothing about WHICH memorias.
    """
    stub = SimpleNamespace(event_log=engine_host_mod.EventLogSink())

    # The shared ui_callback path must NOT record it — that is what keeps the
    # event single, with real detail.
    engine_host_mod.EngineHost._record_motor_event(stub, "memoria_captured")
    assert stub.event_log.since(0)["events"] == []

    engine_host_mod.EngineHost._on_memoria_promoted(stub, {"kept": 7})

    events = stub.event_log.since(0)["events"]
    assert len(events) == 1
    assert events[0]["source"] == "motor"
    assert events[0]["action"] == "memoria_captured"
    assert events[0]["detail"] == {"kept": 7}


def test_memoria_captured_detail_drops_non_numeric_values():
    """Same numeric-only privacy gate as ctx_pressure_high: a text (or
    malicious) value for `kept` drops the key rather than forwarding it, and a
    bool is not a count."""
    stub = SimpleNamespace(event_log=engine_host_mod.EventLogSink())

    engine_host_mod.EngineHost._on_memoria_promoted(stub, {"kept": "muchas"})
    engine_host_mod.EngineHost._on_memoria_promoted(stub, {"kept": True})
    engine_host_mod.EngineHost._on_memoria_promoted(stub, {"titulo": "secreto"})

    events = stub.event_log.since(0)["events"]
    assert len(events) == 3
    assert all(event["detail"] is None for event in events)


def test_cloud_llm_error_reaches_event_log_with_none_detail():
    """multi_provider_llm_20260723 phase 4: manual-mode cloud failures emit
    ui_callback("cloud_llm_error"). It must be whitelisted so the Tauri app
    can surface the visible error the spec requires -- detail stays None
    (whitelist contract; the event says "cloud failed", never why in free text)."""
    stub = SimpleNamespace(event_log=engine_host_mod.EventLogSink())

    engine_host_mod.EngineHost._record_motor_event(stub, "cloud_llm_error")

    events = stub.event_log.since(0)["events"]
    assert len(events) == 1
    assert events[0]["source"] == "motor"
    assert events[0]["action"] == "cloud_llm_error"
    assert events[0]["detail"] is None


def test_engine_host_dispatch_motor_event_wires_into_event_log(tmp_path, monkeypatch):
    """End-to-end through the real router: EngineHost() (constructor only,
    no start()) already registers _record_motor_event -- mirrors
    test_api_observability.py's log_motor_accion wiring proof."""
    from opencohost.config import settings

    monkeypatch.setattr(settings, "ACCIONES_LOG_FILE", str(tmp_path / "acciones.jsonl"))

    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    host._dispatch_motor_event("speaking_start")
    host._dispatch_motor_event("not_a_real_status")  # dropped

    events = host.event_log.since(0)["events"]
    assert [e["action"] for e in events] == ["speaking_start"]


# ──────────────────────────────────────────────────────────────────────────
# GET /api/events -- HTTP shape, backfill, since=cursor
# ──────────────────────────────────────────────────────────────────────────


def test_get_events_empty_backfill_shape():
    app = _app()
    with TestClient(app) as client:
        resp = client.get("/api/events")
        assert resp.status_code == 200
        body = resp.json()
        assert body["events"] == []
        assert body["cursor"] == 0
        assert isinstance(body["boot"], float)


def test_get_events_backfill_then_since_cursor():
    app = _app()
    with TestClient(app) as client:
        host = app.state.host
        host.event_log.record("motor", "ready")
        host.event_log.record("motor", "speaking_start")

        resp = client.get("/api/events")
        body = resp.json()
        assert [e["action"] for e in body["events"]] == ["ready", "speaking_start"]
        assert body["cursor"] == 2
        assert set(body["events"][0].keys()) == {"seq", "ts", "source", "action", "detail"}

        resp2 = client.get("/api/events", params={"since": body["cursor"]})
        body2 = resp2.json()
        assert body2["events"] == []
        assert body2["cursor"] == 2  # unchanged, no new events since
