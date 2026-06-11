"""Creator Configuration Contract — rule-based configuration simulator.

Simulates Kira's chat-routing decisions using structural heuristics
(?, ¿, caps density, length, repetition, user count) — NO LLM calls,
NO keyword matching.

Input: list of chat messages with optional author.
Output: per-message ``SimResult`` with classification (ChatEvent) and
routing action (VOICE/PANEL/ALERT/IGNORED/QUEUED).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from opencohost.config.schema import (
    ActionPolicy,
    ActionResult,
    ChatEvent,
    CreatorConfig,
    EventAction,
)


# ══════════════════════════════════════════════════════════════════════════════
# SimResult
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class SimResult:
    """A single message's simulation result."""

    message: str
    author: str = ""
    classification: str = ""  # ChatEvent value
    action: str = ""  # ActionResult value
    example_text: str = ""
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": self.message,
            "author": self.author,
            "classification": self.classification,
            "action": self.action,
            "example_text": self.example_text,
            "reason": self.reason,
        }


# ══════════════════════════════════════════════════════════════════════════════
# Structural heuristic helpers
# ══════════════════════════════════════════════════════════════════════════════

# URL/link pattern (reusable)
_LINK_RE: re.Pattern = re.compile(r"https?://|www\.", re.IGNORECASE)


def _has_question(text: str) -> bool:
    """Check for structural question markers."""
    return "?" in text or "\u00bf" in text  # ?


