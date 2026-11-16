"""One structured JSONL event per foreground turn (turn-events-20261112 T2)."""

from __future__ import annotations

import json
import logging
import re

import pytest

from opencohost.config import settings
from opencohost.core.engine import turn_events, turn_state
from opencohost.core.engine.turn_state import TurnEvent
from opencohost.core.turn_scheduler import ExpiredTurn
from tests.fakes.fake_ollama import ChatScript
from tests.test_streaming_adverse_path_fake_ollama import (  # noqa: F401  (fixtures)
    SPOKEN,
    _run_bounded,
    engine,
    fake_ollama,
)

SENTINEL = "SENTINEL-reply-text-91bc"
TID_RE = re.compile(r"\btid=(t[0-9a-f]{8})\b")


@pytest.fixture
def events_file(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LOG_DIR", str(tmp_path))
    yield tmp_path / turn_events.FILE_NAME
    sink = turn_events._sink
    if sink is not None:
        for handler in list(sink.handlers):
            handler.close()
            sink.removeHandler(handler)
    turn_events._sink = None
    turn_events._sink_path = None


def _events(path) -> list:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _run_turn(engine, caplog, text="hola"):
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        engine.enqueue(text, priority=0, source="direct")
        _run_bounded(engine._process_priority_queue)


# --- allowlist ---------------------------------------------------------------


def test_sanitize_drops_unknown_keys_and_free_text():
    raw = {
        "tid": "t0123abcd",
        "source": "direct",
        "prompt": SENTINEL,
        "reply_text": SENTINEL,
        "outcome": "completed because the user said " + SENTINEL,  # free text, not a code
        "model": "llama3:8b",
        "phases": ["assembling", "Not A Code", SENTINEL],
        "events": {"begin": 1, SENTINEL: 2, "bad": "x"},
        "timings": {"llm_ms": 12, "free_text": 5, "ttfa_ms": -1, "recall_ms": True},
        "reasons": [{"reason": "engine_error", "exc": "RuntimeError"}, {"reason": SENTINEL, "exc": "X"}],
        "attempts": "3",
        "cloud_retry": 1,
    }

    event = turn_events.sanitize(raw)

    assert event == {
        "tid": "t0123abcd",
        "source": "direct",
        "model": "llama3:8b",
        "phases": ["assembling"],
        "events": {"begin": 1},
        "timings": {"llm_ms": 12},
        "reasons": [{"reason": "engine_error", "exc": "RuntimeError"}],
    }
    assert SENTINEL not in json.dumps(event)


def test_write_requires_a_valid_tid_and_never_raises(events_file, monkeypatch):
    assert turn_events.write({"source": "direct"}) is False
    assert turn_events.write("not a dict") is False
    assert not events_file.exists()
    # An unwritable location is swallowed, not raised.
    blocker = events_file.parent / "blocker"
    blocker.write_text("x")
    monkeypatch.setattr(settings, "LOG_DIR", str(blocker / "sub"))
    assert turn_events.write({"tid": "t0123abcd"}) is False


def test_the_file_is_size_bounded_and_rotated(events_file):
    assert turn_events.write({"tid": "t0123abcd", "source": "direct"}) is True
    handler = turn_events._sink.handlers[0]
    assert handler.maxBytes == 5 * 1024 * 1024
    assert handler.backupCount == 3
    assert turn_events._sink.propagate is False
    assert _events(events_file)[0]["v"] == 1


# --- engine ------------------------------------------------------------------


def test_one_event_per_foreground_turn_joins_every_per_turn_line(
    engine, fake_ollama, caplog, events_file
):
    fake_ollama.script_chat(ChatScript(chunks=[SENTINEL + ". ", "Segunda frase tranquila. "]))

    _run_turn(engine, caplog, "pregunta " + SENTINEL)

    events = _events(events_file)
    assert len(events) == 1
    event = events[0]
    assert event["outcome"] == "completed"
    assert event["source"] == "direct"
    assert event["model"] == engine.current_model
    assert event["phases"][0] == "assembling" and event["phases"][-1] == "completed"
    assert event["events"]["prompt_built"] == 1 and event["events"]["complete"] == 1
    assert event["attempts"] == 1 and event["cloud_retry"] is False
    assert event["timings"]["engine_ms"] >= 0 and "llm_ms" in event["timings"]
    assert event["latency_outcome"] == "ok" and event["path"] == "stream"
    assert SENTINEL not in events_file.read_text(encoding="utf-8")

    tid = event["tid"]
    messages = [r.getMessage() for r in caplog.records]
    for tag in ("[TURN_STATE]", "[TURN_LATENCY]", "[PROMPT_TRACE]"):
        lines = [m for m in messages if m.startswith(tag)]
        assert lines, tag
        assert all(f"tid={tid}" in m for m in lines), (tag, lines)
    trace = [m for m in messages if m.startswith("[PROMPT_TRACE]")][0]
    assert re.search(r"\bturn=[0-9a-f]{8} ", trace)  # the per-call id is unchanged


def test_each_turn_writes_its_own_line(engine, fake_ollama, caplog, events_file):
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN, mode="http_500"))
    fake_ollama.script_chat(ChatScript(chunks=["Vuelvo a estar. "]))

    _run_turn(engine, caplog, "uno")
    _run_turn(engine, caplog, "dos")

    events = _events(events_file)
    assert [e["outcome"] for e in events] == ["failed", "completed"]
    assert events[0]["tid"] != events[1]["tid"]


