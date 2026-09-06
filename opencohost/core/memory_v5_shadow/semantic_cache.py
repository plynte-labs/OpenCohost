"""
Derived, rebuildable SQLite semantic vector cache for Memory v5.

Stores float32 L2-normalized vector embeddings for ConversationalExchanges and Episodes.
100% rebuildable from authoritative Evidence Journal + episode_membership.
Enforces hard profile isolation and cascades privacy operations (purge/forget).
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np


@dataclass(frozen=True)
class ExchangeEmbeddingRecord:
    exchange_key: str
    profile_id: str
    session_id: str
    episode_id: str
    content_hash: str
    model_id: str
    model_version: str
    dimensions: int
    vector: list[float]
    created_at: str
    lexical_tokens: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "exchange_key": self.exchange_key,
            "profile_id": self.profile_id,
            "session_id": self.session_id,
            "episode_id": self.episode_id,
            "content_hash": self.content_hash,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "dimensions": self.dimensions,
            "created_at": self.created_at,
            "lexical_tokens": self.lexical_tokens,
        }


@dataclass(frozen=True)
class EpisodeEmbeddingRecord:
    episode_id: str
    profile_id: str
    model_id: str
    model_version: str
    vector: list[float]
    cohesion_mean: float
    cohesion_min: float
    content_fingerprint: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "profile_id": self.profile_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "cohesion_mean": self.cohesion_mean,
            "cohesion_min": self.cohesion_min,
            "content_fingerprint": self.content_fingerprint,
            "created_at": self.created_at,
        }


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS exchange_embeddings (
    exchange_key TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    episode_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    dimensions INTEGER NOT NULL,
    vector_blob BLOB NOT NULL,
    created_at TEXT NOT NULL,
    lexical_tokens TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ex_profile ON exchange_embeddings(profile_id);
CREATE INDEX IF NOT EXISTS idx_ex_episode ON exchange_embeddings(episode_id);

CREATE TABLE IF NOT EXISTS episode_embeddings (
    episode_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    model_id TEXT NOT NULL,
    model_version TEXT NOT NULL,
    vector_blob BLOB NOT NULL,
    cohesion_mean REAL NOT NULL,
    cohesion_min REAL NOT NULL,
    content_fingerprint TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ep_profile ON episode_embeddings(profile_id);

CREATE TABLE IF NOT EXISTS profile_privacy_fences (
    profile_id TEXT PRIMARY KEY,
    purged_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS global_privacy_fence (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    forgotten_at TEXT NOT NULL
);
"""


