"""T2 (memory-write-quality-20261020): the promotion sweep drains its backlog.

The sweep used to run once per launch, so 195 unjudged drafts never drained.
It now re-arms after an idle cooldown while drafts remain, one bounded batch
per arm, and never while a command is queued. Synthetic data only.
"""
from __future__ import annotations

import pytest

import opencohost.core.llm_engine as llm_engine
from opencohost.config import settings
from tests.owner_reflection_support import make_motor

COOLDOWN = 120
QUIET = 30


class _Clock:
    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _motor(monkeypatch, tmp_path, results):
    """A motor whose sweep returns the queued *results* in order."""
    motor = make_motor(monkeypatch, tmp_path)
    clock = _Clock()
    monkeypatch.setattr(llm_engine, "_promotion_monotonic", clock, raising=False)
    monkeypatch.setattr(llm_engine, "MEMORIAS_PROMOTION_REARM_S", COOLDOWN, raising=False)
    monkeypatch.setattr(llm_engine, "MEMORIAS_PROMOTION_QUIET_S", QUIET, raising=False)
    calls = []
    queued = list(results)

    def fake_sweep(**_kw):
        calls.append(clock.now)
        return queued.pop(0) if queued else {"skipped": "", "unjudged_remaining": 0}

    motor.promote_pending_drafts = fake_sweep
    return motor, clock, calls


def _more(n=10):
    return {"skipped": "", "unjudged_remaining": n}


def test_two_idle_windows_past_the_cooldown_give_two_sweeps(monkeypatch, tmp_path):
    motor, clock, calls = _motor(monkeypatch, tmp_path, [_more(), _more()])
    motor._promotion_idle_step()
    clock.now += COOLDOWN + 1
    motor._promotion_idle_step()
    assert len(calls) == 2


def test_idle_tick_inside_the_cooldown_gives_no_second_sweep(monkeypatch, tmp_path):
    motor, clock, calls = _motor(monkeypatch, tmp_path, [_more(), _more()])
    motor._promotion_idle_step()
    clock.now += COOLDOWN - 1
    motor._promotion_idle_step()
    assert len(calls) == 1


def test_cooldown_counts_from_when_the_sweep_finished(monkeypatch, tmp_path):
    motor, clock, calls = _motor(monkeypatch, tmp_path, [_more(), _more()])

    def slow_sweep(**_kw):
        calls.append(clock.now)
        clock.now += 90  # the judge call itself takes time
        return _more()

    motor.promote_pending_drafts = slow_sweep
    motor._promotion_idle_step()
    clock.now += COOLDOWN - 1  # 209s after start, 119s after finish
    motor._promotion_idle_step()
    assert len(calls) == 1


def test_nothing_remaining_does_not_rearm(monkeypatch, tmp_path):
    """Without a new draft, a fully drained backlog never re-arms."""
    motor, clock, calls = _motor(monkeypatch, tmp_path, [_more(0), _more()])
    motor._promotion_idle_step()
    clock.now += COOLDOWN * 10
    motor._promotion_idle_step()
    assert len(calls) == 1


def test_new_draft_after_a_drained_sweep_rearms_after_the_quiet_window(monkeypatch, tmp_path):
    """S4 contract: capturing a draft marks the sweep dirty, so it re-arms once
    the quiet window passes even though the last sweep left nothing behind."""
    motor, clock, calls = _motor(monkeypatch, tmp_path, [_more(0), _more(0)])
    motor._promotion_idle_step()
    clock.now += 5
    motor._promotion_mark_dirty()
    motor._last_command_at = clock.now - (QUIET - 1)
    motor._promotion_idle_step()
    assert len(calls) == 1  # still inside the quiet window
    clock.now += 2
    motor._promotion_idle_step()
    assert len(calls) == 2


