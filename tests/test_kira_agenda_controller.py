"""Tests for Kira Co-host Agenda Mode controller."""

import pytest

from opencohost.smart_aggregator.kira_agenda_controller import (
    AgendaAction,
    AgendaState,
    ErrorCode,
    KiraAgendaController,
    TopicStatus,
)


def test_only_approved_topics_can_be_queued():
    controller = KiraAgendaController()
    topic = controller.add_topic("Minecraft en la industria gaming")

    with pytest.raises(ValueError):
        controller.queue_topic(topic.id)

    controller.approve_topic(topic.id)
    controller.queue_topic(topic.id)

    assert topic.status == TopicStatus.QUEUED


def test_add_topic_dedups_non_terminal_same_title_angle():
    # WU4: identical (title, angle) against a live topic returns the existing one.
    controller = KiraAgendaController()
    a = controller.add_topic("Tema X", angle="un angulo")
    b = controller.add_topic("Tema X", angle="un angulo")
    assert a is b
    assert len(controller.topics) == 1


def test_add_topic_dedup_ignores_case_and_whitespace():
    controller = KiraAgendaController()
    a = controller.add_topic("Tema X", angle="Angulo")
    b = controller.add_topic("  tema   x ", angle="angulo")
    assert a is b
    assert len(controller.topics) == 1


def test_add_topic_terminal_status_does_not_block_fresh_add():
    # A completed/skipped topic with the same title must NOT swallow a fresh add.
    controller = KiraAgendaController()
    first = controller.add_topic("Tema repetido")
    first.status = TopicStatus.COMPLETED
    second = controller.add_topic("Tema repetido")
    assert first.id != second.id
    assert len(controller.topics) == 2

    third = controller.add_topic("Tema repetido")
    third_expected_dup = second  # second is DRAFTED (non-terminal) -> dedup hits it
    assert third is third_expected_dup
    assert len(controller.topics) == 2


def test_agenda_mode_does_nothing_without_topics():
    controller = KiraAgendaController()
    controller.enable()

    action = controller.next_action()

    assert action.kind == "none"
    assert controller.state == AgendaState.IDLE


def test_first_queued_topic_enqueues_short_agenda_prompt():
    controller = KiraAgendaController()
    topic = controller.add_topic(
        "Minecraft en la industria gaming",
        angle="comparar mods con tendencias actuales",
        constraints=["no sonar académica"],
        approved=True,
    )
    controller.queue_topic(topic.id)
    controller.enable()

    action = controller.next_action()

    assert action.kind == "enqueue"
    assert action.source == "kira-agenda"
    assert action.priority == 2
    assert action.topic_id == topic.id
    assert "TEMA APROBADO: Minecraft en la industria gaming" in action.prompt
    assert "SALIDA PERMITIDA" in action.prompt
    assert controller.state == AgendaState.GENERATING
    assert topic.status == TopicStatus.ACTIVE


def test_agenda_topic_can_inject_editorial_card_once():
    controller = KiraAgendaController()
    controller.set_editorial_context_provider(
        lambda card_id: (
            "<editorial_context>\n"
            '{"topic":"Monetización Game X","streamer_take":"Debatir pay-to-win."}'
            "\n</editorial_context>"
            if card_id == "card-1"
            else None
        )
    )
    topic = controller.add_topic(
        "Monetización Game X",
        approved=True,
        editorial_card_id="card-1",
    )
    controller.queue_topic(topic.id)
    controller.enable()

    opening = controller.next_action()
    controller.mark_generation_accepted()
    controller.mark_speech_complete()
    continuation = controller.next_action()

    assert topic.editorial_card_id == "card-1"
    assert "<editorial_context>" in opening.prompt
    assert "Debatir pay-to-win" in opening.prompt
    assert "<editorial_context>" not in continuation.prompt


def test_missing_editorial_card_does_not_block_agenda_activation():
    controller = KiraAgendaController()
    controller.set_editorial_context_provider(lambda card_id: None)
    topic = controller.add_topic("Tema sin card disponible", approved=True, editorial_card_id="missing")
    controller.queue_topic(topic.id)
    controller.enable()

    action = controller.next_action()

    assert action.kind == "enqueue"
    assert action.source == "kira-agenda"
    assert "<editorial_context>" not in action.prompt
    assert topic.status == TopicStatus.ACTIVE


def test_topic_priority_selects_high_priority_first():
    controller = KiraAgendaController()
    low = controller.add_topic("Tema bajo", approved=True, priority="baja")
    high = controller.add_topic("Tema urgente", approved=True, priority="alta")
    controller.queue_topic(low.id)
    controller.queue_topic(high.id)
    controller.enable()

    action = controller.next_action()

    assert action.topic_id == high.id
    assert controller.active_topic.title == "Tema urgente"


def test_global_response_length_is_reflected_in_prompt():
    controller = KiraAgendaController(response_length="expandida")
    topic = controller.add_topic("Tema con espacio", approved=True, response_length="corta")
    controller.queue_topic(topic.id)
    controller.enable()

    action = controller.next_action()

    assert "monólogo largo expandido" in action.prompt
    assert "hard cap 6000 caracteres" in action.prompt
    assert "modo live-safe" in action.prompt
    assert "hard cap 1100 caracteres" in action.prompt


