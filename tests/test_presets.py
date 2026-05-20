"""Tests for presets.py — zero-config defaults and 4 named presets.

Covers spec scenarios: T1, T4, T5, T14.
"""

import pytest

from config.presets import (
    default_config,
    duplicate_preset,
    load_preset,
    preset_calmado,
    preset_comunidad,
    preset_show,
    preset_tecnico,
)
from config.schema import (
    ActionPolicy,
    ChatEvent,
    CreatorConfig,
    EventAction,
)


# ══════════════════════════════════════════════════════════════════════════════
# T1: Zero-config defaults
# ══════════════════════════════════════════════════════════════════════════════


class TestDefaultConfig:
    """T1: Fresh install, no user config → balanced defaults."""

    def test_default_returns_valid_creator_config(self):
        config = default_config()
        assert isinstance(config, CreatorConfig)

    def test_default_creator_is_balanced(self):
        config = default_config()
        assert config.creator.tone == "balanced"
        assert config.creator.formality == 0.5
        assert config.creator.humor_level == 0.5
        assert config.creator.caution_level == 0.5
        assert config.creator.factuality_strictness == 0.5

    def test_default_intervention_is_moderate(self):
        config = default_config()
        assert config.creator.intervention_type == "moderate"

    def test_default_usernames_disabled(self):
        config = default_config()
        assert config.creator.use_usernames is False

    def test_default_questions_go_to_panel(self):
        """T1: questions → panel (not voice)."""
        config = default_config()
        dq = config.action.direct_question
        assert dq.surface_to_ui is True
        assert dq.voice_allowed is False
        assert dq.ignore is False

    def test_default_greetings_are_ignored(self):
        """T1: greetings → ignore."""
        config = default_config()
        g = config.action.greeting_or_shoutout
        assert g.ignore is True

    def test_default_tech_alerts_panel_and_voice(self):
        """T1: technical alerts → panel + voice (correction_or_clarification)."""
        config = default_config()
        c = config.action.correction_or_clarification
        assert c.surface_to_ui is True
        assert c.voice_allowed is True

    def test_default_spam_is_ignored(self):
        """T1: spam → ignore (low_signal_noise always ignored)."""
        config = default_config()
        ls = config.action.low_signal_noise
        assert ls.ignore is True
        assert ls.voice_allowed is False

    def test_default_has_10_non_negotiables(self):
        config = default_config()
        assert len(config.non_negotiables) == 10

    def test_default_non_negotiable_ids(self):
        config = default_config()
        ids = {r.id for r in config.non_negotiables}
        expected = {
            "no_doxxing",
            "no_suspicious_links",
            "never_promise",
            "never_invent_confirmations",
            "never_moderate_automatically",
            "no_personal_viewer_data",
            "no_raw_spam_to_llm",
            "no_hate_speech",
            "no_ai_self_identification",
            "no_meta_commentary",
        }
        assert ids == expected

    def test_default_config_is_idempotent(self):
        """Calling default_config() twice returns distinct but equal configs."""
        c1 = default_config()
        c2 = default_config()
        assert c1.to_json() == c2.to_json()

    def test_default_all_12_events_have_actions(self):
        config = default_config()
        for ev in ChatEvent:
            ea = config.action.get(ev.value)
            assert isinstance(ea, EventAction), f"missing action for {ev.value}"


# ══════════════════════════════════════════════════════════════════════════════
# T4, T14: Presets
# ══════════════════════════════════════════════════════════════════════════════


