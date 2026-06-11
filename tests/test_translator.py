"""Tests for translator.py — deterministic mapping, completeness, composition.

Covers spec scenario T6.
"""

import pytest

from opencohost.config.schema import (
    ActionPolicy,
    ChatEvent,
    EventAction,
)
from opencohost.config.translator import (
    MAPPING_TABLE,
    ConfigTranslator,
    list_settings,
    list_values,
    validate_mapping,
)


# ══════════════════════════════════════════════════════════════════════════════
# T6: Translation determinism and completeness
# ══════════════════════════════════════════════════════════════════════════════


class TestMappingTable:
    """Structural validation of MAPPING_TABLE."""

    def test_all_6_settings_present(self):
        settings = list_settings()
        assert "preguntas" in settings
        assert "saludos" in settings
        assert "alertas" in settings
        assert "nombres" in settings
        assert "interrupcion" in settings
        assert "humor" in settings
        assert len(settings) == 6

    def test_each_setting_has_multiple_values(self):
        for setting in list_settings():
            values = list_values(setting)
            assert len(values) >= 2, f"{setting} has only {len(values)} values"

    def test_validate_mapping_passes(self):
        """All 12 ChatEvents must be covered by at least one mapping entry."""
        assert validate_mapping() is True

    def test_all_12_chat_events_covered(self):
        """Explicit check: every ChatEvent appears in at least one fragment key."""
        covered: set[str] = set()
        for setting, values in MAPPING_TABLE.items():
            for _value, fragment in values.items():
                for key in fragment:
                    parts = key.split(".", 1)
                    if len(parts) == 2:
                        covered.add(parts[0])
        all_events = {e.value for e in ChatEvent}
        assert covered == all_events, f"Missing: {all_events - covered}"


# ══════════════════════════════════════════════════════════════════════════════
# Translation determinism
# ══════════════════════════════════════════════════════════════════════════════


class TestTranslationDeterminism:
    """T6: same input → same output. No side effects."""

    def setup_method(self):
        self.translator = ConfigTranslator()

    def test_same_input_produces_same_output(self):
        """Calling translate_user_choice twice with same args returns identical result."""
        r1 = self.translator.translate_user_choice("preguntas", "voz")
        r2 = self.translator.translate_user_choice("preguntas", "voz")
        assert r1 == r2
        # Verify it's a copy, not the same object
        assert r1 is not r2

    def test_different_values_produce_different_fragments(self):
        r1 = self.translator.translate_user_choice("preguntas", "voz")
        r2 = self.translator.translate_user_choice("preguntas", "panel")
        assert r1 != r2

    def test_preguntas_voz_maps_voice(self):
        """T6: User sets 'Preguntas: voz' → direct_question.voice_allowed=true."""
        fragment = self.translator.translate_user_choice("preguntas", "voz")
        assert fragment.get("direct_question.voice_allowed") is True
        assert fragment.get("direct_question.surface_to_ui") is False

    def test_preguntas_panel_maps_panel(self):
        fragment = self.translator.translate_user_choice("preguntas", "panel")
        assert fragment.get("direct_question.voice_allowed") is False
        assert fragment.get("direct_question.surface_to_ui") is True
        assert fragment.get("direct_question.ignore") is False

    def test_preguntas_ignorar_maps_ignore(self):
        fragment = self.translator.translate_user_choice("preguntas", "ignorar")
        assert fragment.get("direct_question.ignore") is True

    def test_unknown_setting_raises(self):
        with pytest.raises(KeyError, match="unknown setting"):
            self.translator.translate_user_choice("color_favorito", "azul")

    def test_unknown_value_raises(self):
        with pytest.raises(KeyError, match="unknown value"):
            self.translator.translate_user_choice("preguntas", "telepatia")


# ══════════════════════════════════════════════════════════════════════════════
# Policy composition
# ══════════════════════════════════════════════════════════════════════════════


class TestComposePolicies:
    """Fragment → ActionPolicy composition."""

    def setup_method(self):
        self.translator = ConfigTranslator()

    def test_compose_single_fragment(self):
        fragment = self.translator.translate_user_choice("preguntas", "voz")
        ap = self.translator.compose_policies([fragment])
        assert ap.direct_question.voice_allowed is True
        assert ap.direct_question.surface_to_ui is False
        assert ap.direct_question.priority == "high"

    def test_compose_multiple_fragments(self):
        f1 = self.translator.translate_user_choice("preguntas", "panel")
        f2 = self.translator.translate_user_choice("saludos", "voz")
        ap = self.translator.compose_policies([f1, f2])
        assert ap.direct_question.voice_allowed is False
        assert ap.direct_question.surface_to_ui is True
        assert ap.greeting_or_shoutout.voice_allowed is True

    def test_later_fragments_override_earlier(self):
        f1 = self.translator.translate_user_choice("preguntas", "panel")
        f2 = self.translator.translate_user_choice("preguntas", "voz")  # overrides
        ap = self.translator.compose_policies([f1, f2])
        assert ap.direct_question.voice_allowed is True  # from f2
        assert ap.direct_question.surface_to_ui is False  # from f2

    def test_compose_with_explicit_base(self):
        base = ActionPolicy(
            direct_question=EventAction(voice_allowed=True, priority="high"),
        )
        fragment = self.translator.translate_user_choice("saludos", "voz")
        ap = self.translator.compose_policies([fragment], base=base)
        assert ap.direct_question.voice_allowed is True  # from base
        assert ap.greeting_or_shoutout.voice_allowed is True  # from fragment

    def test_compose_preserves_all_12_events(self):
        ap = self.translator.compose_policies([])
        for ev in ChatEvent:
            ea = ap.get(ev.value)
            assert isinstance(ea, EventAction)

    def test_compose_does_not_mutate_base(self):
        base = ActionPolicy()
        original_json = base.to_json() if hasattr(base, 'to_json') else str(base.to_dict())
        fragment = self.translator.translate_user_choice("preguntas", "voz")
        self.translator.compose_policies([fragment], base=base)
        # base should be unchanged
        assert base.direct_question.voice_allowed is False  # default

    def test_malformed_keys_are_skipped(self):
        """Keys without dot-separated event.field should be safely ignored."""
        bad_fragment = {
            "malformed_key": True,
            "direct_question.voice_allowed": True,
        }
        ap = self.translator.compose_policies([bad_fragment])
        assert ap.direct_question.voice_allowed is True
        # no error from malformed_key

    def test_unknown_event_in_fragment_skipped(self):
        bad_fragment = {"nonexistent_event.voice_allowed": True}
        ap = self.translator.compose_policies([bad_fragment])
        # should not raise; just ignore
        assert isinstance(ap, ActionPolicy)


# ══════════════════════════════════════════════════════════════════════════════
# Helper functions
# ══════════════════════════════════════════════════════════════════════════════


class TestHelpers:
    def test_list_settings(self):
        settings = list_settings()
        assert isinstance(settings, list)
        assert len(settings) == 6

    def test_list_values_valid(self):
        values = list_values("preguntas")
        assert "voz" in values
        assert "panel" in values
        assert "ignorar" in values

    def test_list_values_invalid_raises(self):
        with pytest.raises(KeyError, match="unknown setting"):
            list_values("nonexistent")