def test_guard_verdicts_are_recorded(engine, fake_ollama, caplog, events_file):
    fake_ollama.script_chat(ChatScript(chunks=["Como IA, no puedo opinar. ", "Y sigo hablando. "]))
    fake_ollama.script_chat(ChatScript(chunks=["Un coche tiene chasis y motor."]))

    _run_turn(engine, caplog)

    (event,) = _events(events_file)
    assert event["guards"] == ["guard_revert", "guard_block", "guard_retry"]
    assert event["attempts"] == 2


def test_background_pregen_writes_nothing(engine, fake_ollama, events_file):
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN))

    _run_bounded(lambda: engine._generar_dialogo("hola", source="direct", commit_history=False))

    assert _events(events_file) == []


def test_refused_turn_writes_an_event(engine, fake_ollama, caplog, events_file):
    engine.is_ready = False
    engine._turn_recover_next_at = float("inf")

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        _run_bounded(lambda: engine._dispatch_command("process_context", "hola", source="direct"))

    (event,) = _events(events_file)
    assert event["outcome"] == "refused"
    assert event["phases"] == ["assembling", "refused"]


def test_expired_turn_writes_an_event(engine, fake_ollama, caplog, events_file, monkeypatch):
    scheduler = engine._turn_scheduler
    expired = ExpiredTurn(item=(3, 0.0, "viejo", "direct"), age_seconds=99.0, ttl_seconds=30.0)
    calls = {"n": 0}

    def expire_once(*_a, **_k):
        calls["n"] += 1
        return (expired,) if calls["n"] == 1 else ()

    monkeypatch.setattr(scheduler, "expire", expire_once)

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        _run_bounded(engine._process_priority_queue)

    (event,) = _events(events_file)
    assert event["outcome"] == "expired"


def test_pregen_served_turn_event_has_tid_on_latency_line(
    engine, fake_ollama, caplog, events_file, monkeypatch
):
    monkeypatch.setattr(engine, "_take_pregen_if_match", lambda payload, source: {"text": "listo"})
    monkeypatch.setattr(engine, "_speak_pregenerated", lambda cached, **_k: None)

    _run_turn(engine, caplog)

    (event,) = _events(events_file)
    assert event["outcome"] == "completed" and event["events"].get("served_pregen") == 1


def test_event_write_failure_never_breaks_a_turn(engine, fake_ollama, caplog, monkeypatch):
    def boom(_event):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(turn_events, "write", boom)
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN))

    _run_turn(engine, caplog)

    assert engine.historial[-1]["content"] == "Primera frase limpia. Segunda frase tranquila."


def test_cloud_retry_and_speech_are_accumulated():
    turn = turn_state.TurnState("t0123abcd", "direct")
    turn.apply(TurnEvent.PROMPT_BUILT)
    turn.apply(TurnEvent.CLOUD_RETRY)
    turn.apply(TurnEvent.PROMPT_BUILT)
    turn.apply(TurnEvent.COMPLETE)

    event = turn_events.sanitize(turn.to_event())

    assert event["cloud_retry"] is True
    assert event["attempts"] == 2
    assert event["phases"] == ["assembling", "generating", "assembling", "generating", "completed"]
