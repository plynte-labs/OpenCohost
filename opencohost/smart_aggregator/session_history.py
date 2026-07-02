import sqlite3
import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Optional, List, Dict, Any

logger = logging.getLogger(__name__)

# D2 — persisted-payload redaction (privacy_prereq_fixes_20260701). These
# allowlists gate what add_context_snapshot is allowed to write to disk.
# Anything not listed here is dropped fail-closed. The LIVE prompt built by
# IntentAggregator.to_prompt is untouched — this is a persistence-only
# boundary.
_TOP_INTENT_ALLOWED_KEYS = frozenset({"intent", "label", "count", "duplicates"})
_TRIGGER_ALLOWED_KEYS = frozenset({"rate", "threshold", "window_seconds"})
_METADATA_SCHEMA_VERSION = 1  # PRAGMA user_version marker for the one-time legacy purge
_REFERENCIAS_DETECTADAS_RE = re.compile(r"\s?Referencias detectadas:[^.\n]*\.")


def _sanitize_summary(summary: str) -> str:
    """Strip the "Referencias detectadas: ..." clause from the persisted copy.

    That clause embeds extracted entities verbatim (see
    IntentAggregator.to_prompt). The live prompt sent to Kira is unaffected.
    """
    return _REFERENCIAS_DETECTADAS_RE.sub("", summary)


def _is_scalar(value: Any) -> bool:
    """True for JSON scalar leaf values (allowlisted keys must not smuggle nested data)."""
    return isinstance(value, (str, int, float, bool)) or value is None


def _sanitize_metadata(metadata: Dict[str, Any]) -> Dict[str, Any]:
    """Whole-payload allowlist for persisted snapshot metadata (fail-closed).

    Drops verbatim viewer text (top_intents[].examples/entities) and operator
    config (trigger.actions). Unknown top-level keys are dropped. Under an
    allowed key, only scalar values survive — a nested dict/list smuggled in
    under an allowed key name is dropped too.
    """
    sanitized: Dict[str, Any] = {}
    for key, value in metadata.items():
        if key == "top_intents" and isinstance(value, list):
            sanitized["top_intents"] = [
                {
                    k: v
                    for k, v in item.items()
                    if k in _TOP_INTENT_ALLOWED_KEYS and _is_scalar(v)
                }
                for item in value
                if isinstance(item, dict)
            ]
        elif key == "trigger" and isinstance(value, dict):
            sanitized["trigger"] = {
                k: v
                for k, v in value.items()
                if k in _TRIGGER_ALLOWED_KEYS and _is_scalar(v)
            }
        else:
            logger.debug("Dropping unknown snapshot metadata key: %s", key)
    return sanitized


