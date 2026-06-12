"""Tests for opencohost.core.topic_inbox — TopicInboxStore.

TDD Slice 1: write ALL tests first, confirm they fail, then implement the module.

These tests are designed to be run against a temporary SQLite DB (tmp_path).
No UI modules are imported.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

# This import will FAIL until the module exists — that is intentional for TDD.
from opencohost.core.topic_inbox import (
    TITLE_MAX,
    ANGLE_MAX,
    TAGS_MAX,
    TAG_MAX_CHARS,
    PENDING_CAP,
    TopicInboxStore,
    TopicInboxValidationError,
    TopicInboxCapError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_store(tmp_path: Path) -> tuple[TopicInboxStore, str]:
    db = str(tmp_path / "cards.db")
    return TopicInboxStore(db), db


def valid_propose(store: TopicInboxStore, *, title: str = "My Great Topic", angle: str = "A valid angle for this topic.", tags: list[str] | None = None, source: str = "test-agent") -> dict:
    return store.propose(title=title, angle=angle, tags=tags or [], source=source)


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------

def test_propose_happy_path(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    row = valid_propose(store, title="Python Async Deep Dive", angle="Why async/await matters for streamers.", tags=["python", "async"], source="research-bot")

    assert row["id"].startswith("ti_")
    assert row["status"] == "proposed"
    assert row["title"] == "Python Async Deep Dive"
    assert row["angle"] == "Why async/await matters for streamers."
    assert row["tags"] == ["python", "async"]
    assert row["source"] == "research-bot"
    assert "created_at" in row
    assert "updated_at" in row


# ---------------------------------------------------------------------------
# 2. Title too long
# ---------------------------------------------------------------------------

def test_propose_title_too_long(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    long_title = "A" * (TITLE_MAX + 1)  # 121 chars
    with pytest.raises(TopicInboxValidationError):
        store.propose(title=long_title, angle="Valid angle.", tags=[], source="")


# ---------------------------------------------------------------------------
# 3. Angle too long
# ---------------------------------------------------------------------------

def test_propose_angle_too_long(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    long_angle = "B" * (ANGLE_MAX + 1)  # 601 chars
    with pytest.raises(TopicInboxValidationError):
        store.propose(title="Valid title", angle=long_angle, tags=[], source="")


# ---------------------------------------------------------------------------
# 4. Too many tags
# ---------------------------------------------------------------------------

def test_propose_too_many_tags(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    too_many = [f"tag{i}" for i in range(TAGS_MAX + 1)]  # 9 tags
    with pytest.raises(TopicInboxValidationError):
        store.propose(title="Valid title", angle="Valid angle.", tags=too_many, source="")


# ---------------------------------------------------------------------------
# 5. Tag too long
# ---------------------------------------------------------------------------

def test_propose_tag_too_long(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    long_tag = "X" * (TAG_MAX_CHARS + 1)  # 41 chars
    with pytest.raises(TopicInboxValidationError):
        store.propose(title="Valid title", angle="Valid angle.", tags=[long_tag], source="")


# ---------------------------------------------------------------------------
# 6. Code/HTML in title
# ---------------------------------------------------------------------------

def test_propose_code_in_title(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    with pytest.raises(TopicInboxValidationError):
        store.propose(title="import os\nprint('hack')", angle="Valid angle.", tags=[], source="")


# ---------------------------------------------------------------------------
# 7. HTML in angle
# ---------------------------------------------------------------------------

def test_propose_html_in_angle(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    with pytest.raises(TopicInboxValidationError):
        store.propose(title="Valid title", angle="<script>alert(1)</script>", tags=[], source="")


# ---------------------------------------------------------------------------
# 8. Code in angle (arrow function)
# ---------------------------------------------------------------------------

def test_propose_code_in_angle(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    with pytest.raises(TopicInboxValidationError):
        store.propose(title="Valid title", angle="function() => {}", tags=[], source="")


# ---------------------------------------------------------------------------
# 9. Dedupe: same slug updates instead of duplicating
# ---------------------------------------------------------------------------

def test_dedupe_same_slug_updates_instead_of_duplicate(tmp_path: Path) -> None:
    store, db = make_store(tmp_path)

    # First propose
    row1 = store.propose(title="Python Async", angle="First angle.", tags=["python"], source="bot-a")
    assert row1["id"].startswith("ti_")

    # Second propose with same title but extra spaces → same slug
    row2 = store.propose(title="  Python   Async  ", angle="Updated angle.", tags=["python", "async"], source="bot-b")

    # Must be the SAME row (same id), not a new one
    assert row2["id"] == row1["id"]

    # Content must be updated
    assert row2["angle"] == "Updated angle."
    assert row2["tags"] == ["python", "async"]
    assert row2["source"] == "bot-b"

    # Only 1 row in DB
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM topic_inbox WHERE status='proposed'").fetchone()[0]
    assert count == 1


# ---------------------------------------------------------------------------
# 10. Pending cap refuses at 30
# ---------------------------------------------------------------------------

def test_pending_cap_refuses_at_30(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)

    # Propose exactly PENDING_CAP different topics
    for i in range(PENDING_CAP):
        store.propose(title=f"Unique Topic Number {i:03d}", angle=f"Angle for topic {i}.", tags=[], source="cap-tester")

    # One more must raise TopicInboxCapError
    with pytest.raises(TopicInboxCapError):
        store.propose(title="One Too Many Topic", angle="Should fail.", tags=[], source="cap-tester")


# ---------------------------------------------------------------------------
# 11. Read-time validation: attacker bypass via raw sqlite3
# ---------------------------------------------------------------------------

def test_list_pending_read_time_validation_attacker_bypasses_cli(tmp_path: Path) -> None:
    """An attacker inserts a row with code in the title directly into the DB.
    list_pending() must put it in the 'invalid' bucket, not 'valid'."""
    store, db = make_store(tmp_path)

    # First ensure the table exists by doing a normal propose
    valid_propose(store, title="Legit Topic", angle="Legit angle.", tags=[], source="good-agent")

    # Bypass the CLI: inject a malicious row directly
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    malicious_id = "ti_" + "deadbeef" * 4
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO topic_inbox (id, title, angle, tags, source, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'proposed', ?, ?)""",
            (malicious_id, "import os; os.system('rm -rf /')", "Bad angle.", "[]", "attacker", now, now),
        )

    result = store.list_pending()

    valid_ids = [r["id"] for r in result["valid"]]
    invalid_ids = [r["id"] for r in result["invalid"]]

    assert malicious_id in invalid_ids, "Malicious row must be in invalid bucket"
    assert malicious_id not in valid_ids, "Malicious row must NOT be in valid bucket"

    # The legit topic should be in valid
    assert any(r["title"] == "Legit Topic" for r in result["valid"])

    # Invalid rows have 'invalid_reason' key
    for row in result["invalid"]:
        assert "invalid_reason" in row