class SemanticCacheStore:
    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._conn: Optional[sqlite3.Connection] = None
        self._lock = threading.Lock()

    def initialize(self) -> None:
        with self._lock:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
            self._conn.executescript(_SCHEMA_SQL)
            # Migration check: add lexical_tokens if missing from existing db
            try:
                cols = [r["name"] for r in self._conn.execute("PRAGMA table_info(exchange_embeddings)").fetchall()]
                if "lexical_tokens" not in cols:
                    self._conn.execute("ALTER TABLE exchange_embeddings ADD COLUMN lexical_tokens TEXT NOT NULL DEFAULT ''")
            except Exception:
                pass
            self._conn.commit()

    def _ensure_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self.initialize()
        assert self._conn is not None
        return self._conn

    @staticmethod
    def _parse_ts(ts_str: str) -> float:
        try:
            s = ts_str.replace("Z", "+00:00")
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            return 0.0

    def _is_fenced_out(self, conn: sqlite3.Connection, profile_id: str, created_at: str) -> bool:
        created_ts = self._parse_ts(created_at)

        # Check global privacy fence
        cur = conn.execute("SELECT forgotten_at FROM global_privacy_fence WHERE id = 1")
        row = cur.fetchone()
        if row is not None and created_ts <= self._parse_ts(row["forgotten_at"]):
            return True

        # Check profile privacy fence
        cur = conn.execute("SELECT purged_at FROM profile_privacy_fences WHERE profile_id = ?", (profile_id,))
        p_row = cur.fetchone()
        if p_row is not None and created_ts <= self._parse_ts(p_row["purged_at"]):
            return True

        return False

    def insert_exchange_embedding(self, rec: ExchangeEmbeddingRecord) -> bool:
        with self._lock:
            conn = self._ensure_conn()
            if self._is_fenced_out(conn, rec.profile_id, rec.created_at):
                return False

            blob = np.array(rec.vector, dtype=np.float32).tobytes()
            sql = """
            INSERT OR REPLACE INTO exchange_embeddings (
                exchange_key, profile_id, session_id, episode_id,
                content_hash, model_id, model_version, dimensions,
                vector_blob, created_at, lexical_tokens
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            conn.execute(
                sql,
                (
                    rec.exchange_key,
                    rec.profile_id,
                    rec.session_id,
                    rec.episode_id,
                    rec.content_hash,
                    rec.model_id,
                    rec.model_version,
                    rec.dimensions,
                    blob,
                    rec.created_at,
                    rec.lexical_tokens,
                ),
            )
            conn.commit()
            return True

    def insert_episode_embedding(self, rec: EpisodeEmbeddingRecord) -> bool:
        with self._lock:
            conn = self._ensure_conn()
            if self._is_fenced_out(conn, rec.profile_id, rec.created_at):
                return False

            blob = np.array(rec.vector, dtype=np.float32).tobytes()
            sql = """
            INSERT OR REPLACE INTO episode_embeddings (
                episode_id, profile_id, model_id, model_version,
                vector_blob, cohesion_mean, cohesion_min, content_fingerprint,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """
            conn.execute(
                sql,
                (
                    rec.episode_id,
                    rec.profile_id,
                    rec.model_id,
                    rec.model_version,
                    blob,
                    rec.cohesion_mean,
                    rec.cohesion_min,
                    rec.content_fingerprint,
                    rec.created_at,
                ),
            )
            conn.commit()
            return True

    def get_exchange_embeddings_by_profile(
        self, profile_id: str
    ) -> list[ExchangeEmbeddingRecord]:
        with self._lock:
            conn = self._ensure_conn()
            sql = "SELECT * FROM exchange_embeddings WHERE profile_id = ?"
            cur = conn.execute(sql, (profile_id,))
            records: list[ExchangeEmbeddingRecord] = []
            for row in cur.fetchall():
                vec = np.frombuffer(row["vector_blob"], dtype=np.float32).tolist()
                lex = row["lexical_tokens"] if "lexical_tokens" in row.keys() else ""
                records.append(
                    ExchangeEmbeddingRecord(
                        exchange_key=row["exchange_key"],
                        profile_id=row["profile_id"],
                        session_id=row["session_id"],
                        episode_id=row["episode_id"],
                        content_hash=row["content_hash"],
                        model_id=row["model_id"],
                        model_version=row["model_version"],
                        dimensions=row["dimensions"],
                        vector=vec,
                        created_at=row["created_at"],
                        lexical_tokens=lex,
                    )
                )
            return records

    def get_episode_embeddings_by_profile(
        self, profile_id: str
    ) -> list[EpisodeEmbeddingRecord]:
        with self._lock:
            conn = self._ensure_conn()
            sql = "SELECT * FROM episode_embeddings WHERE profile_id = ?"
            cur = conn.execute(sql, (profile_id,))
            records: list[EpisodeEmbeddingRecord] = []
            for row in cur.fetchall():
                vec = np.frombuffer(row["vector_blob"], dtype=np.float32).tolist()
                records.append(
                    EpisodeEmbeddingRecord(
                        episode_id=row["episode_id"],
                        profile_id=row["profile_id"],
                        model_id=row["model_id"],
                        model_version=row["model_version"],
                        vector=vec,
                        cohesion_mean=row["cohesion_mean"],
                        cohesion_min=row["cohesion_min"],
                        content_fingerprint=row["content_fingerprint"],
                        created_at=row["created_at"],
                    )
                )
            return records

    def get_episode_by_id(self, episode_id: str) -> Optional[EpisodeEmbeddingRecord]:
        with self._lock:
            conn = self._ensure_conn()
            cur = conn.execute("SELECT * FROM episode_embeddings WHERE episode_id = ?", (episode_id,))
            row = cur.fetchone()
            if row is None:
                return None
            vec = np.frombuffer(row["vector_blob"], dtype=np.float32).tolist()
            return EpisodeEmbeddingRecord(
                episode_id=row["episode_id"],
                profile_id=row["profile_id"],
                model_id=row["model_id"],
                model_version=row["model_version"],
                vector=vec,
                cohesion_mean=row["cohesion_mean"],
                cohesion_min=row["cohesion_min"],
                content_fingerprint=row["content_fingerprint"],
                created_at=row["created_at"],
            )

    def delete_episode_cache(self, episode_id: str) -> None:
        with self._lock:
            conn = self._ensure_conn()
            conn.execute("DELETE FROM exchange_embeddings WHERE episode_id = ?", (episode_id,))
            conn.execute("DELETE FROM episode_embeddings WHERE episode_id = ?", (episode_id,))
            conn.commit()

    def purge_profile_cache(self, profile_id: str) -> None:
        with self._lock:
            conn = self._ensure_conn()
            now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            conn.execute("DELETE FROM exchange_embeddings WHERE profile_id = ?", (profile_id,))
            conn.execute("DELETE FROM episode_embeddings WHERE profile_id = ?", (profile_id,))
            conn.execute(
                "INSERT OR REPLACE INTO profile_privacy_fences (profile_id, purged_at) VALUES (?, ?)",
                (profile_id, now_iso),
            )
            conn.commit()

    def forget_all_cache(self) -> None:
        with self._lock:
            conn = self._ensure_conn()
            now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            conn.execute("DELETE FROM exchange_embeddings;")
            conn.execute("DELETE FROM episode_embeddings;")
            conn.execute(
                "INSERT OR REPLACE INTO global_privacy_fence (id, forgotten_at) VALUES (1, ?)",
                (now_iso,),
            )
            conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
