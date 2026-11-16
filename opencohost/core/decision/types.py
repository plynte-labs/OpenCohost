"""Decision-layer types (laya-decision-layer-20261009).

One ``DecisionOutcome`` per decision and turn: what the rule said, what Laya
said, who acted and why. Outcomes carry ids, values, numbers and codes only,
never the user text, so they are safe for the ``[DECISION]`` log line.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, Optional

from opencohost.config import settings

DecisionId = Literal["vision", "memory", "reply_length", "respond", "web", "clock"]
DecisionMode = Literal["off", "shadow", "on"]

VISION = "vision"
MEMORY = "memory"
REPLY_LENGTH = "reply_length"
RESPOND = "respond"
WEB = "web"
CLOCK = "clock"
DECISION_IDS: tuple[str, ...] = settings.DECISION_IDS

MODE_OFF = "off"
MODE_SHADOW = "shadow"
MODE_ON = "on"

ACTED_BY_RULE = "rule"
ACTED_BY_LAYA = "laya"

# ok: Laya answered with confidence >= threshold. low_confidence: it answered
# below the threshold (or without a confidence). skipped: a confident answer
# the consumer does not allow Laya to apply here (explicit trigger, suppressor,
# fixed setting). timeout/unavailable/error: no usable answer.
STATUS_OK = "ok"
STATUS_TIMEOUT = "timeout"
STATUS_UNAVAILABLE = "unavailable"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"
STATUS_LOW_CONFIDENCE = "low_confidence"


@dataclass(frozen=True)
class DecisionOutcome:
    """One decision for one turn. ``acted_by == "laya"`` means Laya changed the
    outcome; an answer that agrees with the rule is acted by the rule."""

    id: str
    mode: str
    rule_value: Optional[str]
    laya_value: Optional[str]
    confidence: Optional[float]
    acted_by: str
    latency_ms: int
    status: str
    reason: str = ""

    @property
    def value(self) -> Optional[str]:
        """The value the turn uses."""
        return self.laya_value if self.acted_by == ACTED_BY_LAYA else self.rule_value

    def to_dict(self) -> dict:
        return asdict(self)