def test_dirty_flag_is_consumed_by_the_sweep_it_triggers(monkeypatch, tmp_path):
    motor, clock, calls = _motor(monkeypatch, tmp_path, [_more(0), _more(0), _more(0)])
    motor._promotion_idle_step()
    motor._promotion_mark_dirty()
    clock.now += QUIET + 1
    motor._promotion_idle_step()
    clock.now += COOLDOWN * 10
    motor._promotion_idle_step()
    assert len(calls) == 2


def test_successful_capture_marks_the_sweep_dirty(monkeypatch, tmp_path):
    motor, clock, calls = _motor(monkeypatch, tmp_path, [_more(0), _more(0)])
    motor._promotion_idle_step()
    assert motor._capture_memoria("profile-1", "k|tokens", "title", "content text", "sig")
    clock.now += QUIET + 1
    motor._promotion_idle_step()
    assert len(calls) == 2


def test_failed_capture_does_not_mark_the_sweep_dirty(monkeypatch, tmp_path):
    motor, clock, calls = _motor(monkeypatch, tmp_path, [_more(0), _more(0)])
    motor._promotion_idle_step()

    class _Boom:
        def upsert_draft(self, *a, **k):
            raise RuntimeError("db locked")

    motor._memoria_store = _Boom()
    assert motor._capture_memoria("profile-1", "k|tokens", "title", "content text", "sig") is False
    clock.now += COOLDOWN * 10
    motor._promotion_idle_step()
    assert len(calls) == 1


def test_queued_command_means_no_sweep_on_that_tick(monkeypatch, tmp_path):
    motor, clock, calls = _motor(monkeypatch, tmp_path, [_more(), _more()])
    motor._promotion_idle_step()
    clock.now += COOLDOWN + 1
    motor.command_queue.put(("process_context", "hello there", None, "direct"))
    motor._promotion_idle_step()
    assert len(calls) == 1
    motor.command_queue.get_nowait()
    motor._promotion_idle_step()  # the command drained: the pending re-arm still fires
    assert len(calls) == 2


def test_skipped_sweep_is_not_retried_before_the_cooldown(monkeypatch, tmp_path):
    gated = {"skipped": "model_not_resident", "unjudged_remaining": 0}
    motor, clock, calls = _motor(monkeypatch, tmp_path, [gated, _more()])
    motor._promotion_idle_step()
    clock.now += 1
    motor._promotion_idle_step()
    clock.now += COOLDOWN - 2
    motor._promotion_idle_step()
    assert len(calls) == 1
    clock.now += 2
    motor._promotion_idle_step()
    assert len(calls) == 2


def test_raising_sweep_is_one_attempt_and_does_not_spin(monkeypatch, tmp_path):
    motor, clock, calls = _motor(monkeypatch, tmp_path, [])

    def boom(**_kw):
        calls.append(clock.now)
        raise RuntimeError("boom")

    motor.promote_pending_drafts = boom
    motor._promotion_idle_step()
    clock.now += COOLDOWN * 10
    motor._promotion_idle_step()
    assert len(calls) == 1


def test_reflection_rearm_still_sweeps_immediately(monkeypatch, tmp_path):
    motor, clock, calls = _motor(monkeypatch, tmp_path, [_more(0), _more(0)])
    motor._promotion_idle_step()
    motor._promotion_swept = False  # what the owner-reflection rearm does
    clock.now += 1
    motor._promotion_idle_step()
    assert len(calls) == 2


