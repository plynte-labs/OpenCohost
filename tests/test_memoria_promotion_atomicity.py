"""Atomic MemoriaStore application contracts for promotion batches."""

from __future__ import annotations

import sqlite3

import pytest

from opencohost.core.memory.memoria_store import MemoriaStore, build_signature


def _seed(store: MemoriaStore, profile_id: str, suffix: str) -> str:
    row_id = store.upsert_draft(
        profile_id,
        f"{profile_id}|{suffix}",
        f"title {suffix}",
        f"original content {suffix} alpha beta",
        signature=f"original signature {suffix}",
    )
    assert isinstance(row_id, str)
    return row_id


def _row(db_path, row_id: str) -> sqlite3.Row:
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM memorias WHERE id = ?", (row_id,),
        ).fetchone()


def _attempt(db_path, row_id: str) -> sqlite3.Row | None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM memoria_promotion_attempts WHERE memoria_id = ?",
            (row_id,),
        ).fetchone()


def _batch(store: MemoriaStore, profile_id: str, now_s: int) -> list:
    return store.list_unjudged_drafts(
        profile_id, limit=8, now_s=now_s, raising=True,
    )


def test_partial_valid_result_applies_siblings_and_charges_only_unresolved(
    tmp_path,
) -> None:
    db_path = tmp_path / "memorias.db"
    store = MemoriaStore(db_path)
    ids = [_seed(store, "p", f"d{i}") for i in range(1, 5)]
    judged = "The operator runs the memory judge locally with Ollama."

    result = store.apply_promotion_batch(
        "p",
        _batch(store, "p", 100),
        decisions=[
            (1, judged, False, ""),
            (2, None, False, "vague"),
        ],
        unresolved={
            3: "missing_decision",
            4: "invalid_decision",
        },
        now_s=100,
    )

    kept = _row(db_path, ids[0])
    assert kept["content"] == judged
    assert kept["signature"] == build_signature(judged)
    assert kept["status"] == "promoted"
    assert kept["judged_at"] != ""
    rejected = _row(db_path, ids[1])
    assert rejected["status"] == "draft"
    assert rejected["inactive"] == 1
    assert rejected["judged_at"] != ""

    missing = _attempt(db_path, ids[2])
    invalid = _attempt(db_path, ids[3])
    assert missing["attempt_count"] == 1
    assert missing["last_failure_code"] == "missing_decision"
    assert missing["next_attempt_at_s"] == 400
    assert invalid["attempt_count"] == 1
    assert invalid["last_failure_code"] == "invalid_decision"
    assert invalid["next_attempt_at_s"] == 400
    assert _row(db_path, ids[2])["status"] == "draft"
    assert _row(db_path, ids[3])["status"] == "draft"
    assert result.kept == 1
    assert result.rejected == 1
    assert result.charged == 2
    assert result.deferred == 0
    assert result.stale == 0


def test_first_second_and_third_semantic_failures_use_exact_deadlines(
    tmp_path,
) -> None:
    db_path = tmp_path / "memorias.db"
    store = MemoriaStore(db_path)
    row_id = _seed(store, "p", "retry")

    first = store.apply_promotion_batch(
        "p",
        _batch(store, "p", 100),
        decisions=[],
        unresolved={1: "missing_decision"},
        now_s=100,
    )
    attempt = _attempt(db_path, row_id)
    assert first.charged == 1
    assert attempt["attempt_count"] == 1
    assert attempt["next_attempt_at_s"] == 400
    assert _batch(store, "p", 399) == []

    second = store.apply_promotion_batch(
        "p",
        _batch(store, "p", 400),
        decisions=[],
        unresolved={1: "duplicate_index"},
        now_s=400,
    )
    attempt = _attempt(db_path, row_id)
    assert second.charged == 1
    assert attempt["attempt_count"] == 2
    assert attempt["last_failure_code"] == "duplicate_index"
    assert attempt["next_attempt_at_s"] == 2_200
    assert _batch(store, "p", 2_199) == []

    third = store.apply_promotion_batch(
        "p",
        _batch(store, "p", 2_200),
        decisions=[],
        unresolved={1: "invalid_decision"},
        now_s=2_200,
    )
    attempt = _attempt(db_path, row_id)
    assert third.charged == 1
    assert third.deferred == 1
    assert attempt["attempt_count"] == 3
    assert attempt["last_failure_code"] == "invalid_decision"
    assert attempt["promotion_state"] == "deferred"
    assert attempt["next_attempt_at_s"] is None
    row = _row(db_path, row_id)
    assert row["status"] == "draft"
    assert row["judged_at"] == ""
    assert row["inactive"] == 0
    assert "original content" in row["content"]


def test_sql_failure_after_first_decision_rolls_back_the_entire_batch(
    tmp_path,
) -> None:
    db_path = tmp_path / "memorias.db"
    store = MemoriaStore(db_path)
    first_id = _seed(store, "p", "a-first")
    second_id = _seed(store, "p", "b-second")
    before_first = dict(_row(db_path, first_id))
    before_second = dict(_row(db_path, second_id))
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            f"""
            CREATE TRIGGER fail_second_promotion
            BEFORE UPDATE OF content ON memorias
            WHEN OLD.id = '{second_id}'
            BEGIN
                SELECT RAISE(ABORT, 'injected promotion failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError):
        store.apply_promotion_batch(
            "p",
            _batch(store, "p", 100),
            decisions=[
                (1, "First rewritten content alpha beta.", False, ""),
                (2, "Second rewritten content alpha beta.", False, ""),
            ],
            unresolved={},
            now_s=100,
        )

    assert dict(_row(db_path, first_id)) == before_first
    assert dict(_row(db_path, second_id)) == before_second
    assert _attempt(db_path, first_id) is None
    assert _attempt(db_path, second_id) is None


@pytest.mark.parametrize(
    "mutation",
    ["revision", "status", "private", "inactive", "profile"],
)
def test_stale_revision_status_privacy_inactive_or_profile_gets_no_charge(
    tmp_path, mutation,
) -> None:
    db_path = tmp_path / "memorias.db"
    store = MemoriaStore(db_path)
    row_id = _seed(store, "p", "stale")
    batch = _batch(store, "p", 100)

    if mutation == "revision":
        store.upsert_draft(
            "p", "p|stale", "fresh", "fresh content alpha beta",
        )
    else:
        column = mutation
        value = "curated" if mutation == "status" else 1
        if mutation == "profile":
            column = "profile_id"
            value = "other"
        with sqlite3.connect(str(db_path)) as conn:
            conn.execute(
                f"UPDATE memorias SET {column} = ? WHERE id = ?",
                (value, row_id),
            )

    result = store.apply_promotion_batch(
        "p",
        batch,
        decisions=[],
        unresolved={1: "missing_decision"},
        now_s=100,
    )

    assert result.stale == 1
    assert result.charged == 0
    assert _attempt(db_path, row_id) is None


def test_foreign_profile_batch_row_is_stale_and_never_charged(
    tmp_path,
) -> None:
    db_path = tmp_path / "memorias.db"
    store = MemoriaStore(db_path)
    mine = _seed(store, "p", "mine")
    theirs = _seed(store, "q", "theirs")
    mixed_batch = [
        store.get(mine, raising=True),
        store.get(theirs, raising=True),
    ]

    result = store.apply_promotion_batch(
        "p",
        mixed_batch,
        decisions=[(1, None, False, "vague")],
        unresolved={2: "missing_decision"},
        now_s=100,
    )

    assert result.rejected == 1
    assert result.stale == 1
    assert result.charged == 0
    assert _attempt(db_path, theirs) is None
