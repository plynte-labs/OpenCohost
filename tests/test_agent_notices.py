"""Strict-TDD tests for opencohost.core.agent_notices — AgentNoticeStore.

Phase 3 of track agent_context_gateway_20260705 (design.md 'POST
/api/agent/notice'). The store clones the TopicInboxStore shape:
connection-per-call, write-time AND read-time validation, bounded sqlite
timeouts, fail-open reads. Table ``agent_notices`` lives in the SAME SQLite
file as editorial_cards/topic_inbox (settings.EDITORIAL_CARDS_DB).

Contract under test:
- id = 'an_' + uuid, text <= 280 chars, source <= 80 chars,
  status proposed|dismissed, created_at/updated_at.
- Cap: 20 undismissed max — the 21st raises AgentNoticeCapError.
- Dedupe: identical (source, text) among undismissed returns the existing
  row (natural idempotency — repeat POSTs are safe, no header needed).
- Read-time validation quarantines hostile rows written directly via
  sqlite3 (mirrors tests/test_topic_inbox.py).

Run against a temporary SQLite DB (tmp_path). No UI modules are imported.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from opencohost.core.agent_notices import (
    ID_PREFIX,
    SOURCE_MAX,
    TEXT_MAX,
    UNDISMISSED_CAP,
    AgentNoticeCapError,
    AgentNoticeStore,
    AgentNoticeValidationError,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_store(tmp_path: Path) -> tuple[AgentNoticeStore, str]:
    db = str(tmp_path / "cards.db")
    return AgentNoticeStore(db), db


def inject_row(db: str, *values: object) -> None:
    """Direct sqlite INSERT bypassing propose() (attacker simulation)."""
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO agent_notices (id, text, source, status, created_at, updated_at)
               VALUES (?, ?, ?, 'proposed', ?, ?)""",
            values,
        )


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------

