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


# ---------------------------------------------------------------------------
# Slice 3: create --expires flag
# ---------------------------------------------------------------------------

def test_create_with_future_expires_succeeds(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    from datetime import date, timedelta
    future_date = (date.today() + timedelta(days=30)).isoformat()

    code, out, err = run([
        "--db", db, "create",
        "--topic", "Expires Future Card",
        "--summary", "Summary for expiry test.",
        "--take", "Take for expiry test.",
        "--expires", future_date,
    ])
    assert code == 0, f"create --expires failed: {err}"
    assert "ec_" in out


def test_create_with_future_expires_json_has_expires_at(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    from datetime import date, timedelta
    future_date = (date.today() + timedelta(days=30)).isoformat()

    run([
        "--db", db, "create",
        "--topic", "Expires Future JSON",
        "--summary", "Summary for expiry JSON test.",
        "--take", "Take for expiry JSON test.",
        "--expires", future_date,
    ])
    code, out, _ = run(["--db", db, "list", "--json"])
    data = json.loads(out)
    assert len(data) == 1
    # expires_at is set via show
    card_id = data[0]["id"]
    code2, out2, _ = run(["--db", db, "show", card_id, "--json"])
    card = json.loads(out2)
    assert card["expires_at"] is not None


def test_create_with_past_expires_exits_1(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    code, out, err = run([
        "--db", db, "create",
        "--topic", "Expires Past Card",
        "--summary", "Summary for past expiry test.",
        "--take", "Take for past expiry test.",
        "--expires", "2000-01-01",
    ])
    assert code == 1
    assert "past" in err.lower() or "expires" in err.lower()


def test_create_with_invalid_expires_exits_1(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    code, out, err = run([
        "--db", db, "create",
        "--topic", "Invalid Expires Card",
        "--summary", "Summary for invalid expiry.",
        "--take", "Take for invalid expiry.",
        "--expires", "not-a-date",
    ])
    assert code == 1
    assert err.strip() != ""


# ---------------------------------------------------------------------------
# Slice 3: rearm subcommand
# ---------------------------------------------------------------------------

def _create_used_card(db: str, topic: str = "Rearm Test Card") -> str:
    """Create, arm, activate, and mark-used a card. Returns card_id."""
    from opencohost.core.editorial_cards import EditorialCardStore
    run(["--db", db, "create",
         "--topic", topic,
         "--summary", f"Summary for {topic}.",
         "--take", f"Take for {topic}."])
    code, out, _ = run(["--db", db, "list", "--json"])
    card_id = json.loads(out)[0]["id"]
    run(["--db", db, "arm", card_id])
    # Activate + mark used via store
    store = EditorialCardStore(db)
    slug = topic.lower().replace(" ", "-")
    store.activate_for_topic(slug)
    store.mark_used(card_id)
    return card_id


def test_rearm_used_card_succeeds(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    card_id = _create_used_card(db)

    code, out, err = run(["--db", db, "rearm", card_id])
    assert code == 0, f"rearm failed: {err}"
    assert "armed" in out.lower()


def test_rearm_used_card_json_output(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    card_id = _create_used_card(db, "Rearm JSON Card")

    code, out, err = run(["--db", db, "--json", "rearm", card_id])
    assert code == 0, f"rearm failed: {err}"
    data = json.loads(out)
    assert data["id"] == card_id
    assert data["status"] == "armed"


def test_rearm_expired_card_needs_clear_expiry_flag(tmp_path: Path) -> None:
    """Rearming an expired card without --clear-expiry should exit 1 with helpful message."""
    db = str(tmp_path / "cards.db")
    run(["--db", db, "create",
         "--topic", "Expired Rearm Card",
         "--summary", "Summary for expired rearm.",
         "--take", "Take for expired rearm."])
    code, out, _ = run(["--db", db, "list", "--json"])
    card_id = json.loads(out)[0]["id"]

    from opencohost.core.editorial_cards import EditorialCardStore
    with EditorialCardStore(db)._connect() as conn:
        conn.execute(
            "UPDATE editorial_cards SET status = 'expired' WHERE id = ?",
            (card_id,),
        )

    code, out, err = run(["--db", db, "rearm", card_id])
    assert code == 1
    assert "clear-expiry" in err.lower() or "EXPIRED" in err


def test_rearm_with_clear_expiry_flag_succeeds(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    run(["--db", db, "create",
         "--topic", "Clear Expiry Rearm",
         "--summary", "Summary for clear expiry.",
         "--take", "Take for clear expiry."])
    code, out, _ = run(["--db", db, "list", "--json"])
    card_id = json.loads(out)[0]["id"]

    from opencohost.core.editorial_cards import EditorialCardStore
    with EditorialCardStore(db)._connect() as conn:
        conn.execute(
            "UPDATE editorial_cards SET status = 'expired', expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (card_id,),
        )

    code, out, err = run(["--db", db, "rearm", "--clear-expiry", card_id])
    assert code == 0, f"rearm --clear-expiry failed: {err}"
    assert "armed" in out.lower()


def test_rearm_missing_card_exits_1(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    code, out, err = run(["--db", db, "rearm", "ec_doesnotexist"])
    assert code == 1


def test_rearm_draft_card_exits_1(tmp_path: Path) -> None:
    """Rearming a DRAFT card must fail (only USED/EXPIRED are eligible)."""
    db = str(tmp_path / "cards.db")
    run(["--db", db, "create",
         "--topic", "Draft Rearm Fail",
         "--summary", "Summary for draft rearm.",
         "--take", "Take for draft rearm."])
    code, out, _ = run(["--db", db, "list", "--json"])
    card_id = json.loads(out)[0]["id"]

    code, _, err = run(["--db", db, "rearm", card_id])
    assert code == 1


# ---------------------------------------------------------------------------
# Slice 3: disable subcommand
# ---------------------------------------------------------------------------

def test_disable_armed_card_succeeds(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    run(["--db", db, "create",
         "--topic", "Disable Armed",
         "--summary", "Summary for disable armed.",
         "--take", "Take for disable armed."])
    code, out, _ = run(["--db", db, "list", "--json"])
    card_id = json.loads(out)[0]["id"]
    run(["--db", db, "arm", card_id])

    code, out, err = run(["--db", db, "disable", card_id])
    assert code == 0, f"disable failed: {err}"
    assert "expired" in out.lower() or "disabled" in out.lower()


def test_disable_json_output(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    run(["--db", db, "create",
         "--topic", "Disable JSON Test",
         "--summary", "Summary for disable JSON.",
         "--take", "Take for disable JSON."])
    code, out, _ = run(["--db", db, "list", "--json"])
    card_id = json.loads(out)[0]["id"]

    code, out, err = run(["--db", db, "--json", "disable", card_id])
    assert code == 0, f"disable --json failed: {err}"
    data = json.loads(out)
    assert data["id"] == card_id
    assert data["status"] == "expired"


def test_disable_used_card_exits_1(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    card_id = _create_used_card(db, "Disable Used Test")
    code, out, err = run(["--db", db, "disable", card_id])
    assert code == 1


def test_disable_missing_card_exits_1(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    code, out, err = run(["--db", db, "disable", "ec_doesnotexist"])
    assert code == 1


# ---------------------------------------------------------------------------
# Slice 3: delete subcommand
# ---------------------------------------------------------------------------

def test_delete_card_succeeds(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    run(["--db", db, "create",
         "--topic", "Delete Test Card",
         "--summary", "Summary for delete test.",
         "--take", "Take for delete test."])
    code, out, _ = run(["--db", db, "list", "--json"])
    card_id = json.loads(out)[0]["id"]

    code, out, err = run(["--db", db, "delete", card_id])
    assert code == 0, f"delete failed: {err}"
    assert "deleted" in out.lower() or card_id in out


def test_delete_json_output(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    run(["--db", db, "create",
         "--topic", "Delete JSON Card",
         "--summary", "Summary for delete JSON test.",
         "--take", "Take for delete JSON test."])
    code, out, _ = run(["--db", db, "list", "--json"])
    card_id = json.loads(out)[0]["id"]

    code, out, err = run(["--db", db, "--json", "delete", card_id])
    assert code == 0
    data = json.loads(out)
    assert data["id"] == card_id
    assert data["deleted"] is True


def test_delete_missing_card_exits_1(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    code, out, err = run(["--db", db, "delete", "ec_doesnotexist"])
    assert code == 1


def test_delete_removed_card_no_longer_in_list(tmp_path: Path) -> None:
    db = str(tmp_path / "cards.db")
    run(["--db", db, "create",
         "--topic", "Gone Card",
         "--summary", "Summary for gone card.",
         "--take", "Take for gone card."])
    code, out, _ = run(["--db", db, "list", "--json"])
    card_id = json.loads(out)[0]["id"]

    run(["--db", db, "delete", card_id])

    code2, out2, _ = run(["--db", db, "list", "--json"])
    data = json.loads(out2)
    assert not any(c["id"] == card_id for c in data)
