"""Failure reason codes in the turn event (turn-events-20261112 T3).

Each outcome-changing ``except`` handler on the foreground path records
``{"reason": <fixed code>, "exc": <exception class name>}`` into the turn
accumulator, never the exception message.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from opencohost.config import settings
from opencohost.core.engine import turn_events, turn_state
from tests.fakes.fake_ollama import ChatScript
from tests.test_llm_is_local_gating import _SEND, _cloud_config, _make_motor
from tests.test_streaming_adverse_path_fake_ollama import (  # noqa: F401  (fixtures)
    SPOKEN,
    TAIL,
    _run_bounded,
    engine,
    fake_ollama,
)

SENTINEL = "SENTINEL-exc-text-5d2e"


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


def _reasons(path) -> list:
    (event,) = _events(path)
    assert SENTINEL not in path.read_text(encoding="utf-8")
    return [(r["reason"], r["exc"]) for r in event["reasons"]]


def _turn(engine, caplog):
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        engine.enqueue("hola", priority=0, source="direct")
        try:
            _run_bounded(engine._process_priority_queue)
        except Exception:
            pass


def test_catch_all_records_engine_error_with_the_class_only(engine, fake_ollama, caplog, events_file, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(engine, "_build_generation_request", boom)

    _turn(engine, caplog)

    assert _reasons(events_file) == [("engine_error", "RuntimeError")]


def test_http_500_records_stream_error_then_engine_error(engine, fake_ollama, caplog, events_file):
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN, mode="http_500"))

    _turn(engine, caplog)

    codes = [reason for reason, _ in _reasons(events_file)]
    assert codes == ["stream_error", "engine_error"]
    assert _events(events_file)[0]["outcome"] == "failed"


def test_idle_stall_records_watchdog_timeout(engine, fake_ollama, caplog, events_file):
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN + TAIL, mode="stall", after=2))

    _turn(engine, caplog)

    reasons = _reasons(events_file)
    assert ("watchdog_timeout", "TimeoutError") in reasons
    assert _events(events_file)[0]["outcome"] == "dropped"


def test_cut_stream_is_recorded_without_the_message(engine, fake_ollama, caplog, events_file):
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN + TAIL, mode="cut", after=2))

    _turn(engine, caplog)

    (event,) = _events(events_file)
    assert event["reasons"], event
    assert all(r["reason"] in turn_events.REASON_CODES for r in event["reasons"])


def test_local_transport_error_records_ollama_transport_error(
    engine, fake_ollama, caplog, events_file, monkeypatch
):
    def boom(*_a, **_k):
        raise ConnectionError(SENTINEL)

    monkeypatch.setattr(engine._generation_orchestrator, "run_streaming_attempt", boom)

    _turn(engine, caplog)

    assert ("ollama_transport_error", "ConnectionError") in _reasons(events_file)


def test_cloud_transport_error_records_the_cloud_class(tmp_path, events_file):
    from opencohost.core.providers.cloud.cloud_llm_client import CloudLLMResponseError

    motor, _, _ = _make_motor(tmp_path, provider_config=_cloud_config())
    motor._handle_cloud_failure = lambda *a, **k: None
    turn_state.open_turn("direct")
    try:
        with patch(_SEND, side_effect=CloudLLMResponseError(SENTINEL)):
            motor._generar_dialogo("hola", source="direct", commit_history=True)
    finally:
        turn_state.close_turn()

    reasons = _reasons(events_file)
    assert ("cloud_transient", "CloudLLMResponseError") in reasons


def test_escaping_exception_records_turn_aborted(engine, fake_ollama, caplog, events_file, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(engine, "_ejecutar_inferencia", boom)

    _turn(engine, caplog)

    assert _reasons(events_file) == [("turn_aborted", "RuntimeError")]


def test_router_off_speech_failure_records_tts_submit_failed(
    engine, fake_ollama, caplog, events_file, monkeypatch
):
    engine._speech_router_enabled = False

    def boom(*_a, **_k):
        raise OSError(SENTINEL)

    monkeypatch.setattr(engine, "_hablar", boom)
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN))

    _turn(engine, caplog)

    assert ("tts_submit_failed", "OSError") in _reasons(events_file)


def test_dialogue_callback_failure_is_recorded(engine, fake_ollama, caplog, events_file):
    def boom(*_a, **_k):
        raise ValueError(SENTINEL)

    engine.dialogue_callback = boom
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN))

    _turn(engine, caplog)

    assert ("dialogue_callback_failed", "ValueError") in _reasons(events_file)
    assert _events(events_file)[0]["outcome"] == "completed"


def test_guardrail_retry_failure_is_recorded(engine, events_file, monkeypatch):
    def boom(*_a, **_k):
        raise ConnectionError(SENTINEL)

    monkeypatch.setattr(engine, "_ollama_chat_with_watchdog", boom)
    turn_state.open_turn("direct")
    try:
        result = engine._retry_after_guard_block(
            messages=[{"role": "user", "content": "hola"}],
            opciones_llm={},
            request_model="llama3",
            chat_timeout=1.0,
            provider_cfg={},
            is_local=True,
        )
    finally:
        turn_state.close_turn()

    assert result == ""
    assert ("guard_retry_failed", "ConnectionError") in _reasons(events_file)


def test_seal_failure_is_recorded(engine, events_file):
    class Router:
        def seal(self, _job):
            raise RuntimeError(SENTINEL)

    state = SimpleNamespace(
        router=Router(), job=object(), spoken_prefix=lambda: "", display_prefix=lambda: "",
    )
    turn_state.open_turn("direct")
    try:
        try:
            engine._generation_orchestrator.stream_partial_exit(state, reason="x")
        except Exception:
            pass  # the rest of the exit needs a full state; only the seal matters here
    finally:
        turn_state.close_turn()

    assert ("stream_seal_failed", "RuntimeError") in _reasons(events_file)


def test_background_work_records_no_reason(engine, fake_ollama, events_file, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError(SENTINEL)

    monkeypatch.setattr(engine, "_build_generation_request", boom)

    _run_bounded(lambda: engine._generar_dialogo("hola", source="direct", commit_history=False))

    assert _events(events_file) == []


def test_reason_codes_outside_the_enum_are_dropped_and_helper_never_raises():
    turn_state.fail_reason("engine_error", RuntimeError(SENTINEL))  # no turn: a no-op
    turn = turn_state.open_turn("direct")
    try:
        turn_state.fail_reason("not a code " + SENTINEL, RuntimeError(SENTINEL))
        turn_state.fail_reason("engine_error", ValueError(SENTINEL))
    finally:
        turn_state.close_turn()

    event = turn_events.sanitize(turn.to_event())
    # The turn was left open, so close adds turn_unclosed; the bad code is gone.
    assert event["reasons"] == [
        {"reason": "engine_error", "exc": "ValueError"},
        {"reason": "turn_unclosed", "exc": "none"},
    ]
    assert SENTINEL not in json.dumps(event)
