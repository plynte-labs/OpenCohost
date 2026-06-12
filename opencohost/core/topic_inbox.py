"""Topic Inbox — untrusted agent proposals awaiting human approval.

External agents (research bots, watch-bots, etc.) can propose stream topics
by writing to this inbox.  A human operator approves or discards proposals
via the app UI.  Approval is NEVER available via CLI (human-only gate).

The topic_inbox table lives in the SAME SQLite file as editorial_cards
(settings.EDITORIAL_CARDS_DB) and is created idempotently alongside it.

Security model
--------------
- All input from agents is untrusted.
- Validation runs at write-time (propose) AND at read-time (list_pending).
- Rows that fail read-time validation land in the 'invalid' bucket and are
  never surfaced as approvable — even if an attacker inserts them directly
  via sqlite3.
- Approval is always human-only via the app UI.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants (importable without UI)
# ---------------------------------------------------------------------------

TITLE_MAX: int = 120
ANGLE_MAX: int = 600
TAGS_MAX: int = 8
TAG_MAX_CHARS: int = 40
PENDING_CAP: int = 30
# Id namespace: the UI routes approve/reject by this prefix, so rows outside
# it are quarantined at read-time (they would render but be undismissable).
ID_PREFIX: str = "ti_"
# list_pending runs on the UI thread; never wait for a writer lock longer
# than this (sqlite default is 5s — a visible freeze).
READ_TIMEOUT_SECONDS: float = 0.5

# ---------------------------------------------------------------------------
# Code/HTML detection patterns
# NOTE: these patterns are mirrored in opencohost/ui/cohost_agenda_panel.py (BULK_CODE_PATTERNS). Keep in sync manually.
# ---------------------------------------------------------------------------

INBOX_CODE_PATTERNS = (
    r"```",
    r"<\/?[a-z][^>]*>",
    r"\b(function|class|import|from|select|insert|update|delete|drop|script|console\.log)\b",
    r"=>",
)


# ---------------------------------------------------------------------------
# Exception classes
# ---------------------------------------------------------------------------

class TopicInboxValidationError(ValueError):
    """Raised when a proposed topic violates safety or schema rules."""


class TopicInboxCapError(ValueError):
    """Raised when the pending inbox is full (PENDING_CAP reached)."""


# ---------------------------------------------------------------------------
# TopicInboxStore
# ---------------------------------------------------------------------------

class TopicInboxStore:
    """SQLite-backed store for topic inbox proposals.

    Uses the connection-per-call pattern (same as EditorialCardStore).
    The topic_inbox table is created idempotently inside the same DB file
    as editorial_cards.
    """

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def propose(
        self,
        title: str,
        angle: str,
        tags: list[str],
        source: str,
    ) -> dict:
        """Validate and insert (or dedupe-upsert) a topic proposal.

        Raises TopicInboxValidationError on any validation failure.
        Raises TopicInboxCapError when the pending queue is already full.
        Returns the stored row as a dict.
        """
        error = self._validate_row(title, angle, tags)
        if error:
            raise TopicInboxValidationError(error)

        slug = self._slug(title)

        with self._connect() as conn:
            self._ensure_table(conn)

            # Dedupe by normalized slug against every proposed row
            deduped_id: str | None = None
            all_proposed = conn.execute(
                "SELECT id, title FROM topic_inbox WHERE status='proposed'"
            ).fetchall()
            for row_id, row_title in all_proposed:
                if self._slug(row_title) == slug:
                    deduped_id = row_id
                    break

            now = _utc_now()

            if deduped_id is not None:
                # Upsert: update existing row
                conn.execute(
                    """UPDATE topic_inbox
                       SET title=?, angle=?, tags=?, source=?, updated_at=?
                       WHERE id=?""",
                    (title, angle, json.dumps(tags, ensure_ascii=False), source, now, deduped_id),
                )
                row = conn.execute(
                    "SELECT * FROM topic_inbox WHERE id=?", (deduped_id,)
                ).fetchone()
                return _row_to_dict(row)

            # Cap check before insert
            pending_count = conn.execute(
                "SELECT COUNT(*) FROM topic_inbox WHERE status='proposed'"
            ).fetchone()[0]
            if pending_count >= PENDING_CAP:
                raise TopicInboxCapError(
                    f"Inbox is full: {PENDING_CAP} proposed rows already exist. "
                    "Approve or discard some before adding more."
                )

            new_id = ID_PREFIX + uuid4().hex
            conn.execute(
                """INSERT INTO topic_inbox
                   (id, title, angle, tags, source, status, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, 'proposed', ?, ?)""",
                (new_id, title, angle, json.dumps(tags, ensure_ascii=False), source, now, now),
            )
            row = conn.execute(
                "SELECT * FROM topic_inbox WHERE id=?", (new_id,)
            ).fetchone()
            return _row_to_dict(row)

    def list_pending(self) -> dict:
        """Return proposed rows split into valid/invalid buckets.

        Re-validates every row at read-time (defense against direct DB writes).
        Returns {'valid': [...], 'invalid': [...]} — never raises (fail-open).
        """
        try:
            with self._connect(timeout=READ_TIMEOUT_SECONDS) as conn:
                self._ensure_table(conn)
                rows = conn.execute(
                    "SELECT * FROM topic_inbox WHERE status='proposed' ORDER BY created_at ASC"
                ).fetchall()
        except Exception as exc:
            logger.warning("topic_inbox list_pending error (fail-open): %s", exc)
            return {"valid": [], "invalid": []}

        valid: list[dict] = []
        invalid: list[dict] = []

        for row in rows:
            d = _row_to_dict(row)
            if not str(d.get("id") or "").startswith(ID_PREFIX):
                d["invalid_reason"] = f"id outside the {ID_PREFIX} namespace"
                invalid.append(d)
                continue
            try:
                error = self._validate_row(d["title"], d["angle"], d["tags"])
            except Exception as exc:
                # Defense-in-depth: a validator crash on a hostile row must
                # bucket the row as invalid, never break the polling caller.
                error = f"validation crashed: {exc}"
            if error:
                d["invalid_reason"] = error
                invalid.append(d)
            else:
                valid.append(d)

        return {"valid": valid, "invalid": invalid}

    def list_all(self, status_filter: str | None = None) -> list[dict]:
        """Return all rows, optionally filtered by status. No read-time validation (audit use)."""
        with self._connect() as conn:
            self._ensure_table(conn)
            if status_filter is not None:
                rows = conn.execute(
                    "SELECT * FROM topic_inbox WHERE status=? ORDER BY created_at ASC",
                    (status_filter,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM topic_inbox ORDER BY created_at ASC"
                ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def approve(self, id: str) -> bool:
        """Set status='approved' for a proposed row. Returns True if 1 row updated."""
        now = _utc_now()
        with self._connect() as conn:
            self._ensure_table(conn)
            cursor = conn.execute(
                "UPDATE topic_inbox SET status='approved', updated_at=? WHERE id=? AND status='proposed'",
                (now, id),
            )
            return cursor.rowcount == 1

    def discard(self, id: str) -> bool:
        """Set status='discarded' for a proposed row. Returns True if 1 row updated."""
        now = _utc_now()
        with self._connect() as conn:
            self._ensure_table(conn)
            cursor = conn.execute(
                "UPDATE topic_inbox SET status='discarded', updated_at=? WHERE id=? AND status='proposed'",
                (now, id),
            )
            return cursor.rowcount == 1

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _ensure_table(self, conn: sqlite3.Connection) -> None:
        """Create the topic_inbox table if it does not exist (idempotent)."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_inbox (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                angle TEXT NOT NULL,
                tags TEXT NOT NULL DEFAULT '[]',
                source TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'proposed',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

    def _connect(self, timeout: float = 5.0) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=timeout)
        conn.row_factory = sqlite3.Row
        return conn

    def _slug(self, title: str) -> str:
        """Normalize title to a stable slug for duplicate detection.

        Standalone implementation — does NOT import from editorial_matching.
        Algorithm: strip accents (NFD + drop Mn category), strip chars >=0x2600
        (emoji/symbols), lowercase, keep only [a-z0-9 ], collapse spaces.
        """
        text = (title or "").strip()
        # NFD decompose then drop combining accent marks (Mn = Mark, Nonspacing)
        text = unicodedata.normalize("NFD", text)
        text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
        # Drop emoji and symbol characters (code points >= 0x2600)
        text = "".join(ch for ch in text if ord(ch) < 0x2600)
        text = text.lower()
        # Keep only alphanumeric and space
        text = re.sub(r"[^a-z0-9 ]", " ", text)
        # Collapse whitespace
        text = " ".join(text.split())
        return text

    def _validate_row(self, title: str, angle: str, tags: list) -> str | None:
        """Return an error string if invalid, None if valid.

        Checks: types, emptiness, length limits, code/HTML patterns.
        Type checks matter at read-time: SQLite stores any type in TEXT
        columns, so a direct INSERT can put integers where strings belong.
        """
        # Title checks
        if not isinstance(title, str) or not title.strip():
            return "title must be a non-empty string"
        if len(title) > TITLE_MAX:
            return f"title exceeds {TITLE_MAX} characters"
        if _looks_like_code(title):
            return "title contains code or HTML"

        # Angle checks
        if not isinstance(angle, str):
            return "angle must be a string"
        if len(angle) > ANGLE_MAX:
            return f"angle exceeds {ANGLE_MAX} characters"
        if _looks_like_code(angle):
            return "angle contains code or HTML"

        # Tag checks
        if not isinstance(tags, list):
            return "tags must be a list"
        if len(tags) > TAGS_MAX:
            return f"too many tags (max {TAGS_MAX})"
        for tag in tags:
            if not isinstance(tag, str):
                return "tags must be strings"
            if len(tag) > TAG_MAX_CHARS:
                return f"tag '{tag[:20]}...' exceeds {TAG_MAX_CHARS} characters"

        return None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _looks_like_code(text: str) -> bool:
    """Return True if text matches any code/HTML detection pattern."""
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in INBOX_CODE_PATTERNS)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: sqlite3.Row) -> dict:
    """Convert a sqlite3.Row to a plain dict with tags as a list."""
    d = dict(row)
    # Deserialize tags JSON
    try:
        d["tags"] = json.loads(d.get("tags") or "[]")
    except (json.JSONDecodeError, TypeError):
        d["tags"] = []
    return d