# ---------------------------------------------------------------------------
# 12. list_pending only shows proposed rows
# ---------------------------------------------------------------------------

def test_list_pending_only_proposed(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)

    row1 = valid_propose(store, title="Proposed Topic", angle="Will stay proposed.", tags=[], source="")
    row2 = valid_propose(store, title="Approved Topic", angle="Will be approved.", tags=[], source="")

    # Approve row2
    store.approve(row2["id"])

    result = store.list_pending()
    valid_ids = [r["id"] for r in result["valid"]]

    assert row1["id"] in valid_ids
    assert row2["id"] not in valid_ids  # approved, not proposed


# ---------------------------------------------------------------------------
# 13. Approve and discard
# ---------------------------------------------------------------------------

def test_approve_and_discard(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)

    row1 = valid_propose(store, title="To Approve", angle="Angle A.", tags=[], source="")
    row2 = valid_propose(store, title="To Discard", angle="Angle B.", tags=[], source="")

    assert store.approve(row1["id"]) is True
    assert store.discard(row2["id"]) is True

    all_rows = {r["id"]: r for r in store.list_all()}
    assert all_rows[row1["id"]]["status"] == "approved"
    assert all_rows[row2["id"]]["status"] == "discarded"


# ---------------------------------------------------------------------------
# 14. Approve already-discarded returns False
# ---------------------------------------------------------------------------

