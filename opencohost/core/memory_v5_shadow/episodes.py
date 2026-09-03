from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from opencohost.core.memory_v5_shadow.sessions import SessionFormationReducer

EPISODE_IDLE_GAP_SECONDS = 600.0  # 10 minutes intra-session idle gap
EPISODE_MAX_TURNS_THRESHOLD = 30  # 30 evidence records max per episode
EPISODE_MAX_DURATION_SECONDS = 2700.0  # 45 minutes max per episode (frozen policy v1)


def _parse_ts(ts_str: str) -> datetime:
    s = ts_str.replace("Z", "+00:00")
    return datetime.fromisoformat(s)


@dataclass
class EpisodeRecord:
    episode_id: str
    profile_id: str
    session_id: str
    state: str
    formation_policy_id: str
    formation_policy_version: str
    started_at: str
    ended_at: Optional[str]
    opened_reason: str
    closure_reason: Optional[str]
    event_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EpisodeMembershipRecord:
    episode_id: str
    event_id: str
    sequence_index: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def compute_canonical_episodes_hash(
    episodes: Sequence[dict[str, Any] | EpisodeRecord],
) -> str:
    rows = [e.to_dict() if isinstance(e, EpisodeRecord) else dict(e) for e in episodes]
    rows.sort(key=lambda r: (r["started_at"], r["episode_id"]))
    keys = [
        "episode_id",
        "profile_id",
        "session_id",
        "state",
        "formation_policy_id",
        "formation_policy_version",
        "started_at",
        "ended_at",
        "opened_reason",
        "closure_reason",
        "event_count",
    ]
    canonical_list = [{k: r.get(k) for k in keys} for r in rows]
    raw = json.dumps(canonical_list, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class EpisodeSegmentationEngine:
    POLICY_ID = "deterministic-temporal"
    POLICY_VERSION = "v1"

    @staticmethod
    def compute_episode_id(
        session_id: str,
        start_event_id: str,
        policy_id: str = "deterministic-temporal",
        policy_version: str = "v1",
    ) -> str:
        payload = f"{session_id}:{start_event_id}:{policy_id}:{policy_version}".encode(
            "utf-8"
        )
        return hashlib.sha256(payload).hexdigest()[:24]

    def segment_session_events(
        self,
        session_id: str,
        profile_id: str,
        events: Sequence[dict[str, Any]],
        is_session_closed: bool = True,
    ) -> tuple[list[EpisodeRecord], list[EpisodeMembershipRecord]]:
        if not events:
            return [], []

        episodes: list[EpisodeRecord] = []
        memberships: list[EpisodeMembershipRecord] = []
        current_chunk: list[dict[str, Any]] = []
        next_opened_reason = "SESSION_START"

        def flush_chunk(
            reason: Optional[str], subsequent_open_reason: str
        ) -> None:
            nonlocal next_opened_reason
            if not current_chunk:
                return
            start_ev = current_chunk[0]
            end_ev = current_chunk[-1]
            eid = self.compute_episode_id(
                session_id, start_ev["event_id"], self.POLICY_ID, self.POLICY_VERSION
            )
            is_closed = reason is not None
            rec = EpisodeRecord(
                episode_id=eid,
                profile_id=profile_id,
                session_id=session_id,
                state="CLOSED" if is_closed else "OPEN",
                formation_policy_id=self.POLICY_ID,
                formation_policy_version=self.POLICY_VERSION,
                started_at=start_ev["occurred_at"],
                ended_at=end_ev["occurred_at"] if is_closed else None,
                opened_reason=next_opened_reason,
                closure_reason=reason if is_closed else None,
                event_count=len(current_chunk),
            )
            episodes.append(rec)
            for idx, ev in enumerate(current_chunk):
                mem = EpisodeMembershipRecord(
                    episode_id=eid,
                    event_id=ev["event_id"],
                    sequence_index=idx,
                )
                memberships.append(mem)
            current_chunk.clear()
            next_opened_reason = subsequent_open_reason

        for ev in events:
            if current_chunk:
                last_ev = current_chunk[-1]
                gap = (
                    _parse_ts(ev["occurred_at"]) - _parse_ts(last_ev["occurred_at"])
                ).total_seconds()
                dur = (
                    _parse_ts(ev["occurred_at"]) - _parse_ts(current_chunk[0]["occurred_at"])
                ).total_seconds()

                if gap >= EPISODE_IDLE_GAP_SECONDS:
                    flush_chunk("IDLE_GAP_EPISODE", "IDLE_GAP_EPISODE")
                elif len(current_chunk) >= EPISODE_MAX_TURNS_THRESHOLD:
                    flush_chunk("MAX_EPISODE_TURNS", "MAX_EPISODE_TURNS")
                elif dur >= EPISODE_MAX_DURATION_SECONDS:
                    flush_chunk("MAX_EPISODE_DURATION", "MAX_EPISODE_DURATION")

            current_chunk.append(ev)

        if current_chunk:
            if is_session_closed:
                flush_chunk("SESSION_CLOSED", "SESSION_START")
            else:
                flush_chunk(None, "SESSION_START")

        return episodes, memberships

    def segment_all_from_db(
        self, db_path: Path | str, persist: bool = True
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        reducer = SessionFormationReducer()
        sessions = reducer.rebuild_from_db(db_path)
        all_episodes: list[dict[str, Any]] = []
        all_memberships: list[dict[str, Any]] = []

        conn_uri = f"file:{db_path}?mode=ro" if not persist else str(db_path)
        conn = sqlite3.connect(conn_uri, uri=(not persist), timeout=5.0)
        conn.row_factory = sqlite3.Row

        try:
            if persist:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("BEGIN IMMEDIATE")

            for s in sessions:
                sid = s["session_id"]
                pid = s["profile_id"]
                is_closed = s["state"] == "CLOSED"
                ev_list = reducer.session_events.get(sid, [])
                eps, mems = self.segment_session_events(
                    sid, pid, ev_list, is_session_closed=is_closed
                )

                if persist:
                    conn.execute(
                        "DELETE FROM episode_membership WHERE episode_id IN (SELECT episode_id FROM episodes WHERE session_id=? AND formation_policy_id=? AND formation_policy_version=?)",
                        (sid, self.POLICY_ID, self.POLICY_VERSION),
                    )
                    conn.execute(
                        "DELETE FROM episodes WHERE session_id=? AND formation_policy_id=? AND formation_policy_version=?",
                        (sid, self.POLICY_ID, self.POLICY_VERSION),
                    )
                    for ep in eps:
                        ep_d = ep.to_dict()
                        conn.execute(
                            (
                                "INSERT OR REPLACE INTO episodes "
                                "(episode_id, profile_id, session_id, state, "
                                "formation_policy_id, formation_policy_version, started_at, "
                                "ended_at, opened_reason, closure_reason, event_count) "
                                "VALUES (?,?,?,?,?,?,?,?,?,?,?)"
                            ),
                            (
                                ep_d["episode_id"],
                                ep_d["profile_id"],
                                ep_d["session_id"],
                                ep_d["state"],
                                ep_d["formation_policy_id"],
                                ep_d["formation_policy_version"],
                                ep_d["started_at"],
                                ep_d["ended_at"],
                                ep_d["opened_reason"],
                                ep_d["closure_reason"],
                                ep_d["event_count"],
                            ),
                        )
                        all_episodes.append(ep_d)

                    for m in mems:
                        m_d = m.to_dict()
                        conn.execute(
                            (
                                "INSERT OR REPLACE INTO episode_membership "
                                "(episode_id, event_id, sequence_index) "
                                "VALUES (?,?,?)"
                            ),
                            (
                                m_d["episode_id"],
                                m_d["event_id"],
                                m_d["sequence_index"],
                            ),
                        )
                        all_memberships.append(m_d)
                else:
                    for ep in eps:
                        all_episodes.append(ep.to_dict())
                    for m in mems:
                        all_memberships.append(m.to_dict() if hasattr(m, "to_dict") else dict(m))

            if persist:
                conn.commit()
            return all_episodes, all_memberships
        finally:
            conn.close()
