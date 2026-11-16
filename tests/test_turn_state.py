"""Unit tests for the foreground-turn state machine (turn-state-machine-20261112 T1)."""

from __future__ import annotations

import logging
import re
import threading

import pytest

from opencohost.core.engine import turn_state
from opencohost.core.engine.turn_state import (
    TERMINAL_PHASES,
    TRANSITIONS,
    TurnEvent,
    TurnPhase,
    TurnState,
)

LOGGER_NAME = "OpenCohost"

# The one allowed shape of a [TURN_STATE] line: codes and integers only.
LINE_RE = re.compile(
    r"^\[TURN_STATE\] tid=t[0-9a-f]{8} source=[A-Za-z0-9_.:-]{1,40} "
    r"[a-z_]+->[a-z_]+ event=[a-z_]+( (ms|attempt)=\d+)*$"
)
ILLEGAL_RE = re.compile(
    r"^\[TURN_STATE\] illegal tid=t[0-9a-f]{8} source=[A-Za-z0-9_.:-]{1,40} "
    r"phase=[a-z_]+ event=[a-z_]+$"
)


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if "[TURN_STATE]" in r.getMessage()]


def _new(source="direct"):
    turn = TurnState(turn_state.new_turn_id(), source)
    return turn


def test_terminal_phases_are_exactly_the_five_outcomes():
    assert TERMINAL_PHASES == {
        TurnPhase.COMPLETED, TurnPhase.DROPPED, TurnPhase.FAILED,
        TurnPhase.REFUSED, TurnPhase.EXPIRED,
    }


def test_table_never_leaves_a_terminal_phase():
    assert not [key for key in TRANSITIONS if key[0] in TERMINAL_PHASES]


def test_table_targets_and_keys_are_known_members():
    for (phase, event), target in TRANSITIONS.items():
        assert isinstance(phase, TurnPhase)
        assert isinstance(event, TurnEvent)
        assert isinstance(target, TurnPhase)


def test_every_active_phase_can_close_in_each_terminal_state():
    for phase in TurnPhase:
        if phase in TERMINAL_PHASES:
            continue
        assert TRANSITIONS[(phase, TurnEvent.COMPLETE)] is TurnPhase.COMPLETED
        assert TRANSITIONS[(phase, TurnEvent.DROP)] is TurnPhase.DROPPED
        assert TRANSITIONS[(phase, TurnEvent.FAIL)] is TurnPhase.FAILED


def test_every_phase_is_reachable_from_assembling():
    seen = {TurnPhase.ASSEMBLING}
    frontier = [TurnPhase.ASSEMBLING]
    while frontier:
        current = frontier.pop()
        for (phase, _event), target in TRANSITIONS.items():
            if phase is current and target not in seen:
                seen.add(target)
                frontier.append(target)
    assert seen == set(TurnPhase)


def test_begin_logs_the_start_line(caplog):
    turn = _new()
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        turn.begin()
    lines = _lines(caplog)
    assert len(lines) == 1 and LINE_RE.match(lines[0]), lines
    assert "start->assembling event=begin" in lines[0]
    assert turn.phase is TurnPhase.ASSEMBLING


def test_streamed_chain_ends_completed(caplog):
    turn = _new("ptt")
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        turn.begin()
        assert turn.apply(TurnEvent.PROMPT_BUILT, ms=12) is TurnPhase.GENERATING
        assert turn.apply(TurnEvent.FIRST_AUDIO, ms=900) is TurnPhase.STREAMING
        assert turn.apply(TurnEvent.COMPLETE) is TurnPhase.COMPLETED
    assert turn.is_terminal
    lines = _lines(caplog)
    assert all(LINE_RE.match(line) for line in lines), lines
    assert [line.split("event=")[1].split()[0] for line in lines] == [
        "begin", "prompt_built", "first_audio", "complete",
    ]
    assert "ms=12" in lines[1] and "ms=900" in lines[2]


