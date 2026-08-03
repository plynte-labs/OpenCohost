"""Tests for non-UI Editorial Cue Card to Agenda integration."""

from __future__ import annotations

import logging
import sqlite3

from opencohost.core.editorial import editorial_cards as editorial_cards_mod
from opencohost.core.editorial.editorial_agenda_bridge import EditorialAgendaBridge
from opencohost.core.editorial.editorial_cards import EditorialCard, EditorialCardStatus, EditorialCardStore
from opencohost.smart_aggregator import AgendaState, KiraAgendaController, TopicStatus


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
        single_use=True,  # D3/D7 pin: recorder retires a single_use card to USED
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
        single_use=True,  # D3/D7 pin: this test asserts recorder→USED
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
        single_use=True,  # D3/D7 pin: single_use card retires to USED
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


def test_bridge_reusable_card_via_recorder_stays_armed_and_records_injection(tmp_path) -> None:
    """D3: a reusable (single_use=False) card run through the agenda recorder
    path stays ARMED with last_injected_at set and the topic link detached —
    the opposite of the single_use pin above (which retires to USED)."""
    store = EditorialCardStore(tmp_path / "cards.db")
    controller = KiraAgendaController()
    bridge = EditorialAgendaBridge(store, controller)
    bridge.register_provider()

    card = store.upsert(EditorialCard(
        topic="Reusable difficulty debate",
        summary="La comunidad discute si el juego se volvió más fácil.",
        streamer_take="Quiero llevarlo a diseño y accesibilidad.",
        single_use=False,
    ))
    store.arm(card.id)
    topic = controller.add_topic("Reusable difficulty debate", approved=True)
    controller.queue_topic(topic.id)
    assert bridge.link_card_to_topic(topic.id, card.id) is True
    assert store.get(card.id).status is EditorialCardStatus.ACTIVE

    controller.active_topic = topic
    topic.editorial_card_consumed = True

    assert bridge.mark_used_after_successful_generation() is True

    refreshed = store.get(card.id)
    assert refreshed.status is EditorialCardStatus.ARMED  # stays eligible (D1/D3)
    assert refreshed.last_injected_at is not None
    assert refreshed.use_count == 1
    assert topic.editorial_card_id is None  # link detached as today


def test_bridge_reusable_completion_failure_releases_active_card(tmp_path, monkeypatch, caplog) -> None:
    """HIGH fix: complete_reusable_injection raising must not leave the card
    stuck ACTIVE forever — a best-effort compensating release resets it back
    to ARMED so the one-ACTIVE gate frees up, and the recorder fail-opens
    (returns False) without raising. Log messages carry the card id only."""
    store = EditorialCardStore(tmp_path / "cards.db")
    controller = KiraAgendaController()
    bridge = EditorialAgendaBridge(store, controller)
    bridge.register_provider()

    card = store.upsert(EditorialCard(
        topic="Reusable completion failure",
        summary="La comunidad discute el balance del ultimo parche.",
        streamer_take="Quiero contrastarlo con la data historica.",
        single_use=False,
    ))
    store.arm(card.id)
    topic = controller.add_topic("Reusable completion failure", approved=True)
    controller.queue_topic(topic.id)
    assert bridge.link_card_to_topic(topic.id, card.id) is True
    controller.active_topic = topic
    topic.editorial_card_consumed = True

    def _raise(self, card_id, injected_at):
        raise sqlite3.Error("disk I/O error")

    monkeypatch.setattr(editorial_cards_mod.EditorialCardStore, "complete_reusable_injection", _raise)

    with caplog.at_level(logging.WARNING):
        result = bridge.mark_used_after_successful_generation()

    assert result is False
    released = store.get(card.id)
    assert released.status is EditorialCardStatus.ARMED
    assert released.last_injected_at is None
    assert released.use_count == 0
    assert card.topic not in caplog.text
    assert card.summary not in caplog.text
    assert card.streamer_take not in caplog.text


def test_bridge_reusable_completion_and_release_both_fail_stays_active(tmp_path, monkeypatch) -> None:
    """When the compensating release ALSO raises, the card stays ACTIVE and
    the recorder still fail-opens (returns False) without raising."""
    store = EditorialCardStore(tmp_path / "cards.db")
    controller = KiraAgendaController()
    bridge = EditorialAgendaBridge(store, controller)
    bridge.register_provider()

    card = store.upsert(EditorialCard(
        topic="Reusable double failure",
        summary="La comunidad discute el balance del ultimo parche.",
        streamer_take="Quiero contrastarlo con la data historica.",
        single_use=False,
    ))
    store.arm(card.id)
    topic = controller.add_topic("Reusable double failure", approved=True)
    controller.queue_topic(topic.id)
    assert bridge.link_card_to_topic(topic.id, card.id) is True
    controller.active_topic = topic
    topic.editorial_card_consumed = True

    def _raise_complete(self, card_id, injected_at):
        raise sqlite3.Error("disk I/O error")

    def _raise_release(self, card_id):
        raise sqlite3.Error("database is locked")

    monkeypatch.setattr(editorial_cards_mod.EditorialCardStore, "complete_reusable_injection", _raise_complete)
    monkeypatch.setattr(editorial_cards_mod.EditorialCardStore, "release_active_card", _raise_release)

    result = bridge.mark_used_after_successful_generation()

    assert result is False
    stuck = store.get(card.id)
    assert stuck.status is EditorialCardStatus.ACTIVE  # both writes failed; no exception escaped


def test_bridge_single_use_transient_failure_self_heals_on_retry(tmp_path, monkeypatch) -> None:
    """single_use gets NO compensation: a transient mark_used failure leaves
    the card ACTIVE (uncompensated by design), and the next accepted-output
    recorder pass (same topic/card still linked) retries and flips it USED —
    proving self-healing without re-arming a single_use card."""
    store = EditorialCardStore(tmp_path / "cards.db")
    controller = KiraAgendaController()
    bridge = EditorialAgendaBridge(store, controller)
    bridge.register_provider()

    card = bridge.create_or_update_card(
        topic="Single use transient failure",
        summary="La comunidad discute el balance del ultimo parche.",
        streamer_take="Quiero contrastarlo con la data historica.",
        single_use=True,
    )
    bridge.arm_card(card.id)
    topic = controller.add_topic("Single use transient failure", approved=True)
    controller.queue_topic(topic.id)
    assert bridge.link_card_to_topic(topic.id, card.id) is True
    controller.active_topic = topic
    topic.editorial_card_consumed = True

    real_mark_used = editorial_cards_mod.EditorialCardStore.mark_used
    calls = {"n": 0}

    def _flaky(self, card_id):
        calls["n"] += 1
        if calls["n"] == 1:
            raise sqlite3.Error("disk I/O error")
        return real_mark_used(self, card_id)

    monkeypatch.setattr(editorial_cards_mod.EditorialCardStore, "mark_used", _flaky)

    # First pass: transient failure — no compensation, card stays ACTIVE and
    # the link stays intact (editorial_card_id/consumed untouched on raise).
    assert bridge.mark_used_after_successful_generation() is False
    assert store.get(card.id).status is EditorialCardStatus.ACTIVE
    assert topic.editorial_card_id == card.id

    # Second pass (retry on the next accepted output): store recovers, USED.
    assert bridge.mark_used_after_successful_generation() is True
    assert store.get(card.id).status is EditorialCardStatus.USED