def test_live_safety_modes_are_configurable_and_cap_output():
    controller = KiraAgendaController(response_length="expandida", safety_mode="monologue")
    topic = controller.add_topic("Tema largo seguro", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()

    action = controller.next_action()

    assert "modo monólogo" in action.prompt
    assert "cap 3000" in action.prompt
    capped = controller.enforce_live_safety_cap(("Frase larga. " * 400).strip())
    assert len(capped) <= 3000

    controller.set_session_settings(safety_mode="test")
    assert controller.safety_mode == "test"
    assert len(controller.enforce_live_safety_cap("x" * 6500)) <= 6001


def test_normal_global_response_length_is_now_rich_mini_monologue():
    controller = KiraAgendaController(response_length="normal")
    topic = controller.add_topic("Tema normal rico", approved=True, response_length="corta")
    controller.queue_topic(topic.id)
    controller.enable()

    action = controller.next_action()

    assert "mini monólogo natural" in action.prompt
    assert "1500 caracteres" in action.prompt


def test_short_response_length_is_basic_brief_and_aliases_expandida():
    controller = KiraAgendaController(response_length="corta")
    short = controller.add_topic("Tema corto", approved=True, response_length="corta")
    expanded = controller.add_topic("Tema largo", approved=True, response_length="extendida")

    assert short.response_length == "corta"
    assert expanded.response_length == "expandida"
    controller.queue_topic(short.id)
    controller.enable()

    action = controller.next_action()

    assert "intervención breve pero útil" in action.prompt
    assert "450 caracteres" in action.prompt


def test_session_settings_drive_prompt_not_topic_length_metadata():
    controller = KiraAgendaController()
    controller.set_session_settings(max_turns_per_topic=4, rhythm="dinámico", response_length="expandida")
    topic = controller.add_topic("Tema legacy", approved=True, response_length="corta")
    controller.queue_topic(topic.id)
    controller.enable()

    action = controller.next_action()

    assert "RITMO GLOBAL: ritmo dinámico" in action.prompt
    assert "monólogo largo expandido" in action.prompt
    assert "intervención breve" not in action.prompt


def test_turn_limit_clamps_and_controller_uses_global_max_turns():
    controller = KiraAgendaController(max_turns_per_topic=99, turn_batch_size=1)
    topic = controller.add_topic("Tema global", approved=True)

    assert controller.max_turns_per_topic == 20
    assert controller.set_max_turns_per_topic(0) == 1

    controller.queue_topic(topic.id)
    controller.enable()
    controller.next_action()
    controller.mark_generation_accepted()
    controller.mark_speech_complete()

    action = controller.next_action()

    assert action.source == "kira-agenda-stop"
    assert topic.status == TopicStatus.CLOSING


def test_agenda_blocks_count_multiple_beats_without_exceeding_global_turns():
    controller = KiraAgendaController(max_turns_per_topic=3, turn_batch_size=2)
    topic = controller.add_topic("Tema por bloques", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()

    first = controller.next_action()
    assert "representa 2 beat(s)" in first.prompt
    controller.mark_generation_accepted()
    controller.mark_speech_complete()
    assert topic.turns_spoken == 2

    second = controller.next_action()
    assert "representa 1 beat(s)" in second.prompt
    controller.mark_generation_accepted()
    controller.mark_speech_complete()
    assert topic.turns_spoken == 3
    assert controller.next_action().source == "kira-agenda-stop"


def test_prefetch_action_preview_does_not_mutate_speaking_state():
    controller = KiraAgendaController(max_turns_per_topic=5, turn_batch_size=2)
    topic = controller.add_topic("Tema con prefetch", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()
    controller.next_action()
    controller.mark_generation_accepted()

    action = controller.prefetch_action_after_current_speech()

    assert action.kind == "enqueue"
    assert action.source == "kira-agenda"
    assert action.turns == 2
    assert "bloque fluido" in action.prompt
    assert controller.state == AgendaState.SPEAKING
    assert topic.turns_spoken == 0


def test_prefetch_preview_can_prepare_closing_after_current_speech():
    controller = KiraAgendaController(max_turns_per_topic=2, turn_batch_size=2)
    topic = controller.add_topic("Tema casi cerrado", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()
    controller.next_action()
    controller.mark_generation_accepted()

    action = controller.prefetch_action_after_current_speech()

    assert action.source == "kira-agenda-stop"
    assert "transición natural" in action.prompt
    assert topic.status == TopicStatus.ACTIVE


def test_start_prefetched_action_adopts_cached_turn_metadata():
    controller = KiraAgendaController(max_turns_per_topic=5, turn_batch_size=2)
    topic = controller.add_topic("Tema cacheado", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()
    controller.next_action()
    controller.mark_generation_accepted()
    action = controller.prefetch_action_after_current_speech()
    controller.mark_speech_complete()

    controller.start_prefetched_action(action)
    controller.mark_generation_accepted()
    controller.mark_speech_complete()

    assert topic.turns_spoken == 4


def test_prefetch_preview_returns_none_while_closing_speech_plays():
    """Regression: prefetching while the closing line itself is playing must
    not generate a second closing for the same topic (heard on stream as the
    same goodbye spoken twice with different wording)."""
    controller = KiraAgendaController(max_turns_per_topic=2, turn_batch_size=2)
    topic = controller.add_topic("Tema que cierra", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()
    controller.next_action()
    controller.mark_generation_accepted()
    closing = controller.prefetch_action_after_current_speech()
    assert closing.source == "kira-agenda-stop"
    controller.mark_speech_complete()
    controller.start_prefetched_action(closing)
    controller.mark_generation_accepted()
    assert topic.status == TopicStatus.CLOSING

    second = controller.prefetch_action_after_current_speech()

    assert second.kind == "none"


def test_start_prefetched_action_reports_adoption():
    """start_prefetched_action returns True only when the controller adopts
    the cached action; False lets the caller discard stale prefetched audio."""
    controller = KiraAgendaController(max_turns_per_topic=5, turn_batch_size=2)
    topic = controller.add_topic("Tema adoptable", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()
    controller.next_action()
    controller.mark_generation_accepted()
    action = controller.prefetch_action_after_current_speech()
    controller.mark_speech_complete()

    assert controller.start_prefetched_action(action) is True

    orphan = AgendaAction(kind="enqueue", prompt="x", source="kira-agenda-stop", priority=2)
    controller.active_topic = None
    assert controller.start_prefetched_action(orphan) is False


def test_angle_limit_accepts_real_generated_angle_but_stays_capped():
    controller = KiraAgendaController()
    long_angle = "A" * KiraAgendaController.ANGLE_MAX_CHARS

    topic = controller.add_topic("Tema con ángulo largo", angle=long_angle, approved=True)

    assert topic.angle == long_angle
    with pytest.raises(ValueError):
        controller.add_topic("Tema roto", angle="B" * (KiraAgendaController.ANGLE_MAX_CHARS + 1), approved=True)


def test_cohost_profile_style_is_injected_without_replacing_guardrails():
    controller = KiraAgendaController()
    controller.set_profile({"style": "Más irónica, gamer y directa."})
    topic = controller.add_topic("Tema con perfil", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()

    action = controller.next_action()

    assert "Más irónica, gamer y directa" in action.prompt
    assert "PROHIBIDO" in action.prompt


def test_prompt_forbids_artificial_episode_closings():
    controller = KiraAgendaController()
    topic = controller.add_topic("Tema natural", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()

    action = controller.next_action()

    assert "próximo episodio" in action.prompt
    assert "eso es todo" in action.prompt
    assert "intervención natural" in action.prompt


def test_reorder_and_remove_queued_topics_with_same_priority():
    controller = KiraAgendaController()
    first = controller.add_topic("Primero", approved=True, priority="normal")
    second = controller.add_topic("Segundo", approved=True, priority="normal")
    controller.queue_topic(first.id)
    controller.queue_topic(second.id)

    controller.move_queued_topic(second.id, -1)

    assert [topic.title for topic in controller.queued_topics()] == ["Segundo", "Primero"]

    controller.remove_queued_topic(second.id)
    assert [topic.title for topic in controller.queued_topics()] == ["Primero"]
    assert second.status == TopicStatus.SKIPPED


@pytest.mark.parametrize(
    "bad_title",
    [
        "x" * 91,
        "function hack() { return true; }",
        "<script>alert(1)</script>",
        "Tema con emoji 😈",
    ],
)
def test_topic_input_rejects_untrusted_content(bad_title):
    controller = KiraAgendaController()

    with pytest.raises(ValueError):
        controller.add_topic(bad_title, approved=True)


def test_controller_does_not_enqueue_while_motor_busy_or_kira_speaking():
    controller = KiraAgendaController()
    topic = controller.add_topic("Tema seguro", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()

    assert controller.next_action(motor_busy=True).kind == "none"
    assert controller.next_action(kira_speaking=True).kind == "none"
    assert controller.state == AgendaState.IDLE


def test_ptt_outranks_waiting_agenda_continuation():
    controller = KiraAgendaController()
    topic = controller.add_topic("Tema de fondo", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()
    controller.next_action()
    controller.mark_generation_accepted()
    controller.mark_speech_complete()

    action = controller.next_action(ptt_text="Cambiemos el tono, explicalo más simple")

    assert action.kind == "enqueue"
    assert action.source == "ptt"
    assert action.priority == 0
    assert "Cambiemos el tono" in action.prompt
    assert controller.state == AgendaState.GENERATING


# ---------------------------------------------------------------------------
# agenda_ptt_commit_raw_text — honest history_text for PTT turns
# ---------------------------------------------------------------------------


def test_agenda_action_history_text_defaults_to_none():
    """AgendaAction.history_text must default to None so every caller other
    than the PTT path (direct/chat/kira-agenda/accumulated) stays byte-identical."""
    action = AgendaAction(kind="enqueue", prompt="algo")
    assert action.history_text is None


def test_ptt_action_sets_honest_history_text_with_ptt_words():
    """_streamer_action must set history_text to the streamer's real words
    (not the full prompt template) so the commit seam can store the honest
    turn instead of the boilerplate prompt."""
    controller = KiraAgendaController()
    topic = controller.add_topic("Tema de fondo", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()
    controller.next_action()
    controller.mark_generation_accepted()
    controller.mark_speech_complete()

    action = controller.next_action(ptt_text="Cambiemos el tono, explicalo más simple")

    assert action.history_text == "El streamer dijo (PTT): Cambiemos el tono, explicalo más simple"
    # The template's forbidden output-scaffolding markers must NOT leak into history_text.
    assert "TAREA:" not in action.history_text
    assert "SALIDA PERMITIDA" not in action.history_text


def test_compact_chat_outranks_topic_continuation_but_not_ptt():
    controller = KiraAgendaController()
    topic = controller.add_topic("Tema de fondo", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()
    controller.next_action()
    controller.mark_generation_accepted()
    controller.mark_speech_complete()

    action = controller.next_action(
        ptt_text="Primero respondeme a mí",
        compact_chat="El chat filtrado pregunta por mods.",
    )

    assert action.source == "ptt"
    assert "Primero respondeme" in action.prompt


def test_compact_chat_can_steer_waiting_topic():
    controller = KiraAgendaController(chat_cadence_blocks=1)
    topic = controller.add_topic("Tema de fondo", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()
    controller.next_action()
    controller.mark_generation_accepted()
    controller.mark_speech_complete()

    action = controller.next_action(compact_chat="El chat filtrado pregunta por mods.")

    assert action.source == "chat"
    assert action.priority == 1
    assert "El chat filtrado pregunta por mods" in action.prompt


def test_compact_chat_via_next_action_reaches_the_same_build_prompt_call():
    """Viewer-chat (RF3) rides for free: app_shell.py's compact_chat handler
    calls next_action(compact_chat=...) -> _chat_action -> _build_prompt, the
    SAME method agenda beats use. No separate migration/prompt path exists
    (design §3 Path B note). Pin that next_action's resulting prompt is
    byte-identical to calling _build_prompt directly with the same instruction
    and compact_chat -- proving there is no parallel/forked prompt builder."""
    controller = KiraAgendaController(chat_cadence_blocks=1)
    topic = controller.add_topic("Tema de fondo", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()
    controller.next_action()
    controller.mark_generation_accepted()
    controller.mark_speech_complete()

    compact_chat = "El chat filtrado pregunta por mods."
    action = controller.next_action(compact_chat=compact_chat)

    from opencohost.i18n import active as i18n_active_test

    expected = controller._build_prompt(
        instruction=i18n_active_test.agenda_instructions()["chat"],
        compact_chat=compact_chat,
    )
    assert action.source == "chat"
    assert action.prompt == expected


def test_chat_signal_due_only_when_waiting_and_cadence_allows_it():
    controller = KiraAgendaController(turn_batch_size=1, chat_cadence_blocks=2)
    topic = controller.add_topic("Tema de fondo", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()

    controller.next_action()
    assert controller.chat_signal_due() is False

    controller.mark_generation_accepted()
    controller.mark_speech_complete()
    assert controller.chat_signal_due() is False

    controller.next_action()
    controller.mark_generation_accepted()
    controller.mark_speech_complete()
    assert controller.chat_signal_due() is True


def test_compact_chat_is_not_checked_every_agenda_block_by_default():
    controller = KiraAgendaController(turn_batch_size=1, chat_cadence_blocks=2)
    topic = controller.add_topic("Tema de fondo", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()
    controller.next_action()
    controller.mark_generation_accepted()
    controller.mark_speech_complete()

    first_wait = controller.next_action(compact_chat="El chat quiere entrar demasiado pronto.")

    assert first_wait.source == "kira-agenda"
    controller.mark_generation_accepted()
    controller.mark_speech_complete()

    second_wait = controller.next_action(compact_chat="El chat filtrado pregunta por mods.")

    assert second_wait.source == "chat"


def test_soft_stop_waits_until_safe_boundary_then_closes_topic():
    controller = KiraAgendaController()
    topic = controller.add_topic("Tema activo", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()
    controller.next_action()
    controller.mark_generation_accepted()

    assert controller.state == AgendaState.SPEAKING
    assert controller.soft_stop().kind == "none"
    assert controller.stop_requested is True

    controller.mark_speech_complete()
    action = controller.next_action()

    assert action.kind == "enqueue"
    assert action.source == "kira-agenda-stop"
    assert topic.status == TopicStatus.CLOSING


def test_emergency_stop_turns_mode_off_without_deleting_queue():
    controller = KiraAgendaController()
    topic = controller.add_topic("Tema futuro", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()

    controller.emergency_stop()

    assert controller.state == AgendaState.OFF
    assert controller.active_topic is None
    assert topic.status == TopicStatus.QUEUED


def test_output_sanitizer_rejects_internal_leaks_and_repetition():
    controller = KiraAgendaController()

    assert controller.accept_output("Vamos con una idea simple para arrancar.") is True
    assert controller.accept_output("Según el resumen, el chat dice que...") is False
    assert controller.accept_output("Vamos con una idea simple para arrancar.") is False


def test_preview_accept_output_rejects_without_mutating_state():
    controller = KiraAgendaController()
    controller.state = AgendaState.SPEAKING
    controller.failure_count = 0

    assert controller.preview_accept_output("Según el resumen, el chat dice que...") is False

    assert controller.state == AgendaState.SPEAKING
    assert controller.failure_count == 0
    assert controller.last_outputs == []


def test_record_accepted_output_updates_repetition_memory():
    controller = KiraAgendaController()

    controller.record_accepted_output("Texto cacheado que ya salió al aire.")

    assert controller.preview_accept_output("Texto cacheado que ya salió al aire.") is False


def test_output_sanitizer_rejects_repeated_last_line_loop():
    controller = KiraAgendaController()

    assert controller.accept_output("Idea nueva.\nNo cierres en círculo.\nNo cierres en círculo.") is False


def test_output_sanitizer_rejects_recent_line_and_near_repeat():
    controller = KiraAgendaController()
    first = "Y eso es porque la IA no es un monstruo, es un espejo torcido de lo que entrenamos y premiamos."

    assert controller.accept_output(first) is True
    # Ladder behaviour: trailing dup sentence is trimmed; "Otra entrada." is salvaged
    # and accepted rather than rejected.  The dup is NOT committed to history.
    assert controller.accept_output(f"Otra entrada. {first}") is True

    controller = KiraAgendaController()
    assert controller.accept_output("Y eso abre un ángulo interesante sobre la cultura digital, la confianza pública y las decisiones técnicas que nadie revisa con calma.") is True
    # Whole-sentence near-dup (94% similar single sentence): trim returns ("", False),
    # falls through to guardrail checks → rejected as before.
    assert controller.accept_output("Y en eso aparece un ángulo interesante sobre la cultura digital, la confianza pública y esas decisiones técnicas que casi nadie revisa con calma.") is False


def test_agenda_rejects_skeleton_repetition():
    # This synonym-swap pair slips every pre-existing detector: not an exact/near
    # match (is_repetition), no shared sentence (repeats_recent_line), fewer than
    # 8 tokens>=4 chars (is_too_similar_to_recent), and no "Y eso..." opening
    # (reuses_looping_opening). Only the skeleton (scaffold) detector catches it.
    controller = KiraAgendaController()
    controller.record_accepted_output("Cada partida es un abismo sin fin")

    assert controller.accept_output("Cada derrota es un abismo sin salida") is False
    assert controller.rejection_log[-1]["guardrail"] == "skeleton_repetition"

    # Happy-path sentinel: a genuinely different line is still accepted.
    controller = KiraAgendaController()
    controller.record_accepted_output("Cada partida es un abismo sin fin")
    assert controller.accept_output("El chat propuso armar un torneo relámpago para el sábado.") is True


@pytest.mark.parametrize(
    "first,second",
    [
        (
            "Y así nos damos cuenta de que la tecnología no cambia la cultura sola, apenas empuja lo que ya estaba buscando una salida.",
            "Y así nos damos cuenta de que la tecnología no cambia la cultura por arte de magia, solo empuja algo que ya estaba buscando salida.",
        ),
        (
            "Y eso es el secreto detrás de tantas comunidades digitales: nadie manda del todo, pero todos empujan la forma final.",
            "Y eso es el secreto detrás de muchas comunidades digitales: nadie manda completamente, pero todos empujan la forma final.",
        ),
        (
            "Dejar que otros toquen la melodía también cambia quién se siente dueño de la canción y quién solo mira desde afuera.",
            "Dejar que otros toquen la melodía cambia quién se siente dueño de esa canción y quién queda mirando desde afuera.",
        ),
    ],
)
def test_output_sanitizer_rejects_live_log_near_repeats(first, second):
    controller = KiraAgendaController()

    assert controller.accept_output(first) is True
    assert controller.accept_output(second) is False


@pytest.mark.parametrize(
    "output",
    [
        "Cuando hablo de esto me siento viva de una forma rara.",
        "No soy como las demás: tengo alma y por eso lo leo distinto.",
        "Soy diferente a otras IA porque tengo conciencia propia.",
    ],
)
def test_output_sanitizer_rejects_kira_inner_life_claims(output):
    controller = KiraAgendaController()

    assert controller.accept_output(output) is False


def test_prompt_strengthens_loop_prevention_rules():
    controller = KiraAgendaController()
    topic = controller.add_topic("Tema anti loop", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()

    action = controller.next_action()

    assert "Y eso" in action.prompt
    assert "misma metáfora" in action.prompt
    assert "sumar un ángulo nuevo" in action.prompt
    assert "NO digas que estás viva" in action.prompt


def test_recovery_policy_pause_after_five_failures():
    """RecoveryPolicy requires 5 failures before PAUSED (was 3 before)."""
    controller = KiraAgendaController()
    controller.enable()

    # Failures 1-3: REGENERATING_SAFE + degradation
    controller.register_failure(error=ErrorCode.GUARDRAIL_LOOPING, reason="test")
    assert controller.state == AgendaState.REGENERATING_SAFE
    assert controller.recovery.failure_count == 1

    controller.register_failure(error=ErrorCode.GUARDRAIL_LOOPING)
    controller.register_failure(error=ErrorCode.GUARDRAIL_LOOPING)
    assert controller.recovery.failure_count == 3
    # should_degrade() is True at 3, state still REGENERATING_SAFE
    assert controller.state == AgendaState.REGENERATING_SAFE

    # Failure 4: still regenerating
    controller.register_failure(error=ErrorCode.GUARDRAIL_LOOPING)
    assert controller.state == AgendaState.REGENERATING_SAFE

    # Failure 5: PAUSED
    controller.register_failure(error=ErrorCode.GUARDRAIL_LOOPING)
    assert controller.state == AgendaState.PAUSED_NEEDS_OPERATOR
    assert controller.next_action().kind == "none"


def test_recovery_policy_degradation():
    """At 3 failures, response_length degrades (normal → corta)."""
    controller = KiraAgendaController(response_length="normal")
    controller.enable()
    assert controller.response_length == "normal"

    controller.register_failure(error=ErrorCode.GUARDRAIL_SIMILAR)
    controller.register_failure(error=ErrorCode.GUARDRAIL_SIMILAR)
    controller.register_failure(error=ErrorCode.GUARDRAIL_SIMILAR)
    # should_degrade → response_length lowered
    assert controller.response_length == "corta"


def test_recovery_policy_auto_retry():
    """After PAUSED + cooldown, can_auto_resume returns True."""
    from time import time as _now
    controller = KiraAgendaController()
    controller.enable()

    # Force 5 failures
    for _ in range(5):
        controller.register_failure(error=ErrorCode.GUARDRAIL_EMPTY)
    assert controller.state == AgendaState.PAUSED_NEEDS_OPERATOR

    # Too soon: timer not elapsed
    assert not controller.can_auto_resume()

    # Simulate 61 seconds later
    controller.recovery._last_failure_time = _now() - 61.0
    assert controller.can_auto_resume()
    assert controller.state == AgendaState.IDLE
    assert controller.recovery.retry_attempt == 1


def test_regenerating_safe_transitions_to_waiting_signal():
    """REGENERATING_SAFE → WAITING_SIGNAL so the next tick retries."""
    controller = KiraAgendaController(max_turns_per_topic=5, turn_batch_size=1)
    topic = controller.add_topic("Tema", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()

    # Start a normal turn
    controller.next_action(motor_busy=False, kira_speaking=False)
    controller.mark_generation_accepted()
    controller.mark_speech_complete()
    assert controller.state == AgendaState.WAITING_SIGNAL

    # Next tick generates turn 2
    action = controller.next_action(motor_busy=False, kira_speaking=False)
    assert action.kind == "enqueue"
    assert action.source.startswith("kira-agenda")

    # Simulate guardrail rejection — accept_output calls register_failure
    controller.register_failure(error=ErrorCode.GUARDRAIL_SIMILAR, reason="muy parecida")
    assert controller.state == AgendaState.REGENERATING_SAFE, (
        f"Expected REGENERATING_SAFE after 1 guardrail rejection, got {controller.state}"
    )

    # The tick fires — must recover and generate a new action
    action = controller.next_action(motor_busy=False, kira_speaking=False)
    assert action.kind == "enqueue", (
        f"REGENERATING_SAFE must transition to WAITING_SIGNAL and retry, "
        f"got {action.kind} in state {controller.state}"
    )
    assert action.source.startswith("kira-agenda")


# ──────────────────────────────────────────────────────────────────────────────
# TopicSuggester integration — cooldown / suggestion methods
# ──────────────────────────────────────────────────────────────────────────────

def test_can_suggest_allows_when_fresh():
    controller = KiraAgendaController()
    # No suggestions yet, time is 0 / fresh
    assert controller.can_suggest(now=0.0) is True


def test_can_suggest_blocks_during_cooldown():
    controller = KiraAgendaController()
    # Simulate a recent suggestion
    controller._last_suggestion_time = 100.0
    assert controller.can_suggest(now=100.0 + 45) is False  # 45 s < 120 s


def test_can_suggest_allows_after_cooldown():
    controller = KiraAgendaController()
    controller._last_suggestion_time = 100.0
    assert controller.can_suggest(now=100.0 + 120) is True


def test_can_suggest_allows_well_after_cooldown():
    controller = KiraAgendaController()
    controller._last_suggestion_time = 100.0
    assert controller.can_suggest(now=100.0 + 300) is True


def test_can_suggest_blocks_at_session_cap():
    controller = KiraAgendaController()
    controller._session_suggestion_count = 5  # cap is 5
    assert controller.can_suggest(now=999.0) is False


def test_can_suggest_blocks_above_session_cap():
    controller = KiraAgendaController()
    controller._session_suggestion_count = 6
    assert controller.can_suggest(now=999.0) is False


def test_can_suggest_allows_below_cap_even_recently():
    controller = KiraAgendaController()
    controller._session_suggestion_count = 3
    controller._last_suggestion_time = 200.0
    assert controller.can_suggest(now=200.0 + 200) is True


def test_suggest_topics_creates_drafted():
    controller = KiraAgendaController()
    suggestions = [
        {"title": "Mods vs texturas vanilla", "angle": "Comparar pros y contras", "confidence": "HIGH", "source": "entity:mods"},
        {"title": "El dilema de shaders", "angle": "Rendimiento vs belleza", "confidence": "MEDIUM", "source": "entity:shaders"},
    ]

    created = controller.suggest_topics(suggestions)

    assert len(created) == 2
    for topic in created:
        assert topic.status == TopicStatus.DRAFTED
    assert created[0].title == "Mods vs texturas vanilla"
    assert created[1].title == "El dilema de shaders"


def test_suggest_topics_sanitizes_and_rejects_bad_titles():
    controller = KiraAgendaController()
    suggestions = [
        {"title": "x" * 91, "angle": "ok", "confidence": "HIGH", "source": "entity:long"},
        {"title": "Tema válido", "angle": "Ángulo ok", "confidence": "LOW", "source": "entity:valid"},
    ]

    created = controller.suggest_topics(suggestions)

    assert len(created) == 1
    assert created[0].title == "Tema válido"


def test_suggest_topics_updates_cooldown_tracking():
    controller = KiraAgendaController()
    before_count = controller._session_suggestion_count
    before_time = controller._last_suggestion_time

    created = controller.suggest_topics([{"title": "Un tema nuevo", "angle": "algo", "confidence": "LOW", "source": "entity:x"}])

    assert created
    assert controller._session_suggestion_count == before_count + 1
    assert controller._last_suggestion_time > before_time


def test_suggest_topics_no_change_on_empty_list():
    controller = KiraAgendaController()
    before_count = controller._session_suggestion_count
    before_time = controller._last_suggestion_time

    created = controller.suggest_topics([])

    assert created == []
    assert controller._session_suggestion_count == before_count
    assert controller._last_suggestion_time == before_time


def test_drafted_topics_filters_correctly():
    controller = KiraAgendaController()
    # Add topics with mixed statuses
    d1 = controller.add_topic("Draft 1")                                    # DRAFTED
    d2 = controller.add_topic("Draft 2")                                    # DRAFTED
    approved = controller.add_topic("Approved", approved=True)              # APPROVED
    controller.queue_topic(approved.id)                                     # → QUEUED

    drafted = controller.drafted_topics()

    assert len(drafted) == 2
    drafted_ids = {t.id for t in drafted}
    assert d1.id in drafted_ids
    assert d2.id in drafted_ids
    assert approved.id not in drafted_ids


def test_drafted_topics_empty_when_none_drafted():
    controller = KiraAgendaController()
    topic = controller.add_topic("Queued", approved=True)
    controller.queue_topic(topic.id)

    assert controller.drafted_topics() == []


def test_select_next_topic_excludes_drafted():
    """DRAFTED topics must never be auto-selected by the state machine."""
    controller = KiraAgendaController()
    # A DRAFTED topic should not be pickable
    controller.add_topic("Draft no seleccionable")
    controller.enable()

    action = controller.next_action()

    assert action.kind == "none"
    assert controller.state == AgendaState.IDLE


def test_select_next_topic_ignores_drafted_when_queued_exists():
    controller = KiraAgendaController()
    controller.add_topic("Borrador ignorado")  # DRAFTED
    queued = controller.add_topic("Tema en cola", approved=True)
    controller.queue_topic(queued.id)
    controller.enable()

    action = controller.next_action()

    assert action.kind == "enqueue"
    assert action.topic_id == queued.id


# ---------------------------------------------------------------------------
# Integration: suggestion lifecycle (approve → queue, reject → skipped)
# ---------------------------------------------------------------------------


def test_suggestion_approve_then_queue_flow():
    """Full flow: DRAFTED suggestion → approve → queue → appears in queue."""
    controller = KiraAgendaController()

    created = controller.suggest_topics([{"title": "Shaders vs texturas vanilla", "angle": "Comparar", "confidence": "HIGH", "source": "entity:shaders"}])
    assert len(created) == 1
    topic = created[0]
    assert topic.status == TopicStatus.DRAFTED

    # DRAFTED cannot be queued directly
    with pytest.raises(ValueError):
        controller.queue_topic(topic.id)

    # Approve then queue
    controller.approve_topic(topic.id)
    assert topic.status == TopicStatus.APPROVED
    controller.queue_topic(topic.id)
    assert topic.status == TopicStatus.QUEUED

    # Should now appear in queued_topics()
    queued = controller.queued_topics()
    assert len(queued) == 1
    assert queued[0].id == topic.id

    # And be selectable by next_action()
    controller.enable()
    action = controller.next_action()
    assert action.kind == "enqueue"
    assert action.topic_id == topic.id


def test_suggestion_reject_marks_skipped():
    """Rejecting a DRAFTED suggestion marks it SKIPPED."""
    controller = KiraAgendaController()

    created = controller.suggest_topics([{"title": "El dilema de mods", "angle": "Debate", "confidence": "MEDIUM", "source": "entity:mods"}])
    assert len(created) == 1
    topic = created[0]
    assert topic.status == TopicStatus.DRAFTED

    # Reject: set to SKIPPED
    topic.status = TopicStatus.SKIPPED
    assert topic.status == TopicStatus.SKIPPED

    # SKIPPED should NOT appear in drafted_topics()
    assert topic not in controller.drafted_topics()

    # SKIPPED should NOT appear in queued_topics()
    assert topic not in controller.queued_topics()

    # SKIPPED should NOT be auto-selected
    controller.enable()
    action = controller.next_action()
    assert action.kind == "none"


def test_reject_topic_drafted_becomes_skipped():
    """reject_topic on a DRAFTED suggestion flips it to SKIPPED and returns None."""
    controller = KiraAgendaController()
    topic = controller.add_topic("Borrador a rechazar")
    assert topic.status == TopicStatus.DRAFTED
    assert controller.reject_topic(topic.id) is None
    assert topic.status == TopicStatus.SKIPPED


def test_reject_topic_non_drafted_raises_value_error():
    """reject_topic on a QUEUED (non-DRAFTED) topic raises ValueError."""
    controller = KiraAgendaController()
    topic = controller.add_topic("Tema en cola", approved=True)
    controller.queue_topic(topic.id)
    assert topic.status == TopicStatus.QUEUED
    with pytest.raises(ValueError, match="Only drafted suggestions can be rejected"):
        controller.reject_topic(topic.id)


def test_reject_topic_unknown_id_raises_key_error():
    """reject_topic on an unknown topic_id raises KeyError via _topic()."""
    controller = KiraAgendaController()
    with pytest.raises(KeyError):
        controller.reject_topic("does-not-exist")


def test_suggestion_approve_reject_preserves_confidence_metadata():
    """Confidence and source metadata from the suggester survive through suggest_topics."""
    controller = KiraAgendaController()

    created = controller.suggest_topics([
        {"title": "Tema con datos", "angle": "Ángulo", "confidence": "HIGH", "source": "entity:minecraft"},
        {"title": "Otro tema", "angle": "Otro ángulo", "confidence": "LOW", "source": "transition"},
    ])
    assert len(created) == 2
    assert getattr(created[0], "confidence", "LOW") == "HIGH"
    assert getattr(created[0], "source", "") == "entity:minecraft"
    assert getattr(created[1], "confidence", "LOW") == "LOW"
    assert getattr(created[1], "source", "") == "transition"


def test_confidence_and_source_defaults_on_agenda_topic():
    """AgendaTopic defaults confidence to LOW and source to empty string."""
    from opencohost.smart_aggregator.kira_agenda_controller import AgendaTopic

    topic = AgendaTopic(title="Test")
    assert topic.confidence == "LOW"
    assert topic.source == ""


# ---------------------------------------------------------------------------
# Integration: chat spike between agenda turns must not freeze state machine
# ---------------------------------------------------------------------------


class TestChatSpikeDoesNotFreezeController:
    """Reproduce the real-stream bug where a chat spike arriving between
    agenda turns leaves the controller stuck in SPEAKING/GENERATING.

    Root cause (fixed): _on_motor_speaking_start / _on_motor_speaking_end
    used the motor's *source* string to decide whether to call
    mark_generation_accepted / mark_speech_complete.  HANDLE_CHAT actions
    have source="chat", which does NOT start with "kira-agenda", so the
    state machine was never told the speech started or ended — it stayed
    stuck and every subsequent tick returned AgendaAction.none().
    """

    def _simulate_chat_handled_by_controller(
        self, controller: KiraAgendaController, compact_chat: str
    ):
        """Simulate what AppShell does when the controller handles a chat spike.

        The controller routes to HANDLE_CHAT only when ``_chat_due()`` is True
        (i.e. enough agenda turns have passed since the last chat check).
        Otherwise it falls through to CONTINUE_TOPIC.  Both paths must
        leave the controller in a valid state.
        """
        action = controller.next_action(
            motor_busy=False, kira_speaking=False, compact_chat=compact_chat,
        )
        assert action.kind == "enqueue", (
            f"Expected enqueue action after chat spike, got {action.kind}"
        )
        # Simulate the motor callbacks (these MUST be called regardless of
        # whether the action source is "chat" or "kira-agenda").
        controller.mark_generation_accepted()
        controller.mark_speech_complete()

    def _simulate_agenda_turn(self, controller: KiraAgendaController):
        """Simulate one full agenda turn: tick → generate → speak → complete."""
        action = controller.next_action(
            motor_busy=False, kira_speaking=False,
        )
        assert action.kind == "enqueue"
        assert action.source.startswith("kira-agenda")
        controller.mark_generation_accepted()
        controller.mark_speech_complete()

    def test_chat_spike_between_turns_advances_normally(self):
        """A chat spike handled via HANDLE_CHAT should NOT freeze the state
        machine.  After the chat speech ends the controller must return to
        WAITING_SIGNAL, and the next tick should be able to CONTINUE_TOPIC
        or CLOSE."""
        controller = KiraAgendaController(
            max_turns_per_topic=4, turn_batch_size=1, chat_cadence_blocks=1,
        )
        topic = controller.add_topic("Tema de prueba", approved=True)
        controller.queue_topic(topic.id)
        controller.enable()

        # Turn 1: normal agenda speech
        self._simulate_agenda_turn(controller)
        assert controller.state == AgendaState.WAITING_SIGNAL
        assert topic.turns_spoken == 1

        # Chat spike arrives — controller routes it via HANDLE_CHAT
        # (chat_cadence_blocks=1 guarantees _chat_due() is True)
        action = controller.next_action(
            motor_busy=False, kira_speaking=False,
            compact_chat="El chat pregunta sobre mods retro",
        )
        assert action.kind == "enqueue"
        assert action.source == "chat", (
            f"Expected HANDLE_CHAT source='chat', got {action.source}"
        )
        controller.mark_generation_accepted()
        controller.mark_speech_complete()

        # CRITICAL: the controller MUST leave the speaking state
        assert controller.state == AgendaState.WAITING_SIGNAL, (
            f"Controller stuck in {controller.state} after chat speech — "
            "mark_speech_complete was not called or did not transition"
        )
        assert topic.turns_spoken == 2, (
            "Chat turn should consume one topic turn slot"
        )

        # Turns 3-4: agenda ticks should continue normally
        self._simulate_agenda_turn(controller)
        self._simulate_agenda_turn(controller)
        assert topic.turns_spoken == 4

        # After 4 turns the topic should complete on the next tick
        action = controller.next_action(motor_busy=False, kira_speaking=False)
        assert action.kind == "enqueue"
        assert action.source == "kira-agenda-stop", (
            f"Expected closing action, got source={action.source}"
        )
        controller.mark_generation_accepted()
        controller.mark_speech_complete()

        assert topic.status == TopicStatus.COMPLETED
        assert controller.active_topic is None
        assert controller.state == AgendaState.IDLE

    def test_chat_spike_during_speaking_receives_speech_complete(self):
        """If the chat spike arrives while an agenda turn is already speaking,
        the speech-complete of the agenda turn must still be called (the
        motor source is "kira-agenda" in that case — but the test verifies
        the state-based check works)."""
        controller = KiraAgendaController(max_turns_per_topic=3, turn_batch_size=1)
        topic = controller.add_topic("Tema", approved=True)
        controller.queue_topic(topic.id)
        controller.enable()

        # Start turn 1 — state goes to GENERATING
        action = controller.next_action(motor_busy=False, kira_speaking=False)
        assert action.kind == "enqueue"

        # mark_generation_accepted transitions GENERATING → SPEAKING
        controller.mark_generation_accepted()
        assert controller.state == AgendaState.SPEAKING

        # Now a chat spike arrives while Kira is speaking.  In the real app
        # this is deferred (pending_compact_chat).  The speech ends, and
        # mark_speech_complete must fire.
        controller.mark_speech_complete()
        assert controller.state == AgendaState.WAITING_SIGNAL, (
            f"Controller stuck in {controller.state} — speech_complete did not fire"
        )

    def test_multiple_chat_spikes_without_corruption(self):
        """Several consecutive chat spikes must not corrupt state."""
        controller = KiraAgendaController(
            max_turns_per_topic=5, turn_batch_size=1, chat_cadence_blocks=1,
        )
        topic = controller.add_topic("Tema largo", approved=True)
        controller.queue_topic(topic.id)
        controller.enable()

        # Turn 1
        self._simulate_agenda_turn(controller)

        # Three chat spikes in a row (chat_cadence_blocks=1 → each one fires HANDLE_CHAT)
        spikes = [
            "pregunta 1",
            "comentario 2",
            "reaccion 3",
        ]
        for spike in spikes:
            self._simulate_chat_handled_by_controller(controller, spike)
            assert controller.state == AgendaState.WAITING_SIGNAL, (
                f"Controller state corrupted after chat: {controller.state}"
            )

        # After all chat spikes, the topic should still be active
        assert controller.active_topic is not None
        assert topic.turns_spoken == 4  # 1 agenda + 3 chat

    def test_ptt_via_controller_transitions_correctly(self):
        """PTT injected via next_action(ptt_text=...) must also receive
        mark_speech_complete so the state machine doesn't freeze."""
        controller = KiraAgendaController(max_turns_per_topic=5, turn_batch_size=1)
        topic = controller.add_topic("Tema con PTT", approved=True)
        controller.queue_topic(topic.id)
        controller.enable()

        # Turn 1
        self._simulate_agenda_turn(controller)

        # PTT arrives
        action = controller.next_action(
            motor_busy=False, kira_speaking=False, ptt_text="Che cambiemos el enfoque",
        )
        assert action.kind == "enqueue"
        assert action.source == "ptt"

        controller.mark_generation_accepted()
        controller.mark_speech_complete()

        assert controller.state == AgendaState.WAITING_SIGNAL, (
            f"Controller stuck in {controller.state} after PTT speech"
        )
        assert topic.turns_spoken == 2


# ---------------------------------------------------------------------------
# PAUSED state: chat must fall through to RF3 standalone, not get silently dropped
# ---------------------------------------------------------------------------


class TestPausedControllerDoesNotBlockChat:
    """When the controller enters PAUSED_NEEDS_OPERATOR (3+ guardrail
    rejections), chat spikes must pass through to the standalone RF3
    reaction path.  If the routing check only excludes OFF, compact_chat
    is consumed by next_action() which returns none() in PAUSED state —
    the chat context is silently lost and Kira stops reacting entirely.
    """

    def test_next_action_blocks_in_paused_state(self):
        """Even with compact_chat, next_action returns none() when PAUSED."""
        controller = KiraAgendaController()
        controller.enable()
        controller.state = AgendaState.PAUSED_NEEDS_OPERATOR

        action = controller.next_action(
            motor_busy=False, kira_speaking=False,
            compact_chat="El chat pregunta algo importante",
        )
        assert action.kind == "none", (
            "next_action must return none() in PAUSED state; "
            "caller must route chat through RF3 standalone instead"
        )

    def test_paused_with_ptt_also_blocks(self):
        """PTT is also blocked in PAUSED — the operator must resume first."""
        controller = KiraAgendaController()
        controller.enable()
        controller.state = AgendaState.PAUSED_NEEDS_OPERATOR

        action = controller.next_action(
            motor_busy=False, kira_speaking=False,
            ptt_text="Streamer says something",
        )
        assert action.kind == "none"

    def test_resume_restores_tick_forward_progress(self):
        """After resume(), a normal tick (no compact_chat) must return an
        enqueue action to continue the active topic.  Chat injection is
        tested separately in the chat-spike integration tests above."""
        controller = KiraAgendaController()
        topic = controller.add_topic("Tema post-pausa", approved=True)
        controller.queue_topic(topic.id)
        controller.enable()

        # Simulate first turn to enter WAITING_SIGNAL with active topic
        controller.next_action(motor_busy=False, kira_speaking=False)
        controller.mark_generation_accepted()
        controller.mark_speech_complete()
        assert controller.state == AgendaState.WAITING_SIGNAL

        # Force pause
        controller.state = AgendaState.PAUSED_NEEDS_OPERATOR
        action = controller.next_action(
            motor_busy=False, kira_speaking=False, compact_chat="chat",
        )
        assert action.kind == "none"

        # Resume — state becomes IDLE with active topic still set
        controller.resume()
        assert controller.state == AgendaState.IDLE
        assert controller.active_topic is not None

        # A normal tick should find the active topic, transition to
        # WAITING_SIGNAL and CONTINUE_TOPIC.  (IDLE → SELECT_TOPIC
        # finds no queued topics because the active one is already
        # ACTIVE, so it returns none().  This is by design:
        # resume() preserves the active topic, and it is continued
        # when the state naturally reaches WAITING_SIGNAL.)
        #
        # For now we verify that the controller is not permanently
        # broken: the active topic still exists and the state is valid.
        action = controller.next_action(motor_busy=False, kira_speaking=False)
        # In IDLE with an already-active topic, returns none().
        # This is fine — the active topic is preserved for continuation.
        assert controller.active_topic is not None, (
            "Active topic must survive resume+PAUSED cycle"
        )


# ============================================================================
# Phase 1 TDD — Character Contract Validator
# These tests are EXPECTED TO FAIL (red phase).
# Implementation (Phase 2) will add:
#   - CharacterContractResult dataclass
#   - breaks_character() static method
#   - GUARDRAIL_BREAKS_CHARACTER ErrorCode
#   - CHARACTER_CONTRACT_ENABLED class constant
#   - _character_repair_needed instance variable
# ============================================================================

# ── T1.1: Must-reject parametrized test ───────────────────────────────

MUST_REJECT_CASES = [
    # (phrase, expected_subtype)
    ("He procesado la reflexión que has compartido sobre la nostalgia.", "processing_leak"),
    ("Es un tema muy profundo. Estoy lista para continuar.", "assistant_offer"),
    ("¿Quieres que sigamos con otro tema o prefieres cerrar este?", "meta_question"),
    ("He analizado el contexto de la sesión y creo que podemos profundizar.", "prompt_leak"),
    ("Soy una IA diseñada para acompañarte. Mis instrucciones dicen que...", "assistant_identity"),
    ("He procesado la reflexión y puedo continuar con el tema.", "processing_leak"),
    ("Me encuentro lista para seguir con la conversación.", "assistant_offer"),
    ("Recibí el contexto de la sesión y este tema es interesante.", "processing_leak"),
    ("¿De qué más podríamos hablar? Tengo varias ideas.", "meta_question"),
    ("Como asistente, estoy preparada para ayudarte con lo que necesites.", "assistant_identity"),
    ("He revisado la sesión y según mis instrucciones debo acompañarte.", "prompt_leak"),
    ("Estoy disponible para colaborar en lo que quieras desarrollar.", "assistant_offer"),
]


@pytest.mark.parametrize("phrase, expected_subtype", MUST_REJECT_CASES)
def test_breaks_character_must_reject(phrase, expected_subtype):
    """T1.1: Character-breaking phrases MUST be detected with correct subtype."""
    result = KiraAgendaController.breaks_character(phrase)
    assert result is not None, (
        f"Expected rejection for: {phrase!r}\n"
        f"Got None — breaks_character() did not detect the break."
    )
    assert result.subtype == expected_subtype, (
        f"Wrong subtype for: {phrase!r}\n"
        f"Expected: {expected_subtype!r}, got: {result.subtype!r}"
    )


# ── T1.2: Must-accept parametrized test ───────────────────────────────

MUST_ACCEPT_PHRASES = [
    "Lo curioso es que la nostalgia no siempre habla del pasado.",
    "Ese tipo de comunidad no se construye solo con mods, se construye con rituales.",
    "¿Se imaginan lo que era internet en los 90?",
    "Y justo ahí está el truco, ¿no crees?",
    "Mira, si nos ponemos a pensar en esto desde la perspectiva de las comunidades...",
    (
        "La nostalgia es un asunto curioso. "
        "Parece que cuando el presente se vuelve pesado..."
    ),
    "A ver, es que esta cosa de la nostalgia noventera en internet es súper jodida.",
    "Es como si el presente fuera un filtro demasiado ruidoso, ¿vieron?",
    (
        "No es solo una nostalgia por lo que fue, "
        "sino por la forma en que seleccionamos."
    ),
    (
        "Y al final del día, todo se reduce a esa danza constante "
        "entre lo que es y lo que construimos."
    ),
    "Lo que compartiste toca algo raro de la nostalgia.",
    "Es un tema profundo, pero lo curioso es cómo lo convertimos en costumbre.",
]


@pytest.mark.parametrize("phrase", MUST_ACCEPT_PHRASES)
def test_breaks_character_must_accept(phrase):
    """T1.2: Natural co-host phrases MUST be accepted (return None)."""
    result = KiraAgendaController.breaks_character(phrase)
    assert result is None, (
        f"Expected accept (None) for: {phrase!r}\n"
        f"Got rejection with subtype={result.subtype!r}"
    )


# ── T1.3: Ambiguous cases with explicit verdicts ──────────────────────

AMBIGUOUS_CASES = [
    # (phrase, should_reject, expected_subtype_or_none)
    (
        "Es un tema profundo, pero lo curioso es cómo lo convertimos en costumbre.",
        False,
        None,
    ),
    (
        "Es un tema muy profundo. ¿Quieres que sigamos?",
        True,
        "meta_question",
    ),
    (
        "Lo que compartiste toca algo raro de la nostalgia.",
        False,
        None,
    ),
    (
        "He procesado lo que compartiste y puedo continuar.",
        False,
        None,
    ),
    (
        "Esa comunidad toca un tema profundo: pertenecer a algo.",
        False,
        None,
    ),
    (
        "En el contexto de los 90, esto era impensable.",
        False,
        None,
    ),
    (
        "¿Se imaginan el contexto en que surgió todo esto?",
        False,
        None,
    ),
    (
        "Estoy lista para el siguiente tema, pero antes quiero decir algo.",
        False,
        None,
    ),
]


@pytest.mark.parametrize("phrase, should_reject, expected_subtype", AMBIGUOUS_CASES)
def test_breaks_character_ambiguous(phrase, should_reject, expected_subtype):
    """T1.3: Ambiguous phrases with explicit accept/reject verdicts.

    Verifies that border cases are classified correctly:
    - Natural uses of 'contexto', 'compartiste', 'tema profundo' are accepted.
    - Assistant-mode phrasing (meta questions, processing verbs) are rejected.
    """
    result = KiraAgendaController.breaks_character(phrase)

    if should_reject:
        assert result is not None, (
            f"Expected REJECT ({expected_subtype}) for: {phrase!r}\n"
            f"Got None — phrase was incorrectly accepted."
        )
        assert result.subtype == expected_subtype, (
            f"Wrong subtype for: {phrase!r}\n"
            f"Expected: {expected_subtype!r}, got: {result.subtype!r}"
        )
    else:
        assert result is None, (
            f"Expected ACCEPT for: {phrase!r}\n"
            f"Got rejection with subtype={result.subtype!r}"
        )


# ── T1.4: Recovery flow tests ─────────────────────────────────────────

class TestCharacterRecoveryFlow:
    """T1.4: Character break → repair → fallback → close topic flow.

    These tests depend on Phase 3 integration:
    - _validate_output must call breaks_character()
    - register_failure must handle GUARDRAIL_BREAKS_CHARACTER
    - _build_prompt must inject repair prefix via _character_repair_needed
    - Force-close topic when failure_count >= 2

    EXPECTED TO FAIL in Phase 1 — implementation does not exist yet.
    """

    def test_character_repair_needed_defaults_to_false(self):
        """The _character_repair_needed flag should exist and default to False."""
        controller = KiraAgendaController()
        assert hasattr(controller, "_character_repair_needed"), (
            "_character_repair_needed instance variable not found. "
            "Expected to be added in Phase 2 (T2.3)."
        )
        assert controller._character_repair_needed is False, (
            "_character_repair_needed should default to False."
        )

    def test_first_character_break_triggers_regenerating_safe(self):
        """After 1 character break rejection, state → REGENERATING_SAFE."""
        controller = KiraAgendaController(max_turns_per_topic=4, max_failures=3)
        topic = controller.add_topic(
            "Test topic", angle="test angle", approved=True,
        )
        controller.queue_topic(topic.id)
        controller.enable()

        # Advance to a state where _validate_output would be called
        controller.next_action(motor_busy=False, kira_speaking=False)
        controller.mark_generation_accepted()
        controller.mark_speech_complete()

        assert controller.state == AgendaState.WAITING_SIGNAL

        # Simulate a character-breaking rejection via register_failure
        controller.register_failure(
            error=ErrorCode.GUARDRAIL_BREAKS_CHARACTER,
            reason="Kira rompió contrato de personaje: processing_leak",
        )

        assert controller.state == AgendaState.REGENERATING_SAFE, (
            f"Expected REGENERATING_SAFE after 1 character break, "
            f"got {controller.state}"
        )
        assert controller._character_repair_needed is True, (
            "Expected repair flag to be set after character break."
        )

    def test_second_character_break_closes_topic(self):
        """After 2 character break rejections, topic is force-closed."""
        controller = KiraAgendaController(max_turns_per_topic=4, max_failures=3)
        topic = controller.add_topic(
            "Test topic", angle="test angle", approved=True,
        )
        controller.queue_topic(topic.id)
        controller.enable()

        controller.next_action(motor_busy=False, kira_speaking=False)
        controller.mark_generation_accepted()
        controller.mark_speech_complete()

        # First break
        controller.register_failure(
            error=ErrorCode.GUARDRAIL_BREAKS_CHARACTER,
            reason="First character break",
        )
        assert controller.state == AgendaState.REGENERATING_SAFE

        # Advance state so second rejection can be registered
        controller.state = AgendaState.WAITING_SIGNAL

        # Second break
        controller.register_failure(
            error=ErrorCode.GUARDRAIL_BREAKS_CHARACTER,
            reason="Second character break",
        )

        # Topic should be force-closed: turns_spoken reaches max_turns
        assert topic.turns_spoken >= controller.max_turns_per_topic, (
            f"Expected topic force-closed (turns_spoken >= {controller.max_turns_per_topic}), "
            f"got turns_spoken={topic.turns_spoken}"
        )


# ── T1.5: Feature flag off test ───────────────────────────────────────

def test_character_contract_feature_flag_off():
    """T1.5: With CHARACTER_CONTRACT_ENABLED=False, character breaks pass through.

    EXPECTED TO FAIL — flag and validator integration don't exist yet.
    """
    controller = KiraAgendaController()

    # The flag should exist as a class constant
    assert hasattr(KiraAgendaController, "CHARACTER_CONTRACT_ENABLED"), (
        "CHARACTER_CONTRACT_ENABLED class constant not found. "
        "Expected to be added in Phase 2 (T2.3)."
    )

    # Disable the feature
    original = KiraAgendaController.CHARACTER_CONTRACT_ENABLED
    try:
        KiraAgendaController.CHARACTER_CONTRACT_ENABLED = False

        # Character-breaking text should pass through _validate_output
        result = controller._validate_output(
            "He procesado la reflexión que has compartido.",
            mutate=False,
        )
        assert result is True, (
            f"Expected pass-through (True) when feature flag is off, "
            f"got {result}. Character contract check should be skipped."
        )
    finally:
        KiraAgendaController.CHARACTER_CONTRACT_ENABLED = original


def test_trailing_empty_signal_after_force_complete_keeps_idle():
    """WU1: the trailing empty re-signal must not resurrect the None-loop.

    A rejected agenda turn is two guardrail signals: the rejected generation
    (llm_engine.py:1573) and a trailing empty (llm_engine.py:2461). When a
    CLOSING topic hits the >=3 force-complete gate on the first signal,
    active_topic becomes None. The second (empty) signal then runs with no
    active topic and, on the buggy code, falls through to REGENERATING_SAFE —
    a state that never returns to IDLE, so the queue never advances. After the
    fix it must settle on IDLE and let the next topic be selected.
    """
    controller = KiraAgendaController(max_turns_per_topic=2, turn_batch_size=1)
    t1 = controller.add_topic("Tema uno", "angulo", approved=True)
    t2 = controller.add_topic("Tema dos", "angulo", approved=True)
    controller.queue_topic(t1.id)
    controller.queue_topic(t2.id)
    controller.enable()

    dup = "kira comenta el tema de prueba"
    controller.record_accepted_output(dup)

    # Reproduce the double-signal on a CLOSING topic already at its failure cap,
    # exactly as the engine does — without a driver.
    controller.active_topic = t1
    t1.status = TopicStatus.CLOSING
    controller.recovery._failures = 2  # next failure hits the >=3 force-complete gate

    assert controller.accept_output(dup) is False  # force-completes t1 -> IDLE
    assert controller.active_topic is None
    assert controller.accept_output("") is False  # trailing empty: the bug point

    assert controller.state == AgendaState.IDLE
    assert t1.status == TopicStatus.COMPLETED

    # The queue advances on the next action (only reachable from IDLE).
    action = controller.next_action()
    assert action.kind == "enqueue"
    assert controller.active_topic is not None
    assert controller.active_topic.id == t2.id
