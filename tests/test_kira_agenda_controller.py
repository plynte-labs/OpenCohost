"""Tests for Kira Co-host Agenda Mode controller."""

import pytest

from smart_aggregator.kira_agenda_controller import (
    AgendaState,
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


def test_output_sanitizer_rejects_repeated_last_line_loop():
    controller = KiraAgendaController()

    assert controller.accept_output("Idea nueva.\nNo cierres en círculo.\nNo cierres en círculo.") is False


def test_output_sanitizer_rejects_recent_line_and_near_repeat():
    controller = KiraAgendaController()
    first = "Y eso es porque la IA no es un monstruo, es un espejo torcido de lo que entrenamos y premiamos."

    assert controller.accept_output(first) is True
    assert controller.accept_output(f"Otra entrada. {first}") is False

    controller = KiraAgendaController()
    assert controller.accept_output("Y eso abre un ángulo interesante sobre la cultura digital, la confianza pública y las decisiones técnicas que nadie revisa con calma.") is True
    assert controller.accept_output("Y en eso aparece un ángulo interesante sobre la cultura digital, la confianza pública y esas decisiones técnicas que casi nadie revisa con calma.") is False


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


def test_three_failures_pause_mode():
    controller = KiraAgendaController(max_failures=3)
    controller.enable()

    controller.register_failure()
    controller.register_failure()
    controller.register_failure()

    assert controller.state == AgendaState.PAUSED_NEEDS_OPERATOR
    assert controller.next_action().kind == "none"
