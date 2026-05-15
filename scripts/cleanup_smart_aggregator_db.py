"""Clean Smart Aggregator raw chat history without touching compact Kira context.

Usage:
    python scripts/cleanup_smart_aggregator_db.py --dry-run
    python scripts/cleanup_smart_aggregator_db.py --execute

By default, this removes raw rows from `messages` and optionally vacuums the DB.
The new `context_snapshots` table is preserved because that is the useful
history: the compact context Kira actually used to speak.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "smart_aggregator" / "sessions.db"
DEFAULT_JSONL = ROOT / "data" / "smart_aggregator" / "chat_log.jsonl"


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    if not table_exists(conn, table):
        return 0
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def cleanup(db_path: Path, jsonl_path: Path, execute: bool, vacuum: bool) -> None:
    if not db_path.exists():
        print(f"DB no encontrada: {db_path}")
        return

    conn = sqlite3.connect(db_path)
    try:
        raw_before = count_rows(conn, "messages")
        snapshots_before = count_rows(conn, "context_snapshots")
        print(f"messages antes: {raw_before}")
        print(f"context_snapshots preservados: {snapshots_before}")

        if execute and table_exists(conn, "messages"):
            conn.execute("DELETE FROM messages")
            conn.commit()
            if vacuum:
                conn.execute("VACUUM")

        raw_after = count_rows(conn, "messages")
        print(f"messages despues: {raw_after}")
    finally:
        conn.close()

    if jsonl_path.exists():
        size = jsonl_path.stat().st_size
        if execute:
            jsonl_path.unlink()
            print(f"JSONL eliminado: {jsonl_path} ({size} bytes)")
        else:
            print(f"JSONL encontrado: {jsonl_path} ({size} bytes). Se elimina con --execute")


def main() -> None:
    parser = argparse.ArgumentParser(description="Limpia mensajes raw del Smart Aggregator.")
    parser.add_argument("--db", default=str(DEFAULT_DB), help="Ruta a sessions.db")
    parser.add_argument("--jsonl", default=str(DEFAULT_JSONL), help="Ruta a chat_log.jsonl")
    parser.add_argument("--execute", action="store_true", help="Aplicar limpieza. Sin esto es dry-run.")
    parser.add_argument("--no-vacuum", action="store_true", help="No compactar SQLite luego de borrar.")
    args = parser.parse_args()

    cleanup(
        Path(args.db),
        Path(args.jsonl),
        execute=bool(args.execute),
        vacuum=not bool(args.no_vacuum),
    )


if __name__ == "__main__":
    main()
