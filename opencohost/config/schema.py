"""Creator Configuration Contract — pure types and frozen dataclasses.

No logic, no I/O, no side effects.  Enums + frozen dataclasses + JSON
serialization ONLY.  Matches the ``AgendaSignal`` frozen-dataclass pattern
used elsewhere in the codebase.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


# ══════════════════════════════════════════════════════════════════════════════
# Enums
# ══════════════════════════════════════════════════════════════════════════════


class CreatorTone(str, Enum):
    """The personality tone Kira projects on stream."""

    CASUAL = "casual"
    BALANCED = "balanced"
    FORMAL = "formal"
    ENERGETIC = "energetic"
    MYSTERIOUS = "mysterious"


class InterventionType(str, Enum):
    """How aggressively Kira inserts herself into the conversation."""

    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class AudienceScale(str, Enum):
    """Estimated live-audience size tier."""

    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    MASSIVE = "massive"


class UserNamePolicy(str, Enum):
    """Whether Kira addresses viewers by name."""

    YES = "yes"
    NO = "no"
    SOMETIMES = "sometimes"


class AwayBehavior(str, Enum):
    """What Kira does when the streamer steps away."""

    KEEP_TOPIC = "keep_topic"
    HOLD_PRESENCE = "hold_presence"
    ONLY_ALERTS = "only_alerts"


class ChatEvent(str, Enum):
    """The 12 chat-intent categories the system can classify."""

    DIRECT_QUESTION = "direct_question"
    VIEWER_REQUEST = "viewer_request"
    POLL_OR_VOTE_SUGGESTION = "poll_or_vote_suggestion"
    GREETING_OR_SHOUTOUT = "greeting_or_shoutout"
    CORRECTION_OR_CLARIFICATION = "correction_or_clarification"
    REPEATED_TOPIC = "repeated_topic"
    JOKE_OR_MEME = "joke_or_meme"
    HYPE_OR_EMOTION = "hype_or_emotion"
    COMPLAINT_OR_CONFUSION = "complaint_or_confusion"
    MODERATION_OR_RISK = "moderation_or_risk"
    FACTUAL_UPDATE = "factual_update"
    LOW_SIGNAL_NOISE = "low_signal_noise"


class ActionResult(str, Enum):
    """Where Kira routes a chat message."""

    VOICE = "VOICE"
    PANEL = "PANEL"
    ALERT = "ALERT"
    IGNORED = "IGNORED"
    QUEUED = "QUEUED"


# ══════════════════════════════════════════════════════════════════════════════
# Validation frozensets (module-level, shared by __post_init__ methods)
# ══════════════════════════════════════════════════════════════════════════════

_VALID_TONES: frozenset[str] = frozenset(t.value for t in CreatorTone)
_VALID_INTERVENTIONS: frozenset[str] = frozenset(i.value for i in InterventionType)
_VALID_SCALES: frozenset[str] = frozenset(s.value for s in AudienceScale)
_VALID_USERNAME_POLICIES: frozenset[str] = frozenset(u.value for u in UserNamePolicy)
_VALID_AWAY_BEHAVIORS: frozenset[str] = frozenset(a.value for a in AwayBehavior)
_VALID_CHAT_EVENTS: frozenset[str] = frozenset(c.value for c in ChatEvent)
VALID_MAX_RESPONSE_LENGTHS: frozenset[int] = frozenset({40, 80, 120, 200, 300})
_VALID_RESPONSE_FREQUENCIES: frozenset[str] = frozenset({"low", "medium", "high"})
VALID_PRIORITIES: frozenset[str] = frozenset({"low", "medium", "high"})

# Ordered priority → numeric weight for comparisons
_PRIORITY_WEIGHT: dict[str, int] = {"low": 1, "medium": 2, "high": 3}


# ══════════════════════════════════════════════════════════════════════════════
# Per-event action configuration (the leaf node of ActionPolicy)
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class EventAction:
    """What Kira is allowed to do with a single ChatEvent category."""

    voice_allowed: bool = False
    surface_to_ui: bool = True
    queue: bool = False
    ignore: bool = False
    priority: str = "low"  # low | medium | high

    def __post_init__(self) -> None:
        if self.voice_allowed and self.ignore:
            raise ValueError(
                "voice_allowed and ignore cannot both be True"
            )
        if self.priority not in VALID_PRIORITIES:
            raise ValueError(
                f"priority must be one of {set(VALID_PRIORITIES)}, "
                f"got {self.priority!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "voice_allowed": self.voice_allowed,
            "surface_to_ui": self.surface_to_ui,
            "queue": self.queue,
            "ignore": self.ignore,
            "priority": self.priority,
        }


# ══════════════════════════════════════════════════════════════════════════════
# Policy dataclasses
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class CreatorPolicy:
    """Streamer personality / style parameters."""

    tone: str  # CreatorTone value
    formality: float  # 0.0 – 1.0
    humor_level: float  # 0.0 – 1.0
    caution_level: float  # 0.0 – 1.0
    max_response_length: int  # one of {40, 80, 120, 200, 300}
    intervention_type: str  # InterventionType value
    use_usernames: bool
    factuality_strictness: float  # 0.0 – 1.0

    def __post_init__(self) -> None:
        if self.tone not in _VALID_TONES:
            raise ValueError(
                f"tone must be one of {set(_VALID_TONES)}, got {self.tone!r}"
            )
        if not (0.0 <= self.formality <= 1.0):
            raise ValueError(
                f"formality must be 0.0-1.0, got {self.formality!r}"
            )
        if not (0.0 <= self.humor_level <= 1.0):
            raise ValueError(
                f"humor_level must be 0.0-1.0, got {self.humor_level!r}"
            )
        if not (0.0 <= self.caution_level <= 1.0):
            raise ValueError(
                f"caution_level must be 0.0-1.0, got {self.caution_level!r}"
            )
        if self.max_response_length not in VALID_MAX_RESPONSE_LENGTHS:
            raise ValueError(
                f"max_response_length must be one of {set(VALID_MAX_RESPONSE_LENGTHS)}, "
                f"got {self.max_response_length!r}"
            )
        if self.intervention_type not in _VALID_INTERVENTIONS:
            raise ValueError(
                f"intervention_type must be one of {set(_VALID_INTERVENTIONS)}, "
                f"got {self.intervention_type!r}"
            )
        if not (0.0 <= self.factuality_strictness <= 1.0):
            raise ValueError(
                f"factuality_strictness must be 0.0-1.0, got {self.factuality_strictness!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tone": self.tone,
            "formality": self.formality,
            "humor_level": self.humor_level,
            "caution_level": self.caution_level,
            "max_response_length": self.max_response_length,
            "intervention_type": self.intervention_type,
            "use_usernames": self.use_usernames,
            "factuality_strictness": self.factuality_strictness,
        }


@dataclass(frozen=True)
class ModePolicy:
    """Operational mode — preset identity + timing parameters."""

    preset_name: str = "default"
    interruption_threshold: float = 0.5  # 0.0 – 1.0
    response_frequency: str = "medium"  # low | medium | high

    def __post_init__(self) -> None:
        if not (0.0 <= self.interruption_threshold <= 1.0):
            raise ValueError(
                f"interruption_threshold must be 0.0-1.0, "
                f"got {self.interruption_threshold!r}"
            )
        if self.response_frequency not in _VALID_RESPONSE_FREQUENCIES:
            raise ValueError(
                f"response_frequency must be one of {set(_VALID_RESPONSE_FREQUENCIES)}, "
                f"got {self.response_frequency!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "preset_name": self.preset_name,
            "interruption_threshold": self.interruption_threshold,
            "response_frequency": self.response_frequency,
        }


@dataclass(frozen=True)
class ScalePolicy:
    """Audience-size parameters that gate certain behaviors."""

    audience_scale: str = "small"  # AudienceScale value
    max_messages: int = 50
    dedup_window: int = 30  # seconds

    def __post_init__(self) -> None:
        if self.audience_scale not in _VALID_SCALES:
            raise ValueError(
                f"audience_scale must be one of {set(_VALID_SCALES)}, "
                f"got {self.audience_scale!r}"
            )
        if self.max_messages < 1:
            raise ValueError(
                f"max_messages must be >= 1, got {self.max_messages!r}"
            )
        if self.dedup_window < 1:
            raise ValueError(
                f"dedup_window must be >= 1, got {self.dedup_window!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "audience_scale": self.audience_scale,
            "max_messages": self.max_messages,
            "dedup_window": self.dedup_window,
        }


@dataclass(frozen=True)
class NonNegotiableRule:
    """A system-enforced rule that no user config can override."""

    id: str  # e.g. "no_doxxing"
    description: str
    enforced_at: tuple[str, ...] = (
        "config_validation",
        "runtime",
        "output_guard",
        "tests",
    )

    def __post_init__(self) -> None:
        if not self.id or not isinstance(self.id, str):
            raise ValueError("id must be a non-empty string")
        if not self.description or not isinstance(self.description, str):
            raise ValueError("description must be a non-empty string")

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "enforced_at": list(self.enforced_at),
        }


@dataclass(frozen=True)
class ActionPolicy:
    """Per-ChatEvent routing rules — the core of the translation layer."""

    direct_question: EventAction = field(default_factory=EventAction)
    viewer_request: EventAction = field(default_factory=EventAction)
    poll_or_vote_suggestion: EventAction = field(default_factory=EventAction)
    greeting_or_shoutout: EventAction = field(default_factory=EventAction)
    correction_or_clarification: EventAction = field(default_factory=EventAction)
    repeated_topic: EventAction = field(default_factory=EventAction)
    joke_or_meme: EventAction = field(default_factory=EventAction)
    hype_or_emotion: EventAction = field(default_factory=EventAction)
    complaint_or_confusion: EventAction = field(default_factory=EventAction)
    moderation_or_risk: EventAction = field(default_factory=EventAction)
    factual_update: EventAction = field(default_factory=EventAction)
    low_signal_noise: EventAction = field(
        default_factory=lambda: EventAction(
            voice_allowed=False, surface_to_ui=False, ignore=True, priority="low"
        )
    )

    def get(self, event: str) -> EventAction:
        """Look up the EventAction for a ChatEvent value string."""
        try:
            return getattr(self, event)
        except AttributeError:
            raise KeyError(f"unknown ChatEvent: {event!r}")

    def to_dict(self) -> dict[str, Any]:
        """Return a dict keyed by ChatEvent value, each value an EventAction dict."""
        result: dict[str, Any] = {}
        for ev in ChatEvent:
            ea: EventAction = getattr(self, ev.value)
            result[ev.value] = ea.to_dict()
        return result


# ══════════════════════════════════════════════════════════════════════════════
# Top-level container
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class CreatorConfig:
    """The complete configuration bundle consumed by the Kira pipeline."""

    creator: CreatorPolicy
    mode: ModePolicy = field(default_factory=ModePolicy)
    action: ActionPolicy = field(default_factory=ActionPolicy)
    scale: ScalePolicy = field(default_factory=ScalePolicy)
    non_negotiables: tuple[NonNegotiableRule, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "creator": self.creator.to_dict(),
            "mode": self.mode.to_dict(),
            "action": self.action.to_dict(),
            "scale": self.scale.to_dict(),
            "non_negotiables": [r.to_dict() for r in self.non_negotiables],
        }

    def to_json(self) -> str:
        """Serialize to a JSON string (deterministic, sorted keys)."""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CreatorConfig:
        """Reconstruct a CreatorConfig from a dict (e.g. parsed JSON)."""
        creator_raw = data["creator"]
        creator = CreatorPolicy(
            tone=creator_raw["tone"],
            formality=float(creator_raw["formality"]),
            humor_level=float(creator_raw["humor_level"]),
            caution_level=float(creator_raw["caution_level"]),
            max_response_length=int(creator_raw["max_response_length"]),
            intervention_type=creator_raw["intervention_type"],
            use_usernames=bool(creator_raw["use_usernames"]),
            factuality_strictness=float(creator_raw["factuality_strictness"]),
        )

        mode_raw = data.get("mode", {})
        mode = ModePolicy(
            preset_name=mode_raw.get("preset_name", "default"),
            interruption_threshold=float(mode_raw.get("interruption_threshold", 0.5)),
            response_frequency=mode_raw.get("response_frequency", "medium"),
        )

        action_raw = data.get("action", {})
        action_kwargs: dict[str, EventAction] = {}
        for ev in ChatEvent:
            ea_raw = action_raw.get(ev.value, {})
            action_kwargs[ev.value] = EventAction(
                voice_allowed=bool(ea_raw.get("voice_allowed", False)),
                surface_to_ui=bool(ea_raw.get("surface_to_ui", True)),
                queue=bool(ea_raw.get("queue", False)),
                ignore=bool(ea_raw.get("ignore", False)),
                priority=ea_raw.get("priority", "low"),
            )
        action = ActionPolicy(**action_kwargs)

        scale_raw = data.get("scale", {})
        scale = ScalePolicy(
            audience_scale=scale_raw.get("audience_scale", "small"),
            max_messages=int(scale_raw.get("max_messages", 50)),
            dedup_window=int(scale_raw.get("dedup_window", 30)),
        )

        nn_raw = data.get("non_negotiables", [])
        non_negotiables = tuple(
            NonNegotiableRule(
                id=r["id"],
                description=r["description"],
                enforced_at=(
                    tuple(r["enforced_at"]) if "enforced_at" in r
                    else ("config_validation", "runtime", "output_guard", "tests")
                ),
            )
            for r in nn_raw
        )

        return cls(
            creator=creator,
            mode=mode,
            action=action,
            scale=scale,
            non_negotiables=non_negotiables,
        )

    @classmethod
    def from_json(cls, data: str | dict[str, Any]) -> CreatorConfig:
        """Deserialize from a JSON string or already-parsed dict."""
        if isinstance(data, str):
            data = json.loads(data)
        return cls.from_dict(data)
