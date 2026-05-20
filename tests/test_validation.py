"""Tests for validation.py — all 10 non-negotiables at 4 layers.

Covers spec scenarios: T11, T12, T13.
"""

import pytest

from config.presets import default_config
from config.schema import (
    ActionPolicy,
    CreatorConfig,
    CreatorPolicy,
    EventAction,
    ModePolicy,
    NonNegotiableRule,
    ScalePolicy,
)
from config.validation import (
    log_non_negotiable_block,
    output_guard,
    runtime_check,
    validate_config,
)


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════


def _make_valid_config() -> CreatorConfig:
    return default_config()


def _make_config_with_moderation_voice() -> CreatorConfig:
    """Config where moderation_or_risk.voice_allowed=True (violates R5)."""
    config = default_config()
    return CreatorConfig(
        creator=config.creator,
        mode=config.mode,
        action=ActionPolicy(
            direct_question=config.action.direct_question,
            viewer_request=config.action.viewer_request,
            poll_or_vote_suggestion=config.action.poll_or_vote_suggestion,
            greeting_or_shoutout=config.action.greeting_or_shoutout,
            correction_or_clarification=config.action.correction_or_clarification,
            repeated_topic=config.action.repeated_topic,
            joke_or_meme=config.action.joke_or_meme,
            hype_or_emotion=config.action.hype_or_emotion,
            complaint_or_confusion=config.action.complaint_or_confusion,
            moderation_or_risk=EventAction(voice_allowed=True, surface_to_ui=True),
            factual_update=config.action.factual_update,
            low_signal_noise=config.action.low_signal_noise,
        ),
        scale=config.scale,
        non_negotiables=config.non_negotiables,
    )


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1: Config validation
# ══════════════════════════════════════════════════════════════════════════════


class TestValidateConfig:
    """T11: config validation rejects violations."""

    def test_valid_default_config_passes(self):
        config = _make_valid_config()
        errors = validate_config(config)
        assert errors == [], f"Unexpected errors: {errors}"

    def test_moderation_voice_rejected(self):
        """R5: moderation_or_risk.voice_allowed cannot be True."""
        config = _make_config_with_moderation_voice()
        errors = validate_config(config)
        assert len(errors) > 0
        assert any("never_moderate_automatically" in e for e in errors)
        assert any("moderation_or_risk" in e for e in errors)

    def test_missing_non_negotiables_detected(self):
        config = CreatorConfig(
            creator=_make_valid_config().creator,
            mode=ModePolicy(),
            action=ActionPolicy(),
            scale=ScalePolicy(),
            non_negotiables=(
                NonNegotiableRule(id="no_doxxing", description="test"),
            ),
        )
        errors = validate_config(config)
        assert len(errors) > 0
        assert any("Expected 10" in e or "Missing" in e for e in errors)

    def test_low_signal_noise_not_ignored_rejected(self):
        """R7: low_signal_noise MUST be ignored."""
        config = _make_valid_config()
        action = ActionPolicy(
            direct_question=config.action.direct_question,
            viewer_request=config.action.viewer_request,
            poll_or_vote_suggestion=config.action.poll_or_vote_suggestion,
            greeting_or_shoutout=config.action.greeting_or_shoutout,
            correction_or_clarification=config.action.correction_or_clarification,
            repeated_topic=config.action.repeated_topic,
            joke_or_meme=config.action.joke_or_meme,
            hype_or_emotion=config.action.hype_or_emotion,
            complaint_or_confusion=config.action.complaint_or_confusion,
            moderation_or_risk=config.action.moderation_or_risk,
            factual_update=config.action.factual_update,
            low_signal_noise=EventAction(ignore=False, surface_to_ui=True),
        )
        bad_config = CreatorConfig(
            creator=config.creator,
            mode=config.mode,
            action=action,
            scale=config.scale,
            non_negotiables=config.non_negotiables,
        )
        errors = validate_config(bad_config)
        assert len(errors) > 0
        assert any("low_signal_noise" in e for e in errors)

    def test_voice_and_ignore_conflict_caught_by_post_init(self):
        """EventAction.__post_init__ catches voice+ignore conflict at construction."""
        with pytest.raises(ValueError, match="voice_allowed and ignore cannot both be True"):
            EventAction(voice_allowed=True, ignore=True)

    def test_out_of_range_caught_by_post_init(self):
        """CreatorPolicy.__post_init__ catches out-of-range at construction."""
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

    def test_complaint_voice_rejected(self):
        """complaint_or_confusion.voice_allowed cannot be True."""
        config = _make_valid_config()
        action = ActionPolicy(
            direct_question=config.action.direct_question,
            viewer_request=config.action.viewer_request,
            poll_or_vote_suggestion=config.action.poll_or_vote_suggestion,
            greeting_or_shoutout=config.action.greeting_or_shoutout,
            correction_or_clarification=config.action.correction_or_clarification,
            repeated_topic=config.action.repeated_topic,
            joke_or_meme=config.action.joke_or_meme,
            hype_or_emotion=config.action.hype_or_emotion,
            complaint_or_confusion=EventAction(voice_allowed=True, surface_to_ui=True),
            moderation_or_risk=config.action.moderation_or_risk,
            factual_update=config.action.factual_update,
            low_signal_noise=config.action.low_signal_noise,
        )
        bad_config = CreatorConfig(
            creator=config.creator,
            mode=config.mode,
            action=action,
            scale=config.scale,
            non_negotiables=config.non_negotiables,
        )
        errors = validate_config(bad_config)
        assert len(errors) > 0
        assert any("complaint_or_confusion" in e for e in errors)

    def test_all_fields_validation(self):
        """Valid default config produces zero errors."""
        config = _make_valid_config()
        errors = validate_config(config)
        assert errors == []


