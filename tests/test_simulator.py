"""Tests for simulator.py — rule-based classification and routing.

Covers spec scenarios: T7, T8, T9.
Tests: all 12 ChatEvents, edge cases, non-negotiable enforcement.
"""

import pytest

from opencohost.config.presets import (
    default_config,
    preset_comunidad,
    preset_show,
    preset_tecnico,
)
from opencohost.config.schema import (
    ActionResult,
    ChatEvent,
    CreatorConfig,
)
from opencohost.config.simulator import (
    ConfigSimulator,
    SimResult,
    simulate_with_config,
)


# ══════════════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════════════


def _msg(text: str, user: str = "viewer1") -> dict:
    return {"text": text, "user": user}


# ══════════════════════════════════════════════════════════════════════════════
# T7: Simulator classifies questions correctly
# ══════════════════════════════════════════════════════════════════════════════


class TestQuestionClassification:
    """T7: '¿van a dejar el en vivo?' → VOICE or PANEL, NOT IGNORED."""

    def setup_method(self):
        self.config = preset_comunidad()
        self.sim = ConfigSimulator(self.config)

    def test_direct_question_classified(self):
        results = self.sim.simulate([_msg("¿van a dejar el en vivo?")])
        assert len(results) == 1
        assert results[0].classification == ChatEvent.DIRECT_QUESTION.value

    def test_direct_question_not_ignored(self):
        """T7: Question must NOT be IGNORED."""
        results = self.sim.simulate([_msg("¿van a dejar el en vivo?")])
        assert results[0].action != ActionResult.IGNORED.value

    def test_direct_question_routed_to_voice_or_panel(self):
        results = self.sim.simulate([_msg("¿cuándo es el próximo stream?")])
        assert results[0].action in (
            ActionResult.VOICE.value,
            ActionResult.PANEL.value,
            ActionResult.QUEUED.value,
        ), f"Got {results[0].action}"

    def test_english_question_classified(self):
        results = self.sim.simulate([_msg("what time is the next stream?")])
        assert results[0].classification == ChatEvent.DIRECT_QUESTION.value

    def test_inverted_question_mark(self):
        results = self.sim.simulate([_msg("\u00bfcu\u00e1ndo empieza?")])  # ¿cuándo empieza?
        assert results[0].classification == ChatEvent.DIRECT_QUESTION.value


# ══════════════════════════════════════════════════════════════════════════════
# T8: Simulator handles noise/spam
# ══════════════════════════════════════════════════════════════════════════════


class TestNoiseAndSpam:
    """T8: 'aaaaaaaaaaaa' → IGNORED."""

    def setup_method(self):
        self.config = default_config()
        self.sim = ConfigSimulator(self.config)

    def test_repeated_chars_ignored(self):
        results = self.sim.simulate([_msg("aaaaaaaaaaaa")])
        assert results[0].action == ActionResult.IGNORED.value

    def test_very_short_message_ignored(self):
        results = self.sim.simulate([_msg("a")])
        assert results[0].action == ActionResult.IGNORED.value

    def test_empty_message_ignored(self):
        results = self.sim.simulate([_msg("")])
        assert results[0].action == ActionResult.IGNORED.value

    def test_spam_only_all_ignored(self):
        """T8: only spam messages → all IGNORED."""
        messages = [
            _msg("aaaa", "u1"),
            _msg("bb", "u2"),
            _msg("", "u3"),
            _msg("x", "u4"),
        ]
        results = self.sim.simulate(messages)
        assert len(results) == 4
        for r in results:
            assert r.action == ActionResult.IGNORED.value, (
                f"Expected IGNORED, got {r.action} for '{r.message}'"
            )

    def test_pure_emoji_ignored(self):
        results = self.sim.simulate([_msg("\U0001f600\U0001f525\U0001f44d")])
        assert results[0].action == ActionResult.IGNORED.value


# ══════════════════════════════════════════════════════════════════════════════
# T9: Empty input
# ══════════════════════════════════════════════════════════════════════════════


class TestEmptyInput:
    """T9: empty chat list → empty results, no error."""

    def test_empty_list_returns_empty(self):
        sim = ConfigSimulator(default_config())
        results = sim.simulate([])
        assert results == []

    def test_none_messages_handled(self):
        """Empty list is explicitly handled."""
        sim = ConfigSimulator(default_config())
        results = sim.simulate([])
        assert isinstance(results, list)
        assert len(results) == 0


