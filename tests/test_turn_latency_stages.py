"""Per-stage fields on the foreground ``[TURN_LATENCY]`` line.

Each stage reuses a duration the turn already measures (memory recall, the web
lookup's fetch and filter, the LLM call, the streamed first-audio submit) and
records it into a sink that only the foreground turn opens. Stages that did not
run print as ``-``. The line stays one line, carries no reply text, and keeps
every field it already had.
"""

from __future__ import annotations

import logging
import queue
import re
import time
from unittest.mock import MagicMock

import pytest

from opencohost.core import llm_engine
from opencohost.core.engine import llm_engine_web, turn_timing
from opencohost.core.memory import episodic_merge
from opencohost.core.scheduling.turn_stamp import TurnStamp
from opencohost.core.web.lookup import OUTCOME_OK, WebLookupResult
from opencohost.mcp_local import logparse

REPLY = "Todo tranquilo por acá."


def _resp(text):
    return {"message": {"content": text}}


def _motor(dialogue_callback=None):
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None,
                                    dialogue_callback=dialogue_callback)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    motor.current_model = "llama3"
    motor._reasoning_model_cache["llama3"] = False
    motor._hablar = MagicMock()
    return motor


def _latency_line(caplog) -> str:
    lines = [r.getMessage() for r in caplog.records if "[TURN_LATENCY]" in r.getMessage()]
    assert len(lines) == 1, lines
    return lines[0]


def _field(line: str, key: str) -> str:
    m = re.search(rf" {key}=(\S+)", line)
    assert m is not None, f"{key} missing from {line!r}"
    return m.group(1)


# ---------------------------------------------------------------- pure helpers


def test_stage_fields_print_a_dash_for_every_stage_that_did_not_run():
    assert turn_timing.stage_fields({}) == "recall_ms=- web_ms=- prompt_build_ms=- llm_ms=- ttfa_ms=-"
    assert turn_timing.stage_fields({"llm_ms": 12}) == (
        "recall_ms=- web_ms=- prompt_build_ms=- llm_ms=12 ttfa_ms=-"
    )


def test_stages_recorded_off_turn_are_dropped_and_nested_turns_restore_the_outer_sink():
    turn_timing.add("llm_ms", 50)  # a pregen or agenda thread has no turn clock

    with turn_timing.turn_clock() as outer:
        with turn_timing.turn_clock() as inner:
            turn_timing.add("llm_ms", 5)
        turn_timing.add("ttfa_ms", 7)

    assert inner == {"llm_ms": 5}
    assert outer == {"ttfa_ms": 7}


def test_prompt_build_excludes_the_recall_that_ran_inside_it(monkeypatch):
    clock = {"t": 10.0}
    monkeypatch.setattr(turn_timing.time, "monotonic", lambda: clock["t"])

    with turn_timing.turn_clock() as sink:
        token = turn_timing.mark(exclude="recall_ms")
        clock["t"] += 0.125  # a 125 ms request build (exact in binary floating point)
        turn_timing.add("recall_ms", 40)  # recall measured inside that build
        turn_timing.close(token, "prompt_build_ms")

    assert sink == {"recall_ms": 40, "prompt_build_ms": 85}


# ------------------------------------------------------- the line, both paths


def test_buffered_turn_line_carries_every_stage_in_order_and_no_reply_text(caplog):
    motor = _motor()
    motor._ollama_chat = MagicMock(return_value=_resp(REPLY))

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._ejecutar_inferencia("hola", source="direct", stamp=TurnStamp(submitted_at=time.monotonic()))

    line = _latency_line(caplog)
    order = [line.index(f" {key}=") for key in (
        "request_to_tts_ms", "queue_wait_ms", "request_to_tts_total_ms",
        "recall_ms", "web_ms", "prompt_build_ms", "llm_ms", "ttfa_ms", "path",
    )]
    assert order == sorted(order)
    assert _field(line, "path") == "buffered"
    assert _field(line, "ttfa_ms") == "-"  # no first-audio submit on the buffered path
    assert _field(line, "web_ms") == "-"  # the web lookup is off in the suite
    assert _field(line, "llm_ms").isdigit()
    assert _field(line, "prompt_build_ms").isdigit()
    assert REPLY not in line
    assert "\n" not in line


