"""Bridge Editorial Cue Cards into the deterministic Agenda/Cohost path."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone

from opencohost.config import settings
from opencohost.core.editorial_cards import EditorialCard, EditorialCardStatus, EditorialCardStore
from opencohost.smart_aggregator.kira_agenda_controller import AgendaTopic, KiraAgendaController

log = logging.getLogger(__name__)


class EditorialAgendaBridge:
    """Small non-UI integration layer for card preparation and Agenda linking."""

    def __init__(self, store: EditorialCardStore, controller: KiraAgendaController) -> None:
        self.store = store
        self.controller = controller
        # Direct-turn USED trigger (D2): resolve_direct_context records the
        # matched card id here; commit_direct_injection (the engine's
        # post-generation hook) reads+clears it. Single engine worker thread is
        # the only caller, so no lock is needed.
        self._pending_direct_card_id: str | None = None

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
        single_use: bool = False,
    ) -> EditorialCard:
        """Create or upsert a structured operator-authored cue card.

        Cards are reusable by default (D1); pass single_use=True to opt a card
        into retiring after its first injection.
        """

        return self.store.upsert(
            EditorialCard(
                topic=topic,
                summary=summary,
                streamer_take=streamer_take,
                counterpoints=counterpoints or [],
                discussion_hooks=discussion_hooks or [],
                triggers=triggers or [],
                single_use=single_use,
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
            now = datetime.now(timezone.utc)
            cooldown_s = settings.EDITORIAL_REUSE_COOLDOWN_SECONDS
            cards = self.store.list_armed()
            eligible = [
                c for c in cards
                if not c.is_expired() and not c.in_cooldown(now, cooldown_s)
            ]
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

        NON-CONSUMING at resolve time: does NOT activate, mark used, or change
        card status. The card stays ARMED so the agenda path can still attach
        it. Records the matched card id as pending (D2) so the engine's
        post-generation hook (commit_direct_injection) can commit the USED /
        record_injection transition. Clears any stale pending id at entry so a
        prior turn that never committed cannot leak into this one.
        Fail-open: any store/matching exception logs a warning and returns None.
        """
        self._pending_direct_card_id = None
        try:
            now = datetime.now(timezone.utc)
            cooldown_s = settings.EDITORIAL_REUSE_COOLDOWN_SECONDS
            cards = self.store.list_armed()
            eligible = [
                c for c in cards
                if not c.is_expired() and not c.in_cooldown(now, cooldown_s)
            ]
            from opencohost.core.editorial_matching import select_card
            card = select_card(query_text, eligible)
            if card is None:
                return None
            self._pending_direct_card_id = card.id
            return card.to_prompt_block()
        except Exception as exc:
            log.warning("editorial direct context: store error: %s", exc)
            return None

    def commit_direct_injection(self) -> None:
        """Commit the pending direct-turn injection recorded by resolve_direct_context.

        Invoked by the engine (direct_editorial_usage_recorder) once, right
        after a successful direct generation (D2). single_use → store.mark_used
        (→USED); reusable → store.record_injection (stays ARMED, last_injected_at
        set). Reads+clears the pending id first so a failed generation or a store
        error can never double-commit on a later turn. Fail-open: store errors
        are logged, never raised — the engine worker thread is the only caller.
        """
        card_id = self._pending_direct_card_id
        self._pending_direct_card_id = None
        if not card_id:
            return
        try:
            card = self.store.get(card_id)
            if card is not None and card.single_use:
                self.store.mark_used(card_id)
            else:
                self.store.record_injection(card_id)
        except Exception as exc:
            log.warning("editorial direct commit: store error: %s", exc)

    def mark_used_after_successful_generation(self) -> bool:
        """Commit the linked card once Agenda accepts a generated response (D3).

        single_use → mark_used (→USED, current behavior). Reusable →
        complete_reusable_injection: one atomic transaction that records the
        injection and resets ACTIVE→ARMED so the one-ACTIVE gate frees up,
        then detach the topic link. Method name/signature unchanged so both
        host recorders need zero changes.
        Fail-open: any persistence error (sqlite3.Error, OSError) anywhere in
        this commit path is logged with the card id only — never card content
        — and this returns False instead of propagating into the
        accepted-output recorder. Mirrors resolve_direct_context's fail-open
        style.
        """
        topic: AgendaTopic | None = self.controller.active_topic
        if not topic or not topic.editorial_card_id or not topic.editorial_card_consumed:
            return False
        card_id = topic.editorial_card_id
        try:
            card = self.store.get(card_id)
            if card is not None and not card.single_use:
                # Reusable: one atomic transaction records usage and resets ARMED.
                if self.store.complete_reusable_injection(card_id, datetime.now(timezone.utc)):
                    topic.editorial_card_id = None
                    topic.editorial_card_consumed = False
                    return True
                return False
            # single_use (or a card that vanished): retire to USED as before.
            if self.store.mark_used(card_id):
                topic.editorial_card_id = None
                topic.editorial_card_consumed = False
                return True
            return False
        except (sqlite3.Error, OSError) as exc:
            log.warning("editorial agenda commit: store error for card %s: %s", card_id, exc)
            return False
