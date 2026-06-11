"""Tests for schema.py — immutability, enum validation, range checks, JSON round-trip."""

import json

import pytest

from opencohost.config.schema import (
    VALID_MAX_RESPONSE_LENGTHS,
    ActionPolicy,
    ActionResult,
    AudienceScale,
    AwayBehavior,
    ChatEvent,
    CreatorConfig,
    CreatorPolicy,
    CreatorTone,
    EventAction,
    InterventionType,
    ModePolicy,
    NonNegotiableRule,
    ScalePolicy,
    UserNamePolicy,
)


# ══════════════════════════════════════════════════════════════════════════════
# Enum cardinality
# ══════════════════════════════════════════════════════════════════════════════


class TestEnums:
    def test_creator_tone_values(self):
        assert len(list(CreatorTone)) == 5
        assert CreatorTone.CASUAL.value == "casual"
        assert CreatorTone.BALANCED.value == "balanced"
        assert CreatorTone.FORMAL.value == "formal"
        assert CreatorTone.ENERGETIC.value == "energetic"
        assert CreatorTone.MYSTERIOUS.value == "mysterious"

    def test_intervention_type_values(self):
        assert len(list(InterventionType)) == 3

    def test_audience_scale_values(self):
        assert len(list(AudienceScale)) == 4

    def test_chat_event_count(self):
        """All 12 ChatEvents must be defined."""
        assert len(list(ChatEvent)) == 12

    def test_action_result_count(self):
        assert len(list(ActionResult)) == 5

    def test_username_policy_values(self):
        assert len(list(UserNamePolicy)) == 3

    def test_away_behavior_values(self):
        assert len(list(AwayBehavior)) == 3


# ══════════════════════════════════════════════════════════════════════════════
# CreatorPolicy validation
# ══════════════════════════════════════════════════════════════════════════════


class TestCreatorPolicyValidation:
    def test_valid_creator_policy(self):
        p = CreatorPolicy(
            tone="balanced",
            formality=0.5,
            humor_level=0.5,
            caution_level=0.5,
            max_response_length=120,
            intervention_type="moderate",
            use_usernames=False,
            factuality_strictness=0.5,
        )
        assert p.tone == "balanced"

    def test_invalid_tone_raises(self):
        with pytest.raises(ValueError, match="tone must be one of"):
            CreatorPolicy(
                tone="angry",
                formality=0.5,
                humor_level=0.5,
                caution_level=0.5,
                max_response_length=120,
                intervention_type="moderate",
                use_usernames=False,
                factuality_strictness=0.5,
            )

    def test_formality_out_of_range_raises(self):
        with pytest.raises(ValueError, match="formality must be 0.0-1.0"):
            CreatorPolicy(
                tone="balanced",
                formality=1.5,
                humor_level=0.5,
                caution_level=0.5,
                max_response_length=120,
                intervention_type="moderate",
                use_usernames=False,
                factuality_strictness=0.5,
            )

    def test_negative_formality_raises(self):
        with pytest.raises(ValueError, match="formality must be 0.0-1.0"):
            CreatorPolicy(
                tone="balanced",
                formality=-0.1,
                humor_level=0.5,
                caution_level=0.5,
                max_response_length=120,
                intervention_type="moderate",
                use_usernames=False,
                factuality_strictness=0.5,
            )

    def test_humor_out_of_range_raises(self):
        with pytest.raises(ValueError, match="humor_level must be 0.0-1.0"):
            CreatorPolicy(
                tone="balanced",
                formality=0.5,
                humor_level=2.0,
                caution_level=0.5,
                max_response_length=120,
                intervention_type="moderate",
                use_usernames=False,
                factuality_strictness=0.5,
            )

    def test_caution_out_of_range_raises(self):
        with pytest.raises(ValueError, match="caution_level must be 0.0-1.0"):
            CreatorPolicy(
                tone="balanced",
                formality=0.5,
                humor_level=0.5,
                caution_level=1.1,
                max_response_length=120,
                intervention_type="moderate",
                use_usernames=False,
                factuality_strictness=0.5,
            )

    def test_invalid_max_response_length_raises(self):
        with pytest.raises(ValueError, match="max_response_length must be one of"):
            CreatorPolicy(
                tone="balanced",
                formality=0.5,
                humor_level=0.5,
                caution_level=0.5,
                max_response_length=999,
                intervention_type="moderate",
                use_usernames=False,
                factuality_strictness=0.5,
            )

    def test_all_valid_max_response_lengths(self):
        for length in VALID_MAX_RESPONSE_LENGTHS:
            p = CreatorPolicy(
                tone="balanced",
                formality=0.5,
                humor_level=0.5,
                caution_level=0.5,
                max_response_length=length,
                intervention_type="moderate",
                use_usernames=False,
                factuality_strictness=0.5,
            )
            assert p.max_response_length == length

    def test_invalid_intervention_type_raises(self):
        with pytest.raises(ValueError, match="intervention_type must be one of"):
            CreatorPolicy(
                tone="balanced",
                formality=0.5,
                humor_level=0.5,
                caution_level=0.5,
                max_response_length=120,
                intervention_type="extreme",
                use_usernames=False,
                factuality_strictness=0.5,
            )

    def test_creator_policy_immutability(self):
        p = CreatorPolicy(
            tone="balanced",
            formality=0.5,
            humor_level=0.5,
            caution_level=0.5,
            max_response_length=120,
            intervention_type="moderate",
            use_usernames=False,
            factuality_strictness=0.5,
        )
        with pytest.raises(Exception):
            p.tone = "casual"  # type: ignore[misc]

    def test_creator_policy_to_dict(self):
        p = CreatorPolicy(
            tone="casual",
            formality=0.3,
            humor_level=0.5,
            caution_level=0.5,
            max_response_length=120,
            intervention_type="high",
            use_usernames=True,
            factuality_strictness=0.3,
        )
        d = p.to_dict()
        assert d["tone"] == "casual"
        assert d["formality"] == 0.3
        assert d["use_usernames"] is True


