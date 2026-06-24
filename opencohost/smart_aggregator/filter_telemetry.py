"""Metadata-only telemetry for the chat-activation filter (measure-first).

Captures, for every filter DECISION (accept AND reject) across the activation
pipeline, enough METADATA to later answer "why is Kira under-fed at high traffic"
WITHOUT a second instrumentation pass — and WITHOUT ever logging raw chat text or
usernames.

Safety invariant (baked into the type): ``FilterDecision`` has no text/user/message
field. ``msg_len`` is an int, ``msg_category`` / ``reason_code`` / ``stage`` are closed
enums. The record physically cannot carry chat content. ``reason`` strings outside the
enum are coerced to ``UNKNOWN`` rather than stored verbatim.

Threading: ``record()`` is called only on the single chat-source daemon thread, so the
ring/counters are intentionally lock-free (a per-message lock would add the very
overhead we are measuring). Do NOT call it cross-thread.

This module only MEASURES. It changes no activation logic and ships off-by-default.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Callable, Optional


# Module default; the aggregator overrides from `smart_aggregator.yaml`
# (`chat_activation_diagnostics.enabled`). Off by default.
FILTER_TELEMETRY_ENABLED = False


class FilterStage(str, Enum):
    MESSAGE_FILTER = "message_filter"      # per-message junk gate (message_filter.py)
    QUALITY_GATE = "quality_gate"          # aggregator quality/spam gate
    CONTEXT_SAMPLING = "context_sampling"  # high-traffic 1-in-N decimation (the starvation vector)
    ACTIVITY_TRIGGER = "activity_trigger"  # rate / cooldown gate
    SHOULD_CALL = "should_call"            # ChatContextPacket decision
    QUEUE_TTL = "queue_ttl"                # priority-queue TTL expiry


class ReasonCode(str, Enum):
    # accept
    ACCEPTED = "accepted"
    WHITELISTED = "whitelisted"
    # message_filter rejects (mirror message_filter._last_rejection_reason 1:1)
    INVALID_TYPE = "invalid_type"
    EMPTY = "empty"
    EMOJI_ONLY = "emoji_only"
    LINK = "link"
    MENTION = "mention"
    TOO_SHORT_CHARS = "too_short_chars"
    TOO_SHORT_WORDS = "too_short_words"
    ASCII_ART = "ascii_art"
    REPETITIVE_CHARS = "repetitive_chars"
    REPEATED_WORDS = "repeated_words"
    GIBBERISH = "gibberish"
    UNKNOWN = "unknown"
    # aggregator quality gate
    QUALITY_TOO_LOW = "quality_too_low"
    SPAM = "spam"
    # context sampling (high-traffic decimation)
    SAMPLED_IN = "sampled_in"
    SAMPLED_OUT = "sampled_out"
    # activity trigger
    TRIGGER_FIRED = "trigger_fired"
    RATE_BELOW_THRESHOLD = "rate_below_threshold"
    COOLDOWN_SUPPRESSED = "cooldown_suppressed"
    # should_call
    SHOULD_CALL_TRUE = "should_call_true"
    SHOULD_CALL_FALSE = "should_call_false"
    # queue TTL
    TTL_EXPIRED = "ttl_expired"


class MsgCategory(str, Enum):
    MICRO = "micro"     # < 10 chars
    SHORT = "short"     # 10-29
    MEDIUM = "medium"   # 30-99
    LONG = "long"       # >= 100
    NA = "na"           # batch/trigger records with no single message


def bucket(n: int) -> str:
    if n < 10:
        return MsgCategory.MICRO.value
    if n < 30:
        return MsgCategory.SHORT.value
    if n < 100:
        return MsgCategory.MEDIUM.value
    return MsgCategory.LONG.value


@dataclass(frozen=True, slots=True)
class FilterDecision:
    ts: float                          # monotonic timestamp (relative, not wall-clock)
    stage: str                         # FilterStage value
    accepted: bool
    reason_code: str                   # ReasonCode value
    score: Optional[float]             # quality | rate | confidence (a number, never text)
    threshold: Optional[float]         # the LIVE threshold this score was compared against
    msg_len: int                       # cleaned-text length or word-count proxy — never the text
    msg_category: str                  # MsgCategory value (length bucket only)
    secs_since_last_activation: float  # gap since the last should_call==True
    secs_since_last_spoken: float      # gap since Kira ACTUALLY spoke (separate clock)
    chat_rate_msg_s: float
    batch_count: Optional[int] = None  # batch size for SHOULD_CALL records only

    def as_log_dict(self) -> dict:
        return asdict(self)


def _coerce_reason(reason: str) -> str:
    try:
        return ReasonCode(reason).value
    except ValueError:
        return ReasonCode.UNKNOWN.value


def _percentile(values, p: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    k = (len(ordered) - 1) * (p / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    if lo == hi:
        return float(ordered[lo])
    return float(ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo))


class FilterTelemetry:
    """Lock-free, O(1)-per-record collector. Off-by-default; read on demand."""

    def __init__(
        self,
        enabled: bool = FILTER_TELEMETRY_ENABLED,
        ring: int = 512,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.enabled = enabled
        self._clock = clock
        self._ring: deque = deque(maxlen=ring)
        self._counts: dict = {}
        self._by_stage: dict = {}
        self._total = 0
        self._accepted = 0
        self._rejected = 0
        now = clock()
        self._last_activation_ts = now
        self._last_spoken_ts = now
        self._activation_gaps: deque = deque(maxlen=ring)
        self._spoken_gaps: deque = deque(maxlen=ring)

    def mark_spoken(self) -> None:
        """Call when Kira ACTUALLY begins/ends a spoken turn (not at should_call==True).

        Cheap and safe to call even when disabled — keeps the spoken clock honest so a
        should_call that later expires via TTL does not corrupt the cadence metric.
        """
        now = self._clock()
        self._spoken_gaps.append(now - self._last_spoken_ts)
        self._last_spoken_ts = now

    def record(
        self,
        *,
        stage: str,
        accepted: bool,
        reason: str,
        score: Optional[float],
        threshold: Optional[float],
        msg_len: int,
        chat_rate: float,
        msg_category: Optional[str] = None,
        batch_count: Optional[int] = None,
    ) -> None:
        if not self.enabled:
            return  # hot-path fast exit — no record constructed

        ts = self._clock()
        rec = FilterDecision(
            ts=ts,
            stage=stage,
            accepted=accepted,
            reason_code=_coerce_reason(reason),
            score=score,
            threshold=threshold,
            msg_len=msg_len,
            msg_category=msg_category or bucket(msg_len),
            secs_since_last_activation=ts - self._last_activation_ts,
            secs_since_last_spoken=ts - self._last_spoken_ts,
            chat_rate_msg_s=chat_rate,
            batch_count=batch_count,
        )

        if stage == FilterStage.SHOULD_CALL.value and accepted:
            self._activation_gaps.append(ts - self._last_activation_ts)
            self._last_activation_ts = ts

        self._ring.append(rec)
        key = (stage, rec.reason_code)
        self._counts[key] = self._counts.get(key, 0) + 1
        bucket_stage = self._by_stage.setdefault(stage, {"accepted": 0, "rejected": 0})
        bucket_stage["accepted" if accepted else "rejected"] += 1
        self._total += 1
        if accepted:
            self._accepted += 1
        else:
            self._rejected += 1

    def recent(self) -> list:
        """Snapshot of the bounded ring of recent decisions (for distribution views)."""
        return list(self._ring)

    def rollup(self) -> dict:
        """Metadata-only summary the operator reads on demand. No raw chat anywhere."""
        return {
            "total": self._total,
            "accepted": self._accepted,
            "rejected": self._rejected,
            "accept_ratio": (self._accepted / self._total) if self._total else 0.0,
            "counts": {f"{stage}:{reason}": n for (stage, reason), n in self._counts.items()},
            "by_stage": {stage: dict(v) for stage, v in self._by_stage.items()},
            "activation_gap_p50": _percentile(self._activation_gaps, 50),
            "activation_gap_p95": _percentile(self._activation_gaps, 95),
            "spoken_gap_p50": _percentile(self._spoken_gaps, 50),
            "spoken_gap_p95": _percentile(self._spoken_gaps, 95),
        }
