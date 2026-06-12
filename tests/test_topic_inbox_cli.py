"""Tests for the topic inbox CLI subcommands (opencohost.editorial_cli topic ...).

All tests invoke the CLI in-process via main(argv) with a --db pointing at a
tmp_path SQLite file.  No UI modules are imported here; the CLI is stdlib-only.

Contract under test (conductor/tracks/backlog_edgecases_20260612):
  - `topic propose/list/discard` available to agents (+ --from-json pipe mode)
  - `topic approve` is REFUSED with an explanation — approval is UI-only
"""

from __future__ import annotations

import io
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from opencohost.editorial_cli import main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(argv: list[str], *, stdin_text: str | None = None) -> tuple[int, str, str]:
    """Call main(argv) and capture stdout/stderr.  Returns (exit_code, out, err)."""
    buf_out = io.StringIO()
    buf_err = io.StringIO()

    if stdin_text is not None:
        fake_stdin = io.StringIO(stdin_text)
        old_stdin = sys.stdin
        sys.stdin = fake_stdin

    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = buf_out, buf_err
    try:
        exit_code = main(argv)
    except SystemExit as exc:
        exit_code = int(exc.code) if exc.code is not None else 0
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr
        if stdin_text is not None:
            sys.stdin = old_stdin

    return exit_code, buf_out.getvalue(), buf_err.getvalue()


def propose(db: str, *, title: str = "Retro Gaming Revival", angle: str = "Why the 90s aesthetic is back.", source: str = "research-bot") -> str:
    """Propose a topic and return its ti_ id."""
    code, out, err = run([
        "--db", db, "topic", "propose",
        "--title", title, "--angle", angle, "--source", source,
    ])
    assert code == 0, f"propose failed: {err}"
    return out.strip().splitlines()[0].split()[0]


# ---------------------------------------------------------------------------
# propose
# ---------------------------------------------------------------------------

def test_topic_propose_happy_path(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    code, out, err = run([
        "--db", db, "topic", "propose",
        "--title", "Speedrun Drama",
        "--angle", "What the latest cheating scandal says about leaderboards.",
        "--tag", "gaming", "--tag", "drama",
        "--source", "watch-bot",
    ])
    assert code == 0, f"propose failed: {err}"
    assert "ti_" in out
    assert "proposed" in out


def test_topic_propose_json_output(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    code, out, err = run([
        "--db", db, "--json", "topic", "propose",
        "--title", "AI Voice Ethics",
        "--angle", "Where is the line for cloned voices on stream?",
        "--source", "research-bot",
    ])
    assert code == 0, f"propose failed: {err}"
    data = json.loads(out)
    assert data["id"].startswith("ti_")
    assert data["status"] == "proposed"
    assert data["angle"].startswith("Where is the line")


def test_topic_propose_from_json_stdin(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    payload = json.dumps({
        "title": "Indie Game Pricing",
        "angle": "Are 30-dollar indies the new normal?",
        "tags": ["indie", "economics"],
        "source": "pipe-agent",
    })
    code, out, err = run(
        ["--db", db, "--json", "topic", "propose", "--from-json"],
        stdin_text=payload,
    )
    assert code == 0, f"propose --from-json failed: {err}"
    data = json.loads(out)
    assert data["title"] == "Indie Game Pricing"
    assert data["tags"] == ["indie", "economics"]


def test_topic_propose_invalid_json_stdin_fails(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    code, out, err = run(
        ["--db", db, "topic", "propose", "--from-json"],
        stdin_text="{not json",
    )
    assert code == 1
    assert "json" in err.lower()


def test_topic_propose_validation_error_exits_1(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    code, out, err = run([
        "--db", db, "topic", "propose",
        "--title", "import os; os.system('boom')",
        "--angle", "Valid angle.",
    ])
    assert code == 1
    assert "code" in err.lower() or "html" in err.lower()


def test_topic_propose_cap_error_exits_1(tmp_path: Path) -> None:
    from opencohost.core.topic_inbox import PENDING_CAP

    db = str(tmp_path / "cards.db")
    for i in range(PENDING_CAP):
        propose(db, title=f"Unique Topic Number {i:03d}", angle=f"Angle {i}.")

    code, out, err = run([
        "--db", db, "topic", "propose",
        "--title", "One Too Many", "--angle", "Should be refused.",
    ])
    assert code == 1
    assert "full" in err.lower() or str(PENDING_CAP) in err


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------

def test_topic_list_text_shows_title_and_angle(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    propose(db, title="Cozy Games Boom", angle="Why low-stakes games dominate twitch.")

    code, out, err = run(["--db", db, "topic", "list"])
    assert code == 0, f"list failed: {err}"
    assert "Cozy Games Boom" in out
    # The angle must be visible — operators approve based on it
    assert "low-stakes" in out


def test_topic_list_json_buckets_valid_and_invalid(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    propose(db, title="Legit Topic", angle="Legit angle.")

    # Attacker writes a hostile row directly to SQLite, bypassing the CLI
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute(
            """INSERT INTO topic_inbox (id, title, angle, tags, source, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 'proposed', ?, ?)""",
            ("ti_evil", "<script>alert(1)</script>", "x", "[]", "attacker", now, now),
        )

    code, out, err = run(["--db", db, "--json", "topic", "list"])
    assert code == 0, f"list failed: {err}"
    data = json.loads(out)
    assert any(r["title"] == "Legit Topic" for r in data["valid"])
    assert any(r["id"] == "ti_evil" for r in data["invalid"])
    assert all(r["id"] != "ti_evil" for r in data["valid"])


def test_topic_list_empty(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    code, out, err = run(["--db", db, "topic", "list"])
    assert code == 0
    assert "no" in out.lower()


# ---------------------------------------------------------------------------
# discard
# ---------------------------------------------------------------------------

def test_topic_discard_happy_path(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    topic_id = propose(db)

    code, out, err = run(["--db", db, "topic", "discard", topic_id])
    assert code == 0, f"discard failed: {err}"

    # No longer listed as pending
    code, out, err = run(["--db", db, "--json", "topic", "list"])
    data = json.loads(out)
    assert all(r["id"] != topic_id for r in data["valid"])


def test_topic_discard_unknown_id_exits_1(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    propose(db)  # ensure table exists
    code, out, err = run(["--db", db, "topic", "discard", "ti_nope"])
    assert code == 1
    assert "ti_nope" in err


# ---------------------------------------------------------------------------
# approve — always refused (human-only gate)
# ---------------------------------------------------------------------------

def test_topic_approve_is_refused_with_explanation(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    topic_id = propose(db)

    code, out, err = run(["--db", db, "topic", "approve", topic_id])
    assert code == 1
    # The refusal must explain WHERE approval happens
    assert "ui" in err.lower() or "app" in err.lower()

    # Status must be unchanged — still proposed
    code, out, err = run(["--db", db, "--json", "topic", "list"])
    data = json.loads(out)
    assert any(r["id"] == topic_id for r in data["valid"])


def test_topic_approve_refused_in_json_mode(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    topic_id = propose(db)

    code, out, err = run(["--db", db, "--json", "topic", "approve", topic_id])
    assert code == 1
    payload = json.loads(err)
    assert "error" in payload