@pytest.mark.parametrize(
    "raw,expected",
    [(None, 120), ("", 120), ("300", 300), (" 45 ", 45), ("garbage", 120), ("0", 120), ("-5", 120), ("1.5", 120)],
)
def test_rearm_setting_resolver(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("OPENCOHOST_MEMORIAS_PROMOTION_REARM_S", raising=False)
    else:
        monkeypatch.setenv("OPENCOHOST_MEMORIAS_PROMOTION_REARM_S", raw)
    assert settings._resolve_memorias_promotion_rearm_s() == expected


def test_rearm_setting_default_is_120():
    assert settings.MEMORIAS_PROMOTION_REARM_S == 120


# --- quiet window: a re-armed sweep never lands right after a live command ---


def test_rearm_due_but_a_command_just_finished_gives_no_sweep(monkeypatch, tmp_path):
    motor, clock, calls = _motor(monkeypatch, tmp_path, [_more(), _more()])
    motor._promotion_idle_step()
    clock.now += COOLDOWN + 1
    motor._last_command_at = clock.now - (QUIET - 1)
    motor._promotion_idle_step()
    assert len(calls) == 1
    clock.now += 2  # the quiet window elapses; the still-pending re-arm fires
    motor._promotion_idle_step()
    assert len(calls) == 2


def test_rearm_due_and_quiet_enough_sweeps(monkeypatch, tmp_path):
    motor, clock, calls = _motor(monkeypatch, tmp_path, [_more(), _more()])
    motor._promotion_idle_step()
    clock.now += COOLDOWN + 1
    motor._last_command_at = clock.now - QUIET
    motor._promotion_idle_step()
    assert len(calls) == 2


def test_skip_retry_waits_for_the_quiet_window_too(monkeypatch, tmp_path):
    gated = {"skipped": "model_not_resident", "unjudged_remaining": 0}
    motor, clock, calls = _motor(monkeypatch, tmp_path, [gated, _more()])
    motor._promotion_idle_step()
    clock.now += COOLDOWN + 1
    motor._last_command_at = clock.now - 1
    motor._promotion_idle_step()
    assert len(calls) == 1


def test_first_sweep_is_not_delayed_by_the_quiet_window(monkeypatch, tmp_path):
    motor, clock, calls = _motor(monkeypatch, tmp_path, [_more()])
    motor._last_command_at = clock.now - 1
    motor._promotion_idle_step()
    assert len(calls) == 1


def test_reflection_rearm_is_not_delayed_by_the_quiet_window(monkeypatch, tmp_path):
    motor, clock, calls = _motor(monkeypatch, tmp_path, [_more(), _more()])
    motor._promotion_idle_step()
    motor._promotion_swept = False  # what the owner-reflection rearm does
    motor._last_command_at = clock.now - 1
    motor._promotion_idle_step()
    assert len(calls) == 2


@pytest.mark.parametrize(
    "raw,expected",
    [(None, 30), ("", 30), ("90", 90), ("garbage", 30), ("0", 30), ("-1", 30)],
)
def test_quiet_setting_resolver(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("OPENCOHOST_MEMORIAS_PROMOTION_QUIET_S", raising=False)
    else:
        monkeypatch.setenv("OPENCOHOST_MEMORIAS_PROMOTION_QUIET_S", raw)
    assert settings._resolve_memorias_promotion_quiet_s() == expected


def test_run_loop_stamps_the_clock_after_a_command(monkeypatch, tmp_path):
    motor, clock, _ = _motor(monkeypatch, tmp_path, [])
    motor._consume_command = lambda c: setattr(clock, "now", clock.now + 5)
    motor._stamp_command_done()
    assert motor._last_command_at == clock.now


def test_turn_completing_without_a_consumed_command_opens_the_quiet_window(monkeypatch, tmp_path):
    """Priority-queue turns (chat, agenda, direct) never pass through run()'s
    dequeue; every turn on both paths ends in _complete_processing_cycle."""
    motor, clock, calls = _motor(monkeypatch, tmp_path, [_more(), _more()])
    motor._promotion_idle_step()
    clock.now += COOLDOWN + 1  # the re-arm is due
    assert motor._last_command_at is None
    motor._complete_processing_cycle(process_queue=False, emit_idle=False)
    assert motor._last_command_at == clock.now
    clock.now += 1
    motor._promotion_idle_step()
    assert len(calls) == 1  # blocked by the quiet window
    clock.now += QUIET
    motor._promotion_idle_step()
    assert len(calls) == 2
