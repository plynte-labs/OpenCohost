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
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY,
                session_id INTEGER,
                user TEXT,
                text TEXT,
                timestamp REAL,
                passed_filter INTEGER,
                vibe_temperature REAL,
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
    
    def add_message(self, session_id: int, message: dict, passed_filter: bool, vibe: float):
        timestamp = message.get("timestamp", time.time())
        user = message.get("user", "")
        text = message.get("text", "")
        
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO messages (session_id, user, text, timestamp, passed_filter, vibe_temperature) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, user, text, timestamp, 1 if passed_filter else 0, vibe)
        )
        conn.commit()
        conn.close()
        
        record = {
            "session_id": session_id,
            "user": user,
            "text": text,
            "timestamp": timestamp,
            "passed_filter": passed_filter,
            "vibe_temperature": vibe
        }
        with open(self.jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    
    def get_session_context(self, session_id: int, max_messages: int) -> List[Dict[str, Any]]:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute(
            "SELECT user, text, timestamp, passed_filter, vibe_temperature FROM messages WHERE session_id = ? ORDER BY timestamp DESC LIMIT ?",
            (session_id, max_messages)
        )
        rows = cursor.fetchall()
        conn.close()
        
        result = []
        for row in reversed(rows):
            result.append({
                "user": row[0],
                "text": row[1],
                "timestamp": row[2],
                "passed_filter": bool(row[3]),
                "vibe_temperature": row[4]
            })
        return result
    
    def cleanup_old_sessions(self):
        cutoff = time.time() - (self.retention_hours * 3600)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT id FROM sessions WHERE start_time < ?", (cutoff,))
        old_sessions = [row[0] for row in cursor.fetchall()]
        
        for sid in old_sessions:
            cursor.execute("DELETE FROM messages WHERE session_id = ?", (sid,))
            cursor.execute("DELETE FROM sessions WHERE id = ?", (sid,))
        
        conn.commit()
        conn.close()
        
        if old_sessions and os.path.exists(self.jsonl_path):
            self._cleanup_jsonl(old_sessions)
    
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