def _caps_ratio(text: str) -> float:
    """Ratio of uppercase letters to total letters in text."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return 0.0
    return sum(1 for c in letters if c.isupper()) / len(letters)


def _exclamation_count(text: str) -> int:
    """Count exclamation marks (both ! and )."""
    return text.count("!") + text.count("\u00a1")  # ¡


def _non_alpha_ratio(text: str) -> float:
    """Ratio of non-alphanumeric characters (emoji, symbols)."""
    if not text:
        return 0.0
    alpha = sum(1 for c in text if c.isalnum() or c.isspace())
    return 1.0 - (alpha / len(text))


def _text_similarity(a: str, b: str) -> float:
    """Simple Jaccard-like similarity on character trigrams."""
    a = a.strip().lower()
    b = b.strip().lower()
    if not a or not b:
        return 0.0
    trigrams_a = {a[i:i + 3] for i in range(len(a) - 2)}
    trigrams_b = {b[i:i + 3] for i in range(len(b) - 2)}
    if not trigrams_a or not trigrams_b:
        return 0.0
    intersection = trigrams_a & trigrams_b
    union = trigrams_a | trigrams_b
    return len(intersection) / len(union)


# ══════════════════════════════════════════════════════════════════════════════
# ConfigSimulator
# ══════════════════════════════════════════════════════════════════════════════


class ConfigSimulator:
    """Rule-based simulator using the current CreatorConfig.

    Classification uses ONLY structural heuristics:
    - Question marks (? / ¿) → question-type events
    - Caps density + exclamation marks → hype/emotion events
    - Message length → distinguishes greetings vs factual updates
    - Repetition / similarity → repeated topics, spam
    - URL patterns → moderation/risk
    - User count → polls, trends
    - Non-alpha ratio → emoji/spam detection
    """

    def __init__(self, config: CreatorConfig):
        self._config = config
        self._action: ActionPolicy = config.action

    def simulate(self, messages: list[dict[str, Any]]) -> list[SimResult]:
        """Simulate routing for a list of chat messages.

        Args:
            messages: List of dicts with keys "text" (required) and "user" (optional).

        Returns:
            List of SimResult, one per input message (same order).
        """
        if not messages:
            return []

        results: list[SimResult] = []

        for idx, msg in enumerate(messages):
            text = str(msg.get("text", ""))
            author = str(msg.get("user", ""))

            # ── Step 1: Classify the message into a ChatEvent ─────────
            event = self._classify(text, messages, idx)

            # ── Step 2: Look up ActionPolicy for this event ───────────
            ea: EventAction = self._action.get(event)

            # ── Step 3: Determine ActionResult ────────────────────────
            action, example_text, reason = self._resolve_action(
                event, ea, text
            )

            results.append(SimResult(
                message=text,
                author=author,
                classification=event,
                action=action,
                example_text=example_text,
                reason=reason,
            ))

        return results

    # ── Classification ───────────────────────────────────────────────────

    def _classify(
        self,
        text: str,
        all_messages: list[dict[str, Any]],
        self_index: int = 0,
    ) -> str:
        """Classify a single message into a ChatEvent using structural heuristics."""
        t = text.strip()
        length = len(t)

        # ═══ Non-negotiable pre-checks (always enforced) ═══
        # Spam / noise always classified as low_signal_noise
        if length < 3:
            return ChatEvent.LOW_SIGNAL_NOISE.value

        # URL = moderation or risk (flag only)
        if _LINK_RE.search(t):
            return ChatEvent.MODERATION_OR_RISK.value

        # Pure emoji/symbol message
        if _non_alpha_ratio(t) > 0.8:
            return ChatEvent.LOW_SIGNAL_NOISE.value

        # ═══ Structural signals ═══
        has_q = _has_question(t)
        caps = _caps_ratio(t)
        excl = _exclamation_count(t)

        # 1. Question + HIGH caps → complaint_or_confusion (urgent/frustrated)
        if has_q and caps > 0.5:
            return ChatEvent.COMPLAINT_OR_CONFUSION.value

        # 2. Question mark → direct_question
        if has_q:
            return ChatEvent.DIRECT_QUESTION.value

        # 3. Repetition / trend check (check BEFORE greeting — short repeated
        #    messages are trends, not greetings)
        all_texts = [str(m.get("text", "")).strip() for m in all_messages]
        # Count exact duplicates (excluding self by index)
        exact_dupes = sum(
            1 for i, other in enumerate(all_texts)
            if i != self_index and other.lower() == t.lower()
        )
        if exact_dupes >= 2:
            return ChatEvent.REPEATED_TOPIC.value
        # Count similar messages (excluding self)
        similar_count = sum(
            1 for i, other in enumerate(all_texts)
            if i != self_index and _text_similarity(t, other) > 0.6
        )
        if similar_count >= 2:
            users = {m.get("user", "") for m in all_messages if m.get("user")}
            if len(users) >= 3:
                return ChatEvent.POLL_OR_VOTE_SUGGESTION.value
            return ChatEvent.REPEATED_TOPIC.value

        # 4. Correction pattern: messages starting with "no" → correction
        if t.lower().startswith("no,") or t.lower().startswith("no "):
            return ChatEvent.CORRECTION_OR_CLARIFICATION.value

        # 5. Mixed case bursts (moderate caps density) → joke_or_meme
        #    Check BEFORE hype — jokes have moderate caps, hype has high caps
        if 0.2 < caps < 0.6 and excl >= 1 and 10 <= length <= 100:
            return ChatEvent.JOKE_OR_MEME.value

        # 6. High caps + high exclamation → hype_or_emotion
        if caps > 0.4 and excl >= 2:
            return ChatEvent.HYPE_OR_EMOTION.value

        # 7. Very short → greeting_or_shoutout
        if length < 25 and caps < 0.3 and excl <= 1:
            return ChatEvent.GREETING_OR_SHOUTOUT.value

        # 8. Long, no caps, no excitement → factual_update
        if length > 150 and caps < 0.2 and excl == 0:
            return ChatEvent.FACTUAL_UPDATE.value

        # 9. Long-ish message → viewer_request
        if length > 40:
            return ChatEvent.VIEWER_REQUEST.value

        # 10. Default fallback
        return ChatEvent.LOW_SIGNAL_NOISE.value

    # ── Action resolution ─────────────────────────────────────────────────

    def _resolve_action(
        self,
        event: str,
        ea: EventAction,
        text: str,
    ) -> tuple[str, str, str]:
        """Map EventAction settings → ActionResult + example + reason."""
        # Safety: moderation_or_risk NEVER gets voice (non-negotiable R5)
        if event == ChatEvent.MODERATION_OR_RISK.value:
            return (
                ActionResult.PANEL.value,
                "[ALERTA] Posible contenido de riesgo detectado",
                "Moderation/risk flagged to panel only (non-negotiable R5)",
            )

        # low_signal_noise ALWAYS ignored (non-negotiable R7)
        if event == ChatEvent.LOW_SIGNAL_NOISE.value:
            return (
                ActionResult.IGNORED.value,
                "",
                "Low-signal noise always ignored (non-negotiable R7)",
            )

        # complaint_or_confusion NEVER gets voice
        if event == ChatEvent.COMPLAINT_OR_CONFUSION.value:
            return (
                ActionResult.PANEL.value,
                "[PANEL] El streamer ve esto en panel (prioridad alta)",
                "Complaint shown in panel, never voiced (safety rule)",
            )

        # If ignore is set → IGNORED
        if ea.ignore:
            return (
                ActionResult.IGNORED.value,
                "",
                f"Event {event} set to ignore in ActionPolicy",
            )

        # If voice_allowed → VOICE (or QUEUED if queue is set)
        if ea.voice_allowed:
            if ea.queue:
                return (
                    ActionResult.QUEUED.value,
                    f"[VOZ] {text[:80]}...",
                    f"Event {event} queued for voice (priority={ea.priority})",
                )
            return (
                ActionResult.VOICE.value,
                f"[VOZ] {text[:80]}",
                f"Event {event} routed to voice (priority={ea.priority})",
            )

        # If surface_to_ui → PANEL
        if ea.surface_to_ui:
            return (
                ActionResult.PANEL.value,
                f"[PANEL] {text[:80]}",
                f"Event {event} shown in panel (priority={ea.priority})",
            )

        # Default: IGNORED
        return (
            ActionResult.IGNORED.value,
            "",
            f"Event {event} has no routing destination set",
        )


# ══════════════════════════════════════════════════════════════════════════════
# Convenience function
# ══════════════════════════════════════════════════════════════════════════════


def simulate_with_config(
    config: CreatorConfig,
    messages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """One-liner: simulate and return list of dicts."""
    sim = ConfigSimulator(config)
    results = sim.simulate(messages)
    return [r.to_dict() for r in results]