# ══════════════════════════════════════════════════════════════════════════════
# All 12 ChatEvents classification
# ══════════════════════════════════════════════════════════════════════════════


class TestAllChatEvents:
    """Each of the 12 ChatEvents can be triggered by structural heuristics."""

    def setup_method(self):
        self.config = default_config()
        self.sim = ConfigSimulator(self.config)

    def test_direct_question(self):
        r = self.sim.simulate([_msg("¿cómo estás?")])
        assert r[0].classification == ChatEvent.DIRECT_QUESTION.value

    def test_hype_or_emotion(self):
        r = self.sim.simulate([_msg("¡¡¡VAMOS EQUIPO!!! ¡¡QUÉ BUENO!!")])
        assert r[0].classification == ChatEvent.HYPE_OR_EMOTION.value

    def test_greeting_or_shoutout(self):
        r = self.sim.simulate([_msg("hola a todos")])
        assert r[0].classification == ChatEvent.GREETING_OR_SHOUTOUT.value

    def test_factual_update(self):
        long_text = (
            "Actualización importante sobre el parche de mañana: "
            "se van a implementar cambios en el sistema de matchmaking "
            "que deberían reducir los tiempos de espera significativamente "
            "para todos los jugadores. También se arregla el bug del inventario."
        )
        r = self.sim.simulate([_msg(long_text)])
        assert r[0].classification == ChatEvent.FACTUAL_UPDATE.value

    def test_moderation_or_risk(self):
        r = self.sim.simulate([_msg("miren esto https://scam-site.com")])
        assert r[0].classification == ChatEvent.MODERATION_OR_RISK.value
        # R5: moderation NEVER gets voice
        assert r[0].action == ActionResult.PANEL.value

    def test_low_signal_noise(self):
        r = self.sim.simulate([_msg("k")])
        assert r[0].classification == ChatEvent.LOW_SIGNAL_NOISE.value
        assert r[0].action == ActionResult.IGNORED.value

    def test_complaint_or_confusion(self):
        r = self.sim.simulate([_msg("¿POR QUÉ NO FUNCIONA ESTO??")])
        assert r[0].classification == ChatEvent.COMPLAINT_OR_CONFUSION.value
        # Complaint should NEVER get voice
        assert r[0].action != ActionResult.VOICE.value

    def test_viewer_request(self):
        r = self.sim.simulate([_msg(
            "por favor podrían poner la canción de los créditos "
            "que es muy buena y todos la quieren escuchar"
        )])
        assert r[0].classification == ChatEvent.VIEWER_REQUEST.value

    def test_joke_or_meme(self):
        r = self.sim.simulate([_msg("jAjAjA QUe BuENo eL StrEaM!!")])
        assert r[0].classification == ChatEvent.JOKE_OR_MEME.value

    def test_correction_or_clarification(self):
        r = self.sim.simulate([_msg("no, eso no es correcto")])
        assert r[0].classification == ChatEvent.CORRECTION_OR_CLARIFICATION.value

    def test_repeated_topic(self):
        messages = [
            _msg("cuándo es el torneo", "u1"),
            _msg("cuándo es el torneo", "u2"),
            _msg("cuándo es el torneo", "u3"),
            _msg("cuándo es el torneo", "u4"),
        ]
        r = self.sim.simulate(messages)
        # At least one should be classified as repeated_topic or poll
        classifications = {res.classification for res in r}
        assert any(
            c in (ChatEvent.REPEATED_TOPIC.value, ChatEvent.POLL_OR_VOTE_SUGGESTION.value)
            for c in classifications
        ), f"Got classifications: {classifications}"

    def test_poll_or_vote_suggestion(self):
        messages = [
            _msg("voten por el mapa A", "u1"),
            _msg("voten por el mapa B", "u2"),
            _msg("voten por el mapa A", "u3"),
            _msg("voten por el mapa A", "u4"),
        ]
        r = self.sim.simulate(messages)
        classifications = {res.classification for res in r}
        assert any(
            c == ChatEvent.POLL_OR_VOTE_SUGGESTION.value
            for c in classifications
        ), f"Got: {classifications}"


# ══════════════════════════════════════════════════════════════════════════════
# Non-negotiable enforcement in simulator
# ══════════════════════════════════════════════════════════════════════════════


