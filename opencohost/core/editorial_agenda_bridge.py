"""Bridge Editorial Cue Cards into the deterministic Agenda/Cohost path."""

from __future__ import annotations

import logging

from opencohost.core.editorial_cards import EditorialCard, EditorialCardStatus, EditorialCardStore
from opencohost.smart_aggregator.kira_agenda_controller import AgendaTopic, KiraAgendaController

log = logging.getLogger(__name__)


class EditorialAgendaBridge:
    """Small non-UI integration layer for card preparation and Agenda linking."""

    def __init__(self, store: EditorialCardStore, controller: KiraAgendaController) -> None:
        self.store = store
        self.controller = controller

    def register_provider(self) -> None:
        """Allow Agenda prompt assembly to resolve linked card ids on demand.

        Also registers the auto-attach provider so armed cards are matched to
        incoming agenda topics automatically at generation time.
        """
        self.controller.set_editorial_context_provider(self.resolve_prompt_block)
        self.controller.set_auto_attach_provider(self.auto_attach)

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

    def auto_attach(self, topic: "AgendaTopic") -> bool:
        """Try to match an armed editorial card to *topic* using token-overlap scoring.

        Returns True when a card was successfully linked; False on no match,
        ambiguity, or any store/matching error (fail-open).
        """
        try:
            cards = self.store.list_armed()
            eligible = [c for c in cards if not c.is_expired()]
            from opencohost.core.editorial_matching import match_score, select_card
            text = (topic.title or "") + (" " + topic.angle if topic.angle else "")
            candidates = [c for c in eligible if match_score(text, c) >= 0.8]
            card = select_card(text, eligible)
            if card is None:
                if len(candidates) > 1:
                    log.info(
                        "editorial auto-attach: ambiguous (%d candidates) for topic %r — skipping",
                        len(candidates),
                        topic.title,
                    )
                else:
                    log.info(
                        "editorial auto-attach: no candidate for topic %r",
                        topic.title,
                    )
                return False
            result = self.link_card_to_topic(topic.id, card.id)
            if result:
                log.info(
                    "editorial auto-attach: attached card %s to topic %r",
                    card.id,
                    topic.title,
                )
            return result
        except Exception as exc:
            log.warning("editorial auto-attach: store error: %s", exc)
            return False

    def resolve_direct_context(self, query_text: str) -> str | None:
        """Return an editorial context block for a direct host query, or None.

        NON-CONSUMING: does NOT activate, mark used, or change card status.
        The card stays ARMED so the agenda path can still attach it.
        Fail-open: any store/matching exception logs a warning and returns None.
        """
        try:
            cards = self.store.list_armed()
            eligible = [c for c in cards if not c.is_expired()]
            from opencohost.core.editorial_matching import select_card
            card = select_card(query_text, eligible)
            if card is None:
                return None
            return card.to_prompt_block()
        except Exception as exc:
            log.warning("editorial direct context: store error: %s", exc)
            return None

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