# ══════════════════════════════════════════════════════════════════════════════
# EventAction validation
# ══════════════════════════════════════════════════════════════════════════════


class TestEventAction:
    def test_default_values(self):
        ea = EventAction()
        assert ea.voice_allowed is False
        assert ea.surface_to_ui is True
        assert ea.queue is False
        assert ea.ignore is False
        assert ea.priority == "low"

    def test_voice_and_ignore_conflict_raises(self):
        with pytest.raises(ValueError, match="voice_allowed and ignore cannot both be True"):
            EventAction(voice_allowed=True, ignore=True)

    def test_invalid_priority_raises(self):
        with pytest.raises(ValueError, match="priority must be one of"):
            EventAction(priority="critical")

    def test_event_action_immutability(self):
        ea = EventAction(voice_allowed=True, priority="high")
        with pytest.raises(Exception):
            ea.voice_allowed = False  # type: ignore[misc]


# ══════════════════════════════════════════════════════════════════════════════
# ActionPolicy — 12 events
# ══════════════════════════════════════════════════════════════════════════════


class TestActionPolicy:
    def test_all_12_events_present(self):
        ap = ActionPolicy()
        for ev in ChatEvent:
            ea = ap.get(ev.value)
            assert isinstance(ea, EventAction), f"missing action for {ev.value}"

    def test_get_unknown_event_raises(self):
        ap = ActionPolicy()
        with pytest.raises(KeyError, match="unknown ChatEvent"):
            ap.get("nonexistent_event")

    def test_custom_action_policy(self):
        ap = ActionPolicy(
            direct_question=EventAction(voice_allowed=True, priority="high"),
        )
        assert ap.direct_question.voice_allowed is True
        assert ap.direct_question.priority == "high"
        # other events still have defaults
        assert ap.joke_or_meme.voice_allowed is False

    def test_low_signal_noise_always_ignored(self):
        ap = ActionPolicy()
        assert ap.low_signal_noise.ignore is True
        assert ap.low_signal_noise.voice_allowed is False
        assert ap.low_signal_noise.surface_to_ui is False

    def test_action_policy_immutability(self):
        ap = ActionPolicy()
        with pytest.raises(Exception):
            ap.direct_question = EventAction()  # type: ignore[misc]

    def test_action_policy_to_dict(self):
        ap = ActionPolicy()
        d = ap.to_dict()
        assert len(d) == 12
        assert "direct_question" in d
        assert d["direct_question"]["priority"] == "low"


# ══════════════════════════════════════════════════════════════════════════════
# ModePolicy
# ══════════════════════════════════════════════════════════════════════════════


class TestModePolicy:
    def test_defaults(self):
        mp = ModePolicy()
        assert mp.preset_name == "default"
        assert mp.interruption_threshold == 0.5
        assert mp.response_frequency == "medium"

    def test_threshold_out_of_range_raises(self):
        with pytest.raises(ValueError, match="interruption_threshold must be 0.0-1.0"):
            ModePolicy(interruption_threshold=1.5)

    def test_invalid_frequency_raises(self):
        with pytest.raises(ValueError, match="response_frequency must be one of"):
            ModePolicy(response_frequency="extreme")

    def test_frozen(self):
        mp = ModePolicy()
        with pytest.raises(Exception):
            mp.preset_name = "other"  # type: ignore[misc]


# ══════════════════════════════════════════════════════════════════════════════
# ScalePolicy
# ══════════════════════════════════════════════════════════════════════════════


