"""T3 (memory-write-quality-20261020): a finished recall is never discarded post hoc.

The budget caps the embed IPC wait (WORKER_UNAVAILABLE -> fallback=timeout). A
recall that finished and hit is used whatever its wall time; `over_budget=0|1`
is telemetry only.
"""
from __future__ import annotations

from types import SimpleNamespace

from opencohost.core.memory import episodic_merge

BUDGET = 400


def _decision(reason="EPISODIC_HIT", hit=True, episodes=("ep",)):
    return SimpleNamespace(
        reason=reason, scope="EPISODIC_TOPIC", top_score=0.9, episodes=episodes, hit=hit,
    )


def test_hit_above_budget_is_used_not_timeout():
    out = episodic_merge.resolve_recall_outcome(_decision(), 900, BUDGET, 2)
    assert out.fallback == episodic_merge.FALLBACK_NONE
    assert out.episodes == ("ep",)


def test_worker_unavailable_is_still_timeout_even_within_budget():
    d = _decision(reason="WORKER_UNAVAILABLE", hit=False, episodes=())
    for ms in (50, 900):
        out = episodic_merge.resolve_recall_outcome(d, ms, BUDGET, 2)
        assert out.fallback == episodic_merge.FALLBACK_TIMEOUT
        assert out.episodes == ()


def test_over_budget_flag_in_telemetry_line():
    over = episodic_merge.resolve_recall_outcome(_decision(), BUDGET + 1, BUDGET, 2)
    at = episodic_merge.resolve_recall_outcome(_decision(), BUDGET, BUDGET, 2)
    below = episodic_merge.resolve_recall_outcome(_decision(), 10, BUDGET, 2)
    assert " over_budget=1" in episodic_merge.format_recall_telemetry(over, 0, 1)
    assert " over_budget=0" in episodic_merge.format_recall_telemetry(at, 0, 1)
    assert " over_budget=0" in episodic_merge.format_recall_telemetry(below, 0, 1)


def test_no_hit_within_budget_unchanged():
    d = _decision(reason="NO_CANDIDATES", hit=False, episodes=())
    out = episodic_merge.resolve_recall_outcome(d, 120, BUDGET, 2)
    assert out.fallback == episodic_merge.FALLBACK_NO_HIT
    assert out.episodes == ()
    assert " over_budget=0" in episodic_merge.format_recall_telemetry(out, 0, 0)
