"""T2 (memory-write-quality-20261020): the cap prune spends dead rows first.

A rejected draft is scrubbed (empty text, inactive, judged) but keeps
status='draft', so it used to compete with unjudged drafts for the 200-row cap
purely by age. Synthetic data only.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from opencohost.core.memory import memoria_store as store_mod
from opencohost.core.memory.memoria_store import MemoriaStore

PROFILE = "p"
BASE = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _insert(db_path, row_id, i, *, dead=False, pinned=False, status="draft"):
    ts = (BASE + timedelta(seconds=i)).isoformat()
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO memorias (id, profile_id, stable_key, revision, title, content, "
            "status, pinned, private, inactive, judged_at, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?, 0, ?, ?, ?, ?)",
            (
                row_id, PROFILE, f"{PROFILE}|{row_id}",
                "" if dead else f"title {i}", "" if dead else f"content {i}",
                status, 1 if pinned else 0, 1 if dead else 0,
                ts if dead else "", ts, ts,
            ),
        )


def _ids(db_path):
    with sqlite3.connect(str(db_path)) as conn:
        return {r[0] for r in conn.execute("SELECT id FROM memorias")}


def _store(monkeypatch, tmp_path, cap):
    monkeypatch.setattr(store_mod, "MEMORIAS_PROFILE_CAP", cap)
    store = MemoriaStore(tmp_path / "memorias.db")
    return store, tmp_path / "memorias.db"


def test_scrubbed_rejected_draft_is_pruned_before_an_older_unjudged_one(monkeypatch, tmp_path):
    store, db = _store(monkeypatch, tmp_path, cap=3)
    _insert(db, "unjudged-oldest", 0)
    _insert(db, "unjudged-1", 1)
    _insert(db, "dead-new", 2, dead=True)
    _insert(db, "unjudged-2", 3)
    store._prune_profile(PROFILE)
    kept = _ids(db)
    assert "dead-new" not in kept
    assert kept == {"unjudged-oldest", "unjudged-1", "unjudged-2"}


def test_dead_rows_go_oldest_first_then_unjudged_oldest_first(monkeypatch, tmp_path):
    store, db = _store(monkeypatch, tmp_path, cap=2)
    _insert(db, "unjudged-a", 0)
    _insert(db, "dead-b", 1, dead=True)
    _insert(db, "unjudged-c", 2)
    _insert(db, "dead-d", 3, dead=True)
    _insert(db, "unjudged-e", 4)
    store._prune_profile(PROFILE)
    # 5 rows, cap 2 -> 3 deletions: both dead rows, then the oldest unjudged.
    assert _ids(db) == {"unjudged-c", "unjudged-e"}


def test_prune_is_a_noop_at_or_below_the_cap(monkeypatch, tmp_path):
    store, db = _store(monkeypatch, tmp_path, cap=3)
    _insert(db, "dead", 0, dead=True)
    _insert(db, "u1", 1)
    _insert(db, "u2", 2)
    store._prune_profile(PROFILE)
    assert _ids(db) == {"dead", "u1", "u2"}


def test_pinned_summary_and_other_status_rows_are_never_candidates(monkeypatch, tmp_path):
    store, db = _store(monkeypatch, tmp_path, cap=1)
    _insert(db, "pinned-dead", 0, dead=True, pinned=True)
    _insert(db, "summary", 1, status="summary")
    _insert(db, "promoted", 2, status="promoted")
    _insert(db, "dead", 3, dead=True)
    _insert(db, "u1", 4)
    store._prune_profile(PROFILE)
    assert _ids(db) == {"pinned-dead", "summary", "promoted", "u1"}
