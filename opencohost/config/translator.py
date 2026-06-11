"""Creator Configuration Contract — translation layer.

Maps user-facing settings (Spanish labels) to ``ActionPolicy`` field
adjustments.  Pure data-driven: a module-level ``MAPPING_TABLE`` nested
dict drives every translation — NO if/else chains.
"""

from __future__ import annotations

from typing import Any

from opencohost.config.schema import (
    VALID_PRIORITIES,
    ActionPolicy,
    ChatEvent,
    EventAction,
)


# ══════════════════════════════════════════════════════════════════════════════
# PolicyFragment — what one user choice contributes
# ══════════════════════════════════════════════════════════════════════════════

# A PolicyFragment is a dict keyed by "<event>.<field>" → value.
# Example: {"direct_question.voice_allowed": True, "direct_question.surface_to_ui": False}
PolicyFragment = dict[str, bool | str]


# ══════════════════════════════════════════════════════════════════════════════
# MAPPING_TABLE — the single source of truth for user-choice → policy mapping
# ══════════════════════════════════════════════════════════════════════════════
#
# Structure:
#   MAPPING_TABLE[setting][value] → PolicyFragment
#
# 6 user-facing settings × 2–3 options each → covers all 12 ChatEvents.

MAPPING_TABLE: dict[str, dict[str, PolicyFragment]] = {
    # ── Preguntas del chat ──────────────────────────────────────────────
    "preguntas": {
        "ignorar": {
            "direct_question.ignore": True,
            "direct_question.voice_allowed": False,
            "direct_question.surface_to_ui": False,
            "viewer_request.ignore": True,
            "viewer_request.voice_allowed": False,
            "viewer_request.surface_to_ui": False,
            "poll_or_vote_suggestion.ignore": True,
            "poll_or_vote_suggestion.voice_allowed": False,
            "poll_or_vote_suggestion.surface_to_ui": False,
            "repeated_topic.ignore": True,
            "repeated_topic.voice_allowed": False,
            "repeated_topic.surface_to_ui": False,
            # low_signal_noise is ALWAYS ignored — forced here for coverage
            "low_signal_noise.ignore": True,
            "low_signal_noise.voice_allowed": False,
            "low_signal_noise.surface_to_ui": False,
        },
        "panel": {
            "direct_question.voice_allowed": False,
            "direct_question.surface_to_ui": True,
            "direct_question.ignore": False,
            "viewer_request.voice_allowed": False,
            "viewer_request.surface_to_ui": True,
            "viewer_request.ignore": False,
            "poll_or_vote_suggestion.voice_allowed": False,
            "poll_or_vote_suggestion.surface_to_ui": True,
            "poll_or_vote_suggestion.ignore": False,
            "repeated_topic.voice_allowed": False,
            "repeated_topic.surface_to_ui": True,
            "repeated_topic.ignore": False,
            "low_signal_noise.ignore": True,
            "low_signal_noise.voice_allowed": False,
            "low_signal_noise.surface_to_ui": False,
        },
        "voz": {
            "direct_question.voice_allowed": True,
            "direct_question.surface_to_ui": False,
            "direct_question.ignore": False,
            "direct_question.priority": "high",
            "viewer_request.voice_allowed": True,
            "viewer_request.surface_to_ui": False,
            "viewer_request.ignore": False,
            "poll_or_vote_suggestion.voice_allowed": True,
            "poll_or_vote_suggestion.surface_to_ui": False,
            "poll_or_vote_suggestion.ignore": False,
            "repeated_topic.voice_allowed": True,
            "repeated_topic.surface_to_ui": False,
            "repeated_topic.ignore": False,
            "low_signal_noise.ignore": True,
            "low_signal_noise.voice_allowed": False,
            "low_signal_noise.surface_to_ui": False,
        },
    },

    # ── Saludos / shoutouts ─────────────────────────────────────────────
    "saludos": {
        "ignorar": {
            "greeting_or_shoutout.ignore": True,
            "greeting_or_shoutout.voice_allowed": False,
            "greeting_or_shoutout.surface_to_ui": False,
        },
        "panel": {
            "greeting_or_shoutout.ignore": False,
            "greeting_or_shoutout.voice_allowed": False,
            "greeting_or_shoutout.surface_to_ui": True,
        },
        "voz": {
            "greeting_or_shoutout.ignore": False,
            "greeting_or_shoutout.voice_allowed": True,
            "greeting_or_shoutout.surface_to_ui": False,
            "greeting_or_shoutout.priority": "medium",
        },
    },

    # ── Alertas técnicas ────────────────────────────────────────────────
    "alertas": {
        "panel": {
            "correction_or_clarification.voice_allowed": False,
            "correction_or_clarification.surface_to_ui": True,
            "correction_or_clarification.ignore": False,
            "factual_update.voice_allowed": False,
            "factual_update.surface_to_ui": True,
            "factual_update.ignore": False,
            "moderation_or_risk.surface_to_ui": True,
            "moderation_or_risk.voice_allowed": False,
            "moderation_or_risk.ignore": False,
        },
        "panel_voz": {
            "correction_or_clarification.voice_allowed": True,
            "correction_or_clarification.surface_to_ui": True,
            "correction_or_clarification.ignore": False,
            "correction_or_clarification.priority": "high",
            "factual_update.voice_allowed": True,
            "factual_update.surface_to_ui": True,
            "factual_update.ignore": False,
            "factual_update.priority": "medium",
            "moderation_or_risk.surface_to_ui": True,
            "moderation_or_risk.voice_allowed": False,
            "moderation_or_risk.ignore": False,
        },
    },

    # ── Nombres de usuario ──────────────────────────────────────────────
    "nombres": {
        "si": {
            "direct_question.use_usernames": True,
            "viewer_request.use_usernames": True,
            "greeting_or_shoutout.use_usernames": True,
        },
        "no": {
            "direct_question.use_usernames": False,
            "viewer_request.use_usernames": False,
            "greeting_or_shoutout.use_usernames": False,
        },
        "a_veces": {
            "direct_question.use_usernames": True,
            "viewer_request.use_usernames": False,
            "greeting_or_shoutout.use_usernames": True,
        },
    },

    # ── Interrupción ────────────────────────────────────────────────────
    "interrupcion": {
        "baja": {
            "interruption_threshold": 0.8,
            "complaint_or_confusion.voice_allowed": False,
            "complaint_or_confusion.surface_to_ui": True,
            "complaint_or_confusion.priority": "high",
        },
        "media": {
            "interruption_threshold": 0.5,
            "complaint_or_confusion.voice_allowed": False,
            "complaint_or_confusion.surface_to_ui": True,
            "complaint_or_confusion.priority": "high",
        },
        "alta": {
            "interruption_threshold": 0.3,
            "complaint_or_confusion.voice_allowed": False,
            "complaint_or_confusion.surface_to_ui": True,
            "complaint_or_confusion.priority": "high",
        },
    },

    # ── Humor / memes ───────────────────────────────────────────────────
    "humor": {
        "ignorar": {
            "joke_or_meme.ignore": True,
            "joke_or_meme.voice_allowed": False,
            "joke_or_meme.surface_to_ui": False,
            "hype_or_emotion.ignore": True,
            "hype_or_emotion.voice_allowed": False,
            "hype_or_emotion.surface_to_ui": False,
        },
        "voz": {
            "joke_or_meme.ignore": False,
            "joke_or_meme.voice_allowed": True,
            "joke_or_meme.surface_to_ui": False,
            "joke_or_meme.priority": "high",
            "hype_or_emotion.ignore": False,
            "hype_or_emotion.voice_allowed": True,
            "hype_or_emotion.surface_to_ui": False,
            "hype_or_emotion.priority": "high",
        },
        "panel": {
            "joke_or_meme.ignore": False,
            "joke_or_meme.voice_allowed": False,
            "joke_or_meme.surface_to_ui": True,
            "hype_or_emotion.ignore": False,
            "hype_or_emotion.voice_allowed": False,
            "hype_or_emotion.surface_to_ui": True,
        },
    },
}