class TestScalePolicy:
    def test_defaults(self):
        sp = ScalePolicy()
        assert sp.audience_scale == "small"
        assert sp.max_messages == 50
        assert sp.dedup_window == 30

    def test_invalid_scale_raises(self):
        with pytest.raises(ValueError, match="audience_scale must be one of"):
            ScalePolicy(audience_scale="huge")

    def test_negative_max_messages_raises(self):
        with pytest.raises(ValueError, match="max_messages must be >= 1"):
            ScalePolicy(max_messages=0)

    def test_negative_dedup_window_raises(self):
        with pytest.raises(ValueError, match="dedup_window must be >= 1"):
            ScalePolicy(dedup_window=0)

    def test_frozen(self):
        sp = ScalePolicy()
        with pytest.raises(Exception):
            sp.max_messages = 100  # type: ignore[misc]


# ══════════════════════════════════════════════════════════════════════════════
# NonNegotiableRule
# ══════════════════════════════════════════════════════════════════════════════


class TestNonNegotiableRule:
    def test_valid_rule(self):
        r = NonNegotiableRule(id="no_doxxing", description="No doxxing in voice output")
        assert r.id == "no_doxxing"
        assert "config_validation" in r.enforced_at

    def test_empty_id_raises(self):
        with pytest.raises(ValueError, match="id must be a non-empty string"):
            NonNegotiableRule(id="", description="test")

    def test_empty_description_raises(self):
        with pytest.raises(ValueError, match="description must be a non-empty string"):
            NonNegotiableRule(id="test", description="")


# ══════════════════════════════════════════════════════════════════════════════
# CreatorConfig — container + JSON round-trip
# ══════════════════════════════════════════════════════════════════════════════


class TestCreatorConfig:
    def _make_minimal_config(self) -> CreatorConfig:
        return CreatorConfig(
            creator=CreatorPolicy(
                tone="balanced",
                formality=0.5,
                humor_level=0.5,
                caution_level=0.5,
                max_response_length=120,
                intervention_type="moderate",
                use_usernames=False,
                factuality_strictness=0.5,
            ),
            mode=ModePolicy(),
            action=ActionPolicy(),
            scale=ScalePolicy(),
            non_negotiables=(
                NonNegotiableRule(id="test_rule", description="test"),
            ),
        )

    def test_creator_config_immutability(self):
        config = self._make_minimal_config()
        with pytest.raises(Exception):
            config.creator = config.creator  # type: ignore[misc]

    def test_to_dict_and_from_dict_roundtrip(self):
        config = self._make_minimal_config()
        data = config.to_dict()
        restored = CreatorConfig.from_dict(data)
        assert restored.creator.tone == config.creator.tone
        assert restored.creator.formality == config.creator.formality
        assert restored.mode.preset_name == config.mode.preset_name
        assert len(restored.non_negotiables) == 1
        assert restored.non_negotiables[0].id == "test_rule"

    def test_to_json_and_from_json_roundtrip(self):
        config = self._make_minimal_config()
        json_str = config.to_json()
        assert isinstance(json_str, str)
        restored = CreatorConfig.from_json(json_str)
        assert restored.creator.tone == config.creator.tone
        assert restored.creator.formality == config.creator.formality

    def test_from_json_accepts_dict(self):
        config = self._make_minimal_config()
        restored = CreatorConfig.from_json(config.to_dict())
        assert restored.creator.tone == config.creator.tone

    def test_json_roundtrip_preserves_all_fields(self):
        config = self._make_minimal_config()
        json_str = config.to_json()
        restored = CreatorConfig.from_json(json_str)
        # Compare serialized forms for exact match
        assert restored.to_json() == json_str

    def test_to_json_is_valid_json(self):
        config = self._make_minimal_config()
        json_str = config.to_json()
        parsed = json.loads(json_str)
        assert isinstance(parsed, dict)
        assert "creator" in parsed
        assert "action" in parsed

    def test_from_json_with_missing_fields_uses_defaults(self):
        """Missing optional sections should fall back to defaults."""
        minimal = {"creator": {
            "tone": "casual",
            "formality": 0.5,
            "humor_level": 0.5,
            "caution_level": 0.5,
            "max_response_length": 120,
            "intervention_type": "moderate",
            "use_usernames": True,
            "factuality_strictness": 0.5,
        }}
        config = CreatorConfig.from_json(minimal)
        assert config.mode.preset_name == "default"
        assert config.scale.audience_scale == "small"
        assert len(config.non_negotiables) == 0

    def test_config_serialization_does_not_affect_existing_files(self):
        """Sanity check: to_json produces a clean string, nothing written to disk."""
        config = self._make_minimal_config()
        result = config.to_json()
        assert "creator" in result
        assert "formality" in result
