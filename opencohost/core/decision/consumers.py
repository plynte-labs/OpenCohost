"""How each consumer applies its decision (laya-decision-layer-20261009).

Every helper takes the turn's ``TurnDecisions`` (or None) and the rule's
result, and returns the rule's result unchanged unless Laya is allowed to act:
mode ``on``, confidence at or above the threshold, and a value the consumer
permits. ``None`` decisions always mean the base behavior.

  * vision: explicit triggers (the armed look, the PTT switch), requested
    rule positives (``screen_ref``, ``look_verb``) and every rule suppressor
    always win. Laya may turn a plain rule "no" into a look, and a
    non-requested rule positive into no look. With the rule gate disabled
    Laya never adds a look.
  * memory: Laya may only SKIP a recall the rule would run (ADR-059: memory
    selection stays deterministic and application-side).
  * reply_length: Laya may only replace the auto heuristic's
    short/medium/long pick. A fixed setting, the profile's extended
    responses and a vision turn are untouched and not even resolved.
  * web (web-fetch-laya-20261009): an explicit request (the rule says
    ``search``) always wins. Laya may only turn a rule ``no_search`` into a
    search.
  * clock (clock-and-compact-polish-20261010): an explicit time/date ask
    (the rule says ``time``) always wins. Laya may only turn a rule
    ``no_time`` into ``time``; the line it adds is harmless.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from opencohost.core.decision.types import (
    ACTED_BY_LAYA, CLOCK, MEMORY, REPLY_LENGTH, STATUS_LOW_CONFIDENCE, VISION, WEB,
)
from opencohost.core.vision.gate import (
    REASON_EXPLICIT_LOOK,
    REASON_LOOK_VERB,
    REASON_PTT_SWITCH,
    REASON_SCREEN_REF,
    VisionDecision,
)

logger = logging.getLogger("OpenCohost")

LOOK = "look"
NO_LOOK = "no_look"
REASON_LAYA_LOOK = "laya_look"
REASON_LAYA_SKIP = "laya_skip"
_PROTECTED = frozenset({REASON_EXPLICIT_LOOK, REASON_PTT_SWITCH, REASON_SCREEN_REF, REASON_LOOK_VERB})
_SUPPRESSED_PREFIX = "suppressed_"
_AUTO_REASONS = frozenset({"brief_input", "default", "depth_request"})
REASON_LAYA = "laya"


def apply_vision_decision(decision: VisionDecision, turn, *, source: str, rule_gate) -> VisionDecision:
    """The gate's decision after the turn's ``vision`` decision. ``rule_gate``
    is the composite gate's rule gate (None when the owner disabled it)."""
    if turn is None:
        return decision
    codes = tuple(decision.reason_codes)
    rule_value = LOOK if decision.needs_vision else NO_LOOK
    protected = (
        rule_gate is None
        or bool(_PROTECTED & set(codes))
        or any(c.startswith(_SUPPRESSED_PREFIX) for c in codes)
        or source not in ("direct", "ptt")
    )
    outcome = turn.resolve(VISION, rule_value, allowed=() if protected else (LOOK, NO_LOOK))
    if outcome is None or outcome.acted_by != ACTED_BY_LAYA:
        return decision
    if outcome.value == LOOK:
        delay = 0.0 if source == "ptt" else float(getattr(rule_gate, "capture_delay_s", 0.0) or 0.0)
        return VisionDecision(True, (*codes, REASON_LAYA_LOOK), delay)
    return VisionDecision(False, (*codes, REASON_LAYA_SKIP), 0.0)


def memory_recall_skipped(turn) -> bool:
    """True when this turn's recall (which the rule would run) is skipped."""
    if turn is None:
        return False
    outcome = turn.resolve(MEMORY, "recall", allowed=("skip",))
    return outcome is not None and outcome.acted_by == ACTED_BY_LAYA


def apply_reply_length(turn, level: str, reason: str) -> tuple[str, str]:
    """``(level, reason)`` after the turn's ``reply_length`` decision; only an
    auto heuristic pick (not vision) is eligible."""
    if turn is None or reason not in _AUTO_REASONS:
        return level, reason
    outcome: Optional[object] = turn.resolve(REPLY_LENGTH, level)
    if outcome is None or outcome.acted_by != ACTED_BY_LAYA:
        return level, reason
    return outcome.value, REASON_LAYA


# web-query-followup-and-retry O2: the Laya override stays at the shared 0.90.
# A below-threshold Laya `search` against a rule `no_search` is counted (never
# acted on, never logged with text) so a later zone decision has data.
_NEAR_MISS_BUCKETS = ((0.80, "80_90"), (0.70, "70_80"), (0.50, "50_70"))
_web_near_miss: dict[str, int] = {}
_web_near_miss_lock = threading.Lock()


def web_near_miss_counts() -> dict[str, int]:
    with _web_near_miss_lock:
        return dict(_web_near_miss)


def reset_web_near_miss() -> None:
    with _web_near_miss_lock:
        _web_near_miss.clear()


def _count_web_near_miss(outcome, rule_value: str) -> None:
    if (
        rule_value != "no_search"
        or outcome.laya_value != "search"
        or outcome.status != STATUS_LOW_CONFIDENCE
        or outcome.confidence is None
    ):
        return
    bucket = next((name for floor, name in _NEAR_MISS_BUCKETS if outcome.confidence >= floor), None)
    if bucket is None:
        return
    with _web_near_miss_lock:
        _web_near_miss[bucket] = count = _web_near_miss.get(bucket, 0) + 1
    logger.info("[WEB] laya_near_miss bucket=%s count=%d", bucket, count)


def apply_web_decision(turn, rule_value: str) -> str:
    """``search`` or ``no_search`` after the turn's ``web`` decision."""
    if turn is None:
        return rule_value
    allowed = () if rule_value == "search" else ("search",)
    outcome = turn.resolve(WEB, rule_value, allowed=allowed)
    if outcome is None:
        return rule_value
    _count_web_near_miss(outcome, rule_value)
    if outcome.acted_by != ACTED_BY_LAYA:
        return rule_value
    return outcome.value


def apply_clock_decision(turn, rule_value: str) -> str:
    """``time`` or ``no_time`` after the turn's ``clock`` decision."""
    if turn is None:
        return rule_value
    allowed = () if rule_value == "time" else ("time",)
    outcome = turn.resolve(CLOCK, rule_value, allowed=allowed)
    if outcome is None or outcome.acted_by != ACTED_BY_LAYA:
        return rule_value
    return outcome.value