def test_unstamped_buffered_turn_keeps_the_old_shape_without_queue_wait(caplog):
    motor = _motor()
    motor._ollama_chat = MagicMock(return_value=_resp(REPLY))

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._ejecutar_inferencia("hola", source="direct")

    line = _latency_line(caplog)
    assert "request_to_tts_ms=" in line
    assert "queue_wait_ms" not in line  # an unstamped turn never fakes a wait
    assert _field(line, "path") == "buffered"


@pytest.fixture
def stream_motor(monkeypatch):
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)
    motor.current_model = "llama3"
    motor.use_system_role = True
    motor.ollama = MagicMock()
    motor._speech_router_enabled = True
    monkeypatch.setattr(llm_engine, "LLM_STREAMING_ENABLED", True)

    class _Job:
        def __init__(self, job_id):
            self.job_id = job_id
            self.sealed = False

    class _Router:
        def __init__(self):
            self.jobs = []

        def submit_streaming(self, source, priority):
            job = _Job(len(self.jobs) + 1)
            self.jobs.append(job)
            return job

        def append_chunks(self, job, chunks):
            return True

        def seal(self, job):
            job.sealed = True

    router = _Router()
    motor._ensure_router = lambda: router
    motor._speech_router = router
    return motor


def _arm_two_sentence_stream(motor):
    class _Msg:
        def __init__(self, content):
            self.content = content
            self.thinking = None

    class _Chunk:
        def __init__(self, content="", done=False):
            self.message = _Msg(content)
            self.done = done

        def get(self, key, default=None):
            return getattr(self, key, default)

    chunks = [_Chunk("Primera frase limpia. "), _Chunk("Segunda frase tranquila."), _Chunk("", done=True)]

    def _streaming(*, timeout, **kwargs):
        def _gen():
            yield from chunks

        return _gen()

    motor._ollama_chat_streaming = _streaming


def test_streamed_turn_line_reports_path_stream_with_first_audio_and_llm_stages(stream_motor, caplog):
    _arm_two_sentence_stream(stream_motor)
    stream_motor.dialogue_callback = lambda text, source, **kw: None

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        stream_motor._ejecutar_inferencia("hola", source="direct")

    line = _latency_line(caplog)
    assert _field(line, "path") == "stream"
    assert _field(line, "ttfa_ms").isdigit()
    assert _field(line, "llm_ms").isdigit()
    assert _field(line, "prompt_build_ms").isdigit()
    assert _field(line, "recall_ms") == "-"
    assert _field(line, "web_ms") == "-"
    assert "Primera frase" not in line
    assert "Segunda frase" not in line


# ------------------------------------------------- reused measured values


def test_recall_stage_reuses_the_recall_outcome_ms_only_when_recall_ran():
    motor = _motor()
    outcome = episodic_merge.RecallOutcome((), "hit", "episodic", 0.9, 37, "none")

    with turn_timing.turn_clock() as sink:
        motor._log_recall_telemetry(outcome, {"called": True})
    assert sink["recall_ms"] == 37

    with turn_timing.turn_clock() as skipped:
        motor._log_recall_telemetry(outcome, {"called": False})
    assert "recall_ms" not in skipped


def test_web_stage_reuses_the_lookup_fetch_and_filter_ms(monkeypatch):
    motor = _motor()
    monkeypatch.setattr(motor, "_web_enabled", lambda: True)
    monkeypatch.setattr(motor, "_web_ledger_add", lambda *a, **k: None)
    monkeypatch.setattr(motor, "_web_followup_subject", lambda *a, **k: "clima en Madrid")
    monkeypatch.setattr(llm_engine_web, "is_bare_search_command", lambda clean: True)
    monkeypatch.setattr(
        motor, "_web_lookup",
        lambda question, query: WebLookupResult(OUTCOME_OK, block="Resultado.", fetch_ms=120, filter_ms=30),
    )

    with turn_timing.turn_clock() as sink:
        motor._run_web_step("busca", "direct")
    assert sink["web_ms"] == 150


def test_web_stage_is_absent_when_no_lookup_ran(monkeypatch):
    motor = _motor()
    monkeypatch.setattr(motor, "_web_enabled", lambda: True)

    with turn_timing.turn_clock() as sink:
        motor._run_web_step("hola", "kira-agenda")  # not a web source: no lookup
    assert "web_ms" not in sink


# ------------------------------------------------------ mcp_local parser