def test_propose_happy_path(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    row = store.propose("Research digest ready for tonight's stream", source="research-bot")

    assert row["id"].startswith(ID_PREFIX)
    assert row["status"] == "proposed"
    assert row["text"] == "Research digest ready for tonight's stream"
    assert row["source"] == "research-bot"
    assert "created_at" in row
    assert "updated_at" in row


# ---------------------------------------------------------------------------
# 2. Write-time validation
# ---------------------------------------------------------------------------

def test_propose_text_too_long(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    with pytest.raises(AgentNoticeValidationError):
        store.propose("A" * (TEXT_MAX + 1), source="bot")


def test_propose_blank_text_rejected(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    for blank in ("", "   ", "\t\n"):
        with pytest.raises(AgentNoticeValidationError):
            store.propose(blank, source="bot")


def test_propose_source_too_long(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    with pytest.raises(AgentNoticeValidationError):
        store.propose("A valid notice", source="S" * (SOURCE_MAX + 1))


def test_propose_non_string_source_rejected(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    with pytest.raises(AgentNoticeValidationError):
        store.propose("A valid notice", source=123)  # type: ignore[arg-type]


def test_propose_code_in_text_rejected(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    with pytest.raises(AgentNoticeValidationError):
        store.propose("look <script>alert(1)</script>", source="bot")


# ---------------------------------------------------------------------------
# 3. Dedupe: identical (source, text) among undismissed
# ---------------------------------------------------------------------------

def test_dedupe_identical_source_text_returns_existing_row(tmp_path: Path) -> None:
    store, db = make_store(tmp_path)
    row1 = store.propose("Same notice twice", source="bot-a")
    row2 = store.propose("Same notice twice", source="bot-a")

    assert row2["id"] == row1["id"]
    with sqlite3.connect(db) as conn:
        count = conn.execute("SELECT COUNT(*) FROM agent_notices").fetchone()[0]
    assert count == 1


def test_same_text_different_source_is_a_new_row(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    row_a = store.propose("Shared text", source="bot-a")
    row_b = store.propose("Shared text", source="bot-b")
    assert row_a["id"] != row_b["id"]


def test_dismissed_row_does_not_dedupe(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    row1 = store.propose("Notice to dismiss", source="bot")
    assert store.dismiss(row1["id"]) is True
    row2 = store.propose("Notice to dismiss", source="bot")
    assert row2["id"] != row1["id"]


# ---------------------------------------------------------------------------
# 4. Cap: 20 undismissed max
# ---------------------------------------------------------------------------

def test_cap_refuses_at_20_undismissed(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    for i in range(UNDISMISSED_CAP):
        store.propose(f"Unique notice number {i:03d}", source="cap-tester")
    with pytest.raises(AgentNoticeCapError):
        store.propose("One too many notices", source="cap-tester")


def test_dedupe_still_returns_existing_when_full(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    first = store.propose("Notice number 000 first in", source="cap-tester")
    for i in range(1, UNDISMISSED_CAP):
        store.propose(f"Unique notice number {i:03d}", source="cap-tester")
    # Board is full, but a repeat of an existing (source, text) must dedupe,
    # never raise the cap error.
    again = store.propose("Notice number 000 first in", source="cap-tester")
    assert again["id"] == first["id"]


def test_dismiss_frees_cap(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    rows = [
        store.propose(f"Unique notice number {i:03d}", source="cap-tester")
        for i in range(UNDISMISSED_CAP)
    ]
    assert store.dismiss(rows[0]["id"]) is True
    row = store.propose("Fits after a dismiss", source="cap-tester")
    assert row["status"] == "proposed"


# ---------------------------------------------------------------------------
# 5. Dismiss semantics
# ---------------------------------------------------------------------------

def test_dismiss_unknown_or_already_dismissed_returns_false(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    row = store.propose("Dismiss me once", source="bot")
    assert store.dismiss("an_" + "00" * 16) is False
    assert store.dismiss(row["id"]) is True
    assert store.dismiss(row["id"]) is False


def test_dismissed_rows_leave_list_pending(tmp_path: Path) -> None:
    store, _ = make_store(tmp_path)
    keep = store.propose("Keep this one", source="bot")
    hide = store.propose("Hide this one", source="bot")
    store.dismiss(hide["id"])
    valid_ids = [r["id"] for r in store.list_pending()["valid"]]
    assert keep["id"] in valid_ids
    assert hide["id"] not in valid_ids


# ---------------------------------------------------------------------------
# 6. Read-time validation: hostile rows injected directly via sqlite3
# ---------------------------------------------------------------------------

def test_code_text_injected_directly_lands_in_invalid(tmp_path: Path) -> None:
    store, db = make_store(tmp_path)
    store.propose("Legit notice", source="good-agent")  # ensure table exists

    evil_id = ID_PREFIX + "deadbeef" * 4
    inject_row(db, evil_id, "import os; os.system('rm -rf /')", "attacker", _now(), _now())

    result = store.list_pending()
    assert evil_id in [r["id"] for r in result["invalid"]]
    assert evil_id not in [r["id"] for r in result["valid"]]
    for row in result["invalid"]:
        assert "invalid_reason" in row


def test_oversized_text_injected_directly_lands_in_invalid(tmp_path: Path) -> None:
    store, db = make_store(tmp_path)
    store.propose("Legit notice", source="good-agent")

    evil_id = ID_PREFIX + "ab" * 16
    inject_row(db, evil_id, "S" * 100_000, "attacker", _now(), _now())

    assert evil_id in [r["id"] for r in store.list_pending()["invalid"]]


def test_hostile_typed_rows_do_not_break_list_pending(tmp_path: Path) -> None:
    """BLOB text/source survives direct INSERT as bytes; the row must land in
    'invalid' — never raise (fail-open, same as topic_inbox)."""
    store, db = make_store(tmp_path)
    store.propose("Legit notice", source="good-agent")

    inject_row(
        db,
        ID_PREFIX + "blobtext",
        sqlite3.Binary(b"\x00evil"),
        sqlite3.Binary(b"\x01"),
        _now(),
        _now(),
    )

    result = store.list_pending()  # must not raise
    assert (ID_PREFIX + "blobtext") in [r["id"] for r in result["invalid"]]
    assert len(result["valid"]) == 1


def test_hostile_blob_timestamp_rows_land_in_invalid(tmp_path: Path) -> None:
    """A BLOB created_at/updated_at survives a direct INSERT (TEXT affinity
    never coerces BLOBs); the row must land in 'invalid' — the API builds
    AgentNoticeOut(created_at=...) from every valid row, so one bytes
    timestamp would 500 the whole operator notice board."""
    store, db = make_store(tmp_path)
    legit = store.propose("Legit notice", source="good-agent")

    inject_row(
        db,
        ID_PREFIX + "blobcreated",
        "Looks totally legit",
        "attacker",
        sqlite3.Binary(b"\xff\xfe"),
        _now(),
    )
    inject_row(
        db,
        ID_PREFIX + "blobupdated",
        "Looks totally legit too",
        "attacker",
        _now(),
        sqlite3.Binary(b"\xff\xfe"),
    )

    result = store.list_pending()  # must not raise
    invalid_ids = [r["id"] for r in result["invalid"]]
    assert (ID_PREFIX + "blobcreated") in invalid_ids
    assert (ID_PREFIX + "blobupdated") in invalid_ids
    assert [r["id"] for r in result["valid"]] == [legit["id"]]


def test_foreign_namespace_and_bare_prefix_ids_land_in_invalid(tmp_path: Path) -> None:
    """Ids outside the an_ namespace (or the bare prefix) can only come from
    a direct DB write; they must never surface as dismissable."""
    store, db = make_store(tmp_path)
    store.propose("Legit notice", source="good-agent")

    inject_row(db, "evil-no-prefix", "Looks totally legit", "attacker", _now(), _now())
    inject_row(db, ID_PREFIX, "Bare prefix id", "attacker", _now(), _now())

    result = store.list_pending()
    invalid_ids = [r["id"] for r in result["invalid"]]
    valid_ids = [r["id"] for r in result["valid"]]
    assert "evil-no-prefix" in invalid_ids
    assert ID_PREFIX in invalid_ids
    assert "evil-no-prefix" not in valid_ids
    assert ID_PREFIX not in valid_ids


def test_fail_open_corrupt_db(tmp_path: Path) -> None:
    """list_pending() on a non-sqlite file returns empty buckets, never raises."""
    bad_db = str(tmp_path / "corrupt.db")
    Path(bad_db).write_bytes(b"this is not sqlite\x00\x01\x02")

    store = AgentNoticeStore(bad_db)
    assert store.list_pending() == {"valid": [], "invalid": []}


# ---------------------------------------------------------------------------
# 7. Bounded sqlite timeouts + connection hygiene (UI-thread protection)
# ---------------------------------------------------------------------------

def test_list_pending_uses_short_read_timeout(tmp_path: Path) -> None:
    from unittest.mock import patch

    from opencohost.core.agent_notices import READ_TIMEOUT_SECONDS

    store, _ = make_store(tmp_path)
    store.propose("Timeout probe", source="bot")

    with patch(
        "opencohost.core.agent_notices.sqlite3.connect", wraps=sqlite3.connect
    ) as connect:
        store.list_pending()

    assert connect.call_args.kwargs.get("timeout") == READ_TIMEOUT_SECONDS
    assert READ_TIMEOUT_SECONDS < 5.0


def test_dismiss_uses_short_write_timeout(tmp_path: Path) -> None:
    from unittest.mock import patch

    from opencohost.core.agent_notices import WRITE_TIMEOUT_SECONDS

    store, _ = make_store(tmp_path)
    row = store.propose("Write timeout probe", source="bot")

    with patch(
        "opencohost.core.agent_notices.sqlite3.connect", wraps=sqlite3.connect
    ) as connect:
        store.dismiss(row["id"])

    assert connect.call_args.kwargs.get("timeout") == WRITE_TIMEOUT_SECONDS
    assert WRITE_TIMEOUT_SECONDS < 5.0


def test_store_closes_sqlite_connections(tmp_path: Path) -> None:
    """`with sqlite3.connect(...)` only commits on exit; the store must close
    every connection (mirrors the topic_inbox polling-loop guarantee)."""
    from unittest.mock import patch

    store, _ = make_store(tmp_path)
    created: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def recording_connect(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        created.append(conn)
        return conn

    with patch(
        "opencohost.core.agent_notices.sqlite3.connect", side_effect=recording_connect
    ):
        row = store.propose("Close me", source="bot")
        store.list_pending()
        store.list_all()
        store.dismiss(row["id"])

    assert created, "test must capture real connections"
    for conn in created:
        with pytest.raises(sqlite3.ProgrammingError):
            conn.execute("SELECT 1")
