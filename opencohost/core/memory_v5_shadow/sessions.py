from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

IDLE_GAP_THRESHOLD_SECONDS = 1800.0  # 30 minutes
MAX_DURATION_SECONDS = 28800.0  # 8 hours
MAX_TURNS_THRESHOLD = 500  # 500 durable Evidence records


def _parse_ts(ts_str: str) -> datetime:
    s = ts_str.replace("Z", "+00:00")
    return datetime.fromisoformat(s)


@dataclass
class SessionRecord:
    session_id: str
    run_id: str
    profile_id: str
    state: str
    started_at: str
    ended_at: Optional[str]
    opened_reason: str
    closure_reason: Optional[str]
    event_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_canonical_sessions_hash(
    sessions: Sequence[dict[str, Any] | SessionRecord],
) -> str:
    rows = [s.to_dict() if isinstance(s, SessionRecord) else dict(s) for s in sessions]
    rows.sort(key=lambda r: (r["started_at"], r["session_id"]))
    keys = [
        "session_id",
        "run_id",
        "profile_id",
        "state",
        "started_at",
        "ended_at",
        "opened_reason",
        "closure_reason",
        "event_count",
    ]
    canonical_list = [{k: r.get(k) for k in keys} for r in rows]
    raw = json.dumps(canonical_list, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class SessionFormationReducer:
    @staticmethod
    def compute_session_id(
        run_id: str, profile_id: str, start_seq: int, opened_reason: str
    ) -> str:
        payload = f"{run_id}:{profile_id}:{start_seq}:{opened_reason}".encode("utf-8")
        return hashlib.sha256(payload).hexdigest()[:24]

    def __init__(self) -> None:
        self.active_sessions: dict[str, SessionRecord] = {}  # profile_id -> active session
        self.last_turn_times: dict[str, str] = {}  # profile_id -> occurred_at
        self.profile_switch_in_seen: set[tuple[str, str]] = set()  # (run_id, pid)
        self.completed_sessions: list[SessionRecord] = []
        self.session_events: dict[str, list[dict[str, Any]]] = {}  # session_id -> list of events

    def purge_profile(self, profile_id: str) -> None:
        active = self.active_sessions.pop(profile_id, None)
        if active:
            self.session_events.pop(active.session_id, None)
        self.last_turn_times.pop(profile_id, None)
        self.profile_switch_in_seen = {
            p for p in self.profile_switch_in_seen if p[1] != profile_id
        }
        purged_sids = {s.session_id for s in self.completed_sessions if s.profile_id == profile_id}
        for sid in purged_sids:
            self.session_events.pop(sid, None)
        self.completed_sessions = [
            s for s in self.completed_sessions if s.profile_id != profile_id
        ]

    def reset(self) -> None:
        self.active_sessions.clear()
        self.last_turn_times.clear()
        self.profile_switch_in_seen.clear()
        self.completed_sessions.clear()
        self.session_events.clear()

    def _close_session(
        self,
        profile_id: str,
        ended_at: str,
        closure_reason: str,
    ) -> Optional[SessionRecord]:
        sess = self.active_sessions.pop(profile_id, None)
        last_occ = self.last_turn_times.pop(profile_id, None)
        if sess is None:
            return None
        sess.state = "CLOSED"
        sess.ended_at = last_occ or ended_at
        sess.closure_reason = closure_reason
        self.completed_sessions.append(sess)
        return sess

    def _open_session(
        self,
        run_id: str,
        profile_id: str,
        start_seq: int,
        started_at: str,
        opened_reason: str,
    ) -> SessionRecord:
        sid = self.compute_session_id(run_id, profile_id, start_seq, opened_reason)
        sess = SessionRecord(
            session_id=sid,
            run_id=run_id,
            profile_id=profile_id,
            state="OPEN",
            started_at=started_at,
            ended_at=None,
            opened_reason=opened_reason,
            closure_reason=None,
            event_count=0,
        )
        self.active_sessions[profile_id] = sess
        self.session_events.setdefault(sid, [])
        return sess

    def process_event(self, event: dict[str, Any]) -> list[SessionRecord]:
        etype = event["stream_type"]
        mutations: list[SessionRecord] = []

        if etype == "lifecycle":
            kind = event["kind"]
            pid = event.get("owner_profile_id")
            run_id = event["run_id"]
            seq = int(event["stream_sequence"])
            occ = event["occurred_at"]

            if kind == "PROFILE_SWITCH_OUT" and pid:
                closed = self._close_session(pid, occ, "PROFILE_SWITCH_OUT")
                if closed:
                    mutations.append(closed)
            elif kind == "PROFILE_SWITCH_IN" and pid:
                self.profile_switch_in_seen.add((run_id, pid))
                if pid in self.active_sessions:
                    closed = self._close_session(pid, occ, "PROFILE_SWITCH_OUT")
                    if closed:
                        mutations.append(closed)
                opened = self._open_session(run_id, pid, seq, occ, "PROFILE_SWITCH_IN")
                mutations.append(opened)
            elif kind == "SHUTDOWN":
                for p in list(self.active_sessions.keys()):
                    closed = self._close_session(p, occ, "SHUTDOWN")
                    if closed:
                        mutations.append(closed)
            elif kind == "EXPLICIT_SESSION_END" and pid:
                closed = self._close_session(pid, occ, "EXPLICIT_END")
                if closed:
                    mutations.append(closed)
            elif kind == "STARTUP_RECOVERY" and pid:
                closed = self._close_session(pid, occ, "CRASH_RECOVERY_CLOSED")
                if closed:
                    mutations.append(closed)
            return mutations

        if etype == "evidence":
            pid = event["profile_id"]
            run_id = event["run_id"]
            seq = int(event["stream_sequence"])
            occ = event["occurred_at"]
            last_occ = self.last_turn_times.get(pid)
            sess = self.active_sessions.get(pid)

            if sess is None:
                has_sw_in = (run_id, pid) in self.profile_switch_in_seen
                opened_reason = "PROFILE_SWITCH_IN" if has_sw_in else "STARTUP"
                sess = self._open_session(run_id, pid, seq, occ, opened_reason)
                mutations.append(sess)
            else:
                closure_trigger: Optional[str] = None
                if last_occ is not None:
                    gap = (_parse_ts(occ) - _parse_ts(last_occ)).total_seconds()
                    if gap >= IDLE_GAP_THRESHOLD_SECONDS:
                        closure_trigger = "IDLE_GAP"
                dur = (_parse_ts(occ) - _parse_ts(sess.started_at)).total_seconds()
                if not closure_trigger and dur >= MAX_DURATION_SECONDS:
                    closure_trigger = "MAX_DURATION"
                if not closure_trigger and sess.event_count >= MAX_TURNS_THRESHOLD:
                    closure_trigger = "MAX_TURNS"

                if closure_trigger:
                    close_ts = last_occ or occ
                    closed = self._close_session(pid, close_ts, closure_trigger)
                    if closed:
                        mutations.append(closed)
                    sess = self._open_session(run_id, pid, seq, occ, "IDLE_GAP")
                    mutations.append(sess)

            sess.event_count += 1
            self.session_events.setdefault(sess.session_id, []).append(event)
            self.last_turn_times[pid] = occ
            if sess not in mutations:
                mutations.append(sess)
            return mutations

        return mutations

    def rebuild_from_stream(
        self, events: Sequence[dict[str, Any]]
    ) -> list[SessionRecord]:
        for ev in events:
            self.process_event(ev)
        all_sessions = list(self.completed_sessions) + list(self.active_sessions.values())
        all_sessions.sort(key=lambda s: (s.started_at, s.session_id))
        return all_sessions

    def rebuild_from_db(
        self, db_path: Path | str, *, include_evidence_payload: bool = False
    ) -> list[dict[str, Any]]:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            if include_evidence_payload:
                ev_role = "e.role"
                ev_content = "e.content"
            else:
                ev_role = "NULL AS role"
                ev_content = "NULL AS content"

            sql = f"""
            SELECT events.* FROM (
                SELECT 'evidence' AS stream_type, e.event_id, e.profile_id, e.run_id, e.stream_sequence, e.occurred_at, NULL AS kind, NULL AS owner_profile_id, COALESCE(r.started_at, e.occurred_at) AS run_started_at, {ev_role}, {ev_content}
                FROM evidence_journal e
                LEFT JOIN shadow_runs r ON e.run_id = r.run_id
                UNION ALL
                SELECT 'lifecycle' AS stream_type, NULL AS event_id, l.owner_profile_id AS profile_id, l.run_id, l.stream_sequence, l.occurred_at, l.kind, l.owner_profile_id, COALESCE(r.started_at, l.occurred_at) AS run_started_at, NULL AS role, NULL AS content
                FROM lifecycle_events l
                LEFT JOIN shadow_runs r ON l.run_id = r.run_id
            ) events
            ORDER BY events.run_started_at ASC, events.run_id ASC, events.stream_sequence ASC
            """
            cur = conn.execute(sql)
            events = [dict(r) for r in cur.fetchall()]
            records = self.rebuild_from_stream(events)
            return [r.to_dict() for r in records]
        finally:
            conn.close()