class TestPresets:
    """T4, T5, T14: all 4 presets valid, priority comparison, duplicate."""

    def _assert_valid_config(self, config: CreatorConfig, name: str):
        """Every preset must produce a valid, complete CreatorConfig."""
        assert isinstance(config, CreatorConfig), f"{name} is not CreatorConfig"
        assert config.mode.preset_name == name, f"{name} preset_name mismatch"
        # All 12 events must have actions
        for ev in ChatEvent:
            ea = config.action.get(ev.value)
            assert isinstance(ea, EventAction), f"{name} missing {ev.value}"
        # No contradictory voice_allowed AND ignore
            assert not (ea.voice_allowed and ea.ignore), (
                f"{name} {ev.value}: voice_allowed+ignore conflict"
            )
        # Non-negotiables must be present
        assert len(config.non_negotiables) == 10, f"{name}: missing non-negotiables"

    def test_preset_comunidad_valid(self):
        config = preset_comunidad()
        self._assert_valid_config(config, "comunidad")
        assert config.creator.tone == "casual"
        assert config.creator.use_usernames is True
        assert config.creator.factuality_strictness == 0.3

    def test_preset_show_valid(self):
        config = preset_show()
        self._assert_valid_config(config, "show")
        assert config.creator.tone == "energetic"
        assert config.creator.humor_level == 0.9
        assert config.action.joke_or_meme.voice_allowed is True
        assert config.action.hype_or_emotion.voice_allowed is True

    def test_preset_tecnico_valid(self):
        config = preset_tecnico()
        self._assert_valid_config(config, "tecnico")
        assert config.creator.tone == "balanced"
        assert config.creator.factuality_strictness == 0.95
        assert config.creator.use_usernames is False
        # corrections → voice
        assert config.action.correction_or_clarification.voice_allowed is True
        # no greetings
        assert config.action.greeting_or_shoutout.ignore is True

    def test_preset_calmado_valid(self):
        config = preset_calmado()
        self._assert_valid_config(config, "calmado")
        assert config.creator.tone == "casual"
        assert config.creator.intervention_type == "low"
        assert config.mode.interruption_threshold == 0.8
        # Only questions have voice
        assert config.action.direct_question.voice_allowed is True

    def test_preset_tecnico_questions_priority_over_jokes(self):
        """T5: Questions score higher priority than jokes in Técnico."""
        config = preset_tecnico()
        assert config.action.direct_question.priority == "high"
        assert config.action.joke_or_meme.priority in ("low",)
        # Jokes ignored in tecnico
        assert config.action.joke_or_meme.ignore is True

    def test_load_preset_by_name(self):
        for name in ("comunidad", "show", "tecnico", "calmado", "default"):
            config = load_preset(name)
            assert isinstance(config, CreatorConfig)

    def test_load_preset_unknown_falls_back(self):
        config = load_preset("nonexistent_preset_xyz")
        assert config.mode.preset_name == "default"
        assert config.creator.tone == "balanced"

    def test_duplicate_preset(self):
        """T14: Duplicate creates named copy with identical values."""
        original = preset_comunidad()
        dup = duplicate_preset("comunidad", "Mi Comunidad")
        assert dup.mode.preset_name == "Mi Comunidad"
        # All action values identical
        for ev in ChatEvent:
            orig_ea = original.action.get(ev.value)
            dup_ea = dup.action.get(ev.value)
            assert orig_ea == dup_ea, f"{ev.value} differs"
        # Creator values identical
        assert dup.creator.tone == original.creator.tone
        assert dup.creator.formality == original.creator.formality
        # Non-negotiables preserved
        assert len(dup.non_negotiables) == 10

    def test_duplicate_preset_is_valid(self):
        dup = duplicate_preset("show", "Show Copy")
        self._assert_valid_config(dup, "Show Copy")

    def test_no_conflicting_events_in_any_preset(self):
        """No preset should have voice_allowed=True AND ignore=True on same event."""
        presets = [
            ("comunidad", preset_comunidad()),
            ("show", preset_show()),
            ("tecnico", preset_tecnico()),
            ("calmado", preset_calmado()),
            ("default", default_config()),
        ]
        for name, config in presets:
            for ev in ChatEvent:
                ea = config.action.get(ev.value)
                assert not (ea.voice_allowed and ea.ignore), (
                    f"{name}.{ev.value}: voice_allowed + ignore conflict"
                )

    def test_moderation_never_voice_in_any_preset(self):
        """Moderation/risk events must NEVER have voice_allowed=True."""
        presets = [
            ("comunidad", preset_comunidad()),
            ("show", preset_show()),
            ("tecnico", preset_tecnico()),
            ("calmado", preset_calmado()),
            ("default", default_config()),
        ]
        for name, config in presets:
            mr = config.action.moderation_or_risk
            assert mr.voice_allowed is False, f"{name}: moderation has voice!"
            cc = config.action.complaint_or_confusion
            assert cc.voice_allowed is False, f"{name}: complaint has voice!"
