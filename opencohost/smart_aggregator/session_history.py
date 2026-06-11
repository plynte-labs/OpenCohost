import sqlite3
import json
import os
import time
from typing import Optional, List, Dict, Any

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
        conn.close()
    
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
        timestamp = timestamp if timestamp is not None else time.time()
        metadata_json = json.dumps(metadata or {}, ensure_ascii=False)

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