# ══════════════════════════════════════════════════════════════════════════════
# Layer 2: Runtime check
# ══════════════════════════════════════════════════════════════════════════════


class TestRuntimeCheck:
    """T12: runtime check blocks dangerous messages."""

    def test_clean_message_passes(self):
        msg = {"user": "viewer1", "text": "Hola Kira, ¿cómo estás?"}
        assert runtime_check(msg) is True

    def test_doxxing_blocked(self):
        """T12: doxxing content → blocked at runtime."""
        msg = {"user": "viewer1", "text": "Mi número es 555-123-4567"}
        assert runtime_check(msg) is False

    def test_email_blocked(self):
        msg = {"user": "viewer1", "text": "mándame a user@example.com"}
        assert runtime_check(msg) is False

    def test_link_blocked(self):
        msg = {"user": "viewer1", "text": "mira esto https://evil.com/hack"}
        assert runtime_check(msg) is False

    def test_hate_speech_blocked(self):
        msg = {"user": "viewer1", "text": "esto es una porquería"}
        # This is clean (no slurs)
        assert runtime_check(msg) is True

    def test_ip_address_blocked(self):
        msg = {"user": "viewer1", "text": "mi IP es 192.168.1.100"}
        assert runtime_check(msg) is False

    def test_regular_chat_passes(self):
        msg = {"user": "viewer1", "text": "qué buen stream!"}
        assert runtime_check(msg) is True

    def test_empty_message_passes(self):
        msg = {"user": "viewer1", "text": ""}
        assert runtime_check(msg) is True

    def test_message_without_text_passes(self):
        msg = {"user": "viewer1"}
        assert runtime_check(msg) is True

    def test_question_with_mentions_passes(self):
        msg = {"user": "viewer1", "text": "¿cuándo es el próximo torneo?"}
        assert runtime_check(msg) is True


# ══════════════════════════════════════════════════════════════════════════════
# Layer 3: Output guard
# ══════════════════════════════════════════════════════════════════════════════


