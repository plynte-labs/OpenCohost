"""Agent Notices — short agent-to-operator messages awaiting human review.

External agents (via POST /api/agent/notice) can leave a bounded text note
for the streamer/operator ("digest ready", "source X updated", ...). The
operator reads and dismisses notices; agents can only create them. Notices
never reach Kira, prompts, or persistence beyond this table.

The agent_notices table lives in the SAME SQLite file as editorial_cards
and topic_inbox (settings.EDITORIAL_CARDS_DB) and is created idempotently
alongside them. The store clones the TopicInboxStore shape: connection per
call, bounded sqlite timeouts, write-time AND read-time validation.

Security model
--------------
- All input from agents is untrusted.
- Validation runs at write-time (propose) AND at read-time (list_pending).
- Rows that fail read-time validation land in the 'invalid' bucket and are
  never surfaced — even if an attacker inserts them directly via sqlite3.
- Cap: 20 undismissed notices max (the API maps the cap error to 429).
- Idempotency: an identical (source, text) pair among undismissed rows
  returns the existing row — repeat POSTs are safe, no header needed.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

# Same code/HTML detection as the topic inbox — notices render in an
# operator-facing panel, so the same content rules apply. Single source of
# truth on purpose (topic_inbox.py already warns its patterns are mirrored
# in the UI; do not add a third copy).
from opencohost.core.agenda.topic_inbox import _looks_like_code

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level constants (importable without UI)
# ---------------------------------------------------------------------------

TEXT_MAX: int = 280
SOURCE_MAX: int = 80
UNDISMISSED_CAP: int = 20
# Id namespace: rows outside it are quarantined at read-time (they would
# render but be undismissable) — same rule as topic_inbox's ti_ prefix.
ID_PREFIX: str = "an_"
# Reads may run on the UI thread; bounded waits mirror topic_inbox.
READ_TIMEOUT_SECONDS: float = 0.5
WRITE_TIMEOUT_SECONDS: float = 1.0


# ---------------------------------------------------------------------------
# Exception classes
# ---------------------------------------------------------------------------

class AgentNoticeValidationError(ValueError):
    """Raised when a proposed notice violates safety or schema rules."""


class AgentNoticeCapError(ValueError):
    """Raised when the undismissed notice board is full (UNDISMISSED_CAP)."""


# ---------------------------------------------------------------------------
# AgentNoticeStore
# ---------------------------------------------------------------------------

class AgentNoticeStore:
    """SQLite-backed store for agent notices (TopicInboxStore shape)."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def propose(self, text: str, source: str) -> dict:
        """Validate and insert a notice, deduping identical (source, text).

        Raises AgentNoticeValidationError on any validation failure.
        Raises AgentNoticeCapError when UNDISMISSED_CAP is reached.
        Returns the stored row as a dict.
        """
        error = self._validate_row(text, source)
        if error:
            raise AgentNoticeValidationError(error)

        with closing(self._connect()) as conn, conn:
            self._ensure_table(conn)

            # Dedupe BEFORE the cap check: repeating an existing notice must
            # stay idempotent even when the board is full.
            existing = conn.execute(
                "SELECT * FROM agent_notices WHERE status='proposed' AND source=? AND text=?",
                (source, text),
            ).fetchone()
            if existing is not None:
                return dict(existing)

            undismissed = conn.execute(
                "SELECT COUNT(*) FROM agent_notices WHERE status='proposed'"
            ).fetchone()[0]
            if undismissed >= UNDISMISSED_CAP:
                raise AgentNoticeCapError(
                    f"Notice board is full: {UNDISMISSED_CAP} undismissed notices "
                    "already exist. The operator must dismiss some before adding more."
                )

            new_id = ID_PREFIX + uuid4().hex
            now = _utc_now()
            conn.execute(
                """INSERT INTO agent_notices
                   (id, text, source, status, created_at, updated_at)
                   VALUES (?, ?, ?, 'proposed', ?, ?)""",
                (new_id, text, source, now, now),
            )
            row = conn.execute(
                "SELECT * FROM agent_notices WHERE id=?", (new_id,)
            ).fetchone()
            return dict(row)

    def list_pending(self) -> dict:
        """Return undismissed rows split into valid/invalid buckets.

        Re-validates every row at read-time (defense against direct DB
        writes). Returns {'valid': [...], 'invalid': [...]} — never raises
        (fail-open), mirroring TopicInboxStore.list_pending.
        """
        try:
            with closing(self._connect(timeout=READ_TIMEOUT_SECONDS)) as conn, conn:
                self._ensure_table(conn)
                rows = conn.execute(
                    "SELECT * FROM agent_notices WHERE status='proposed' ORDER BY created_at ASC"
                ).fetchall()
        except Exception as exc:
            logger.warning("agent_notices list_pending error (fail-open): %s", exc)
            return {"valid": [], "invalid": []}

        valid: list[dict] = []
        invalid: list[dict] = []

        for row in rows:
            d = dict(row)
            if not _is_valid_notice_id(d.get("id")):
                d["invalid_reason"] = f"id outside the {ID_PREFIX} namespace"
                invalid.append(d)
                continue
            try:
                error = self._validate_row(
                    d["text"],
                    d.get("source"),
                    d.get("created_at"),
                    d.get("updated_at"),
                )
            except Exception as exc:
                # Defense-in-depth: a validator crash on a hostile row must
                # bucket the row as invalid, never break the caller.
                error = f"validation crashed: {exc}"
            if error:
                d["invalid_reason"] = error
                invalid.append(d)
            else:
                valid.append(d)

        return {"valid": valid, "invalid": invalid}

    def list_all(self, status_filter: str | None = None) -> list[dict]:
        """Return all rows, optionally filtered by status. No read-time validation (audit use)."""
        with closing(self._connect()) as conn, conn:
            self._ensure_table(conn)
            if status_filter is not None:
                rows = conn.execute(
                    "SELECT * FROM agent_notices WHERE status=? ORDER BY created_at ASC",
                    (status_filter,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM agent_notices ORDER BY created_at ASC"
                ).fetchall()
        return [dict(row) for row in rows]

    def dismiss(self, id: str) -> bool:
        """Set status='dismissed' for a proposed row. True if 1 row updated."""
        now = _utc_now()
        with closing(self._connect(timeout=WRITE_TIMEOUT_SECONDS)) as conn, conn:
            self._ensure_table(conn)
            cursor = conn.execute(
                "UPDATE agent_notices SET status='dismissed', updated_at=? "
                "WHERE id=? AND status='proposed'",
                (now, id),
            )
            return cursor.rowcount == 1

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _ensure_table(self, conn: sqlite3.Connection) -> None:
        """Create the agent_notices table if it does not exist (idempotent)."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agent_notices (
                id TEXT PRIMARY KEY,
                text TEXT NOT NULL,
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

    def _validate_row(
        self,
        text: object,
        source: object = "",
        created_at: object = "",
        updated_at: object = "",
    ) -> str | None:
        """Return an error string if invalid, None if valid.

        Type checks matter at read-time: SQLite stores any type in TEXT
        columns, so a direct INSERT can put BLOBs where strings belong.
        Timestamps included: the API renders created_at straight into a
        response model, so one hostile BLOB would break the whole board.
        """
        for name, value in (("created_at", created_at), ("updated_at", updated_at)):
            if not isinstance(value, str):
                return f"{name} must be a string"
        if source is not None and not isinstance(source, str):
            return "source must be a string"
        if source and len(source) > SOURCE_MAX:
            return f"source exceeds {SOURCE_MAX} characters"
        if not isinstance(text, str) or not text.strip():
            return "text must be a non-empty string"
        if len(text) > TEXT_MAX:
            return f"text exceeds {TEXT_MAX} characters"
        if _looks_like_code(text):
            return "text contains code or HTML"
        return None


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

def _is_valid_notice_id(value: object) -> bool:
    """True for ids inside the an_ namespace with a non-empty suffix."""
    return isinstance(value, str) and value.startswith(ID_PREFIX) and len(value) > len(ID_PREFIX)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