def test_stall_chain_ends_dropped():
    turn = _new()
    turn.begin()
    turn.apply(TurnEvent.PROMPT_BUILT)
    turn.apply(TurnEvent.FIRST_AUDIO)
    assert turn.apply(TurnEvent.STALL) is TurnPhase.RECOVERING
    assert turn.apply(TurnEvent.ROLLBACK) is TurnPhase.RECOVERING
    assert turn.apply(TurnEvent.DROP) is TurnPhase.DROPPED


def test_guard_revert_then_block_then_retry_chain():
    turn = _new()
    turn.begin()
    turn.apply(TurnEvent.PROMPT_BUILT)
    assert turn.apply(TurnEvent.GUARD_REVERT) is TurnPhase.REVERTED
    assert turn.apply(TurnEvent.GUARD_BLOCK) is TurnPhase.GUARD_BLOCKED
    assert turn.apply(TurnEvent.GUARD_RETRY) is TurnPhase.GENERATING
    assert turn.apply(TurnEvent.COMPLETE) is TurnPhase.COMPLETED


def test_stream_truncate_then_complete():
    turn = _new()
    turn.begin()
    turn.apply(TurnEvent.PROMPT_BUILT)
    turn.apply(TurnEvent.FIRST_AUDIO)
    assert turn.apply(TurnEvent.STREAM_TRUNCATE) is TurnPhase.GUARD_BLOCKED
    assert turn.apply(TurnEvent.COMPLETE) is TurnPhase.COMPLETED


def test_cloud_retry_goes_back_to_assembling():
    turn = _new()
    turn.begin()
    turn.apply(TurnEvent.PROMPT_BUILT)
    assert turn.apply(TurnEvent.CLOUD_RETRY) is TurnPhase.ASSEMBLING
    assert turn.apply(TurnEvent.PROMPT_BUILT) is TurnPhase.GENERATING


def test_failed_terminal_reachable_from_assembling():
    turn = _new()
    turn.begin()
    assert turn.apply(TurnEvent.FAIL) is TurnPhase.FAILED
    assert turn.is_terminal


def test_illegal_transition_warns_and_keeps_phase(caplog):
    turn = _new()
    turn.begin()
    caplog.clear()  # setup lines are not under test
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        result = turn.apply(TurnEvent.FIRST_AUDIO)  # not legal from assembling
    assert result is TurnPhase.ASSEMBLING
    assert turn.phase is TurnPhase.ASSEMBLING
    records = [r for r in caplog.records if "[TURN_STATE]" in r.getMessage()]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert ILLEGAL_RE.match(records[0].getMessage()), records[0].getMessage()
    assert "phase=assembling event=first_audio" in records[0].getMessage()


def test_event_after_terminal_is_ignored_with_debug_log(caplog):
    turn = _new()
    turn.begin()
    turn.apply(TurnEvent.DROP)
    caplog.clear()  # setup lines are not under test
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        assert turn.apply(TurnEvent.COMPLETE) is TurnPhase.DROPPED
    records = [r for r in caplog.records if "[TURN_STATE]" in r.getMessage()]
    assert len(records) == 1 and records[0].levelno == logging.DEBUG
    assert turn.phase is TurnPhase.DROPPED


def test_exactly_one_terminal_line_even_if_closed_twice(caplog):
    turn = _new()
    turn.begin()
    caplog.clear()  # the begin line is not under test
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        turn.apply(TurnEvent.COMPLETE)
        turn.apply(TurnEvent.COMPLETE)
        turn.apply(TurnEvent.DROP)
    info = [r for r in caplog.records if r.levelno == logging.INFO and "[TURN_STATE]" in r.getMessage()]
    assert len(info) == 1


def test_log_carries_only_codes_and_integers(caplog):
    secret = "reply text that must never be logged"
    turn = TurnState("tdeadbeef", "direct some free text/with spaces")
    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        turn.begin()
        turn.apply(TurnEvent.PROMPT_BUILT, ms=5, text=secret, attempt="two", extra=7)
        turn.apply(TurnEvent.COMPLETE, attempt=2)
    lines = _lines(caplog)
    assert lines and all(LINE_RE.match(line) for line in lines), lines
    assert secret not in " ".join(lines)
    assert "attempt=2" in lines[-1]
    assert "extra" not in " ".join(lines)


