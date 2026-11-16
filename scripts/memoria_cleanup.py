"""Owner-run, reversible cleanup of poisoned memorias drafts.

memory-grounding-and-recall-budget (owner decision M3, 2026-10-16). On 2026-10-16
Kira confabulated a Tesla reply and two drafts built from that reply were stored.
This tool deactivates them (inactive=1, which removes them from injection and
from the promotion sweep) and lists the other drafts from the same window for
owner review. It NEVER deletes a row.

Run it with the app CLOSED, against the live DB, after reading the dry run:

    python scripts/memoria_cleanup.py --db data/memorias/memorias.db            # dry run (default, read-only)
    python scripts/memoria_cleanup.py --db data/memorias/memorias.db --apply    # backup, then mute
    python scripts/memoria_cleanup.py --db data/memorias/memorias.db --revert   # restore the saved prior state

``--apply`` mutes like the app's own mute (``set_flags``): inactive=1 and
judged_at cleared, so a later capture cannot read the stamp as "the judge hid
this" and silently un-hide the row. It first records each row's prior
``inactive`` and ``judged_at`` in ``memoria_cleanup_manifest.json`` next to the
backup; ``--revert`` restores exactly those values (never a blanket inactive=0).

``--ids a,b`` adds ids to the default poisoned pair (apply) or the revert set.
``--show-content`` prints the review rows' text; off by default so the terminal
and scrollback do not carry memory content unless the owner asks.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

POISONED_IDS = (
    "mem_af6598d79d6b4f0a8211cc9c82d54cd9",
    "mem_2e44c69428dd4eee8e0567e2b8081287",
)
# Web-answer drafts the owner wants to review (UTC, ISO strings compare lexically).
REVIEW_FROM = "2026-10-17T02:39:00"
REVIEW_TO = "2026-10-17T03:00:00"
KIRA_MARK = "→ Kira:"


def _connect(path: Path, read_only: bool) -> sqlite3.Connection:
    if read_only:
        return sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5.0)
    return sqlite3.connect(str(path), timeout=5.0)


def backup(db: Path, backup_dir: Path) -> Path:
    """Consistent copy (sqlite backup API, WAL-safe) into *backup_dir*."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = backup_dir / f"memorias-{stamp}.db"
    src = sqlite3.connect(str(db), timeout=5.0)
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return target


def find_rows(conn: sqlite3.Connection, ids) -> dict:
    ids = list(ids)
    if not ids:
        return {}
    marks = ",".join("?" for _ in ids)
    cur = conn.execute(
        f"SELECT id, status, inactive, created_at, length(content) FROM memorias WHERE id IN ({marks})",
        ids,
    )
    return {r[0]: r for r in cur.fetchall()}


def review_rows(conn: sqlite3.Connection, exclude) -> list:
    cur = conn.execute(
        "SELECT id, created_at, length(content), instr(content, ?) > 0, content "
        "FROM memorias WHERE status = 'draft' AND created_at >= ? AND created_at < ? "
        "ORDER BY created_at, id",
        (KIRA_MARK, REVIEW_FROM, REVIEW_TO),
    )
    return [r for r in cur.fetchall() if r[0] not in set(exclude)]


MANIFEST_NAME = "memoria_cleanup_manifest.json"


def prior_state(conn: sqlite3.Connection, ids) -> dict:
    """{id: {"inactive": int, "judged_at": str}} as stored right now."""
    ids = list(ids)
    marks = ",".join("?" for _ in ids)
    cur = conn.execute(
        f"SELECT id, inactive, judged_at FROM memorias WHERE id IN ({marks})", ids)
    return {r[0]: {"inactive": int(r[1]), "judged_at": r[2] or ""} for r in cur.fetchall()}


def load_manifest(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_manifest(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def mute(conn: sqlite3.Connection, ids) -> int:
    """Mute like MemoriaStore.set_flags(inactive=True): hide AND clear judged_at."""
    ids = list(ids)
    marks = ",".join("?" for _ in ids)
    cur = conn.execute(
        f"UPDATE memorias SET inactive = 1, judged_at = '' WHERE id IN ({marks})", ids)
    conn.commit()
    return cur.rowcount


def restore(conn: sqlite3.Connection, saved: dict) -> int:
    """Put every saved row back to its recorded inactive/judged_at, in one commit."""
    rows = [(v["inactive"], v["judged_at"], i) for i, v in saved.items()]
    cur = conn.executemany(
        "UPDATE memorias SET inactive = ?, judged_at = ? WHERE id = ?", rows)
    conn.commit()
    return cur.rowcount


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", required=True, help="path to memorias.db (the app must be closed)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true", help="backup, then set inactive=1")
    mode.add_argument("--revert", action="store_true", help="set inactive=0 for the same ids")
    ap.add_argument("--ids", default="", help="comma-separated extra ids")
    ap.add_argument("--backup-dir", default="", help="default: <db dir>/backups")
    ap.add_argument("--show-content", action="store_true", help="print review rows' text")
    args = ap.parse_args(argv)

    db = Path(args.db)
    if not db.is_file():
        print(f"error: {db} does not exist (nothing was created)")
        return 2
    extra = [i.strip() for i in args.ids.split(",") if i.strip()]
    ids = list(dict.fromkeys([*POISONED_IDS, *extra]))
    writing = args.apply or args.revert

    conn = _connect(db, read_only=not writing)
    try:
        found = find_rows(conn, ids)
        missing = [i for i in ids if i not in found]
        print(f"target ids ({len(ids)}):")
        for i in ids:
            row = found.get(i)
            state = "MISSING" if row is None else f"status={row[1]} inactive={row[2]} created={row[3]} chars={row[4]}"
            print(f"  {i}  {state}")
        review = review_rows(conn, ids)
        print(f"\nreview list: {len(review)} draft(s) created {REVIEW_FROM}..{REVIEW_TO} UTC (not touched)")
        for rid, created, chars, has_kira, content in review:
            line = f"  {rid}  {created}  chars={chars}  kira_side={'yes' if has_kira else 'no'}"
            if args.show_content:
                line += f"\n      {content}"
            print(line)
        if missing and writing:
            print(f"\nerror: unknown id(s): {', '.join(missing)}; nothing changed")
            return 3
        if not writing:
            print("\ndry run: nothing changed. Re-run with --apply (or --revert) with the app closed.")
            return 0
        backup_dir = Path(args.backup_dir) if args.backup_dir else db.parent / "backups"
        manifest_path = backup_dir / MANIFEST_NAME
        manifest = load_manifest(manifest_path)
        if args.revert:
            saved = {i: manifest[i] for i in ids if i in manifest}
            unsaved = [i for i in ids if i not in saved]
            if unsaved:
                print(f"\nerror: no saved prior state for: {', '.join(unsaved)}; nothing changed")
                return 4
        target = backup(db, backup_dir)
        print(f"\nbackup written: {target}")
        if args.apply:
            # Keep the FIRST recorded prior state: a second apply would
            # otherwise record the already-muted values as "prior".
            for rid, state in prior_state(conn, ids).items():
                manifest.setdefault(rid, state)
            save_manifest(manifest_path, manifest)
            changed = mute(conn, ids)
            print(f"muted {changed} row(s) (inactive=1, judged_at cleared). Nothing was deleted.")
            print(f"prior state saved: {manifest_path}")
        else:
            changed = restore(conn, saved)
            for rid in saved:
                manifest.pop(rid, None)
            save_manifest(manifest_path, manifest)
            print(f"restored the saved prior state on {changed} row(s). Nothing was deleted.")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
