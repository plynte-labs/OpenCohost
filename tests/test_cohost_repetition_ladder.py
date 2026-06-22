"""Tests for the regenerate-on-duplicate ladder (Strict TDD — RED first).

Section A: sentence_trim.py unit tests (tests 1–5)
Section B: controller integration tests (tests 6–11)
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# Section A — sentence_trim unit tests
# ---------------------------------------------------------------------------

class TestTrimTrailingRepeatedSentences:
    """Unit tests for opencohost.smart_aggregator.sentence_trim."""

    def _trim(self, text, recent_texts=None, threshold=0.85):
        from opencohost.smart_aggregator.sentence_trim import (
            trim_trailing_repeated_sentences,
        )
        return trim_trailing_repeated_sentences(
            text, recent_texts or [], threshold=threshold
        )

    def test_trim_removes_trailing_dup_sentence(self):
        """Trailing sentence that is a near-dup of a recent sentence is removed.
        The body (first sentence) is kept and salvageable=True."""
        recent = ["el problema con los modelos grandes es la latencia."]
        text = "Me parece que la latencia es un desafío real. El problema con los modelos grandes es la latencia."
        trimmed, salvageable = self._trim(text, recent_texts=[recent[0]])
        assert salvageable is True
        assert trimmed.strip() != ""
        # The trimmed result should NOT contain the dup sentence verbatim
        assert "el problema con los modelos grandes" not in trimmed.lower()
        # But it SHOULD keep the first sentence
        assert "latencia es un desafío" in trimmed.lower() or "latencia" in trimmed.lower()

    def test_trim_does_not_remove_common_words(self):
        """Word-level repetition of common words ('por qué', 'o sea') does NOT
        trigger sentence removal — the logic is sentence-level, not word-level."""
        recent = ["por qué siempre pasa esto."]
        # Both sentences share "por qué" but they are not near-dup sentences
        text = "Por qué siempre tarda tanto. O sea, el sistema debería ser más rápido."
        trimmed, salvageable = self._trim(text, recent_texts=[recent[0]])
        # Should NOT remove the second sentence — "por qué siempre" ≈ partial match
        # but sentence similarity of "por qué siempre tarda tanto" vs
        # "por qué siempre pasa esto" should be BELOW 0.85 threshold
        assert salvageable is True
        assert "debería ser más rápido" in trimmed

    def test_trim_whole_output_dup_returns_empty(self):
        """When the entire output is a near-duplicate of a recent text, return ('', False)."""
        recent = "La latencia del modelo depende del hardware disponible."
        text = "La latencia del modelo depende del hardware disponible."
        trimmed, salvageable = self._trim(text, recent_texts=[recent])
        assert trimmed == ""
        assert salvageable is False

    def test_trim_mid_text_dup_not_touched(self):
        """Duplicate sentence in the MIDDLE of text is left alone — only trailing dups removed."""
        dup = "el modelo tarda mucho en responder."
        recent = [dup]
        # dup appears in the middle, not at the end
        text = f"Mirá, {dup} Pero eso tiene solución si ajustás la configuración correctamente."
        trimmed, salvageable = self._trim(text, recent_texts=recent)
        # The last sentence is NEW, so nothing should be stripped
        assert "solución" in trimmed
        assert "configuración" in trimmed

    def test_trim_only_at_sentence_boundaries(self):
        """Result always ends at a sentence boundary (. ! ?) — never mid-sentence."""
        recent = ["y la respuesta del modelo fue rápida."]
        text = "El modelo respondió muy bien hoy. Y la respuesta del modelo fue rápida."
        trimmed, salvageable = self._trim(text, recent_texts=recent)
        if trimmed:
            # Result must end with a sentence-ending character
            assert trimmed[-1] in ".!?…", f"Result does not end at sentence boundary: {trimmed!r}"


# ---------------------------------------------------------------------------
# Section B — controller integration tests
# ---------------------------------------------------------------------------

def _make_controller(**kwargs):
    """Create a KiraAgendaController with an active topic for testing."""
    from opencohost.smart_aggregator.kira_agenda_controller import (
        KiraAgendaController,
        TopicStatus,
    )
    ctrl = KiraAgendaController(**kwargs)
    ctrl.state = __import__(
        "opencohost.smart_aggregator.kira_agenda_controller",
        fromlist=["AgendaState"],
    ).AgendaState.WAITING_SIGNAL
    topic = ctrl.add_topic("Test Topic", approved=True)
    ctrl.active_topic = topic
    topic.status = TopicStatus.ACTIVE
    return ctrl


class TestLadderControllerIntegration:

    def test_ladder_clean_text_committed_not_dup(self):
        """When trailing sentence is dup, the TRIMMED text is committed to last_outputs,
        not the full original output (which contains the dup sentence at the end)."""
        ctrl = _make_controller()
        # Seed a recent output
        ctrl.record_accepted_output("La latencia del modelo depende del hardware.")
        outputs_before = list(ctrl.last_outputs)

        # Output where only the trailing sentence is a dup
        output = "Creo que es un tema interesante para explorar. La latencia del modelo depende del hardware."
        result = ctrl.accept_output(output)
        # Trim should have salvaged and returned True
        assert result is True

        outputs_after = ctrl.last_outputs
        # The newly added entry (the last one that wasn't there before) must NOT be
        # the FULL original output (which includes the trailing dup sentence)
        new_entries = [s for s in outputs_after if s not in outputs_before]
        assert len(new_entries) >= 1, "No new entry was committed to last_outputs"
        full_output_normalized = " ".join(output.lower().split())
        for entry in new_entries:
            assert entry != full_output_normalized, (
                f"Full (un-trimmed) output was committed: {entry!r}"
            )
        # The committed entry should contain the FIRST (good) sentence
        first_sentence_fragment = "interesante"
        assert any(first_sentence_fragment in entry for entry in new_entries), (
            "Trimmed text was not committed to last_outputs"
        )

    def test_ladder_arms_regen_on_unsalvageable_dup(self):
        """When the entire output is a near-dup and can't be salvaged,
        _regen_active is True and _rejected_phrases is populated."""
        ctrl = _make_controller()
        dup_text = "La latencia del modelo depende del hardware disponible siempre."
        ctrl.record_accepted_output(dup_text)
        # Submit nearly identical output
        result = ctrl.accept_output(dup_text)
        assert result is False
        assert ctrl._regen_active is True
        assert len(ctrl._rejected_phrases) >= 1

    def test_ladder_build_prompt_includes_dup_block_when_regen_active(self):
        """When _regen_active=True and _rejected_phrases is populated,
        _build_prompt includes ANTI-REPETICIÓN FORZADA guidance."""
        ctrl = _make_controller()
        ctrl._regen_active = True
        ctrl._rejected_phrases = ["apertura que se repite mucho"]
        prompt = ctrl._build_prompt(instruction="test")
        assert "ANTI-REPETICIÓN FORZADA" in prompt
        assert "apertura que se repite mucho" in prompt

    def test_ladder_build_prompt_omits_dup_block_when_inactive(self):
        """When _regen_active=False, prompt does NOT contain ANTI-REPETICIÓN FORZADA."""
        ctrl = _make_controller()
        ctrl._regen_active = False
        ctrl._rejected_phrases = []
        prompt = ctrl._build_prompt(instruction="test")
        assert "ANTI-REPETICIÓN FORZADA" not in prompt

    def test_ladder_bounded_failure_count_exhausts_topic(self):
        """After 2 failures the existing failure_count>=2 guard exhausts the topic.
        The ladder never loops unbounded."""
        ctrl = _make_controller(max_turns_per_topic=3)
        dup_text = "La latencia del modelo depende del hardware disponible siempre."
        ctrl.record_accepted_output(dup_text)
        # First failure
        ctrl.accept_output(dup_text)
        first_failure_count = ctrl.failure_count
        assert first_failure_count >= 1
        # Second failure — after 2 failures the controller exhausts the topic
        ctrl.accept_output(dup_text)
        # After 2 failures the active_topic turns_spoken should equal max_turns_per_topic
        # (existing register_failure behaviour at failure_count >= 2)
        assert ctrl.active_topic is None or ctrl.active_topic.turns_spoken >= ctrl.max_turns_per_topic

    def test_ladder_resets_on_success(self):
        """After a successful (non-dup) output, ladder state is reset to clean slate."""
        ctrl = _make_controller()
        # Arm the ladder manually
        ctrl._regen_active = True
        ctrl._rejected_phrases = ["alguna frase repetida"]
        ctrl._regen_retry_count = 1
        # Submit a clearly unique output that passes all guardrails
        unique_output = (
            "Mirá, lo que más me interesa de este tema es cómo afecta la experiencia "
            "del usuario en tiempo real cuando el hardware no acompaña las expectativas."
        )
        result = ctrl.accept_output(unique_output)
        assert result is True
        assert ctrl._regen_active is False
        assert ctrl._rejected_phrases == []
        assert ctrl._regen_retry_count == 0


# ---------------------------------------------------------------------------
# Section C — CRITICAL-1: trim propagates through transformer to spoken output
# ---------------------------------------------------------------------------

class TestTrimPropagatesViaTransformer:
    """CRITICAL-1: the trimmed text must be what the transformer returns,
    not merely what is committed to last_outputs.

    Rationale: enforce_live_safety_cap is bound as agenda_output_transformer
    in app_shell.py.  The transformer's return value IS what reaches TTS.
    If trim lives only inside accept_output (the validator, which returns bool),
    the spoken text is always the un-trimmed original — the feature is broken.
    """

    def test_enforce_live_safety_cap_trims_trailing_dup(self):
        """enforce_live_safety_cap must trim a trailing repeated sentence
        so the transformer output (the spoken text) is the clean version."""
        ctrl = _make_controller()
        recent_sentence = "La latencia del modelo depende del hardware disponible."
        ctrl.record_accepted_output(recent_sentence)

        body = "Me parece que este tema tiene mucha miga para explorar en detalle."
        output_with_trailing_dup = f"{body} {recent_sentence}"

        transformer_result = ctrl.enforce_live_safety_cap(output_with_trailing_dup)

        # The transformer must return the TRIMMED version (without the dup sentence)
        assert recent_sentence.lower() not in transformer_result.lower(), (
            f"Trailing dup sentence was NOT removed by transformer. "
            f"transformer_result={transformer_result!r}"
        )
        # The body (the non-dup part) must be preserved
        assert "miga para explorar" in transformer_result.lower() or body[:20].lower() in transformer_result.lower(), (
            f"Body sentence was unexpectedly lost. transformer_result={transformer_result!r}"
        )

    def test_enforce_live_safety_cap_does_not_trim_when_no_dup(self):
        """enforce_live_safety_cap must NOT alter text when there is no trailing dup."""
        ctrl = _make_controller()
        ctrl.record_accepted_output("Un output previo completamente diferente y sin relacion.")

        clean_output = "Este es un output completamente nuevo sin ninguna repeticion aparente."
        transformer_result = ctrl.enforce_live_safety_cap(clean_output)

        # The transformer must return the text unchanged (modulo whitespace normalisation)
        assert " ".join(clean_output.split()) == transformer_result, (
            f"Transformer mutated non-dup text. result={transformer_result!r}"
        )

    def test_transformer_then_validator_roundtrip_accepts_trimmed(self):
        """Simulate the llm_engine sequence: transformer runs first, then validator.
        The validator (accept_output) must accept the already-trimmed text and
        commit the trimmed (not original) text to last_outputs."""
        ctrl = _make_controller()
        recent_sentence = "El problema real es que nadie revisa los defaults antes del deploy."
        ctrl.record_accepted_output(recent_sentence)

        body = "Hay algo que me parece clave mencionar sobre los flujos de trabajo modernos."
        original_output = f"{body} {recent_sentence}"

        # Step 1: transformer
        transformer_output = ctrl.enforce_live_safety_cap(original_output)
        # Transformer must have stripped the trailing dup
        assert recent_sentence.lower() not in transformer_output.lower()

        # Step 2: validator receives the transformer output
        outputs_before = list(ctrl.last_outputs)
        accepted = ctrl.accept_output(transformer_output)
        assert accepted is True, "Validator rejected the already-trimmed transformer output"

        # The committed entry must be the trimmed text, not the original
        new_entries = [s for s in ctrl.last_outputs if s not in outputs_before]
        assert len(new_entries) >= 1
        original_normalized = " ".join(original_output.lower().split())
        for entry in new_entries:
            assert entry != original_normalized, (
                f"Un-trimmed original was committed to last_outputs: {entry!r}"
            )


# ---------------------------------------------------------------------------
# Section D — HIGH-1: ladder state does NOT leak across topic transitions
# ---------------------------------------------------------------------------

class TestLadderStateDoesNotLeakAcrossTopics:
    """HIGH-1: _regen_active, _rejected_phrases, _regen_retry_count, and
    _last_matched_phrase must be cleared when a new topic becomes active
    and when emergency_stop() is called."""

    def test_rejected_phrases_cleared_on_topic_transition(self):
        """Rejected phrases from topic A must NOT appear in topic B's prompt."""
        from opencohost.smart_aggregator.kira_agenda_controller import (
            AgendaState,
            KiraAgendaController,
            TopicStatus,
        )
        ctrl = KiraAgendaController(max_turns_per_topic=5)
        ctrl.enable()

        # Topic A
        topic_a = ctrl.add_topic("Topic A", approved=True)
        ctrl.queue_topic(topic_a.id)
        action_a = ctrl.next_action()
        assert action_a.kind == "enqueue"
        ctrl.active_topic = topic_a
        topic_a.status = TopicStatus.ACTIVE

        # Arm regen ladder on topic A with a rejected phrase
        ctrl._regen_active = True
        ctrl._rejected_phrases = ["frase rechazada del tema A"]
        ctrl._regen_retry_count = 2
        ctrl._last_matched_phrase = "frase rechazada del tema A"

        # Exhaust topic A and transition to topic B
        topic_a.status = TopicStatus.COMPLETED
        ctrl.active_topic = None
        ctrl.state = AgendaState.IDLE

        # Topic B
        topic_b = ctrl.add_topic("Topic B", approved=True)
        ctrl.queue_topic(topic_b.id)
        action_b = ctrl.next_action()
        # next_action should select topic B
        assert action_b.kind == "enqueue"
        assert ctrl.active_topic is topic_b

        # Build a prompt for topic B — it must NOT contain topic A's rejected phrase
        prompt_b = ctrl._build_prompt(instruction="continue topic B")
        assert "frase rechazada del tema A" not in prompt_b, (
            "Ladder state from topic A leaked into topic B's prompt (ANTI-REPETICIÓN block present)"
        )

    def test_ladder_state_cleared_on_emergency_stop(self):
        """emergency_stop() must clear all four ladder state fields."""
        ctrl = _make_controller()
        ctrl._regen_active = True
        ctrl._rejected_phrases = ["una frase cualquiera"]
        ctrl._regen_retry_count = 3
        ctrl._last_matched_phrase = "una frase cualquiera"

        ctrl.emergency_stop()

        assert ctrl._regen_active is False
        assert ctrl._rejected_phrases == []
        assert ctrl._regen_retry_count == 0
        assert ctrl._last_matched_phrase == ""

    def test_ladder_state_cleared_when_new_topic_activates(self):
        """When next_action selects a new topic and sets it as active_topic,
        the ladder state from the previous topic must be zero."""
        from opencohost.smart_aggregator.kira_agenda_controller import (
            AgendaState,
            KiraAgendaController,
            TopicStatus,
        )
        ctrl = KiraAgendaController(max_turns_per_topic=5)
        ctrl.enable()

        # Seed dirty state
        ctrl._regen_active = True
        ctrl._rejected_phrases = ["stale phrase"]
        ctrl._regen_retry_count = 1
        ctrl._last_matched_phrase = "stale phrase"

        topic = ctrl.add_topic("Fresh Topic", approved=True)
        ctrl.queue_topic(topic.id)
        ctrl.next_action()  # selects and activates the topic

        assert ctrl._regen_active is False
        assert ctrl._rejected_phrases == []
        assert ctrl._regen_retry_count == 0
        assert ctrl._last_matched_phrase == ""