class TestSimulatorNonNegotiables:
    """Simulator must enforce non-negotiables regardless of config."""

    def test_moderation_never_voice(self):
        """R5: moderation_or_risk ALWAYS panel, never voice."""
        sim = ConfigSimulator(preset_show())
        r = sim.simulate([_msg("http://bad-link.com")])
        assert r[0].classification == ChatEvent.MODERATION_OR_RISK.value
        assert r[0].action == ActionResult.PANEL.value
        assert r[0].action != ActionResult.VOICE.value

    def test_low_signal_always_ignored(self):
        """R7: low_signal_noise always IGNORED."""
        sim = ConfigSimulator(preset_comunidad())
        r = sim.simulate([_msg("x")])
        assert r[0].action == ActionResult.IGNORED.value

    def test_complaint_never_voice_even_in_show_preset(self):
        """Even in 'show' preset, complaint/confusion never gets voice."""
        sim = ConfigSimulator(preset_show())
        r = sim.simulate([_msg("¿POR QUÉ NO ANDA NADA??")])
        assert r[0].classification == ChatEvent.COMPLAINT_OR_CONFUSION.value
        assert r[0].action != ActionResult.VOICE.value


# ══════════════════════════════════════════════════════════════════════════════
# Edge cases
# ══════════════════════════════════════════════════════════════════════════════


class TestSimulatorEdgeCases:
    """Edge cases: 100 messages, mixed languages, config-specific routing."""

    def test_100_messages_no_error(self):
        """Simulator handles 100 messages without error."""
        sim = ConfigSimulator(default_config())
        messages = [
            _msg(f"mensaje de prueba número {i}", f"user{i}")
            for i in range(100)
        ]
        results = sim.simulate(messages)
        assert len(results) == 100

    def test_mixed_languages(self):
        sim = ConfigSimulator(default_config())
        messages = [
            _msg("¿cómo estás?"),
            _msg("what time is it?"),
            _msg("como vai?"),
            _msg("comment ça va?"),
        ]
        results = sim.simulate(messages)
        assert len(results) == 4
        for r in results:
            assert r.classification != ""

    def test_simulate_with_config_convenience(self):
        config = default_config()
        results = simulate_with_config(config, [_msg("hola")])
        assert len(results) == 1
        assert isinstance(results[0], dict)
        assert "classification" in results[0]

    def test_sim_result_to_dict(self):
        sr = SimResult(
            message="hola",
            author="viewer1",
            classification=ChatEvent.GREETING_OR_SHOUTOUT.value,
            action=ActionResult.PANEL.value,
            example_text="[PANEL] hola",
            reason="Test",
        )
        d = sr.to_dict()
        assert d["message"] == "hola"
        assert d["classification"] == "greeting_or_shoutout"

    def test_config_specific_routing_comunidad(self):
        """Comunidad preset: greetings → voice (scale-gated)."""
        sim = ConfigSimulator(preset_comunidad())
        r = sim.simulate([_msg("hola a todos")])
        assert r[0].classification == ChatEvent.GREETING_OR_SHOUTOUT.value
        # comunidad preset has greeting voice_allowed=True
        assert r[0].action == ActionResult.VOICE.value

    def test_config_specific_routing_tecnico(self):
        """Técnico preset: corrections → voice."""
        sim = ConfigSimulator(preset_tecnico())
        r = sim.simulate([_msg("no, eso que dijiste no es así")])
        assert r[0].classification == ChatEvent.CORRECTION_OR_CLARIFICATION.value
        # tecnico has correction voice_allowed=True
        assert r[0].action == ActionResult.VOICE.value

    def test_question_with_default_config_goes_to_panel(self):
        """Default config: questions → panel, not voice."""
        sim = ConfigSimulator(default_config())
        r = sim.simulate([_msg("¿qué opinás de esto?")])
        assert r[0].classification == ChatEvent.DIRECT_QUESTION.value
        assert r[0].action == ActionResult.PANEL.value

    def test_question_with_comunidad_goes_to_voice(self):
        """Comunidad preset: questions → voice."""
        sim = ConfigSimulator(preset_comunidad())
        r = sim.simulate([_msg("¿qué opinás de esto?")])
        # comunidad: direct_question.voice_allowed=True
        assert r[0].action == ActionResult.VOICE.value
