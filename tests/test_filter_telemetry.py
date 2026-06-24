"""Unit specs for the chat-activation filter telemetry (measure-first, A-lite).

RED before ``opencohost/smart_aggregator/filter_telemetry.py`` exists.

The load-bearing invariant: the record is metadata-only by TYPE — it physically
cannot carry raw chat text or usernames. Everything else (counters, clocks, rollup)
exists to later answer "why is Kira under-fed" WITHOUT a second instrumentation pass.
"""
from __future__ import annotations

import json

from opencohost.smart_aggregator.filter_telemetry import (
    FilterTelemetry,
    FilterDecision,
    FilterStage,
    ReasonCode,
    MsgCategory,
    bucket,
)


class FakeClock:
    """Injectable monotonic clock so timing assertions are deterministic."""

    def __init__(self, start: float = 1000.0):
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


# ── Safety invariant (load-bearing) ──────────────────────────────────────────

def test_decision_has_no_text_or_user_fields():
    fields = set(FilterDecision.__dataclass_fields__)
    for forbidden in ("text", "user", "username", "message", "raw", "content", "author"):
        assert forbidden not in fields, forbidden


def test_record_cannot_carry_raw_chat_text():
    tel = FilterTelemetry(enabled=True)
    sentinel = "ZZ_SECRET_PAYLOAD_42"
    # Only the LENGTH of a sentinel-bearing message may ever flow in.
    tel.record(
        stage=FilterStage.MESSAGE_FILTER.value,
        accepted=False,
        reason=ReasonCode.REPETITIVE_CHARS.value,
        score=None,
        threshold=None,
        msg_len=len(sentinel + " hola mundo test"),
        chat_rate=3.0,
    )
    blob = json.dumps(tel.recent()[-1].as_log_dict()) + json.dumps(tel.rollup())
    assert sentinel not in blob


# ── Enable/disable + recording ───────────────────────────────────────────────

def test_disabled_is_a_noop():
    tel = FilterTelemetry(enabled=False)
    tel.record(
        stage=FilterStage.MESSAGE_FILTER.value, accepted=False,
        reason=ReasonCode.EMPTY.value, score=None, threshold=None, msg_len=5, chat_rate=0.0,
    )
    assert tel.recent() == []
    assert tel.rollup()["total"] == 0


def test_enabled_records_and_counts():
    tel = FilterTelemetry(enabled=True)
    tel.record(
        stage=FilterStage.QUALITY_GATE.value, accepted=True,
        reason=ReasonCode.ACCEPTED.value, score=0.7, threshold=0.5, msg_len=42, chat_rate=2.0,
    )
    assert len(tel.recent()) == 1
    r = tel.recent()[-1]
    assert r.accepted is True and r.reason_code == ReasonCode.ACCEPTED.value
    assert r.score == 0.7 and r.threshold == 0.5
    assert r.msg_category == MsgCategory.MEDIUM.value  # 42 -> medium


def test_unknown_reason_is_coerced_to_unknown():
    tel = FilterTelemetry(enabled=True)
    tel.record(
        stage=FilterStage.MESSAGE_FILTER.value, accepted=False,
        reason="brand_new_reason_not_in_enum", score=None, threshold=None, msg_len=10, chat_rate=1.0,
    )
    assert tel.recent()[-1].reason_code == ReasonCode.UNKNOWN.value


# ── The two activation clocks (the should_call vs actually-spoken distinction) ─

def test_should_call_accept_resets_activation_clock():
    clk = FakeClock()
    tel = FilterTelemetry(enabled=True, clock=clk)
    clk.advance(30)
    tel.record(
        stage=FilterStage.MESSAGE_FILTER.value, accepted=False, reason=ReasonCode.EMPTY.value,
        score=None, threshold=None, msg_len=3, chat_rate=0.0,
    )
    assert tel.recent()[-1].secs_since_last_activation == 30.0
    tel.record(
        stage=FilterStage.SHOULD_CALL.value, accepted=True, reason=ReasonCode.SHOULD_CALL_TRUE.value,
        score=0.9, threshold=None, msg_len=0, chat_rate=1.0, msg_category=MsgCategory.NA.value,
    )
    clk.advance(5)
    tel.record(
        stage=FilterStage.MESSAGE_FILTER.value, accepted=False, reason=ReasonCode.EMPTY.value,
        score=None, threshold=None, msg_len=3, chat_rate=0.0,
    )
    assert tel.recent()[-1].secs_since_last_activation == 5.0


def test_spoken_clock_is_separate_from_should_call():
    clk = FakeClock()
    tel = FilterTelemetry(enabled=True, clock=clk)
    tel.mark_spoken()
    clk.advance(12)
    # A should_call accept must NOT move the spoken clock (it may expire via TTL).
    tel.record(
        stage=FilterStage.SHOULD_CALL.value, accepted=True, reason=ReasonCode.SHOULD_CALL_TRUE.value,
        score=0.9, threshold=None, msg_len=0, chat_rate=1.0, msg_category=MsgCategory.NA.value,
    )
    assert tel.recent()[-1].secs_since_last_spoken == 12.0
    clk.advance(3)
    tel.mark_spoken()  # now Kira actually spoke
    clk.advance(4)
    tel.record(
        stage=FilterStage.MESSAGE_FILTER.value, accepted=False, reason=ReasonCode.EMPTY.value,
        score=None, threshold=None, msg_len=3, chat_rate=0.0,
    )
    assert tel.recent()[-1].secs_since_last_spoken == 4.0


# ── Rollup + bounds ──────────────────────────────────────────────────────────

def test_bucket_mapping():
    assert bucket(5) == MsgCategory.MICRO.value
    assert bucket(20) == MsgCategory.SHORT.value
    assert bucket(50) == MsgCategory.MEDIUM.value
    assert bucket(200) == MsgCategory.LONG.value


def test_counts_persist_beyond_the_bounded_ring():
    tel = FilterTelemetry(enabled=True, ring=8)
    for _ in range(50):
        tel.record(
            stage=FilterStage.MESSAGE_FILTER.value, accepted=False, reason=ReasonCode.EMPTY.value,
            score=None, threshold=None, msg_len=3, chat_rate=0.0,
        )
    assert len(tel.recent()) == 8                      # ring bounded
    roll = tel.rollup()
    assert roll["counts"]["message_filter:empty"] == 50  # counters never evict
    assert roll["total"] == 50 and roll["rejected"] == 50 and roll["accepted"] == 0
    assert roll["accept_ratio"] == 0.0


def test_rollup_accept_ratio_and_by_stage():
    tel = FilterTelemetry(enabled=True)
    tel.record(stage=FilterStage.QUALITY_GATE.value, accepted=True, reason=ReasonCode.ACCEPTED.value,
               score=0.8, threshold=0.5, msg_len=40, chat_rate=1.0)
    tel.record(stage=FilterStage.QUALITY_GATE.value, accepted=False, reason=ReasonCode.QUALITY_TOO_LOW.value,
               score=0.2, threshold=0.5, msg_len=40, chat_rate=1.0)
    roll = tel.rollup()
    assert roll["accept_ratio"] == 0.5
    assert roll["by_stage"]["quality_gate"] == {"accepted": 1, "rejected": 1}
