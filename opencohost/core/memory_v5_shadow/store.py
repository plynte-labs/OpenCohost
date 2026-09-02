from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from opencohost.core.memory_v5_shadow.evidence import CommittedTurnSnapshot


def _ddl_path() -> Path:
    return (
        Path(__file__).resolve().parents[3]
        / "docs"
        / "memory_v5"
        / "memory_v5_shadow_ddl_v1.sql"
    )


@dataclass(frozen=True)
class InsertOutcome:
    outcome: str
    reason_code: str


class ShadowStore:
    def __init__(self, db_path: Path | str | None = None) -> None:
        from opencohost.config.settings import MEMORY_V5_SHADOW_DB

        p = Path(db_path) if db_path is not None else Path(MEMORY_V5_SHADOW_DB)
        self.db_path = p
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=5.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        sql = _ddl_path().read_text(encoding="utf-8")
        with self._lock:
            self._conn.executescript(sql)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.commit()

    def insert_snapshot(self, snap: CommittedTurnSnapshot) -> InsertOutcome:
        eid = hashlib.sha256(snap.committed_turn_id.encode()).hexdigest()[:24]
        ch = hashlib.sha256(snap.content.encode()).hexdigest()
        cat = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._lock:
            try:
                self._conn.execute(
                    (
                        "INSERT INTO evidence_journal "
                        "(event_id, committed_turn_id, profile_id, run_id, "
                        "stream_sequence, role, source, occurred_at, content, "
                        "content_hash, is_private, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)"
                    ),
                    (
                        eid,
                        snap.committed_turn_id,
                        snap.profile_id,
                        snap.run_id,
                        snap.stream_sequence,
                        snap.role,
                        snap.source,
                        snap.occurred_at,
                        snap.content,
                        ch,
                        1 if snap.is_private else 0,
                        cat,
                    ),
                )
                self._conn.commit()
                return InsertOutcome("INSERTED", "inserted")
            except sqlite3.IntegrityError:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                existing = None
                try:
                    cur = self._conn.execute(
                        "SELECT * FROM evidence_journal WHERE event_id=? OR committed_turn_id=? OR (run_id=? AND stream_sequence=?)",
                        (eid, snap.committed_turn_id, snap.run_id, snap.stream_sequence),
                    )
                    existing = cur.fetchone()
                except Exception:
                    return InsertOutcome("STORE_FAILURE", "integrity_lookup_failed")
                if existing is None:
                    return InsertOutcome("INTEGRITY_COLLISION", "unique_violation_no_row")
                same = (
                    existing["event_id"] == eid
                    and existing["committed_turn_id"] == snap.committed_turn_id
                    and existing["profile_id"] == snap.profile_id
                    and existing["run_id"] == snap.run_id
                    and int(existing["stream_sequence"]) == int(snap.stream_sequence)
                    and existing["role"] == snap.role
                    and existing["source"] == snap.source
                    and existing["occurred_at"] == snap.occurred_at
                    and existing["content"] == snap.content
                    and existing["content_hash"] == ch
                )
                if same:
                    return InsertOutcome("EXACT_DUPLICATE", "exact_duplicate")
                coll = "integrity_collision"
                if existing["event_id"] == eid:
                    coll = "event_id_collision"
                elif existing["committed_turn_id"] == snap.committed_turn_id:
                    coll = "committed_id_collision"
                elif existing["run_id"] == snap.run_id and int(existing["stream_sequence"]) == int(snap.stream_sequence):
                    coll = "run_sequence_collision"
                return InsertOutcome("INTEGRITY_COLLISION", coll)
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                return InsertOutcome("STORE_FAILURE", "store_failure")

    def list_evidence(self, profile_id: Optional[str] = None):
        with self._lock:
            if profile_id is None:
                cur = self._conn.execute(
                    "SELECT * FROM evidence_journal ORDER BY stream_sequence"
                )
            else:
                cur = self._conn.execute(
                    "SELECT * FROM evidence_journal WHERE profile_id=? "
                    "ORDER BY stream_sequence",
                    (profile_id,),
                )
            return [dict(r) for r in cur.fetchall()]

    def count_evidence(self, profile_id: Optional[str] = None) -> int:
        with self._lock:
            if profile_id is None:
                cur = self._conn.execute("SELECT COUNT(*) FROM evidence_journal")
            else:
                cur = self._conn.execute(
                    "SELECT COUNT(*) FROM evidence_journal WHERE profile_id=?",
                    (profile_id,),
                )
            return int(cur.fetchone()[0])

    def fetch_one_by_event(self, event_id: str):
        with self._lock:
            cur = self._conn.execute(
                "SELECT * FROM evidence_journal WHERE event_id=?", (event_id,)
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def purge_profile(self, profile_id: str) -> None:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "DELETE FROM lifecycle_events WHERE owner_profile_id=?",
                    (profile_id,),
                )
                self._conn.execute(
                    "DELETE FROM sessions WHERE profile_id=?",
                    (profile_id,),
                )
                self._conn.execute(
                    "DELETE FROM evidence_journal WHERE profile_id=?",
                    (profile_id,),
                )
                self._conn.execute(
                    "DELETE FROM retention_state WHERE profile_id=?",
                    (profile_id,),
                )
                self._conn.commit()
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                raise

    def forget_all(self) -> None:
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                for t in (
                    "episode_membership",
                    "episodes",
                    "sessions",
                    "lifecycle_events",
                    "evidence_journal",
                    "control_failures",
                    "shadow_runs",
                    "shadow_diagnostics",
                    "retention_state",
                ):
                    self._conn.execute(f"DELETE FROM {t}")
                self._conn.commit()
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                raise

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except Exception:
                pass
