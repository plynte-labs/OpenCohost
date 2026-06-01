"""AgendaSignal: structured chat-intent signal for Kira AgendaController.

Phase A — Shadow Mode:
  Builds and logs AgendaSignal objects alongside old compact_chat strings
  WITHOUT changing any controller, routing, prompt, or behavioral logic.
  All shadow code is gated by AGENDA_SIGNAL_SHADOW_MODE; set it to False
  for zero overhead.
"""

from __future__ import annotations

import json
import time as _time_module
from dataclasses import dataclass, field
from typing import Any, Optional

# ── constants ────────────────────────────────────────────────────────────────

SIGNAL_TYPES: frozenset[str] = frozenset({"direct_question", "hype", "trend", "low_signal"})
PRIORITY_LEVELS: frozenset[str] = frozenset({"high", "medium", "low"})
RESPONSE_GOALS: frozenset[str] = frozenset({"surface_to_streamer", "summarize_trend", "add_color", "ignore"})
SUGGESTED_ACTIONS: frozenset[str] = frozenset({"integrate_next_turn", "surface_to_streamer", "acknowledge_briefly", "ignore"})

# ── feature flag ─────────────────────────────────────────────────────────────

AGENDA_SIGNAL_SHADOW_MODE: bool = True

# ── frozen dataclass ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class AgendaSignal:
    """Structured intent signal built from raw chat context.

    All 11 fields are immutable after construction.  ``__post_init__``
    validates enum membership and numeric ranges.
    """

    signal_type: str
    priority: str
    response_goal: str
    primary_highlight: Optional[dict]
    supporting_summary: str
    related_to_active_topic: bool
    can_interrupt_current_topic: bool
    suggested_action: str
    confidence: float
    safety_notes: tuple[str, ...] = ()
    memory_updates: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.signal_type not in SIGNAL_TYPES:
            raise ValueError(
                f"signal_type must be one of {set(SIGNAL_TYPES)}, got {self.signal_type!r}"
            )
        if self.priority not in PRIORITY_LEVELS:
            raise ValueError(
                f"priority must be one of {set(PRIORITY_LEVELS)}, got {self.priority!r}"
            )
        if self.response_goal not in RESPONSE_GOALS:
            raise ValueError(
                f"response_goal must be one of {set(RESPONSE_GOALS)}, got {self.response_goal!r}"
            )
        if self.suggested_action not in SUGGESTED_ACTIONS:
            raise ValueError(
                f"suggested_action must be one of {set(SUGGESTED_ACTIONS)}, got {self.suggested_action!r}"
            )
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"confidence must be 0.0-1.0, got {self.confidence!r}"
            )
        if len(self.supporting_summary) > 200:
            raise ValueError(
                f"supporting_summary must be ≤ 200 chars, got {len(self.supporting_summary)}"
            )
        if self.safety_notes and not isinstance(self.safety_notes, tuple):
            object.__setattr__(self, "safety_notes", tuple(self.safety_notes))

    def to_dict(self) -> dict:
        """Return JSON-safe dict for logging / serialization."""
        return {
            "signal_type": self.signal_type,
            "priority": self.priority,
            "response_goal": self.response_goal,
            "primary_highlight": self.primary_highlight,
            "supporting_summary": self.supporting_summary,
            "related_to_active_topic": self.related_to_active_topic,
            "can_interrupt_current_topic": self.can_interrupt_current_topic,
            "suggested_action": self.suggested_action,
            "confidence": self.confidence,
            "safety_notes": list(self.safety_notes),
            "memory_updates": dict(self.memory_updates),
        }

    def __repr__(self) -> str:
        return (
            f"AgendaSignal(signal_type={self.signal_type!r}, priority={self.priority!r}, "
            f"confidence={self.confidence:.2f}, suggested_action={self.suggested_action!r})"
        )


# ── builder ──────────────────────────────────────────────────────────────────