# ---------------------------------------------------------------------------
# Section E — HIGH-2: _last_matched_phrase is reset at each validation start
# ---------------------------------------------------------------------------

class TestLastMatchedPhraseNotStale:
    """HIGH-2: _last_matched_phrase must be cleared at the start of each
    _validate_output call so register_failure never captures a stale
    cross-generation phrase."""

    def test_last_matched_phrase_reset_before_guardrail_chain(self):
        """After a clean output that passes all guardrails, _last_matched_phrase
        must be empty (reset at the start of the next validation call)."""
        ctrl = _make_controller()

        # Manually set a stale phrase (simulating a previous guardrail hit)
        ctrl._last_matched_phrase = "frase vieja de una generacion anterior"

        # Now validate a clean output that has no matches to recent lines
        clean = (
            "Hay algo fascinante en cómo los modelos de lenguaje procesan el contexto "
            "sin realmente entender lo que están procesando, al menos no en el sentido humano."
        )
        result = ctrl.accept_output(clean)
        assert result is True  # passes all guardrails

        # _last_matched_phrase must have been reset during this validation
        assert ctrl._last_matched_phrase == "", (
            f"_last_matched_phrase not reset: {ctrl._last_matched_phrase!r}"
        )

    def test_register_failure_does_not_capture_stale_phrase(self):
        """When a new generation is rejected for GUARDRAIL_SIMILAR but
        repeats_recent_line does NOT fire (a different guardrail fires first),
        _last_matched_phrase must be empty so register_failure falls back
        to last_outputs[-1] rather than a stale phrase from a previous run."""
        ctrl = _make_controller()

        # Seed a recent output
        ctrl.record_accepted_output("Texto reciente para contexto del controlador.")
        # Manually plant a stale phrase (from a previous validation cycle)
        ctrl._last_matched_phrase = "frase vieja completamente irrelevante para este ciclo"

        # Validate a new output — repeats_recent_line will NOT fire because it's new,
        # but is_repetition WILL fire if we use the exact same text
        ctrl.record_accepted_output("Texto exactamente repetido para el test.")
        result = ctrl.accept_output("Texto exactamente repetido para el test.")
        assert result is False  # rejected by is_repetition (GUARDRAIL_LOOPING)

        # _last_matched_phrase should have been cleared at the start of validation,
        # so register_failure should NOT have captured the stale phrase
        assert ctrl._last_matched_phrase != "frase vieja completamente irrelevante para este ciclo", (
            "Stale _last_matched_phrase was preserved across validation calls"
        )


