"""`web_searched` is a per-turn VALUE, never shared instance state.

The flag used to live on the motor (`_current_turn_web_searched`), read and
cleared non-atomically by `_emit_dialogue`, which pregen daemon threads also
call. A pregen emit could therefore steal or clear a foreground web turn's
flag. These tests pin the per-turn contract.
"""
from __future__ import annotations

import queue
import threading
from types import SimpleNamespace

import pytest

from opencohost.config import web_fetch
from opencohost.core import llm_engine
from opencohost.core.llm_engine import MotorVocalIA
from tests.test_decision_engine_wiring import _motor
from tests.test_web_lookup import Net

ASK = "busca en internet zanahoriaconfidencial parche nuevo"
PLAIN = "hola Kira zanahoriaconfidencial"


@pytest.fixture
def web_on():
    web_fetch.set_enabled(True)
    yield
    web_fetch.set_enabled(False)


def _bare_motor():
    return MotorVocalIA(queue.Queue(), lambda event: None)


def test_pregen_emit_never_steals_or_inherits_a_foreground_flag():
    motor = _bare_motor()
    calls: list[tuple[str, dict]] = []
    lock = threading.Lock()

    def _cb(text, source, **kwargs):
        with lock:
            calls.append((source, kwargs))
        return 1

    motor.dialogue_callback = _cb
    # A foreground web turn is "in flight" (legacy shared flag, if it still exists).
    motor._current_turn_web_searched = True

    pregen = threading.Thread(
        target=lambda: motor._emit_dialogue("pregen draft", "kira-agenda")
    )
    pregen.start()
    pregen.join(5.0)
    motor._emit_dialogue("web answer", "direct", web_searched=True)
    # A later pregen emit that omits the value must also stay False.
    motor._emit_dialogue("pregen 2", "kira-agenda")

    by_text = {src: kw for src, kw in calls}
    assert len(calls) == 3
    assert calls[0] == ("kira-agenda", {})  # pregen: no web flag, did not consume anything
    assert calls[1] == ("direct", {"web_searched": True})  # foreground keeps its own flag
    assert calls[2] == ("kira-agenda", {})
    assert by_text  # sanity


def test_emit_dialogue_passes_web_searched_with_queue_wait():
    motor = _bare_motor()
    seen: list[dict] = []
    motor.dialogue_callback = lambda text, source, **kw: seen.append(kw) or 1
    motor._emit_dialogue("t", "direct", queue_wait_ms=5, web_searched=True)
    motor._emit_dialogue("t", "direct", queue_wait_ms=5)
    assert seen[0] == {"queue_wait_ms": 5, "web_searched": True}
    assert seen[1] == {"queue_wait_ms": 5}


def test_stream_chunk_uses_the_turn_value_not_instance_state():
    motor = _bare_motor()
    got: list[dict] = []

    class _Sink:
        def record_stream_chunk(self, **kw):
            got.append(kw)
            return 7

        def record(self, text, source, **kw):
            return 7

    sink = _Sink()
    motor.dialogue_callback = sink.record
    motor._current_turn_web_searched = True  # must be ignored
    job = SimpleNamespace(job_id=1, chunks=["a"], turn_id=None, feed_turn_id=None)
    state = SimpleNamespace(
        job=job, source="direct", raw_sentences=["a"], web_searched=False,
    )
    motor._emit_stream_chunk(state)
    state.web_searched = True
    motor._emit_stream_chunk(state)
    assert [kw["web_searched"] for kw in got] == [False, True]


def test_turn_result_carries_web_searched_for_a_search_turn(monkeypatch, web_on):
    motor, _ = _motor(monkeypatch)
    motor._web_fetcher_factory = Net().fetcher
    result = motor._generar_dialogo(ASK, source="direct", commit_history=True)
    assert result.web_searched is True


def test_turn_result_is_not_web_searched_for_a_plain_turn(monkeypatch, web_on):
    motor, _ = _motor(monkeypatch)
    motor._web_fetcher_factory = Net().fetcher
    result = motor._generar_dialogo(PLAIN, source="direct", commit_history=True)
    assert result.web_searched is False


def test_web_step_marks_the_duckduckgo_lookup_as_searched(monkeypatch, web_on):
    motor, _ = _motor(monkeypatch)
    motor._web_fetcher_factory = Net().fetcher
    look = motor._run_web_step(ASK, "direct")
    assert look.searched is True
    assert not hasattr(motor, "_current_turn_web_searched")


def test_web_step_off_is_not_searched(monkeypatch):
    web_fetch.set_enabled(False)
    motor, _ = _motor(monkeypatch)
    look = motor._run_web_step(ASK, "direct")
    assert look.searched is False