def test_approve_already_discarded_returns_false(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)

    row = valid_propose(store, title="Discard First", angle="Angle.", tags=[], source="")
    store.discard(row["id"])

    result = store.approve(row["id"])
    assert result is False


# ---------------------------------------------------------------------------
# 15a. Blank title rejected at propose (write-time)
# ---------------------------------------------------------------------------

def test_propose_blank_title_rejected(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    for blank in ("", "   ", "\t\n"):
        with pytest.raises(TopicInboxValidationError):
            store.propose(title=blank, angle="Valid angle.", tags=[], source="")


# ---------------------------------------------------------------------------
# 15b. Blank title injected directly lands in invalid bucket (read-time)
# ---------------------------------------------------------------------------

def test_blank_title_injected_directly_lands_in_invalid(tmp_path: Path) -> None:
    store, db = make_store(tmp_path)
    valid_propose(store)  # ensure table exists

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    blank_id = "ti_" + "00" * 16
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO topic_inbox (id, title, angle, tags, source, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'proposed', ?, ?)""",
            (blank_id, "   ", "Angle.", "[]", "attacker", now, now),
        )

    result = store.list_pending()
    assert blank_id in [r["id"] for r in result["invalid"]]
    assert blank_id not in [r["id"] for r in result["valid"]]


# ---------------------------------------------------------------------------
# 15c. Non-string tag rejected at propose with the domain error, not TypeError
# ---------------------------------------------------------------------------

def test_propose_non_string_tag_rejected(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    with pytest.raises(TopicInboxValidationError):
        store.propose(title="Valid title", angle="Valid angle.", tags=[123], source="")


# ---------------------------------------------------------------------------
# 15d. Hostile types injected directly must not break list_pending
# ---------------------------------------------------------------------------

def test_hostile_typed_rows_injected_directly_do_not_break_list_pending(tmp_path: Path) -> None:
    """SQLite TEXT affinity converts ints to text on direct INSERT, but BLOBs
    survive as bytes. Rows with non-string-element tags or BLOB title/angle
    must land in 'invalid' — never raise (fail-open)."""
    store, db = make_store(tmp_path)
    valid_propose(store)  # ensure table exists

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO topic_inbox (id, title, angle, tags, source, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'proposed', ?, ?)""",
            ("ti_inttags", "Valid looking title", "Angle.", "[1, 2, 3]", "attacker", now, now),
        )
        conn.execute(
            """INSERT INTO topic_inbox (id, title, angle, tags, source, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'proposed', ?, ?)""",
            ("ti_blobtitle", sqlite3.Binary(b"\x00evil"), sqlite3.Binary(b"\x01"), "[]", "attacker", now, now),
        )

    result = store.list_pending()  # must not raise

    invalid_ids = [r["id"] for r in result["invalid"]]
    valid_ids = [r["id"] for r in result["valid"]]
    assert "ti_inttags" in invalid_ids
    assert "ti_blobtitle" in invalid_ids
    assert "ti_inttags" not in valid_ids
    assert "ti_blobtitle" not in valid_ids
    # The legit row still surfaces
    assert len(valid_ids) == 1


# ---------------------------------------------------------------------------
# 15. Fail-open: corrupt DB returns empty dict
# ---------------------------------------------------------------------------

def test_fail_open_corrupt_db(tmp_path: Path) -> None:
    """list_pending() on a non-sqlite file must return empty buckets without raising."""
    bad_db = str(tmp_path / "corrupt.db")
    # Write garbage bytes — not a valid SQLite file
    Path(bad_db).write_bytes(b"this is not sqlite\x00\x01\x02")

    store = TopicInboxStore(bad_db)
    result = store.list_pending()

    assert result == {"valid": [], "invalid": []}