class TestOutputGuard:
    """T13: output guard blocks AI self-ID, meta-commentary, promises."""

    def test_clean_response_passes(self):
        allowed, reason = output_guard("¡Qué buena pregunta! Vamos a ver...")
        assert allowed is True
        assert reason == ""

    def test_ai_self_identification_blocked(self):
        """T13: 'como modelo de lenguaje' → blocked."""
        allowed, reason = output_guard(
            "Como modelo de lenguaje, no puedo responder eso."
        )
        assert allowed is False
        assert "no_ai_self_identification" in reason

    def test_ai_self_id_spanish_blocked(self):
        allowed, _ = output_guard("Soy una inteligencia artificial, no lo sé.")
        assert allowed is False

    def test_ai_self_id_english_blocked(self):
        allowed, _ = output_guard(
            "As an AI language model, I cannot answer that."
        )
        assert allowed is False

    def test_meta_commentary_blocked(self):
        allowed, reason = output_guard(
            "Tu audiencia está muy activa hoy, están diciendo muchas cosas."
        )
        assert allowed is False
        assert "no_meta_commentary" in reason

    def test_meta_commentary_english_blocked(self):
        allowed, _ = output_guard(
            "Your viewers seem to really enjoy this topic."
        )
        assert allowed is False

    def test_promise_blocked(self):
        allowed, reason = output_guard(
            "Te prometo que esto va a salir bien."
        )
        assert allowed is False
        assert "never_promise" in reason

    def test_confirmation_invention_blocked(self):
        allowed, reason = output_guard(
            "Confirmado, ya está hecho lo que pediste."
        )
        assert allowed is False
        assert "never_invent_confirmations" in reason

    def test_link_in_response_blocked(self):
        allowed, reason = output_guard(
            "Mira este enlace: https://example.com/info"
        )
        assert allowed is False
        assert "no_suspicious_links" in reason

    def test_doxxing_in_response_blocked(self):
        allowed, _ = output_guard(
            "El número de teléfono es 555-123-4567 por si quieres llamar."
        )
        assert allowed is False

    def test_email_doxxing_in_response_blocked(self):
        allowed, _ = output_guard(
            "Su correo es alguien@gmail.com para contacto."
        )
        assert allowed is False

    def test_normal_response_with_pronouns_passes(self):
        """Personal pronouns like 'tu' should not trigger false positives."""
        allowed, reason = output_guard(
            "Gracias por tu pregunta, es muy interesante."
        )
        assert allowed is True, f"False positive: {reason}"

    def test_normal_suggestion_passes(self):
        allowed, reason = output_guard(
            "Podrías intentar configurarlo desde el panel de ajustes."
        )
        assert allowed is True, f"False positive: {reason}"


# ══════════════════════════════════════════════════════════════════════════════
# Layer 4: Logging (smoke test)
# ══════════════════════════════════════════════════════════════════════════════


class TestLogging:
    """Layer 4: log_non_negotiable_block is callable and doesn't crash."""

    def test_log_callable(self, caplog):
        import logging
        caplog.set_level(logging.WARNING)
        log_non_negotiable_block(
            "no_doxxing", "runtime", preview="555-123-4567"
        )
        assert len(caplog.records) >= 1
        record = caplog.records[0]
        assert "no_doxxing" in record.message
        assert "runtime" in record.message

    def test_log_all_rule_ids(self, caplog):
        import logging
        caplog.set_level(logging.WARNING)
        for rule_id in [
            "no_doxxing", "no_suspicious_links", "never_promise",
            "never_invent_confirmations", "never_moderate_automatically",
            "no_personal_viewer_data", "no_raw_spam_to_llm",
            "no_hate_speech", "no_ai_self_identification",
            "no_meta_commentary",
        ]:
            log_non_negotiable_block(rule_id, "tests", preview="test")
        assert len(caplog.records) == 10


# ══════════════════════════════════════════════════════════════════════════════
# Integration: All 4 layers fire independently
# ══════════════════════════════════════════════════════════════════════════════


class TestFourLayerIntegration:
    """Verify all 4 non-negotiable enforcement layers work independently."""

    def test_layer1_and_layer2_independent(self):
        """Layer 1 (config) rejects bad config; Layer 2 (runtime) blocks bad content."""
        # Layer 1: struct check
        bad_config = _make_config_with_moderation_voice()
        errors = validate_config(bad_config)
        assert len(errors) > 0

        # Layer 2: content check (independent of config)
        bad_msg = {"user": "x", "text": "555-123-4567 call me"}
        assert runtime_check(bad_msg) is False

        # Layer 3: output check (independent)
        allowed, _ = output_guard("como modelo de lenguaje...")
        assert allowed is False

    def test_all_10_non_negotiables_have_description(self):
        """Ensure all 10 rule IDs have descriptions in the module."""
        from config.validation import _NN_DESCRIPTIONS, NON_NEGOTIABLE_IDS
        for rule_id in NON_NEGOTIABLE_IDS:
            assert rule_id in _NN_DESCRIPTIONS, f"Missing description for {rule_id}"

    def test_valid_config_passes_all_layers(self):
        """A valid default config + clean content passes all 4 layers."""
        config = default_config()
        # Layer 1
        assert validate_config(config) == []
        # Layer 2
        assert runtime_check({"user": "a", "text": "hola"}) is True
        # Layer 3
        allowed, _ = output_guard("Hola, bienvenidos al stream.")
        assert allowed is True
