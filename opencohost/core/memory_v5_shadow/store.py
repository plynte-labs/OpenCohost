from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from opencohost.core.memory_v5_shadow.evidence import CommittedTurnSnapshot


def _ddl_path() -> Path:
    packaged = Path(__file__).resolve().parent / "schema_v1.sql"
    if packaged.exists():
        return packaged
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
        self._conn: Optional[sqlite3.Connection] = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            timeout=5.0,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_db()

    def _init_db(self) -> None:
        sql = _ddl_path().read_text(encoding="utf-8")
        with self._lock:
            if self._conn is not None:
                self._conn.executescript(sql)
                self._conn.execute("PRAGMA journal_mode=WAL")
                self._conn.execute("PRAGMA synchronous=NORMAL")
                self._conn.execute("PRAGMA foreign_keys=ON")
                self._conn.execute("PRAGMA busy_timeout=5000")
                self._conn.commit()

    def start_run(self, run_id: str, started_at: str) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.execute(
                        (
                            "INSERT OR IGNORE INTO shadow_runs "
                            "(run_id, started_at, degraded, capture_enabled, "
                            "dropped_evidence_total, control_failures_total, created_at) "
                            "VALUES (?,?,?,?,?,?,?)"
                        ),
                        (run_id, started_at, 0, 1, 0, 0, started_at),
                    )
                    self._conn.commit()
                except Exception:
                    pass

    def record_run_health(
        self, run_id: str, degraded: bool, control_failures: int, dropped: int
    ) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.execute(
                        (
                            "UPDATE shadow_runs SET degraded=?, "
                            "control_failures_total=?, dropped_evidence_total=? "
                            "WHERE run_id=?"
                        ),
                        (1 if degraded else 0, control_failures, dropped, run_id),
                    )
                    self._conn.commit()
                except Exception:
                    pass

    def insert_lifecycle_event(
        self,
        *,
        kind: str,
        owner_profile_id: Optional[str],
        transition_id: Optional[str],
        run_id: str,
        stream_sequence: int,
        occurred_at: str,
    ) -> None:
        lid_raw = f"{run_id}:{stream_sequence}:{kind}:{owner_profile_id or ''}:{transition_id or ''}"
        lid = hashlib.sha256(lid_raw.encode()).hexdigest()[:24]
        cat = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute(
                    (
                        "INSERT OR IGNORE INTO lifecycle_events "
                        "(lifecycle_id, owner_profile_id, kind, transition_id, "
                        "run_id, stream_sequence, occurred_at, created_at) "
                        "VALUES (?,?,?,?,?,?,?,?)"
                    ),
                    (
                        lid,
                        owner_profile_id,
                        kind,
                        transition_id,
                        run_id,
                        stream_sequence,
                        occurred_at,
                        cat,
                    ),
                )
                self._conn.commit()
            except Exception:
                pass

    def insert_snapshot(self, snap: CommittedTurnSnapshot) -> InsertOutcome:
        eid = hashlib.sha256(snap.committed_turn_id.encode()).hexdigest()[:24]
        ch = hashlib.sha256(snap.content.encode()).hexdigest()
        cat = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._lock:
            if self._conn is None:
                return InsertOutcome("STORE_FAILURE", "db_closed")
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
                        (
                            "SELECT * FROM evidence_journal WHERE event_id=? "
                            "OR committed_turn_id=? OR (run_id=? AND stream_sequence=?)"
                        ),
                        (
                            eid,
                            snap.committed_turn_id,
                            snap.run_id,
                            snap.stream_sequence,
                        ),
                    )
                    existing = cur.fetchone()
                except Exception:
                    return InsertOutcome(
                        "STORE_FAILURE", "integrity_lookup_failed"
                    )
                if existing is None:
                    return InsertOutcome(
                        "INTEGRITY_COLLISION", "unique_violation_no_row"
                    )
                same = (
                    existing["event_id"] == eid
                    and existing["committed_turn_id"]
                    == snap.committed_turn_id
                    and existing["profile_id"] == snap.profile_id
                    and existing["run_id"] == snap.run_id
                    and int(existing["stream_sequence"])
                    == int(snap.stream_sequence)
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
                elif existing["run_id"] == snap.run_id and int(
                    existing["stream_sequence"]
                ) == int(snap.stream_sequence):
                    coll = "run_sequence_collision"
                return InsertOutcome("INTEGRITY_COLLISION", coll)
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                return InsertOutcome("STORE_FAILURE", "store_failure")

    def insert_snapshots(
        self, snapshots: Sequence[CommittedTurnSnapshot]
    ) -> list[InsertOutcome]:
        return [self.insert_snapshot(snap) for snap in snapshots]

    def list_evidence(self, profile_id: Optional[str] = None):
        with self._lock:
            if self._conn is None:
                return []
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
            if self._conn is None:
                return 0
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
            if self._conn is None:
                return None
            cur = self._conn.execute(
                "SELECT * FROM evidence_journal WHERE event_id=?", (event_id,)
            )
            row = cur.fetchone()
            return dict(row) if row else None

    def purge_profile(self, profile_id: str) -> None:
        with self._lock:
            if self._conn is None:
                return
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
            if self._conn is None:
                return
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
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
