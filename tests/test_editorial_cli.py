"""Tests for the editorial cards CLI (opencohost.editorial_cli).

All tests invoke the CLI in-process via main(argv) with a --db pointing at a
tmp_path SQLite file.  No UI modules are imported here; the CLI is stdlib-only.
"""

from __future__ import annotations

import io
import json
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


# ---------------------------------------------------------------------------
# Happy-path: create → list → show → arm
# ---------------------------------------------------------------------------

def test_create_list_show_arm_happy_path(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")

    # create — output is "<card_id> <status>"
    code, create_out, err = run([
        "--db", db, "create",
        "--topic", "Game Monetization",
        "--summary", "The community criticizes the new pricing model.",
        "--take", "I want to debate whether this crosses pay-to-win.",
        "--counterpoint", "The devs say all items are cosmetic.",
        "--hook", "Where is the fair line?",
        "--trigger", "monetization",
    ])
    assert code == 0, f"create failed: {err}"
    assert "ec_" in create_out

    # parse the card id from the create output (first token of first line)
    card_id = create_out.strip().splitlines()[0].split()[0]

    # list
    code, out, err = run(["--db", db, "list"])
    assert code == 0, f"list failed: {err}"
    assert "Game Monetization" in out

    # show
    code, out, err = run(["--db", db, "show", card_id])
    assert code == 0, f"show failed: {err}"
    assert "pay-to-win" in out
    assert "cosmetic" in out

    # arm
    code, out, err = run(["--db", db, "arm", card_id])
    assert code == 0, f"arm failed: {err}"
    assert "armed" in out.lower()


# ---------------------------------------------------------------------------
# --json flag: list and show return valid JSON
# ---------------------------------------------------------------------------

def test_list_json_output_is_valid(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    run(["--db", db, "create",
         "--topic", "JSON Test Topic",
         "--summary", "Summary for JSON test.",
         "--take", "My take on JSON output."])

    code, out, err = run(["--db", db, "list", "--json"])
    assert code == 0
    data = json.loads(out)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["topic"] == "JSON Test Topic"


def test_show_json_output_is_valid(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    code, out, _ = run(["--db", db, "create",
                         "--topic", "Show JSON",
                         "--summary", "Summary for show JSON test.",
                         "--take", "Take for show JSON."])
    card_id = out.strip().split()[0]

    code, out, err = run(["--db", db, "show", card_id, "--json"])
    assert code == 0
    data = json.loads(out)
    assert data["id"] == card_id
    assert data["topic"] == "Show JSON"


# ---------------------------------------------------------------------------
# create --from-json via stdin
# ---------------------------------------------------------------------------

def test_create_from_json_stdin(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    payload = json.dumps({
        "topic": "Stdin JSON Card",
        "summary": "Created via stdin JSON pipe.",
        "streamer_take": "My take from JSON input.",
        "counterpoints": ["One counterpoint."],
    })

    # --from-json reads from stdin; the "-" is omitted because --from-json already
    # signals "read from stdin" and argparse would interpret bare "-" as an error.
    code, out, err = run(["--db", db, "create", "--from-json"], stdin_text=payload)
    assert code == 0, f"create --from-json failed: {err}"
    assert "ec_" in out

    # confirm it appears in list
    code2, out2, _ = run(["--db", db, "list", "--json"])
    data = json.loads(out2)
    assert any(c["topic"] == "Stdin JSON Card" for c in data)


# ---------------------------------------------------------------------------
# Validation error → exit 1 + stderr message
# ---------------------------------------------------------------------------

def test_validation_error_exits_1_with_stderr_message(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    code, out, err = run([
        "--db", db, "create",
        "--topic", "",          # empty topic — validation must fail
        "--summary", "Some summary.",
        "--take", "Some take.",
    ])
    assert code == 1
    assert err.strip() != ""  # something printed to stderr


def test_validation_error_with_json_flag_prints_json_error(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    # Pass a topic that exceeds 120 chars
    long_topic = "X" * 121
    code, out, err = run([
        "--db", db, "--json", "create",
        "--topic", long_topic,
        "--summary", "Some summary.",
        "--take", "Some take.",
    ])
    assert code == 1
    err_data = json.loads(err)
    assert "error" in err_data


# ---------------------------------------------------------------------------
# arm on missing card → exit 1
# ---------------------------------------------------------------------------

def test_arm_missing_card_exits_1(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    code, out, err = run(["--db", db, "arm", "ec_doesnotexist"])
    assert code == 1


def test_arm_used_card_exits_1(tmp_path: Path) -> None:
    """Arming a USED card returns False from the store, so the CLI exits 1."""
    from opencohost.core.editorial_cards import EditorialCardStore

    db = str(tmp_path / "cards.db")
    run(["--db", db, "create",
         "--topic", "Used Card Arm",
         "--summary", "Summary for used card arm test.",
         "--take", "Take for used card arm."])

    code, out, _ = run(["--db", db, "list", "--json"])
    cards = json.loads(out)
    card_id = cards[0]["id"]

    # arm it
    run(["--db", db, "arm", card_id])

    # manually mark as USED via the store to bypass the CLI (CLI has no 'use' command)
    store = EditorialCardStore(db)
    store.mark_used(card_id)

    # arm a USED card → store.arm returns False → exit 1
    code, _, _ = run(["--db", db, "arm", card_id])
    assert code == 1


# ---------------------------------------------------------------------------
# list on empty DB
# ---------------------------------------------------------------------------

def test_list_empty_db_exits_0(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    code, out, err = run(["--db", db, "list"])
    assert code == 0


def test_list_empty_db_json_returns_empty_list(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    code, out, _ = run(["--db", db, "list", "--json"])
    assert code == 0
    assert json.loads(out) == []


# ---------------------------------------------------------------------------
# link subcommand
# ---------------------------------------------------------------------------

def test_link_armed_card_to_topic_succeeds(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    # create + arm
    run(["--db", db, "create",
         "--topic", "Link Test",
         "--summary", "Summary for link test.",
         "--take", "Take for link test."])
    code, out, _ = run(["--db", db, "list", "--json"])
    card_id = json.loads(out)[0]["id"]
    run(["--db", db, "arm", card_id])

    # link: topic_slug derived from topic = "link-test"
    code, out, err = run(["--db", db, "link", "link-test", card_id])
    assert code == 0, f"link failed: {err}"


def test_link_unarmed_card_exits_1(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    run(["--db", db, "create",
         "--topic", "Unarmed Link",
         "--summary", "Summary for unarmed link test.",
         "--take", "Take for unarmed link."])
    code, out, _ = run(["--db", db, "list", "--json"])
    card_id = json.loads(out)[0]["id"]

    # link without arming first — should fail
    code, out, err = run(["--db", db, "link", "unarmed-link", card_id])
    assert code == 1


def test_link_wrong_topic_id_exits_1(tmp_path: Path) -> None:
    """link with a topic_id that doesn't match the card's topic slug must exit 1
    and leave the card in ARMED state (not activate it)."""
    db = str(tmp_path / "cards.db")
    run(["--db", db, "create",
         "--topic", "Correct Topic",
         "--summary", "Summary.",
         "--take", "Take."])
    code, out, _ = run(["--db", db, "list", "--json"])
    card_id = json.loads(out)[0]["id"]

    # Arm the card so it is eligible for link
    run(["--db", db, "arm", card_id])

    # link with the WRONG topic_id — must fail
    code, out, err = run(["--db", db, "link", "wrong-topic", card_id])
    assert code == 1, f"Expected exit 1, got {code}"

    # Error message must mention both the supplied topic_id and the card's slug
    assert "wrong-topic" in err, f"Error missing supplied topic_id: {err}"
    assert "correct-topic" in err, f"Error missing card slug: {err}"

    # Card must NOT have been activated — show must report armed
    code2, out2, _ = run(["--db", db, "show", card_id, "--json"])
    assert code2 == 0
    card_data = json.loads(out2)
    assert card_data["status"] == "armed", (
        f"Card should still be armed after mismatch, got: {card_data['status']}"
    )