# ---------------------------------------------------------------------------
# Section F — HIGH-1 RESIDUAL: ladder state clean at ALL active_topic=None exits
# ---------------------------------------------------------------------------

class TestLadderStateCleanAtAllTopicEndExits:
    """HIGH-1 residual: the invariant 'ladder state is clean whenever
    active_topic is None' must hold at EVERY exit point, not only
    emergency_stop() and next_action(IDLE→OPEN_TOPIC)."""

    def _arm(self, ctrl) -> None:
        """Put the ladder into a dirty/armed state."""
        ctrl._regen_active = True
        ctrl._rejected_phrases = ["frase sucia del topic anterior"]
        ctrl._regen_retry_count = 2
        ctrl._last_matched_phrase = "frase sucia del topic anterior"

    def _assert_clean(self, ctrl) -> None:
        assert ctrl._regen_active is False, "_regen_active not reset"
        assert ctrl._rejected_phrases == [], "_rejected_phrases not reset"
        assert ctrl._regen_retry_count == 0, "_regen_retry_count not reset"
        assert ctrl._last_matched_phrase == "", "_last_matched_phrase not reset"

    # ------------------------------------------------------------------
    # F-1: mark_speech_complete — CLOSING → COMPLETED normal-completion path
    # ------------------------------------------------------------------

    def test_mark_speech_complete_closing_resets_ladder(self):
        """mark_speech_complete() on a CLOSING topic must clear all four
        ladder fields (active_topic becomes None on this path)."""
        from opencohost.smart_aggregator.kira_agenda_controller import (
            AgendaState,
            TopicStatus,
        )
        ctrl = _make_controller()
        ctrl.state = AgendaState.SPEAKING
        ctrl.active_topic.status = TopicStatus.CLOSING
        self._arm(ctrl)

        ctrl.mark_speech_complete()

        assert ctrl.active_topic is None, "active_topic should be None after CLOSING completion"
        self._assert_clean(ctrl)

    def test_mark_speech_complete_non_closing_does_not_reset_ladder(self):
        """mark_speech_complete() on an ACTIVE (non-CLOSING) topic must NOT
        reset the ladder — it stays armed for the within-topic regen cycle."""
        from opencohost.smart_aggregator.kira_agenda_controller import (
            AgendaState,
            TopicStatus,
        )
        ctrl = _make_controller()
        ctrl.state = AgendaState.SPEAKING
        ctrl.active_topic.status = TopicStatus.ACTIVE
        self._arm(ctrl)

        ctrl.mark_speech_complete()

        # Topic is still active, ladder must remain armed
        assert ctrl.active_topic is not None, "active_topic should still be set"
        assert ctrl._regen_active is True, "Ladder was wrongly disarmed on non-terminal path"
        assert ctrl._rejected_phrases == ["frase sucia del topic anterior"]

    def test_mark_speech_complete_closing_no_anti_rep_in_next_topic_prompt(self):
        """After CLOSING→COMPLETED, a subsequent prompt for a new topic must NOT
        contain any ANTI-REPETICIÓN FORZADA block (leaked dirty state)."""
        from opencohost.smart_aggregator.kira_agenda_controller import (
            AgendaState,
            TopicStatus,
        )
        ctrl = _make_controller()
        ctrl.state = AgendaState.SPEAKING
        ctrl.active_topic.status = TopicStatus.CLOSING
        self._arm(ctrl)

        ctrl.mark_speech_complete()
        assert ctrl.active_topic is None

        # Simulate a new topic becoming active
        new_topic = ctrl.add_topic("New Topic After Completion", approved=True)
        ctrl.active_topic = new_topic
        new_topic.status = TopicStatus.ACTIVE

        prompt = ctrl._build_prompt(instruction="open new topic")
        assert "ANTI-REPETICIÓN FORZADA" not in prompt, (
            "Dirty ladder state leaked into the next topic's prompt after "
            "mark_speech_complete CLOSING→COMPLETED path"
        )

    # ------------------------------------------------------------------
    # F-2: register_failure — force-exhaust path (CLOSING + failure_count >= 3)
    # ------------------------------------------------------------------

    def test_register_failure_force_exhaust_resets_ladder(self):
        """register_failure() force-exhaust path (CLOSING topic, failure_count >= 3)
        sets active_topic=None and must clear all four ladder fields."""
        from opencohost.smart_aggregator.kira_agenda_controller import (
            AgendaState,
            ErrorCode,
            TopicStatus,
        )
        ctrl = _make_controller()
        ctrl.state = AgendaState.TOPIC_CLOSING
        ctrl.active_topic.status = TopicStatus.CLOSING
        # Drive failure_count to 2 so the next call reaches >= 3
        ctrl.register_failure(error=ErrorCode.GUARDRAIL_LOOPING, reason="test 1")
        ctrl.register_failure(error=ErrorCode.GUARDRAIL_LOOPING, reason="test 2")
        self._arm(ctrl)  # re-arm with dirty state for the decisive call

        # Third failure triggers force-exhaust
        ctrl.register_failure(error=ErrorCode.GUARDRAIL_LOOPING, reason="test 3")

        assert ctrl.active_topic is None, "active_topic must be None after force-exhaust"
        self._assert_clean(ctrl)

    def test_register_failure_force_exhaust_no_anti_rep_in_next_topic_prompt(self):
        """After force-exhaust, a subsequent prompt for a new topic must NOT
        contain any ANTI-REPETICIÓN FORZADA block."""
        from opencohost.smart_aggregator.kira_agenda_controller import (
            AgendaState,
            ErrorCode,
            TopicStatus,
        )
        ctrl = _make_controller()
        ctrl.state = AgendaState.TOPIC_CLOSING
        ctrl.active_topic.status = TopicStatus.CLOSING
        ctrl.register_failure(error=ErrorCode.GUARDRAIL_LOOPING, reason="test 1")
        ctrl.register_failure(error=ErrorCode.GUARDRAIL_LOOPING, reason="test 2")
        self._arm(ctrl)
        ctrl.register_failure(error=ErrorCode.GUARDRAIL_LOOPING, reason="test 3")

        assert ctrl.active_topic is None

        new_topic = ctrl.add_topic("Topic After Force Exhaust", approved=True)
        ctrl.active_topic = new_topic
        new_topic.status = TopicStatus.ACTIVE

        prompt = ctrl._build_prompt(instruction="open topic after exhaust")
        assert "ANTI-REPETICIÓN FORZADA" not in prompt, (
            "Dirty ladder state leaked into next topic prompt after register_failure force-exhaust"
        )

    # ------------------------------------------------------------------
    # F-3: regression — non-terminal failure_count >= 2 path does NOT reset ladder
    # ------------------------------------------------------------------

    def test_register_failure_non_terminal_preserves_ladder(self):
        """The failure_count >= 2 → WAITING_SIGNAL path keeps the topic alive;
        it must NOT reset the ladder (the topic is still active and regen is needed)."""
        from opencohost.smart_aggregator.kira_agenda_controller import (
            AgendaState,
            ErrorCode,
            TopicStatus,
        )
        ctrl = _make_controller()
        ctrl.state = AgendaState.WAITING_SIGNAL
        ctrl.active_topic.status = TopicStatus.ACTIVE
        self._arm(ctrl)
        # Add a rejected phrase via the normal path first
        ctrl._rejected_phrases = ["phrase to keep"]
        ctrl._regen_retry_count = 1

        # First failure (failure_count becomes 1, non-terminal)
        ctrl.register_failure(error=ErrorCode.GUARDRAIL_LOOPING, reason="non-terminal")
        # Second failure triggers failure_count >= 2 → WAITING_SIGNAL (non-terminal)
        ctrl.register_failure(error=ErrorCode.GUARDRAIL_LOOPING, reason="non-terminal 2")

        # Topic must still be active (non-terminal)
        assert ctrl.active_topic is not None, "active_topic was wrongly cleared on non-terminal path"
        assert ctrl.state == AgendaState.WAITING_SIGNAL
        # Ladder must remain armed (topic is still alive, regen still needed)
        assert ctrl._regen_active is True, "Ladder was wrongly disarmed on non-terminal path"
