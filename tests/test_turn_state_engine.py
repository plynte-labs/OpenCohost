"""Engine-thread TurnState instrumentation (turn-state-machine-20261112 T2).

Drives real foreground turns through ``_process_priority_queue`` ->
``_ejecutar_inferencia`` against the fake Ollama server (same harness as the
streaming adverse-path gate) and asserts the ``[TURN_STATE]`` chain: every
turn starts at ``begin`` and ends in exactly one terminal line.
"""

from __future__ import annotations

import logging
import re
import threading

from opencohost.core.engine import turn_state
from opencohost.core.engine.turn_state import TurnEvent
from opencohost.core.turn_scheduler import ExpiredTurn
from tests.fakes.fake_ollama import ChatScript
from tests.test_streaming_adverse_path_fake_ollama import (  # noqa: F401  (fixtures)
    GOOD_MODEL,
    SPOKEN,
    TAIL,
    _run_bounded,
    engine,
    fake_ollama,
)

LINE_RE = re.compile(
    r"^\[TURN_STATE\] tid=(?P<turn>t[0-9a-f]{8}) source=(?P<source>[A-Za-z0-9_.:-]{1,40}) "
    r"(?P<from>[a-z_]+)->(?P<to>[a-z_]+) event=(?P<event>[a-z_]+)( (ms|attempt)=\d+)*$"
)
TERMINALS = {"completed", "dropped", "failed", "refused", "expired"}


def _turn_lines(caplog):
    lines = [r.getMessage() for r in caplog.records if r.getMessage().startswith("[TURN_STATE]")]
    assert not [line for line in lines if "illegal" in line], lines
    parsed = []
    for line in lines:
        match = LINE_RE.match(line)
        assert match, line
        parsed.append(match.groupdict())
    return parsed


def _run_turn(engine, caplog, text="hola"):
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        engine.enqueue(text, priority=0, source="direct")
        _run_bounded(engine._process_priority_queue)
    return _turn_lines(caplog)


def _assert_one_turn(parsed, events, terminal):
    assert [p["event"] for p in parsed] == events, parsed
    assert len({p["turn"] for p in parsed}) == 1
    assert {p["source"] for p in parsed} == {"direct"}
    assert [p["to"] for p in parsed if p["to"] in TERMINALS] == [terminal]
    assert parsed[0]["from"] == "start" and parsed[0]["to"] == "assembling"


def test_normal_streamed_turn_ends_completed(engine, fake_ollama, caplog):
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN))

    parsed = _run_turn(engine, caplog)

    _assert_one_turn(parsed, ["begin", "prompt_built", "first_audio", "complete"], "completed")
    assert [p["to"] for p in parsed] == ["assembling", "generating", "streaming", "completed"]
    joined = " ".join(r.getMessage() for r in caplog.records if "[TURN_STATE]" in r.getMessage())
    assert "Primera" not in joined and "hola" not in joined


def test_idle_stall_recovers_then_drops(engine, fake_ollama, caplog):
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN + TAIL, mode="stall", after=2))

    parsed = _run_turn(engine, caplog)

    _assert_one_turn(
        parsed,
        ["begin", "prompt_built", "first_audio", "stall", "rollback", "drop"],
        "dropped",
    )
    assert engine.current_model == GOOD_MODEL


def test_guard_revert_blocks_retries_and_completes(engine, fake_ollama, caplog):
    fake_ollama.script_chat(ChatScript(chunks=["Como IA, no puedo opinar. ", "Y sigo hablando. "]))
    fake_ollama.script_chat(ChatScript(chunks=["Un coche tiene chasis y motor."]))

    parsed = _run_turn(engine, caplog)

    _assert_one_turn(
        parsed,
        ["begin", "prompt_built", "guard_revert", "guard_block", "guard_retry", "complete"],
        "completed",
    )
    assert [p["to"] for p in parsed][2:] == ["reverted", "guard_blocked", "generating", "completed"]


def test_guard_revert_with_blocked_retry_ends_on_the_fallback_line(engine, fake_ollama, caplog):
    fake_ollama.script_chat(ChatScript(chunks=["Como IA, no puedo opinar. ", "Y sigo hablando. "]))
    fake_ollama.script_chat(ChatScript(chunks=["Como IA, no puedo opinar."]))

    parsed = _run_turn(engine, caplog)

    _assert_one_turn(
        parsed,
        ["begin", "prompt_built", "guard_revert", "guard_block", "guard_retry", "guard_block", "complete"],
        "completed",
    )


def test_empty_reply_turn_is_dropped(engine, fake_ollama, caplog):
    for _ in range(5):  # every in-turn retry gets an empty reply too
        fake_ollama.script_chat(ChatScript(chunks=[""]))

    parsed = _run_turn(engine, caplog)

    _assert_one_turn(parsed, ["begin", "prompt_built", "drop"], "dropped")


def test_http_500_raises_into_the_catch_all_and_is_failed_not_dropped(engine, fake_ollama, caplog):
    # A 500 is not a transport error: it reaches _generar_dialogo's catch-all,
    # which returns an empty result. The trace says failed, once.
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN, mode="http_500"))

    parsed = _run_turn(engine, caplog)

    _assert_one_turn(parsed, ["begin", "prompt_built", "fail"], "failed")