class SessionHistory:
    def __init__(self, db_path: str, jsonl_path: str, retention_hours: int):
        self.db_path = db_path
        self.jsonl_path = jsonl_path
        self.retention_hours = retention_hours

        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        os.makedirs(os.path.dirname(jsonl_path), exist_ok=True)

        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(self.db_path)
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS sessions (
                    id INTEGER PRIMARY KEY,
                    platform TEXT,
                    channel TEXT,
                    start_time REAL,
                    end_time REAL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS context_snapshots (
                    id INTEGER PRIMARY KEY,
                    session_id INTEGER,
                    summary TEXT,
                    timestamp REAL,
                    message_count INTEGER,
                    vibe_temperature REAL,
                    metadata_json TEXT,
                    FOREIGN KEY (session_id) REFERENCES sessions(id)
                )
            """)
            conn.commit()
            self._purge_legacy_data(conn)
        finally:
            conn.close()

    def _purge_legacy_data(self, conn: sqlite3.Connection) -> None:
        """One-time purge of pre-redaction liability data (D2).

        Guarded by PRAGMA user_version on THIS db file (sessions.db) so it
        runs exactly once. Deletes context_snapshots rows persisted before
        the whole-payload redaction fix (they may carry verbatim viewer chat
        via top_intents.examples/entities or "Referencias detectadas" in
        summary), drops the pre-refactor raw `messages` table if present,
        and unlinks the legacy chat_log.jsonl. Mirrors
        scripts/cleanup_smart_aggregator_db.py. No-op on fresh installs.
        """
        cursor = conn.cursor()
        version = cursor.execute("PRAGMA user_version").fetchone()[0]
        if version >= _METADATA_SCHEMA_VERSION:
            return
        cursor.execute("DELETE FROM context_snapshots")
        cursor.execute("DROP TABLE IF EXISTS messages")
        conn.commit()
        try:
            Path(self.jsonl_path).unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "legacy chat_log jsonl could not be removed; purge will retry next startup"
            )
            return
        cursor.execute(f"PRAGMA user_version = {_METADATA_SCHEMA_VERSION}")
        conn.commit()
    
    def start_session(self, platform: str, channel: str) -> int:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO sessions (platform, channel, start_time, end_time) VALUES (?, ?, ?, ?)",
            (platform, channel, time.time(), None)
        )
        session_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return session_id
    
    def end_session(self, session_id: int):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE sessions SET end_time = ? WHERE id = ?",
            (time.time(), session_id)
        )
        conn.commit()
        conn.close()
    
    def add_context_snapshot(
        self,
        session_id: int,
        summary: str,
        message_count: int = 0,
        vibe: Optional[float] = None,
        metadata: Optional[Dict[str, Any]] = None,
        timestamp: Optional[float] = None,
    ):
        """Persist the compact context actually sent to Kira.

        This is the only production history path. Raw chat persistence is
        intentionally prohibited: chat is filtered, aggregated in memory, and
        persisted only as compact context snapshots.
        """
        summary = (summary or "").strip()
        if not summary:
            return
        summary = _sanitize_summary(summary)
        timestamp = timestamp if timestamp is not None else time.time()
        metadata_json = json.dumps(_sanitize_metadata(metadata or {}), ensure_ascii=False)

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO context_snapshots
                (session_id, summary, timestamp, message_count, vibe_temperature, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (session_id, summary, timestamp, int(message_count or 0), vibe, metadata_json),
        )
        conn.commit()
        conn.close()
    
    def get_session_context(self, session_id: int, max_messages: int) -> List[Dict[str, Any]]:
        """Legacy compatibility shim: raw chat retrieval is disabled."""
        return []

    def get_recent_context_snapshots(self, session_id: int, max_items: int) -> List[Dict[str, Any]]:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT summary, timestamp, message_count, vibe_temperature, metadata_json
            FROM context_snapshots
            WHERE session_id = ?
            ORDER BY timestamp DESC
            LIMIT ?
            """,
            (session_id, max_items),
        )
        rows = cursor.fetchall()
        conn.close()

        result = []
        for row in reversed(rows):
            try:
                metadata = json.loads(row[4] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            result.append({
                "summary": row[0],
                "timestamp": row[1],
                "message_count": row[2],
                "vibe_temperature": row[3],
                "metadata": metadata,
            })
        return result
    
    def cleanup_old_sessions(self):
        cutoff = time.time() - (self.retention_hours * 3600)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM sessions WHERE start_time < ?", (cutoff,))
        old_sessions = [row[0] for row in cursor.fetchall()]
        
        for sid in old_sessions:
            cursor.execute("DELETE FROM context_snapshots WHERE session_id = ?", (sid,))
            if self._table_exists(cursor, "messages"):
                cursor.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            cursor.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        
        conn.commit()
        conn.close()
        
        if old_sessions and os.path.exists(self.jsonl_path):
            self._cleanup_jsonl(old_sessions)

    @staticmethod
    def _table_exists(cursor: sqlite3.Cursor, table_name: str) -> bool:
        cursor.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        )
        return cursor.fetchone() is not None
    
    def _cleanup_jsonl(self, old_sessions: List[int]):
        if not os.path.exists(self.jsonl_path):
            return
        old_session_ids = set(old_sessions)
        temp_path = self.jsonl_path + ".tmp"
        with open(self.jsonl_path, "r", encoding="utf-8") as fin:
            with open(temp_path, "w", encoding="utf-8") as fout:
                for line in fin:
                    try:
                        record = json.loads(line)
                        if record.get("session_id") not in old_session_ids:
                            fout.write(json.dumps(record, ensure_ascii=False) + "\n")
                    except json.JSONDecodeError:
                        continue
        os.replace(temp_path, self.jsonl_path)