def test_new_turn_id_is_short_unique_hex():
    ids = {turn_state.new_turn_id() for _ in range(200)}
    assert len(ids) == 200
    assert all(re.fullmatch(r"t[0-9a-f]{8}", i) for i in ids)
    # Never the shape of prompt_trace's per-call turn= id (diagnostics _TURN_RE).
    assert not any(re.fullmatch(r"[0-9a-f]{4,16}", i) for i in ids)


def test_apply_never_raises_on_bad_input():
    turn = _new()
    turn.begin()
    assert turn.apply("not-an-event") is TurnPhase.ASSEMBLING  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "event, terminal",
    [
        (TurnEvent.REFUSE, TurnPhase.REFUSED),
        (TurnEvent.EXPIRE, TurnPhase.EXPIRED),
        (TurnEvent.SERVED_PREGEN, TurnPhase.COMPLETED),
    ],
)
def test_queue_outcomes_close_straight_from_assembling(event, terminal):
    turn = _new()
    turn.begin()
    assert turn.apply(event) is terminal
    assert turn.is_terminal


@pytest.mark.parametrize("event", [TurnEvent.REFUSE, TurnEvent.EXPIRE, TurnEvent.SERVED_PREGEN])
def test_queue_outcomes_are_illegal_once_generating(event):
    turn = _new()
    turn.begin()
    turn.apply(TurnEvent.PROMPT_BUILT)
    assert turn.apply(event) is TurnPhase.GENERATING


def test_emit_without_a_bound_turn_is_a_no_op():
    assert turn_state.current() is None
    turn_state.emit(TurnEvent.PROMPT_BUILT)  # must not raise


def test_open_binds_to_the_calling_thread_and_close_unbinds(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        turn = turn_state.open_turn("ptt")
        try:
            assert turn_state.current() is turn
            turn_state.emit(TurnEvent.PROMPT_BUILT, ms=3)
            assert turn.phase is TurnPhase.GENERATING
            turn_state.emit(TurnEvent.COMPLETE)
        finally:
            turn_state.close_turn()
    assert turn_state.current() is None
    lines = _lines(caplog)
    assert [line.split("event=")[1].split()[0] for line in lines] == [
        "begin", "prompt_built", "complete",
    ]


def test_close_fails_a_turn_left_open(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        turn = turn_state.open_turn("direct")
        turn_state.close_turn()
    assert turn.phase is TurnPhase.FAILED
    assert "assembling->failed event=fail" in _lines(caplog)[-1]


def test_other_threads_never_see_the_bound_turn():
    turn = turn_state.open_turn("direct")
    seen = {}

    def background():
        seen["current"] = turn_state.current()
        turn_state.emit(TurnEvent.PROMPT_BUILT)
        turn_state.emit(TurnEvent.DROP)

    try:
        worker = threading.Thread(target=background)
        worker.start()
        worker.join(5)
        assert seen["current"] is None
        assert turn.phase is TurnPhase.ASSEMBLING
    finally:
        turn_state.close_turn()


def test_emit_swallows_tracker_errors(monkeypatch):
    def boom(self, *a, **k):
        raise RuntimeError("tracker bug")

    turn_state.open_turn("direct")
    try:
        monkeypatch.setattr(TurnState, "apply", boom)
        turn_state.emit(TurnEvent.PROMPT_BUILT)  # must not raise
        turn_state.close_turn()  # must not raise either
    finally:
        monkeypatch.undo()
        turn_state.close_turn()
    assert turn_state.current() is None


def test_open_never_raises_and_returns_none_on_error(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("id source broken")

    monkeypatch.setattr(turn_state, "new_turn_id", boom)
    assert turn_state.open_turn("direct") is None
    assert turn_state.current() is None


@pytest.mark.parametrize("event", list(TurnEvent))
def test_every_event_is_used_by_the_table(event):
    if event is TurnEvent.BEGIN:
        pytest.skip("begin is logged by TurnState.begin(), not the table")
    assert any(key[1] is event for key in TRANSITIONS)
