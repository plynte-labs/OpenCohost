"""Editorial cards CLI — human and agent interface for creating/managing Kira cue cards.

Runnable as::

    python -m opencohost.editorial_cli [--db PATH] [--json] <subcommand> ...

Exit codes:
  0  success
  1  validation error / not found
  2  usage error (argparse handles these)

Every subcommand accepts ``--db <path>`` to override the default DB location.
The ``--json`` flag (placed before the subcommand) switches all output to
machine-readable JSON; errors are also written as JSON to stderr.

This module MUST NOT import any opencohost.ui or customtkinter code.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Default DB path — mirrors app_shell.py resolution exactly
# ---------------------------------------------------------------------------

def _default_db_path() -> str:
    """Return the canonical editorial cards DB path used by the running app.

    Resolution mirrors opencohost/config/settings.py EDITORIAL_CARDS_DB:
      USER_DATA_DIR / "data" / "editorial_cards" / "cards.db"

    USER_DATA_DIR is the repo root in dev mode; %APPDATA%/OpenCohost when frozen.
    """
    # Import lazily so the module can still be used even if config is unavailable
    try:
        from opencohost.config.settings import EDITORIAL_CARDS_DB  # type: ignore[import]

        return EDITORIAL_CARDS_DB
    except Exception:
        # Fallback: resolve relative to this file (opencohost/editorial_cli.py →
        # opencohost → repo root → data/editorial_cards/cards.db)
        repo_root = Path(__file__).resolve().parent.parent
        return str(repo_root / "data" / "editorial_cards" / "cards.db")


# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

def _print_json(data: Any, *, file: Any = None) -> None:
    print(json.dumps(data, ensure_ascii=False, default=str), file=file or sys.stdout)


def _error(message: str, *, use_json: bool) -> None:
    if use_json:
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
    else:
        print(f"error: {message}", file=sys.stderr)


def _card_summary(card: Any) -> dict[str, Any]:
    """Compact dict for list output."""
    return {
        "id": card.id,
        "topic": card.topic,
        "topic_slug": card.topic_slug,
        "status": card.status.value,
        "updated_at": _format_dt(card.updated_at),
        "use_count": card.use_count,
    }


def _card_full(card: Any) -> dict[str, Any]:
    """Full dict for show output."""
    return {
        "id": card.id,
        "topic": card.topic,
        "topic_slug": card.topic_slug,
        "status": card.status.value,
        "summary": card.summary,
        "streamer_take": card.streamer_take,
        "counterpoints": card.counterpoints,
        "discussion_hooks": card.discussion_hooks,
        "triggers": card.triggers,
        "created_at": _format_dt(card.created_at),
        "updated_at": _format_dt(card.updated_at),
        "expires_at": _format_dt(card.expires_at),
        "last_used_at": _format_dt(card.last_used_at),
        "use_count": card.use_count,
    }


def _format_dt(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _cmd_create(args: argparse.Namespace, use_json: bool) -> int:
    from opencohost.core.editorial_cards import (
        EditorialCard,
        EditorialCardStore,
        EditorialCardValidationError,
    )

    store = EditorialCardStore(args.db)

    if args.from_json:
        # Read from stdin (agent-friendly pipe mode)
        try:
            raw = sys.stdin.read()
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            _error(f"invalid JSON on stdin: {exc}", use_json=use_json)
            return 1

        topic = data.get("topic", "")
        summary = data.get("summary", "")
        streamer_take = data.get("streamer_take", "")
        counterpoints = data.get("counterpoints") or []
        discussion_hooks = data.get("discussion_hooks") or []
        triggers = data.get("triggers") or []
    else:
        topic = args.topic or ""
        summary = args.summary or ""
        streamer_take = args.take or ""
        counterpoints = args.counterpoint or []
        discussion_hooks = args.hook or []
        triggers = args.trigger or []

    # Parse optional --expires date
    expires_at = None
    if getattr(args, "expires", None):
        from datetime import date as _date
        try:
            try:
                d = _date.fromisoformat(args.expires)
                expires_at = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
            except ValueError:
                expires_at = datetime.fromisoformat(args.expires)
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            _error(f"invalid date: {args.expires!r}", use_json=use_json)
            return 1
        if expires_at <= datetime.now(timezone.utc):
            _error(f"expires date is in the past: {args.expires!r}", use_json=use_json)
            return 1

    try:
        card = store.upsert(
            EditorialCard(
                topic=topic,
                summary=summary,
                streamer_take=streamer_take,
                counterpoints=counterpoints,
                discussion_hooks=discussion_hooks,
                triggers=triggers,
                expires_at=expires_at,
            )
        )
    except EditorialCardValidationError as exc:
        _error(str(exc), use_json=use_json)
        return 1

    if use_json:
        _print_json(_card_full(card))
    else:
        print(f"{card.id} {card.status.value}")
    return 0


def _cmd_list(args: argparse.Namespace, use_json: bool) -> int:
    from opencohost.core.editorial_cards import EditorialCardStore

    store = EditorialCardStore(args.db)
    cards = store.list_all()

    if use_json:
        _print_json([_card_summary(c) for c in cards])
    else:
        if not cards:
            print("(no cards)")
        else:
            header = f"{'ID':<36}  {'STATUS':<8}  {'UPDATED':<25}  {'USES':>4}  TOPIC"
            print(header)
            print("-" * len(header))
            for c in cards:
                updated = _format_dt(c.updated_at) or ""
                print(f"{c.id:<36}  {c.status.value:<8}  {updated:<25}  {c.use_count:>4}  {c.topic}")
    return 0


def _cmd_show(args: argparse.Namespace, use_json: bool) -> int:
    from opencohost.core.editorial_cards import EditorialCardStore

    store = EditorialCardStore(args.db)
    card = store.get(args.card_id)
    if card is None:
        _error(f"card not found: {args.card_id}", use_json=use_json)
        return 1

    if use_json:
        _print_json(_card_full(card))
    else:
        print(f"id:            {card.id}")
        print(f"topic:         {card.topic}")
        print(f"topic_slug:    {card.topic_slug}")
        print(f"status:        {card.status.value}")
        print(f"summary:       {card.summary}")
        print(f"streamer_take: {card.streamer_take}")
        if card.counterpoints:
            print("counterpoints:")
            for item in card.counterpoints:
                print(f"  - {item}")
        if card.discussion_hooks:
            print("discussion_hooks:")
            for item in card.discussion_hooks:
                print(f"  - {item}")
        if card.triggers:
            print("triggers:")
            for item in card.triggers:
                print(f"  - {item}")
        print(f"created_at:    {_format_dt(card.created_at)}")
        print(f"updated_at:    {_format_dt(card.updated_at)}")
        print(f"use_count:     {card.use_count}")
    return 0


def _cmd_arm(args: argparse.Namespace, use_json: bool) -> int:
    from opencohost.core.editorial_cards import EditorialCardStore

    store = EditorialCardStore(args.db)
    result = store.arm(args.card_id)
    if not result:
        _error(
            f"could not arm card {args.card_id!r}: card missing, already consumed, or expired",
            use_json=use_json,
        )
        return 1

    if use_json:
        _print_json({"id": args.card_id, "status": "armed"})
    else:
        print(f"{args.card_id} armed")
    return 0


def _cmd_link(args: argparse.Namespace, use_json: bool) -> int:
    """Activate an armed card and attach it to a topic by topic_id (slug).

    The bridge's link_card_to_topic requires a KiraAgendaController which is
    owned by the UI.  The CLI replicates the underlying store calls directly
    so no UI context is needed:
      1. Verify the card is ARMED and not expired.
      2. Call store.activate_for_topic(topic_slug) — this sets status ACTIVE.
         The topic_id argument is used as the topic_slug (the Agenda topic id
         is its slug in the in-process controller, but the CLI only needs the
         store link which is slug-based).
    """
    from opencohost.core.editorial_cards import EditorialCardStatus, EditorialCardStore

    store = EditorialCardStore(args.db)
    card = store.get(args.card_id)
    if card is None:
        _error(f"card not found: {args.card_id}", use_json=use_json)
        return 1
    if card.status is not EditorialCardStatus.ARMED or card.is_expired():
        _error(
            f"card {args.card_id!r} is not in ARMED state (current: {card.status.value})",
            use_json=use_json,
        )
        return 1

    # Validate that the caller-supplied topic_id matches the card's own slug.
    # activate_for_topic is slug-based, so a mismatched topic_id would silently
    # activate the wrong card from the caller's perspective.
    if args.topic_id != card.topic_slug:
        _error(
            f"topic_id {args.topic_id!r} does not match card's topic slug {card.topic_slug!r}",
            use_json=use_json,
        )
        return 1

    active = store.activate_for_topic(card.topic_slug)
    if active is None:
        _error(
            "could not activate card: another card may already be ACTIVE",
            use_json=use_json,
        )
        return 1

    if use_json:
        _print_json({"id": active.id, "topic_id": args.topic_id, "status": "active"})
    else:
        print(f"{active.id} linked to topic {args.topic_id!r} (status: active)")
    return 0


def _cmd_rearm(args: argparse.Namespace, use_json: bool) -> int:
    from opencohost.core.editorial_cards import EditorialCardStatus, EditorialCardStore

    store = EditorialCardStore(args.db)
    card = store.get(args.card_id)
    if card is None:
        _error(f"card not found: {args.card_id}", use_json=use_json)
        return 1
    # Expired cards without --clear-expiry: warn the operator
    if card.status is EditorialCardStatus.EXPIRED and not args.clear_expiry:
        _error(
            f"card {args.card_id!r} is EXPIRED; use --clear-expiry to also null expires_at, "
            "otherwise the card will remain expired next time it is checked",
            use_json=use_json,
        )
        return 1
    result = store.rearm(args.card_id, clear_expiry=args.clear_expiry)
    if not result:
        _error(
            f"could not rearm card {args.card_id!r}: card missing or status not USED/EXPIRED",
            use_json=use_json,
        )
        return 1
    if use_json:
        _print_json({"id": args.card_id, "status": "armed"})
    else:
        print(f"{args.card_id} re-armed")
    return 0


def _cmd_disable(args: argparse.Namespace, use_json: bool) -> int:
    from opencohost.core.editorial_cards import EditorialCardStatus, EditorialCardStore

    store = EditorialCardStore(args.db)
    card = store.get(args.card_id)
    if card is None:
        _error(f"card not found: {args.card_id}", use_json=use_json)
        return 1
    if card.status is EditorialCardStatus.USED:
        _error(
            f"card {args.card_id!r} is USED and cannot be disabled; history is preserved",
            use_json=use_json,
        )
        return 1
    store.disable(args.card_id)
    if use_json:
        _print_json({"id": args.card_id, "status": "expired"})
    else:
        print(f"{args.card_id} disabled (expired)")
    return 0


def _cmd_delete(args: argparse.Namespace, use_json: bool) -> int:
    from opencohost.core.editorial_cards import EditorialCardStore

    store = EditorialCardStore(args.db)
    result = store.delete(args.card_id)
    if not result:
        _error(f"card not found: {args.card_id}", use_json=use_json)
        return 1
    if use_json:
        _print_json({"id": args.card_id, "deleted": True})
    else:
        print(f"{args.card_id} deleted")
    return 0


# ---------------------------------------------------------------------------
# Topic inbox subcommand handlers
# ---------------------------------------------------------------------------

def _cmd_topic(args: argparse.Namespace, use_json: bool) -> int:
    """Dispatch `topic <action>` to the matching handler."""
    handlers = {
        "propose": _cmd_topic_propose,
        "list": _cmd_topic_list,
        "discard": _cmd_topic_discard,
        "approve": _cmd_topic_approve,
    }
    handler = handlers.get(getattr(args, "topic_action", None) or "")
    if handler is None:
        _error("usage: topic {propose,list,discard,approve} ...", use_json=use_json)
        return 2
    return handler(args, use_json)


def _cmd_topic_propose(args: argparse.Namespace, use_json: bool) -> int:
    from opencohost.core.topic_inbox import (
        TopicInboxCapError,
        TopicInboxStore,
        TopicInboxValidationError,
    )

    if args.from_json:
        try:
            data = json.loads(sys.stdin.read())
        except json.JSONDecodeError as exc:
            _error(f"invalid JSON on stdin: {exc}", use_json=use_json)
            return 1
        title = data.get("title", "")
        angle = data.get("angle", "")
        tags = data.get("tags") or []
        source = data.get("source", "")
    else:
        title = args.title or ""
        angle = args.angle or ""
        tags = args.tag or []
        source = args.source or ""

    store = TopicInboxStore(args.db)
    try:
        row = store.propose(title=title, angle=angle, tags=tags, source=source)
    except (TopicInboxValidationError, TopicInboxCapError) as exc:
        _error(str(exc), use_json=use_json)
        return 1

    if use_json:
        _print_json(row)
    else:
        print(f"{row['id']} {row['status']}")
    return 0


def _cmd_topic_list(args: argparse.Namespace, use_json: bool) -> int:
    from opencohost.core.topic_inbox import TopicInboxStore

    store = TopicInboxStore(args.db)
    result = store.list_pending()

    if use_json:
        _print_json(result)
        return 0

    valid = result["valid"]
    invalid = result["invalid"]
    if not valid and not invalid:
        print("(no pending topic proposals)")
        return 0
    for row in valid:
        tags = ", ".join(row["tags"]) if row["tags"] else "-"
        print(f"{row['id']}  [{row['source'] or 'unknown'}]  {row['title']}")
        print(f"    angle: {row['angle']}")
        print(f"    tags:  {tags}")
    if invalid:
        print(f"({len(invalid)} invalid row(s) hidden — failed read-time validation; "
              "use --json for details)")
    return 0


def _cmd_topic_discard(args: argparse.Namespace, use_json: bool) -> int:
    from opencohost.core.topic_inbox import TopicInboxStore

    store = TopicInboxStore(args.db)
    if not store.discard(args.topic_id):
        _error(f"topic not found or not proposed: {args.topic_id}", use_json=use_json)
        return 1
    if use_json:
        _print_json({"id": args.topic_id, "status": "discarded"})
    else:
        print(f"{args.topic_id} discarded")
    return 0


def _cmd_topic_approve(args: argparse.Namespace, use_json: bool) -> int:
    """Always refused: approval is a human-only gate in the app UI."""
    _error(
        "topic approval is human-only and not available from the CLI: open the "
        "OpenCohost app and approve the proposal from the agenda suggestions "
        "panel, where the operator can read the title and angle first",
        use_json=use_json,
    )
    return 1


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m opencohost.editorial_cli",
        description=(
            "Manage Kira editorial cue cards. "
            "Use --json before the subcommand for machine-readable output."
        ),
    )
    parser.add_argument(
        "--db",
        metavar="PATH",
        default=None,
        help="Path to the editorial cards SQLite database (default: app data dir)",
    )
    parser.add_argument(
        "--json",
        dest="json_output",
        action="store_true",
        default=False,
        help="Emit machine-readable JSON output; errors are also JSON on stderr",
    )

    sub = parser.add_subparsers(dest="subcommand", metavar="SUBCOMMAND")
    sub.required = True

    # -- create ---------------------------------------------------------------
    create_p = sub.add_parser("create", help="Create or update a cue card")
    create_p.add_argument("--from-json", action="store_true", dest="from_json",
                          help="Read card fields from a JSON object on stdin")
    create_p.add_argument("--topic", metavar="TEXT", help="Topic title (max 120 chars)")
    create_p.add_argument("--summary", metavar="TEXT", help="Summary (max 1200 chars)")
    create_p.add_argument("--take", metavar="TEXT", dest="take",
                          help="Streamer take / angle (max 800 chars)")
    create_p.add_argument("--counterpoint", metavar="TEXT", action="append",
                          dest="counterpoint", default=[],
                          help="Add a counterpoint (repeatable, max 8)")
    create_p.add_argument("--hook", metavar="TEXT", action="append",
                          dest="hook", default=[],
                          help="Add a discussion hook (repeatable, max 8)")
    create_p.add_argument("--trigger", metavar="TEXT", action="append",
                          dest="trigger", default=[],
                          help="Add a trigger keyword (repeatable, max 8)")
    create_p.add_argument("--expires", metavar="DATE", dest="expires",
                          help="Set expiry date (ISO 8601 or YYYY-MM-DD). Past dates are rejected.")

    # -- list -----------------------------------------------------------------
    list_p = sub.add_parser("list", help="List all cards")
    list_p.add_argument("--json", dest="json_sub", action="store_true", default=False,
                        help="JSON output")

    # -- show -----------------------------------------------------------------
    show_p = sub.add_parser("show", help="Show full details of a card")
    show_p.add_argument("card_id", metavar="CARD_ID")
    show_p.add_argument("--json", dest="json_sub", action="store_true", default=False,
                        help="JSON output")

    # -- arm ------------------------------------------------------------------
    arm_p = sub.add_parser("arm", help="Move a DRAFT card to ARMED state")
    arm_p.add_argument("card_id", metavar="CARD_ID")

    # -- link -----------------------------------------------------------------
    link_p = sub.add_parser(
        "link",
        help="Activate an ARMED card and attach it to an agenda topic",
    )
    link_p.add_argument("topic_id", metavar="TOPIC_ID",
                        help="Agenda topic identifier (slug)")
    link_p.add_argument("card_id", metavar="CARD_ID")

    # -- rearm ----------------------------------------------------------------
    rearm_p = sub.add_parser(
        "rearm",
        help="Move a USED or EXPIRED card back to ARMED state",
    )
    rearm_p.add_argument("card_id", metavar="CARD_ID")
    rearm_p.add_argument(
        "--clear-expiry",
        action="store_true",
        dest="clear_expiry",
        default=False,
        help="Also null out expires_at so the card does not immediately re-expire",
    )

    # -- disable --------------------------------------------------------------
    disable_p = sub.add_parser(
        "disable",
        help="Move any non-USED card to EXPIRED (idempotent on already-expired cards)",
    )
    disable_p.add_argument("card_id", metavar="CARD_ID")

    # -- delete ---------------------------------------------------------------
    delete_p = sub.add_parser(
        "delete",
        help="Hard-delete a card and its ratings",
    )
    delete_p.add_argument("card_id", metavar="CARD_ID")

    # -- topic (inbox) ---------------------------------------------------------
    topic_p = sub.add_parser(
        "topic",
        help="Topic inbox: agents propose stream topics; humans approve in the app UI",
    )
    topic_sub = topic_p.add_subparsers(dest="topic_action", metavar="ACTION")
    topic_sub.required = True

    propose_p = topic_sub.add_parser("propose", help="Propose a topic for human review")
    propose_p.add_argument("--from-json", action="store_true", dest="from_json",
                           help="Read topic fields from a JSON object on stdin")
    propose_p.add_argument("--title", metavar="TEXT", help="Topic title (max 120 chars)")
    propose_p.add_argument("--angle", metavar="TEXT", help="Suggested angle (max 600 chars)")
    propose_p.add_argument("--tag", metavar="TEXT", action="append", dest="tag",
                           default=[], help="Add a tag (repeatable, max 8)")
    propose_p.add_argument("--source", metavar="TEXT", default="",
                           help="Identifier of the proposing agent")

    list_p2 = topic_sub.add_parser("list", help="List pending proposals (valid/invalid buckets)")
    list_p2.add_argument("--json", dest="json_sub", action="store_true", default=False,
                         help="JSON output")

    discard_p = topic_sub.add_parser("discard", help="Discard a pending proposal")
    discard_p.add_argument("topic_id", metavar="TOPIC_ID")

    approve_p = topic_sub.add_parser(
        "approve",
        help="Refused: approval is human-only, in the app UI",
    )
    approve_p.add_argument("topic_id", metavar="TOPIC_ID")

    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    """Parse argv and dispatch to the appropriate subcommand handler.

    Returns an integer exit code (0 success, 1 error, 2 usage error).
    """
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Resolve DB path
    if args.db is None:
        args.db = _default_db_path()

    # Merge global --json with any subcommand-level --json flag.
    # The subparser defines it as json_sub to avoid argparse's subparser-defaults
    # overwriting the parent's json_output when the global flag is placed before
    # the subcommand name.
    use_json: bool = getattr(args, "json_output", False) or getattr(args, "json_sub", False)

    dispatch = {
        "create": _cmd_create,
        "list": _cmd_list,
        "show": _cmd_show,
        "arm": _cmd_arm,
        "link": _cmd_link,
        "rearm": _cmd_rearm,
        "disable": _cmd_disable,
        "delete": _cmd_delete,
        "topic": _cmd_topic,
    }

    handler = dispatch.get(args.subcommand)
    if handler is None:
        parser.print_help(sys.stderr)
        return 2

    return handler(args, use_json)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
