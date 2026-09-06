from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

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
    ) -> bool:
        lid_raw = f"{run_id}:{stream_sequence}:{kind}:{owner_profile_id or ''}:{transition_id or ''}"
        lid = hashlib.sha256(lid_raw.encode()).hexdigest()[:24]
        cat = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._lock:
            if self._conn is None:
                return False
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
                return True
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                return False

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
                    "SELECT * FROM evidence_journal ORDER BY occurred_at ASC, stream_sequence ASC"
                )
            else:
                cur = self._conn.execute(
                    "SELECT * FROM evidence_journal WHERE profile_id=? "
                    "ORDER BY occurred_at ASC, stream_sequence ASC",
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

    def upsert_session(self, sess: Any) -> bool:
        d = sess.to_dict() if hasattr(sess, "to_dict") else dict(sess)
        with self._lock:
            if self._conn is None:
                return False
            try:
                self._conn.execute(
                    (
                        "INSERT INTO sessions "
                        "(session_id, run_id, profile_id, state, started_at, ended_at, "
                        "opened_reason, closure_reason, event_count) "
                        "VALUES (?,?,?,?,?,?,?,?,?) "
                        "ON CONFLICT(session_id) DO UPDATE SET "
                        "state=excluded.state, ended_at=excluded.ended_at, "
                        "closure_reason=excluded.closure_reason, event_count=excluded.event_count"
                    ),
                    (
                        d["session_id"],
                        d["run_id"],
                        d["profile_id"],
                        d["state"],
                        d["started_at"],
                        d.get("ended_at"),
                        d["opened_reason"],
                        d.get("closure_reason"),
                        int(d.get("event_count", 0)),
                    ),
                )
                self._conn.commit()
                return True
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
                return False

    def list_sessions(self, profile_id: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock:
            if self._conn is None:
                return []
            if profile_id is None:
                cur = self._conn.execute(
                    "SELECT * FROM sessions ORDER BY started_at, session_id"
                )
            else:
                cur = self._conn.execute(
                    "SELECT * FROM sessions WHERE profile_id=? ORDER BY started_at, session_id",
                    (profile_id,),
                )
            return [dict(r) for r in cur.fetchall()]

    def recover_unclean_runs(
        self, new_run_id: str, get_next_seq_fn: Callable[[], int]
    ) -> list[str]:
        recovered_profiles: list[str] = []
        with self._lock:
            if self._conn is None:
                return []
            try:
                cur = self._conn.execute(
                    (
                        "SELECT s.session_id, s.profile_id, s.run_id, s.started_at, "
                        "(SELECT MAX(occurred_at) FROM evidence_journal WHERE profile_id=s.profile_id AND run_id=s.run_id) AS last_occ "
                        "FROM sessions s "
                        "WHERE s.state='OPEN' AND s.run_id != ?"
                    ),
                    (new_run_id,),
                )
                orphans = cur.fetchall()
                if not orphans:
                    return []
                now_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                for o in orphans:
                    sid = o["session_id"]
                    pid = o["profile_id"]
                    end_ts = o["last_occ"] or o["started_at"]
                    self._conn.execute(
                        (
                            "UPDATE sessions SET state='CLOSED', "
                            "closure_reason='CRASH_RECOVERY_CLOSED', ended_at=? "
                            "WHERE session_id=?"
                        ),
                        (end_ts, sid),
                    )
                    seq = get_next_seq_fn()
                    lid_raw = f"{new_run_id}:{seq}:STARTUP_RECOVERY:{pid}:"
                    lid = hashlib.sha256(lid_raw.encode()).hexdigest()[:24]
                    self._conn.execute(
                        (
                            "INSERT OR IGNORE INTO lifecycle_events "
                            "(lifecycle_id, owner_profile_id, kind, transition_id, "
                            "run_id, stream_sequence, occurred_at, created_at) "
                            "VALUES (?,?,?,?,?,?,?,?)"
                        ),
                        (lid, pid, "STARTUP_RECOVERY", None, new_run_id, seq, now_str, now_str),
                    )
                    recovered_profiles.append(pid)
                self._conn.commit()
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass
        return recovered_profiles

    def reconcile_sessions_from_journals(self) -> None:
        from opencohost.core.memory_v5_shadow.sessions import SessionFormationReducer
        reducer = SessionFormationReducer()
        rebuilt = reducer.rebuild_from_db(self.db_path)
        with self._lock:
            if self._conn is None:
                return
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                for s in rebuilt:
                    self._conn.execute(
                        (
                            "INSERT INTO sessions "
                            "(session_id, run_id, profile_id, state, started_at, ended_at, "
                            "opened_reason, closure_reason, event_count) "
                            "VALUES (?,?,?,?,?,?,?,?,?) "
                            "ON CONFLICT(session_id) DO UPDATE SET "
                            "state=excluded.state, ended_at=excluded.ended_at, "
                            "closure_reason=excluded.closure_reason, event_count=excluded.event_count"
                        ),
                        (
                            s["session_id"],
                            s["run_id"],
                            s["profile_id"],
                            s["state"],
                            s["started_at"],
                            s.get("ended_at"),
                            s["opened_reason"],
                            s.get("closure_reason"),
                            int(s.get("event_count", 0)),
                        ),
                    )
                self._conn.commit()
            except Exception:
                try:
                    self._conn.rollback()
                except Exception:
                    pass

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
