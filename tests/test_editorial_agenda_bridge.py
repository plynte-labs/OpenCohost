"""Tests for non-UI Editorial Cue Card to Agenda integration."""

from __future__ import annotations

from core.editorial_agenda_bridge import EditorialAgendaBridge
from core.editorial_cards import EditorialCardStatus, EditorialCardStore
from smart_aggregator import AgendaState, KiraAgendaController, TopicStatus


def test_bridge_create_arm_link_inject_and_mark_used(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    controller = KiraAgendaController()
    bridge = EditorialAgendaBridge(store, controller)
    bridge.register_provider()

    card = bridge.create_or_update_card(
        topic="Monetización Game X",
        summary="La comunidad critica precios nuevos.",
        streamer_take="Quiero debatir si cruza a pay-to-win.",
        discussion_hooks=["¿Dónde está la línea justa?"],
    )
    assert bridge.arm_card(card.id) is True

    topic = controller.add_topic("Monetización Game X", approved=True)
    controller.queue_topic(topic.id)

    assert bridge.link_card_to_topic(topic.id, card.id) is True
    assert store.get(card.id).status is EditorialCardStatus.ACTIVE
    assert topic.editorial_card_id == card.id

    controller.enable()
    action = controller.next_action()

    assert action.kind == "enqueue"
    assert "<editorial_context>" in action.prompt
    assert "pay-to-win" in action.prompt
    assert topic.editorial_card_consumed is True

    assert bridge.mark_used_after_successful_generation() is True
    used = store.get(card.id)
    assert used.status is EditorialCardStatus.USED
    assert used.use_count == 1
    assert topic.editorial_card_id is None


def test_bridge_does_not_link_missing_unarmed_or_used_cards(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    controller = KiraAgendaController()
    bridge = EditorialAgendaBridge(store, controller)
    bridge.register_provider()

    card = bridge.create_or_update_card(
        topic="Notas del parche",
        summary="El parche ajusta balance y progresión.",
        streamer_take="Quiero compararlo con lo que pedía la comunidad.",
    )
    topic = controller.add_topic("Notas del parche", approved=True)

    assert bridge.link_card_to_topic(topic.id, "missing") is False
    assert bridge.link_card_to_topic(topic.id, card.id) is False
    assert topic.editorial_card_id is None

    assert bridge.arm_card(card.id) is True
    assert bridge.link_card_to_topic(topic.id, card.id) is True
    assert bridge.mark_used_after_successful_generation() is False

    topic.editorial_card_consumed = True
    controller.active_topic = topic
    topic.editorial_card_consumed = True
    assert bridge.mark_used_after_successful_generation() is True
    assert store.get(card.id).status is EditorialCardStatus.USED

    next_topic = controller.add_topic("Notas del parche 2", approved=True)
    assert bridge.link_card_to_topic(next_topic.id, card.id) is False
    assert bridge.resolve_prompt_block(card.id) is None


def test_bridge_marks_used_through_controller_recorder_after_generation(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    controller = KiraAgendaController()
    bridge = EditorialAgendaBridge(store, controller)
    bridge.register_provider()

    card = bridge.create_or_update_card(
        topic="Dificultad del juego",
        summary="La comunidad discute si el juego se volvió más fácil.",
        streamer_take="Quiero llevarlo a diseño y accesibilidad.",
    )
    bridge.arm_card(card.id)
    topic = controller.add_topic("Dificultad del juego", approved=True)
    controller.queue_topic(topic.id)
    bridge.link_card_to_topic(topic.id, card.id)
    controller.enable()

    action = controller.next_action()
    controller.record_accepted_output("Respuesta aceptada de Kira")
    bridge.mark_used_after_successful_generation()

    assert action.topic_id == topic.id
    assert controller.state is AgendaState.GENERATING
    assert topic.status is TopicStatus.ACTIVE
    assert store.get(card.id).status is EditorialCardStatus.USED
    assert "respuesta aceptada" in controller.last_outputs[-1]