def test_logparse_summarizes_each_stage_and_ignores_dashes():
    lines = [
        "[12:00:00] [INFO] [OpenCohost] [TURN_LATENCY] source=direct request_to_tts_ms=900 "
        "queue_wait_ms=10 request_to_tts_total_ms=910 recall_ms=- web_ms=- prompt_build_ms=30 "
        "llm_ms=800 ttfa_ms=- path=buffered",
        "[12:00:05] [INFO] [OpenCohost] [TURN_LATENCY] source=direct request_to_tts_ms=500 "
        "recall_ms=20 web_ms=- prompt_build_ms=40 llm_ms=400 ttfa_ms=250 path=stream",
    ]

    summary = logparse.summarize(lines)

    stages = summary["turn_latency_stages"]
    assert stages["llm_ms"]["count"] == 2
    assert stages["llm_ms"]["median"] == 600
    assert stages["ttfa_ms"]["count"] == 1
    assert "web_ms" not in stages
    assert summary["turn_latency_path"] == {"buffered": 1, "stream": 1}
    assert summary["turn_latency_ms"]["direct"]["count"] == 2  # the existing total is unchanged


def test_logparse_reads_a_line_with_outcome_without_changing_the_stage_summary():
    lines = [
        "[12:00:00] [INFO] [OpenCohost] [TURN_LATENCY] source=direct request_to_tts_ms=900 "
        "queue_wait_ms=10 request_to_tts_total_ms=910 recall_ms=- web_ms=- prompt_build_ms=30 "
        "llm_ms=800 ttfa_ms=- path=buffered outcome=retried",
    ]

    summary = logparse.summarize(lines)

    assert summary["malformed_lines"] == 0
    assert summary["turn_latency_stages"]["llm_ms"]["median"] == 800
    assert summary["turn_latency_path"] == {"buffered": 1}
    assert summary["turn_latency_ms"]["direct"]["count"] == 1


# ------------------------------------------------- outcome on every exit

CODE_REPLY = "Claro:\n```python\nprint('hola')\n```"
SPOKEN_RETRY = "Mejor te lo explico hablando, sin leer símbolos."


def test_stage_fields_and_off_turn_marks_ignore_the_outcome_key():
    turn_timing.mark_outcome("retried")  # no turn clock open: a no-op

    with turn_timing.turn_clock() as sink:
        turn_timing.mark_outcome("fallback")

    assert sink == {"outcome": "fallback"}
    assert turn_timing.stage_fields(sink) == "recall_ms=- web_ms=- prompt_build_ms=- llm_ms=- ttfa_ms=-"


def test_clean_turn_line_reports_outcome_ok(caplog):
    motor = _motor()
    motor._ollama_chat = MagicMock(return_value=_resp(REPLY))

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._ejecutar_inferencia("hola", source="direct")

    assert _field(_latency_line(caplog), "outcome") == "ok"


def test_guardrail_retry_turn_reports_outcome_retried(caplog):
    motor = _motor()
    motor._ollama_chat_with_watchdog = MagicMock(side_effect=[
        {"message": {"content": CODE_REPLY}},
        {"message": {"content": SPOKEN_RETRY}},
    ])

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._ejecutar_inferencia("dame el codigo", source="direct")

    line = _latency_line(caplog)
    assert _field(line, "outcome") == "retried"
    assert SPOKEN_RETRY not in line
    assert CODE_REPLY not in line


def test_guardrail_fallback_turn_reports_outcome_fallback(caplog):
    motor = _motor()
    motor._ollama_chat_with_watchdog = MagicMock(return_value={"message": {"content": CODE_REPLY}})

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._ejecutar_inferencia("dame el codigo", source="direct")

    assert _field(_latency_line(caplog), "outcome") == "fallback"


def test_dropped_turn_still_emits_the_latency_line_with_outcome_dropped(caplog):
    motor = _motor()
    motor._ollama_chat_with_watchdog = MagicMock(return_value={"message": {"content": ""}})

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        motor._ejecutar_inferencia("hola", source="direct", stamp=TurnStamp(submitted_at=time.monotonic()))

    line = _latency_line(caplog)
    assert _field(line, "outcome") == "dropped"
    assert _field(line, "path") == "buffered"
    assert _field(line, "queue_wait_ms").isdigit()
    assert _field(line, "llm_ms").isdigit()
