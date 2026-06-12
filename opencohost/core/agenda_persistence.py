"""Agenda persistence — queued/approved topics survive app restarts.

Owner-approved design (engram sdd/agenda-persistence/design):
  - Persist topic DEFINITIONS only (title, angle, constraints, priority,
    response_length, position, editorial card link + consumed flag) plus the
    session settings. Runtime counters (turns_spoken, state machine) never
    hit disk; an ACTIVE topic is saved as queued and re-hosted fresh.
  - Write-through on real change: the full persistable set is written in one
    transaction, preceded by a cheap fingerprint comparison so unchanged
    state never touches disk.
  - Fail-open everywhere: a failed write logs and warns the operator ONCE
    ("persistence degraded"); a failed load starts with an empty agenda.
    Persistence must never break the stream or block the launch.
  - The agenda switch is NOT persisted: after any restart the operator must
    explicitly re-enable Kira (broadcast safety).

The agenda_topics table lives in the same SQLite file as editorial cards and
the topic inbox (settings.EDITORIAL_CARDS_DB). Schema is versioned via the
agenda_meta table (file-wide PRAGMA user_version is avoided on purpose — the
DB file is shared with other subsystems).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

SCHEMA_VERSION: int = 1
# Safety bound for pathological DBs; the UI should never ingest more rows.
RESTORE_CAP: int = 50
# Both paths run on the Tk main thread; never wait for a writer lock longer
# than these (sqlite default is 5s — a visible freeze).
READ_TIMEOUT_SECONDS: float = 0.5
WRITE_TIMEOUT_SECONDS: float = 1.0

# Statuses worth surviving a restart. ACTIVE is demoted to queued on save:
# the runtime context (state machine, Kira's conversational memory) dies
# with the process, so re-hosting fresh is the only honest restore.
_PERSISTED_STATUSES = ("approved", "queued", "active")


class AgendaPersistence:
    """SQLite-backed snapshot store for the agenda queue and settings."""

    def __init__(self, db_path: str | Path, log_fn: Callable[[str], None] | None = None) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._log = log_fn or (lambda message: None)
        self._last_fingerprint: str | None = None
        self._write_failure_warned = False

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save_if_changed(self, controller: Any) -> bool:
        """Persist the controller's pending topics + settings when they changed.

        Returns True only when a write actually happened. Fail-open: any
        storage error logs, warns the operator once, and returns False —
        the in-memory agenda stays the source of truth for the session.
        """
        payload = self._snapshot(controller)
        fingerprint = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        if fingerprint == self._last_fingerprint:
            return False

        try:
            with closing(self._connect(timeout=WRITE_TIMEOUT_SECONDS)) as conn, conn:
                self._ensure_schema(conn)
                conn.execute("DELETE FROM agenda_topics")
                conn.executemany(
                    "INSERT INTO agenda_topics "
                    "(position, title, angle, constraints, priority, response_length, status, editorial_card_id, editorial_card_consumed) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            row["position"],
                            row["title"],
                            row["angle"],
                            json.dumps(row["constraints"], ensure_ascii=False),
                            row["priority"],
                            row["response_length"],
                            row["status"],
                            row["editorial_card_id"],
                            1 if row["editorial_card_consumed"] else 0,
                        )
                        for row in payload["topics"]
                    ],
                )
                conn.execute("DELETE FROM agenda_settings")
                conn.execute(
                    "INSERT INTO agenda_settings (id, max_turns_per_topic, rhythm, response_length, safety_mode) "
                    "VALUES (1, ?, ?, ?, ?)",
                    (
                        payload["settings"]["max_turns_per_topic"],
                        payload["settings"]["rhythm"],
                        payload["settings"]["response_length"],
                        payload["settings"]["safety_mode"],
                    ),
                )
        except Exception as exc:
            logger.warning("agenda persistence save failed (fail-open): %s", exc)
            if not self._write_failure_warned:
                self._write_failure_warned = True
                self._log(
                    "[Kira Agenda] Persistencia degradada: no se pudo guardar la cola "
                    f"({exc}). La agenda sigue funcionando, pero no sobrevivirá un reinicio."
                )
            return False

        self._last_fingerprint = fingerprint
        return True

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def load_into(self, controller: Any) -> int:
        """Restore persisted topics + settings into a fresh controller.

        Returns the number of topics restored. Each row passes through the
        controller's own sanitizer (add_topic); invalid rows are skipped with
        a log. The agenda switch state is never touched. Fail-open: any
        storage error returns 0 and the app launches with an empty agenda.
        """
        try:
            with closing(self._connect(timeout=READ_TIMEOUT_SECONDS)) as conn, conn:
                self._ensure_schema(conn)
                settings_row = conn.execute(
                    "SELECT max_turns_per_topic, rhythm, response_length, safety_mode FROM agenda_settings WHERE id=1"
                ).fetchone()
                rows = conn.execute(
                    "SELECT * FROM agenda_topics WHERE status IN ('approved', 'queued') "
                    "ORDER BY position ASC LIMIT ?",
                    (RESTORE_CAP,),
                ).fetchall()
        except Exception as exc:
            logger.warning("agenda persistence load failed (fail-open): %s", exc)
            return 0

        if settings_row is not None:
            controller.set_session_settings(
                max_turns_per_topic=settings_row["max_turns_per_topic"],
                rhythm=settings_row["rhythm"],
                response_length=settings_row["response_length"],
                safety_mode=settings_row["safety_mode"],
            )

        restored = 0
        for row in rows:
            try:
                constraints = json.loads(row["constraints"] or "[]")
                if not isinstance(constraints, list):
                    constraints = []
                topic = controller.add_topic(
                    str(row["title"]),
                    str(row["angle"] or ""),
                    [str(c) for c in constraints],
                    approved=True,
                    priority=str(row["priority"] or "normal"),
                    response_length=str(row["response_length"] or "normal"),
                    editorial_card_id=row["editorial_card_id"],
                )
                if row["status"] == "queued":
                    controller.queue_topic(topic.id)
                topic.editorial_card_consumed = bool(row["editorial_card_consumed"])
                restored += 1
            except (ValueError, KeyError, TypeError) as exc:
                logger.warning("agenda persistence skipped an invalid row (fail-open): %s", exc)

        # Disk already mirrors what we just loaded — avoid an immediate
        # no-op rewrite on the first status tick after startup.
        self._last_fingerprint = json.dumps(
            self._snapshot(controller), sort_keys=True, ensure_ascii=False
        )
        return restored

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _snapshot(self, controller: Any) -> dict:
        """Serialize the persistable slice of the controller's state."""
        topics = []
        for position, topic in enumerate(controller.topics):
            status = topic.status.value if hasattr(topic.status, "value") else str(topic.status)
            if status not in _PERSISTED_STATUSES:
                continue
            topics.append(
                {
                    "position": position,
                    "title": topic.title,
                    "angle": topic.angle,
                    "constraints": list(topic.constraints),
                    "priority": topic.priority,
                    "response_length": topic.response_length,
                    # ACTIVE demotes to queued: re-host fresh after a restart.
                    "status": "queued" if status == "active" else status,
                    "editorial_card_id": topic.editorial_card_id,
                    "editorial_card_consumed": bool(topic.editorial_card_consumed),
                }
            )
        return {
            "topics": topics,
            "settings": {
                "max_turns_per_topic": controller.max_turns_per_topic,
                "rhythm": controller.rhythm,
                "response_length": controller.response_length,
                "safety_mode": controller.safety_mode,
            },
        }

    def _connect(self, timeout: float) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=timeout)
        conn.row_factory = sqlite3.Row
        return conn

    def _ensure_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agenda_topics (
                position INTEGER NOT NULL,
                title TEXT NOT NULL,
                angle TEXT NOT NULL DEFAULT '',
                constraints TEXT NOT NULL DEFAULT '[]',
                priority TEXT NOT NULL DEFAULT 'normal',
                response_length TEXT NOT NULL DEFAULT 'normal',
                status TEXT NOT NULL DEFAULT 'queued',
                editorial_card_id TEXT,
                editorial_card_consumed INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS agenda_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                max_turns_per_topic INTEGER NOT NULL,
                rhythm TEXT NOT NULL,
                response_length TEXT NOT NULL,
                safety_mode TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS agenda_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT OR IGNORE INTO agenda_meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
