"""Bridge between the topic inbox store and the agenda suggestions UI.

Surfaces valid pending agent proposals as agenda suggestions (tagged with a
robot emoji, angle visible), polls the store fail-open on a fixed interval,
and routes approve/discard for ti_-prefixed suggestion ids.

Approval is the human-only gate: it happens here (triggered by the UI button),
never via CLI. Approving creates an APPROVED+QUEUED agenda topic and consumes
the inbox row. The agenda controller's sanitizer is stricter than the inbox
validator (title max 90 vs 120, emoji rejected) — when it refuses a title,
the row stays pending and the operator is told why.

Pure logic, no customtkinter import: the scheduler is injected so the module
is fully testable headless.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from opencohost.core.topic_inbox import ID_PREFIX as INBOX_ID_PREFIX
from opencohost.smart_aggregator.kira_agenda_controller import TopicStatus

logger = logging.getLogger(__name__)

POLL_INTERVAL_MS = 7000
INBOX_SOURCE_TAG = "🤖"


class TopicInboxBridge:
    """Polls TopicInboxStore and adapts proposals for the agenda panel."""

    def __init__(
        self,
        store: Any,
        log_fn: Callable[[str], None] | None = None,
        persist_fn: Callable[[], Any] | None = None,
    ) -> None:
        self._store = store
        self._log = log_fn or (lambda message: None)
        # Called after the agenda topic is created but BEFORE the inbox row
        # is claimed: if the app dies in between, the operator sees the
        # proposal again (visible duplicate) instead of losing both.
        self._persist_fn = persist_fn
        self._pending_cache: list[dict] = []
        self._known_ids: frozenset[str] = frozenset()

    # ------------------------------------------------------------------
    # Suggestion building
    # ------------------------------------------------------------------

    @staticmethod
    def is_inbox_id(topic_id: Any) -> bool:
        """True when a suggestion id belongs to the inbox namespace."""
        return str(topic_id or "").startswith(INBOX_ID_PREFIX)

    def pending_suggestions(self) -> list[dict]:
        """Cached valid proposals shaped for the agenda panel renderer."""
        return [
            {
                "title": row["title"],
                "angle": row["angle"],
                "confidence": "LOW",
                "source": f"{INBOX_SOURCE_TAG} {row['source'] or 'agente'}",
                "topic_id": row["id"],
            }
            for row in self._pending_cache
        ]

    def build_suggestions(self, agenda_controller: Any) -> list[dict]:
        """Merge the controller's DRAFTED topics with pending inbox proposals."""
        drafted = [
            {
                "title": topic.title,
                "angle": topic.angle,
                "confidence": topic.confidence,
                "source": topic.source,
                "topic_id": topic.id,
            }
            for topic in agenda_controller.drafted_topics()
        ]
        return drafted + self.pending_suggestions()

    # ------------------------------------------------------------------
    # Polling
    # ------------------------------------------------------------------

    def refresh(self) -> bool:
        """Re-read pending proposals. Returns True when the set changed.

        Fail-open: on any store error the previous cache is kept so the UI
        does not flicker empty, and False is returned.
        """
        try:
            rows = self._store.list_pending().get("valid", [])
        except Exception as exc:
            logger.warning("topic inbox poll failed (fail-open): %s", exc)
            return False
        ids = frozenset(row["id"] for row in rows)
        changed = ids != self._known_ids
        self._pending_cache = rows
        self._known_ids = ids
        return changed

    def start_polling(self, after_fn: Callable[[Callable[[], None], int], None], on_change: Callable[[], None]) -> None:
        """Poll every POLL_INTERVAL_MS via after_fn(fn, delay_ms).

        on_change fires only when the pending set actually changed; an
        exception inside it never kills the polling loop.
        """

        def _tick() -> None:
            if self.refresh():
                try:
                    on_change()
                except Exception as exc:
                    logger.warning("topic inbox on_change failed: %s", exc)
            after_fn(_tick, POLL_INTERVAL_MS)

        after_fn(_tick, POLL_INTERVAL_MS)

    # ------------------------------------------------------------------
    # Approve / discard routing
    # ------------------------------------------------------------------

    def approve(self, topic_id: str, agenda_controller: Any) -> bool:
        """Approve any suggestion: inbox proposal or controller DRAFTED topic.

        Inbox path (ti_ ids): the controller sanitizer runs first (validation
        gate), the store row is claimed second, and the created topic is
        rolled back if the claim fails (row consumed elsewhere).
        Controller path: mark APPROVED then QUEUED, mirroring the legacy
        suggestion flow.
        """
        if not self.is_inbox_id(topic_id):
            try:
                agenda_controller.approve_topic(topic_id)
                agenda_controller.queue_topic(topic_id)
                return True
            except (ValueError, KeyError):
                return False

        row = next((r for r in self._pending_cache if r["id"] == topic_id), None)
        if row is None:
            self.refresh()
            row = next((r for r in self._pending_cache if r["id"] == topic_id), None)
        if row is None:
            return False

        try:
            topic = agenda_controller.add_topic(row["title"], row["angle"], approved=True)
        except (ValueError, KeyError) as exc:
            self._log(f"[Topic Inbox] No se pudo aprobar “{str(row['title'])[:40]}”: {exc}")
            return False
        try:
            agenda_controller.queue_topic(topic.id)
        except (ValueError, KeyError) as exc:
            agenda_controller.topics.remove(topic)
            self._log(f"[Topic Inbox] No se pudo encolar “{str(row['title'])[:40]}”: {exc}")
            return False

        if self._persist_fn is not None:
            try:
                self._persist_fn()
            except Exception as exc:
                logger.warning("topic inbox persist hook failed (fail-open): %s", exc)

        if not self._store.approve(topic_id):
            agenda_controller.topics.remove(topic)
            self._log("[Topic Inbox] La propuesta ya no estaba pendiente; no se agendó.")
            self._drop_from_cache(topic_id)
            return False

        self._drop_from_cache(topic_id)
        return True

    def discard(self, topic_id: str) -> bool:
        """Discard a pending proposal; True when the store accepted it."""
        if not self._store.discard(topic_id):
            return False
        self._drop_from_cache(topic_id)
        return True

    def reject(self, topic_id: str, agenda_controller: Any) -> bool:
        """Reject any suggestion: discard inbox proposals, skip DRAFTED topics."""
        if self.is_inbox_id(topic_id):
            return self.discard(topic_id)
        topic = next((t for t in agenda_controller.topics if t.id == topic_id), None)
        if topic is None or topic.status is not TopicStatus.DRAFTED:
            return False
        topic.status = TopicStatus.SKIPPED
        return True

    def _drop_from_cache(self, topic_id: str) -> None:
        self._pending_cache = [r for r in self._pending_cache if r["id"] != topic_id]
        self._known_ids = frozenset(r["id"] for r in self._pending_cache)