def test_engine_error_is_failed_with_one_terminal_line(engine, fake_ollama, caplog, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("build failed")

    monkeypatch.setattr(engine, "_build_generation_request", boom)

    parsed = _run_turn(engine, caplog)

    _assert_one_turn(parsed, ["begin", "fail"], "failed")


def test_exception_escaping_the_turn_still_closes_it_as_failed(engine, fake_ollama, caplog, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("tail failed")

    monkeypatch.setattr(engine, "_ejecutar_inferencia", boom)

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        engine.enqueue("hola", priority=0, source="direct")
        try:
            _run_bounded(engine._process_priority_queue)
        except RuntimeError:
            pass

    _assert_one_turn(_turn_lines(caplog), ["begin", "fail"], "failed")


def test_dispatched_turn_is_traced_and_closed(engine, fake_ollama, caplog):
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN))

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        _run_bounded(lambda: engine._dispatch_command("process_context", "hola", source="direct"))

    _assert_one_turn(
        _turn_lines(caplog), ["begin", "prompt_built", "first_audio", "complete"], "completed"
    )


def test_refused_turn_when_ollama_is_not_ready(engine, fake_ollama, caplog):
    engine.is_ready = False
    engine._turn_recover_next_at = float("inf")  # no readiness probe

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        _run_bounded(lambda: engine._dispatch_command("process_context", "hola", source="direct"))

    _assert_one_turn(_turn_lines(caplog), ["begin", "refuse"], "refused")


def test_expired_queue_item_is_its_own_expired_turn(engine, fake_ollama, caplog, monkeypatch):
    scheduler = engine._turn_scheduler
    expired = ExpiredTurn(item=(3, 0.0, "viejo", "direct"), age_seconds=99.0, ttl_seconds=30.0)
    calls = {"n": 0}

    def expire_once(*_a, **_k):
        calls["n"] += 1
        return (expired,) if calls["n"] == 1 else ()

    monkeypatch.setattr(scheduler, "expire", expire_once)

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        _run_bounded(engine._process_priority_queue)

    _assert_one_turn(_turn_lines(caplog), ["begin", "expire"], "expired")


def test_pregen_cache_hit_is_served_pregen(engine, fake_ollama, caplog, monkeypatch):
    spoken = []
    monkeypatch.setattr(engine, "_take_pregen_if_match", lambda payload, source: {"text": "listo"})
    monkeypatch.setattr(
        engine, "_speak_pregenerated", lambda cached, **_k: spoken.append(cached["text"])
    )

    parsed = _run_turn(engine, caplog)

    assert spoken == ["listo"]
    _assert_one_turn(parsed, ["begin", "served_pregen"], "completed")
    assert fake_ollama.chat_calls == []


def test_cached_reasoning_budget_exhaustion_is_dropped(engine, fake_ollama, caplog, monkeypatch):
    monkeypatch.setattr(
        engine, "_take_pregen_if_match",
        lambda payload, source: {"text": "", "failure_reason": "reasoning_budget_exhausted"},
    )

    parsed = _run_turn(engine, caplog)

    _assert_one_turn(parsed, ["begin", "drop"], "dropped")


def test_concurrent_background_work_never_touches_the_foreground_turn(
    engine, fake_ollama, caplog, monkeypatch
):
    """A pregen generation on a daemon thread during a foreground turn shares
    the same call sites; the turn is bound per thread, so it stays untouched."""
    fake_ollama.script_chat(ChatScript(chunks=["Borrador de fondo. "]))  # pregen
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN))  # foreground
    real_build = engine._build_generation_request
    state = {"spawned": False, "pregen": None}

    def build_then_run_background(*args, **kwargs):
        if not state["spawned"]:
            state["spawned"] = True

            def background():
                turn_state.emit(TurnEvent.DROP)
                turn_state.emit(TurnEvent.FAIL)
                state["pregen"] = engine._generar_dialogo(
                    "otra cosa", source="direct", commit_history=False
                )

            worker = threading.Thread(target=background, daemon=True)
            worker.start()
            worker.join(10)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(engine, "_build_generation_request", build_then_run_background)

    parsed = _run_turn(engine, caplog)

    assert state["pregen"] == "Borrador de fondo."
    _assert_one_turn(parsed, ["begin", "prompt_built", "first_audio", "complete"], "completed")


def test_each_turn_gets_its_own_id_and_the_queue_continues(engine, fake_ollama, caplog):
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN, mode="http_500"))
    fake_ollama.script_chat(ChatScript(chunks=["Vuelvo a estar. "]))

    _run_turn(engine, caplog, "uno")
    parsed = _run_turn(engine, caplog, "dos")

    turns = []
    for p in parsed:
        if p["turn"] not in turns:
            turns.append(p["turn"])
    assert len(turns) == 2
    second = [p for p in parsed if p["turn"] == turns[1]]
    assert [p["to"] for p in second if p["to"] in TERMINALS] == ["completed"]


def test_tracker_errors_never_break_a_turn(engine, fake_ollama, caplog, monkeypatch):
    from opencohost.core.engine import turn_state

    def broken_apply(self, *_a, **_k):
        raise RuntimeError("tracker bug")

    monkeypatch.setattr(turn_state.TurnState, "apply", broken_apply)
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN))

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        engine.enqueue("hola", priority=0, source="direct")
        _run_bounded(engine._process_priority_queue)

    assert engine.historial[-1]["content"] == "Primera frase limpia. Segunda frase tranquila."


def test_background_pregen_opens_no_tracker(engine, fake_ollama, caplog):
    fake_ollama.script_chat(ChatScript(chunks=SPOKEN))

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        _run_bounded(
            lambda: engine._generar_dialogo("hola", source="direct", commit_history=False)
        )

    assert not [r for r in caplog.records if "[TURN_STATE]" in r.getMessage()]
