"""Bridge Editorial Cue Cards into the deterministic Agenda/Cohost path."""

from __future__ import annotations

from opencohost.core.editorial_cards import EditorialCard, EditorialCardStatus, EditorialCardStore
from opencohost.smart_aggregator.kira_agenda_controller import AgendaTopic, KiraAgendaController


class EditorialAgendaBridge:
    """Small non-UI integration layer for card preparation and Agenda linking."""

    def __init__(self, store: EditorialCardStore, controller: KiraAgendaController) -> None:
        self.store = store
        self.controller = controller

    def register_provider(self) -> None:
        """Allow Agenda prompt assembly to resolve linked card ids on demand."""

        self.controller.set_editorial_context_provider(self.resolve_prompt_block)

    def create_or_update_card(
        self,
        *,
        topic: str,
        summary: str,
        streamer_take: str,
        counterpoints: list[str] | None = None,
        discussion_hooks: list[str] | None = None,
        triggers: list[str] | None = None,
    ) -> EditorialCard:
        """Create or upsert a structured operator-authored cue card."""

        return self.store.upsert(
            EditorialCard(
                topic=topic,
                summary=summary,
                streamer_take=streamer_take,
                counterpoints=counterpoints or [],
                discussion_hooks=discussion_hooks or [],
                triggers=triggers or [],
            )
        )

    def arm_card(self, card_id: str) -> bool:
        """Make a draft card eligible for deterministic Agenda linking."""

        return self.store.arm(card_id)

    def link_card_to_topic(self, topic_id: str, card_id: str) -> bool:
        """Activate an armed card and attach it to a queued/approved Agenda topic."""

        card = self.store.get(card_id)
        if card is None or card.status is not EditorialCardStatus.ARMED or card.is_expired():
            return False
        active = self.store.activate_for_topic(card.topic_slug)
        if active is None:
            return False
        topic = self.controller._topic(topic_id)
        topic.editorial_card_id = active.id
        topic.editorial_card_consumed = False
        return True

    def resolve_prompt_block(self, card_id: str) -> str | None:
        """Return prompt context only for the currently active, non-expired card."""

        card = self.store.get(card_id)
        if card is None or card.status is not EditorialCardStatus.ACTIVE or card.is_expired():
            return None
        return card.to_prompt_block()

    def mark_used_after_successful_generation(self) -> bool:
        """Mark the linked card used once Agenda accepts a generated response."""

        topic: AgendaTopic | None = self.controller.active_topic
        if not topic or not topic.editorial_card_id or not topic.editorial_card_consumed:
            return False
        if self.store.mark_used(topic.editorial_card_id):
            topic.editorial_card_id = None
            topic.editorial_card_consumed = False
            return True
        return False