class AgendaSignalBuilder:
    """Constructs AgendaSignal from raw chat context using structural heuristics.

    Uses **only** structural features (punctuation, user diversity, caps
    density, message frequency) — intentionally NOT the IntentClassifier,
    so the builder works even when the classifier returns the defective
    default string.
    """

    def build(
        self,
        intent_summary: dict[str, Any],
        context: list[dict[str, Any]],
        vibe: Optional[dict] = None,
        active_topic: Optional[dict] = None,
    ) -> Optional[AgendaSignal]:
        """Construct an AgendaSignal or return None when context is empty/useless."""

        # Guard: empty context → no useful signal
        if not context:
            return None

        signal_type = self._detect_signal_type(context)
        if signal_type == "low_signal" and len(context) < 2:
            return None

        priority = self._compute_priority(signal_type, context)
        primary_highlight = self._extract_primary_highlight(context)
        confidence = self._compute_confidence(context, signal_type)
        supporting_summary = self._build_supporting_summary(context, signal_type)

        related = bool(active_topic)
        can_interrupt = signal_type in {"direct_question", "trend"} and priority in {"high", "medium"}
        suggested_action = self._compute_suggested_action(signal_type, priority, related)
        safety_notes = self._check_safety(context)

        # ── response_goal mapping ────────────────────────────────────────
        _goal_map: dict[str, str] = {
            "direct_question": "surface_to_streamer",
            "hype": "add_color",
            "trend": "summarize_trend",
            "low_signal": "ignore",
        }
        response_goal = _goal_map.get(signal_type, "ignore")

        return AgendaSignal(
            signal_type=signal_type,
            priority=priority,
            response_goal=response_goal,
            primary_highlight=primary_highlight,
            supporting_summary=supporting_summary,
            related_to_active_topic=related,
            can_interrupt_current_topic=can_interrupt,
            suggested_action=suggested_action,
            confidence=confidence,
            safety_notes=tuple(safety_notes),
            memory_updates={},
        )

    # ── private heuristics ───────────────────────────────────────────────

    def _detect_signal_type(self, context: list[dict]) -> str:
        texts = [m.get("text", "") for m in context]
        users = {m.get("user", "") for m in context if m.get("user")}

        # Direct question: any message contains ? or ¿
        if any("?" in t or "¿" in t for t in texts):
            return "direct_question"

        # Hype: caps density > 30% AND ≥ 3 exclamation marks (¡ / !)
        caps_chars = sum(1 for t in texts for c in t if c.isupper())
        total_chars = sum(len(t) for t in texts) or 1
        exclamation_count = sum(t.count("!") + t.count("¡") for t in texts)
        if caps_chars / total_chars > 0.3 and exclamation_count >= 3:
            return "hype"

        # Trend: 3+ unique users
        if len(users) >= 3:
            return "trend"

        return "low_signal"

    def _extract_primary_highlight(self, context: list[dict]) -> Optional[dict]:
        """Pick the highest-scoring message as primary highlight."""
        best_score = 0.0
        best_msg: Optional[dict] = None
        for msg in context:
            text = str(msg.get("text", ""))
            if not text.strip():
                continue
            score = 0.5  # base
            if "?" in text or "¿" in text:
                score += 0.2
            caps = sum(1 for c in text if c.isupper())
            total = max(len(text), 1)
            if caps / total > 0.5:
                score += 0.1
            if len(text) > 30:
                score += 0.1
            if score > best_score:
                best_score = min(score, 1.0)
                best_msg = msg
        if best_msg is None:
            return None
        return {
            "author": best_msg.get("user", ""),
            "message": best_msg.get("text", ""),
            "score": round(best_score, 2),
        }

    def _compute_priority(self, signal_type: str, context: list[dict]) -> str:
        users = {m.get("user", "") for m in context if m.get("user")}
        n = len(context)
        if len(users) >= 5:
            return "high"
        if signal_type == "direct_question":
            return "medium"
        if signal_type == "trend" and n >= 3:
            return "medium"
        return "low"

    def _compute_confidence(self, context: list[dict], signal_type: str) -> float:
        n = len(context)
        if n >= 5:
            return 0.85
        if n >= 3:
            return 0.7
        return 0.5

    def _build_supporting_summary(self, context: list[dict], signal_type: str) -> str:
        texts = [m.get("text", "") for m in context if m.get("text", "").strip()]
        if not texts:
            return "(sin mensajes)"
        summary = " | ".join(texts[:5])
        if len(summary) > 200:
            summary = summary[:197] + "..."
        return summary

    def _compute_suggested_action(
        self, signal_type: str, priority: str, related_to_topic: bool
    ) -> str:
        if priority == "high":
            return "integrate_next_turn"
        if signal_type == "direct_question" and not related_to_topic:
            return "surface_to_streamer"
        if signal_type == "trend":
            return "acknowledge_briefly"
        return "ignore"

    def _check_safety(self, context: list[dict]) -> list[str]:
        """Flag risky patterns (placeholder — no LLM dependency)."""
        notes: list[str] = []
        texts = [m.get("text", "") for m in context]

        # Very short, repeated messages may be spam
        unique_short = {t.strip().lower() for t in texts if len(t.strip()) < 5}
        if len(unique_short) == 1 and len(texts) > 5:
            notes.append("spam_pattern_short_repeated")

        return notes


# ── convenience wrapper ──────────────────────────────────────────────────────


def build_agenda_signal(
    intent_summary: dict[str, Any],
    context: list[dict[str, Any]],
    vibe: Optional[dict] = None,
) -> Optional[AgendaSignal]:
    """Module-level convenience: build a signal in one call."""
    return AgendaSignalBuilder().build(intent_summary=intent_summary, context=context, vibe=vibe)