# ══════════════════════════════════════════════════════════════════════════════
# ConfigTranslator
# ══════════════════════════════════════════════════════════════════════════════


class ConfigTranslator:
    """Translates user-facing choices → internal ``ActionPolicy`` settings.

    Data-driven: all logic flows through ``MAPPING_TABLE``.
    """

    def translate_user_choice(self, setting: str, value: str) -> PolicyFragment:
        """Look up one (setting, value) pair in the mapping table.

        Returns:
            A PolicyFragment dict of ``"event.field"`` → value entries.

        Raises:
            KeyError: if setting or value is unknown.
        """
        try:
            return dict(MAPPING_TABLE[setting][value])
        except KeyError as exc:
            available_settings = list(MAPPING_TABLE.keys())
            if setting not in MAPPING_TABLE:
                raise KeyError(
                    f"unknown setting {setting!r}. Available: {available_settings}"
                ) from exc
            available_values = list(MAPPING_TABLE[setting].keys())
            raise KeyError(
                f"unknown value {value!r} for setting {setting!r}. "
                f"Available: {available_values}"
            ) from exc

    def compose_policies(
        self,
        fragments: list[PolicyFragment],
        base: ActionPolicy | None = None,
    ) -> ActionPolicy:
        """Merge a list of PolicyFragments into a single ``ActionPolicy``.

        Args:
            fragments: Ordered list of PolicyFragment dicts. Later entries
                       override earlier ones for the same key.
            base: Optional starting ActionPolicy. If None, uses defaults.

        Returns:
            A new ActionPolicy with all fragment adjustments applied.
        """
        if base is None:
            base = ActionPolicy()

        # Build a mutable dict keyed by ChatEvent value → dict of field overrides
        merged: dict[str, dict[str, Any]] = {}
        for ev in ChatEvent:
            ea: EventAction = getattr(base, ev.value)
            merged[ev.value] = ea.to_dict()

        # Apply each fragment in order
        for fragment in fragments:
            for key, val in fragment.items():
                # key is "event.field", e.g. "direct_question.voice_allowed"
                parts = key.split(".", 1)
                if len(parts) != 2:
                    continue  # malformed key — skip
                event_name, field_name = parts
                if event_name not in merged:
                    continue
                merged[event_name][field_name] = val

        # Build ActionPolicy from merged dict
        kwargs: dict[str, EventAction] = {}
        for ev in ChatEvent:
            d = merged[ev.value]
            kwargs[ev.value] = EventAction(
                voice_allowed=bool(d.get("voice_allowed", False)),
                surface_to_ui=bool(d.get("surface_to_ui", True)),
                queue=bool(d.get("queue", False)),
                ignore=bool(d.get("ignore", False)),
                priority=str(d.get("priority", "low")),
            )

        return ActionPolicy(**kwargs)


# ══════════════════════════════════════════════════════════════════════════════
# Validation helpers
# ══════════════════════════════════════════════════════════════════════════════


def validate_mapping() -> bool:
    """Assert that all 12 ChatEvents have complete coverage in the mapping table.

    "Complete" means every event is referenced in at least one setting/value pair.
    Returns True if the mapping is complete; raises AssertionError otherwise.
    """
    covered: set[str] = set()
    for setting, values in MAPPING_TABLE.items():
        for _value, fragment in values.items():
            for key in fragment:
                parts = key.split(".", 1)
                if len(parts) == 2:
                    covered.add(parts[0])

    all_events: set[str] = {e.value for e in ChatEvent}
    missing = all_events - covered
    if missing:
        raise AssertionError(
            f"MAPPING_TABLE is missing coverage for: {sorted(missing)}"
        )
    return True


def list_settings() -> list[str]:
    """Return the list of user-facing setting names (Spanish labels)."""
    return list(MAPPING_TABLE.keys())


def list_values(setting: str) -> list[str]:
    """Return the available values for a given setting."""
    if setting not in MAPPING_TABLE:
        raise KeyError(f"unknown setting: {setting!r}")
    return list(MAPPING_TABLE[setting].keys())
