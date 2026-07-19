"""Editorial Cue Cards data model and local SQLite store.

The MVP keeps cards as structured, operator-curated context.  Raw chat and raw
copied pages are deliberately rejected at the model boundary so they cannot leak
into persistence or prompt context.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable
from uuid import uuid4


class EditorialCardValidationError(ValueError):
    """Raised when a cue card violates MVP safety or schema rules."""


class EditorialCardStatus(str, Enum):
    """Lifecycle state for an Editorial Cue Card."""

    DRAFT = "draft"
    ARMED = "armed"
    ACTIVE = "active"
    USED = "used"
    EXPIRED = "expired"


class EditorialCardRatingValue(str, Enum):
    """Operator utility rating for a used Editorial Cue Card."""

    USEFUL = "useful"
    NOT_USEFUL = "not_useful"
    UNSURE = "unsure"


@dataclass
class EditorialCardRating:
    """Post-use utility signal that avoids raw chat or page persistence."""

    card_id: str
    rating: EditorialCardRatingValue
    reason_code: str = ""
    id: str = field(default_factory=lambda: f"ecr_{uuid4().hex}")
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    raw_chat: str | None = field(default=None, repr=False)

    REASON_MAX_CHARS = 80

    def __post_init__(self) -> None:
        if self.raw_chat:
            raise EditorialCardValidationError("Editorial card ratings cannot store raw chat")
        self.card_id = " ".join((self.card_id or "").split())
        if not self.card_id:
            raise EditorialCardValidationError("card_id is required")
        if not isinstance(self.rating, EditorialCardRatingValue):
            self.rating = EditorialCardRatingValue(str(self.rating))
        self.reason_code = normalize_reason_code(self.reason_code)
        if len(self.reason_code) > self.REASON_MAX_CHARS:
            raise EditorialCardValidationError("reason_code is too long")


@dataclass
class EditorialCard:
    """Structured, streamer-curated context for one future Kira response."""

    topic: str
    summary: str
    streamer_take: str
    counterpoints: list[str] = field(default_factory=list)
    discussion_hooks: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    topic_slug: str = ""
    id: str = field(default_factory=lambda: f"ec_{uuid4().hex}")
    status: EditorialCardStatus = EditorialCardStatus.DRAFT
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    expires_at: datetime | None = None
    last_used_at: datetime | None = None
    last_injected_at: datetime | None = None
    use_count: int = 0
    # Reusable by default (D1): a card stays eligible across many replies.
    # single_use=True opts a card into retiring (→USED) on its first injection.
    single_use: bool = False
    # Provenance of the write (agent_context_gateway): the agent name for
    # cards written through /api/agent/cards; '' means operator (CLI/UI
    # flows never set it — behavior unchanged).
    origin: str = ""
    raw_chat: str | None = field(default=None, repr=False)
    raw_page: str | None = field(default=None, repr=False)

    SUMMARY_MAX_CHARS = 1200
    STREAMER_TAKE_MAX_CHARS = 800
    TOPIC_MAX_CHARS = 120
    ITEM_MAX_CHARS = 240
    MAX_ITEMS = 8
    ORIGIN_MAX_CHARS = 80

    def __post_init__(self) -> None:
        if self.raw_chat or self.raw_page:
            raise EditorialCardValidationError("Editorial cards cannot store raw chat or raw copied pages")
        self.topic = self._clean_required(self.topic, "topic", self.TOPIC_MAX_CHARS)
        self.summary = self._clean_required(self.summary, "summary", self.SUMMARY_MAX_CHARS)
        self.streamer_take = self._clean_required(
            self.streamer_take,
            "streamer_take",
            self.STREAMER_TAKE_MAX_CHARS,
        )
        self.counterpoints = self._clean_list(self.counterpoints, "counterpoints")
        self.discussion_hooks = self._clean_list(self.discussion_hooks, "discussion_hooks")
        self.triggers = self._clean_list(self.triggers, "triggers")
        self.topic_slug = self.topic_slug or slugify(self.topic)
        if not self.topic_slug:
            raise EditorialCardValidationError("topic_slug is required")
        if not isinstance(self.status, EditorialCardStatus):
            self.status = EditorialCardStatus(str(self.status))
        # origin is optional ('' = operator) but bounded: it is rendered in
        # operator-facing labels, same cap as topic_inbox.SOURCE_MAX.
        self.origin = " ".join((self.origin or "").split())
        if len(self.origin) > self.ORIGIN_MAX_CHARS:
            raise EditorialCardValidationError(
                f"origin exceeds {self.ORIGIN_MAX_CHARS} characters"
            )

    @classmethod
    def _clean_required(cls, value: str, field_name: str, max_chars: int) -> str:
        text = " ".join((value or "").split())
        if not text:
            raise EditorialCardValidationError(f"{field_name} is required")
        if len(text) > max_chars:
            raise EditorialCardValidationError(f"{field_name} exceeds {max_chars} characters")
        return text

    @classmethod
    def _clean_list(cls, values: Iterable[str], field_name: str) -> list[str]:
        cleaned: list[str] = []
        for value in values or []:
            text = " ".join((value or "").split())
            if not text:
                continue
            if len(text) > cls.ITEM_MAX_CHARS:
                raise EditorialCardValidationError(f"{field_name} item exceeds {cls.ITEM_MAX_CHARS} characters")
            cleaned.append(text)
        return cleaned[: cls.MAX_ITEMS]

    def is_expired(self, now: datetime | None = None) -> bool:
        """Return True when this card has passed its optional expiration."""

        if self.expires_at is None:
            return False
        current = now or datetime.now(timezone.utc)
        return self.expires_at <= current

    def in_cooldown(self, now: datetime, cooldown_s: float) -> bool:
        """Return True while this card was injected within *cooldown_s* seconds.

        A never-injected card (last_injected_at is None) is never in cooldown.
        Used by the bridge to space out reuse of a reusable card (D4).
        """
        if self.last_injected_at is None:
            return False
        return (now - self.last_injected_at).total_seconds() < cooldown_s

    def to_prompt_block(self, *, max_chars: int = 1200) -> str:
        """Render a bounded structured prompt block for one-turn use."""

        payload = {
            "topic": self.topic,
            "summary": self.summary,
            "streamer_take": self.streamer_take,
            "counterpoints": self.counterpoints,
            "discussion_hooks": self.discussion_hooks,
            "triggers": self.triggers,
            "instruction": "Usá este contexto una sola vez; priorizá claridad sobre chistes y no anuncies la estructura.",
        }
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        block = f"<editorial_context>\n{body}\n</editorial_context>"
        if len(block) <= max_chars:
            return block
        safe_payload = dict(payload)
        overhead = len(f"<editorial_context>\n\n</editorial_context>") + 20
        available = max(80, max_chars - overhead)
        safe_payload["summary"] = self.summary[: max(20, available // 2)].rstrip()
        safe_payload["streamer_take"] = self.streamer_take[: max(20, available // 3)].rstrip()
        safe_payload["counterpoints"] = self.counterpoints[:2]
        safe_payload["discussion_hooks"] = self.discussion_hooks[:2]
        body = json.dumps(safe_payload, ensure_ascii=False, separators=(",", ":"))
        block = f"<editorial_context>\n{body}\n</editorial_context>"
        return block[:max_chars]


class EditorialCardStore:
    """SQLite-backed store for deterministic Editorial Cue Card lookup."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def upsert(self, card: EditorialCard) -> EditorialCard:
        """Insert a card or update the existing card with the same topic slug."""

        existing = self.find_by_topic_slug(card.topic_slug)
        now = datetime.now(timezone.utc)
        if existing is not None:
            card.id = existing.id
            card.created_at = existing.created_at
            card.status = existing.status
            card.use_count = existing.use_count
            card.last_used_at = existing.last_used_at
            # last_injected_at is usage history like use_count/last_used_at:
            # preserved across upserts. single_use is content-like (the writer
            # redeclares intent) so it is taken from the incoming card.
            card.last_injected_at = existing.last_injected_at
        card.updated_at = now
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO editorial_cards (
                    id, topic_slug, status, topic, summary, streamer_take,
                    counterpoints_json, discussion_hooks_json, triggers_json,
                    created_at, updated_at, expires_at, last_used_at, use_count,
                    origin, single_use, last_injected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(topic_slug) DO UPDATE SET
                    status=excluded.status,
                    topic=excluded.topic,
                    summary=excluded.summary,
                    streamer_take=excluded.streamer_take,
                    counterpoints_json=excluded.counterpoints_json,
                    discussion_hooks_json=excluded.discussion_hooks_json,
                    triggers_json=excluded.triggers_json,
                    updated_at=excluded.updated_at,
                    expires_at=excluded.expires_at,
                    last_used_at=excluded.last_used_at,
                    use_count=excluded.use_count,
                    origin=excluded.origin,
                    single_use=excluded.single_use,
                    last_injected_at=excluded.last_injected_at
                """,
                self._to_row(card),
            )
        return self.get(card.id) or card

    def get(self, card_id: str) -> EditorialCard | None:
        """Return a card by id, if it exists."""

        with self._connect() as conn:
            row = conn.execute("SELECT * FROM editorial_cards WHERE id = ?", (card_id,)).fetchone()
        return self._from_row(row) if row else None

    def find_by_topic_slug(self, topic_slug: str) -> EditorialCard | None:
        """Return a card by topic slug, if it exists."""

        with self._connect() as conn:
            row = conn.execute("SELECT * FROM editorial_cards WHERE topic_slug = ?", (topic_slug,)).fetchone()
        return self._from_row(row) if row else None

    def find_armed_by_topic_slug(self, topic_slug: str) -> EditorialCard | None:
        """Return an armed, non-expired card by topic slug."""

        card = self.find_by_topic_slug(topic_slug)
        if card is None or card.status is not EditorialCardStatus.ARMED or card.is_expired():
            return None
        return card

    def arm(self, card_id: str) -> bool:
        """Move a draft card to armed unless it is expired or already consumed."""

        card = self.get(card_id)
        if card is None or card.is_expired() or card.status in {EditorialCardStatus.USED, EditorialCardStatus.ACTIVE}:
            if card is not None and card.is_expired():
                self._set_status(card.id, EditorialCardStatus.EXPIRED)
            return False
        self._set_status(card.id, EditorialCardStatus.ARMED)
        return True

    def activate_for_topic(self, topic_slug: str) -> EditorialCard | None:
        """Activate one armed card if no other card is currently active."""

        with self._connect() as conn:
            active = conn.execute(
                "SELECT id FROM editorial_cards WHERE status = ? LIMIT 1",
                (EditorialCardStatus.ACTIVE.value,),
            ).fetchone()
            if active is not None:
                return None
        card = self.find_armed_by_topic_slug(topic_slug)
        if card is None:
            return None
        self._set_status(card.id, EditorialCardStatus.ACTIVE)
        return self.get(card.id)

    def mark_used(self, card_id: str) -> bool:
        """Mark a card used after a successful one-turn generation."""

        card = self.get(card_id)
        if card is None:
            return False
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE editorial_cards
                SET status = ?, last_used_at = ?, use_count = use_count + 1, updated_at = ?
                WHERE id = ?
                """,
                (EditorialCardStatus.USED.value, _dt_to_text(now), _dt_to_text(now), card_id),
            )
        return True

    def record_injection(self, card_id: str) -> bool:
        """Record a reusable-card injection without retiring it.

        Mirrors mark_used's shape but NEVER touches status: sets
        last_injected_at, bumps use_count, and refreshes updated_at so a
        reusable card stays eligible for future injections (D1/D2).
        """
        card = self.get(card_id)
        if card is None:
            return False
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE editorial_cards
                SET last_injected_at = ?, use_count = use_count + 1, updated_at = ?
                WHERE id = ?
                """,
                (_dt_to_text(now), _dt_to_text(now), card_id),
            )
        return True

    def complete_reusable_injection(self, card_id: str, injected_at: datetime) -> bool:
        """Atomically record a reusable-card injection and reset ACTIVE->ARMED.

        Single-transaction replacement for record_injection + a separate
        _set_status(ARMED) call on the reusable-completion path (D3): one
        UPDATE both records the injection and validates the card is still
        ACTIVE at commit time, so a status change racing between the caller's
        get() and this call cannot double-apply. Returns False (no exception
        raised) when the card is missing or no longer ACTIVE — the WHERE
        clause simply matches zero rows; nothing is stuck in that case.

        Real guarantee: atomic when it commits (the row either fully updates
        or the WHERE match leaves it untouched). If the UPDATE itself raises
        (sqlite3.Error/OSError), the transaction rolls back and the card
        stays ACTIVE — this method does not self-heal that case. The caller
        (EditorialAgendaBridge.mark_used_after_successful_generation) is
        responsible for a best-effort compensating release via
        release_active_card().
        """
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE editorial_cards
                SET last_injected_at = ?, use_count = use_count + 1, updated_at = ?, status = ?
                WHERE id = ? AND status = ?
                """,
                (
                    _dt_to_text(injected_at),
                    _dt_to_text(now),
                    EditorialCardStatus.ARMED.value,
                    card_id,
                    EditorialCardStatus.ACTIVE.value,
                ),
            )
        return cur.rowcount > 0

    def release_active_card(self, card_id: str) -> bool:
        """Best-effort compensation: reset a stuck ACTIVE card back to ARMED.

        Used by the bridge when complete_reusable_injection raises and the
        card is left occupying the one-ACTIVE gate. Single conditional
        UPDATE, rowcount-validated; deliberately does not touch
        last_injected_at/use_count since it is unknown whether that write
        landed before the failure. Returns False when the card is missing
        or already left ACTIVE (nothing to release).
        """
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE editorial_cards SET status = ?, updated_at = ? WHERE id = ? AND status = ?",
                (
                    EditorialCardStatus.ARMED.value,
                    _dt_to_text(now),
                    card_id,
                    EditorialCardStatus.ACTIVE.value,
                ),
            )
        return cur.rowcount > 0

    def record_rating(self, rating: EditorialCardRating) -> EditorialCardRating:
        """Persist a post-use rating without storing raw chat context."""

        if self.get(rating.card_id) is None:
            raise EditorialCardValidationError("Cannot rate an unknown editorial card")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO editorial_card_ratings (id, card_id, rating, reason_code, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    rating.id,
                    rating.card_id,
                    rating.rating.value,
                    rating.reason_code,
                    _dt_to_text(rating.created_at),
                ),
            )
        return rating

    def ratings_for_card(self, card_id: str) -> list[EditorialCardRating]:
        """Return ratings recorded for a card in insertion order."""

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM editorial_card_ratings WHERE card_id = ? ORDER BY created_at ASC, id ASC",
                (card_id,),
            ).fetchall()
        return [self._rating_from_row(row) for row in rows]

    def list_all(self) -> list[EditorialCard]:
        """Return all cards ordered by updated_at descending (most recently changed first)."""

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM editorial_cards ORDER BY updated_at DESC, id ASC"
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def list_armed(self) -> list[EditorialCard]:
        """Return all ARMED, non-expired cards."""

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM editorial_cards WHERE status = ?",
                (EditorialCardStatus.ARMED.value,),
            ).fetchall()
        cards = [self._from_row(row) for row in rows]
        return [c for c in cards if not c.is_expired()]

    def rearm(self, card_id: str, *, clear_expiry: bool = False) -> bool:
        """Move a USED or EXPIRED card back to ARMED.

        Returns False if the card is not found or its status is not eligible
        (only USED and EXPIRED cards can be re-armed).

        When clear_expiry is False and the card already has a past expires_at,
        arming it would produce an ARMED-yet-immediately-expired card.  In that
        case return False without changing the card's state.  Use
        clear_expiry=True to null out expires_at and produce a clean ARMED card.
        """
        card = self.get(card_id)
        if card is None:
            return False
        if card.status not in {EditorialCardStatus.USED, EditorialCardStatus.EXPIRED}:
            return False
        # Reject if the existing expiry is in the past and caller did not ask to
        # clear it — the resulting ARMED card would be immediately re-expired.
        if not clear_expiry and card.expires_at is not None and card.is_expired():
            return False
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            if clear_expiry:
                conn.execute(
                    "UPDATE editorial_cards SET status = ?, expires_at = NULL, updated_at = ? WHERE id = ?",
                    (EditorialCardStatus.ARMED.value, _dt_to_text(now), card_id),
                )
            else:
                conn.execute(
                    "UPDATE editorial_cards SET status = ?, updated_at = ? WHERE id = ?",
                    (EditorialCardStatus.ARMED.value, _dt_to_text(now), card_id),
                )
        return True

    def demote_to_draft(self, card_id: str) -> bool:
        """Move an ARMED or ACTIVE card back to DRAFT; True when demoted.

        Agent-surface demotion rule (agent_context_gateway design): upsert
        preserves an existing card's status, so an agent rewriting an ARMED
        card would otherwise keep it auto-firing with unreviewed content.
        The /api/agent/cards handler calls this right after upsert; CLI/UI
        flows deliberately never do (their behavior is unchanged).
        """
        card = self.get(card_id)
        if card is None or card.status not in {
            EditorialCardStatus.ARMED,
            EditorialCardStatus.ACTIVE,
        }:
            return False
        self._set_status(card.id, EditorialCardStatus.DRAFT)
        return True

    def disable(self, card_id: str) -> bool:
        """Move any non-USED card to EXPIRED. Idempotent when already EXPIRED.

        Returns False if the card is not found or is in USED status (history
        must be preserved for used cards).
        """
        card = self.get(card_id)
        if card is None:
            return False
        if card.status is EditorialCardStatus.USED:
            return False
        if card.status is EditorialCardStatus.EXPIRED:
            return True  # Already expired — idempotent
        self._set_status(card.id, EditorialCardStatus.EXPIRED)
        return True

    def delete(self, card_id: str) -> bool:
        """Hard-delete a card and its associated ratings.

        Returns False if the card does not exist.
        """
        card = self.get(card_id)
        if card is None:
            return False
        with self._connect() as conn:
            conn.execute("DELETE FROM editorial_card_ratings WHERE card_id = ?", (card_id,))
            conn.execute("DELETE FROM editorial_cards WHERE id = ?", (card_id,))
        return True

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS editorial_cards (
                    id TEXT PRIMARY KEY,
                    topic_slug TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    streamer_take TEXT NOT NULL,
                    counterpoints_json TEXT NOT NULL,
                    discussion_hooks_json TEXT NOT NULL,
                    triggers_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT,
                    last_used_at TEXT,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    origin TEXT NOT NULL DEFAULT '',
                    single_use INTEGER NOT NULL DEFAULT 0,
                    last_injected_at TEXT
                )
                """
            )
            # Idempotent migration for DBs created before the origin column
            # existed (agent_context_gateway provenance). PRAGMA-guarded so
            # re-running is a no-op — same connection-per-call store.
            columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(editorial_cards)")
            }
            if "origin" not in columns:
                conn.execute(
                    "ALTER TABLE editorial_cards ADD COLUMN origin TEXT NOT NULL DEFAULT ''"
                )
            # Reusable-by-default columns (D5): same PRAGMA-guarded pattern.
            # Existing rows land reusable via the column default (owner intent).
            if "single_use" not in columns:
                conn.execute(
                    "ALTER TABLE editorial_cards ADD COLUMN single_use INTEGER NOT NULL DEFAULT 0"
                )
            if "last_injected_at" not in columns:
                conn.execute(
                    "ALTER TABLE editorial_cards ADD COLUMN last_injected_at TEXT"
                )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS editorial_card_ratings (
                    id TEXT PRIMARY KEY,
                    card_id TEXT NOT NULL,
                    rating TEXT NOT NULL,
                    reason_code TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(card_id) REFERENCES editorial_cards(id)
                )
                """
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _set_status(self, card_id: str, status: EditorialCardStatus) -> None:
        now = datetime.now(timezone.utc)
        with self._connect() as conn:
            conn.execute(
                "UPDATE editorial_cards SET status = ?, updated_at = ? WHERE id = ?",
                (status.value, _dt_to_text(now), card_id),
            )

    @staticmethod
    def _to_row(card: EditorialCard) -> tuple[object, ...]:
        return (
            card.id,
            card.topic_slug,
            card.status.value,
            card.topic,
            card.summary,
            card.streamer_take,
            json.dumps(card.counterpoints, ensure_ascii=False),
            json.dumps(card.discussion_hooks, ensure_ascii=False),
            json.dumps(card.triggers, ensure_ascii=False),
            _dt_to_text(card.created_at),
            _dt_to_text(card.updated_at),
            _dt_to_text(card.expires_at),
            _dt_to_text(card.last_used_at),
            int(card.use_count),
            card.origin,
            int(card.single_use),
            _dt_to_text(card.last_injected_at),
        )

    @staticmethod
    def _from_row(row: sqlite3.Row) -> EditorialCard:
        return EditorialCard(
            id=row["id"],
            topic_slug=row["topic_slug"],
            status=EditorialCardStatus(row["status"]),
            topic=row["topic"],
            summary=row["summary"],
            streamer_take=row["streamer_take"],
            counterpoints=json.loads(row["counterpoints_json"] or "[]"),
            discussion_hooks=json.loads(row["discussion_hooks_json"] or "[]"),
            triggers=json.loads(row["triggers_json"] or "[]"),
            created_at=_dt_from_text(row["created_at"]),
            updated_at=_dt_from_text(row["updated_at"]),
            expires_at=_dt_from_text(row["expires_at"]),
            last_used_at=_dt_from_text(row["last_used_at"]),
            last_injected_at=_dt_from_text(row["last_injected_at"]),
            use_count=int(row["use_count"] or 0),
            origin=row["origin"] or "",
            single_use=bool(row["single_use"]),
        )

    @staticmethod
    def _rating_from_row(row: sqlite3.Row) -> EditorialCardRating:
        return EditorialCardRating(
            id=row["id"],
            card_id=row["card_id"],
            rating=EditorialCardRatingValue(row["rating"]),
            reason_code=row["reason_code"],
            created_at=_dt_from_text(row["created_at"]) or datetime.now(timezone.utc),
        )


def slugify(value: str) -> str:
    """Return a stable ASCII-ish slug for duplicate card detection."""

    text = (value or "").strip().lower()
    replacements = str.maketrans("áéíóúüñ", "aeiouun")
    text = text.translate(replacements)
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")


def normalize_reason_code(value: str) -> str:
    """Return a compact reason code safe for persistence and analytics."""

    text = (value or "").strip().lower().replace(" ", "_")
    return re.sub(r"[^a-z0-9_-]", "", text)


def _dt_to_text(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _dt_from_text(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
