"""Centralized physical SQLite sanitization and cascading profile purge coordinator.

Coordinates physical deletions across all memory SQLite stores (memorias.db,
memoria_promotion_backoff.db, Memory v5 ShadowStore + SemanticCacheStore) and
removes stale cleanup backups so deleted profile data cannot survive in
orphaned rows, SQLite freelist pages, WAL files, or backup snapshots.

Logs carry metadata/counts/exception types only — never profile names or memory text.
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import closing, suppress
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_CLEANUP_MANIFEST_NAME = "memoria_cleanup_manifest.json"
_WRITE_TIMEOUT_SECONDS = 1.0


def _normalize_keys(keys: Iterable[str | None]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for raw in keys:
        if raw is None:
            continue
        val = str(raw).strip()
        if val and val not in seen:
            seen.add(val)
            out.append(val)
    return out


def enable_secure_delete(conn: sqlite3.Connection) -> None:
    """Ask SQLite to overwrite freed pages with zeroes on delete (best-effort)."""
    with suppress(sqlite3.Error):
        conn.execute("PRAGMA secure_delete = ON")


def sanitize_sqlite_db(db_path: str | Path, timeout: float = _WRITE_TIMEOUT_SECONDS) -> bool:
    """Checkpoint WAL and VACUUM *db_path* so deleted plaintext cannot linger on disk."""
    if not db_path:
        return False
    p = Path(db_path)
    if not p.is_file():
        return False
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(p), timeout=timeout, isolation_level=None)
        enable_secure_delete(conn)
        with suppress(sqlite3.Error):
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
        return True
    except (sqlite3.Error, OSError) as exc:
        logger.warning("sqlite sanitize failed (fail-open): %s", type(exc).__name__)
        return False
    finally:
        if conn is not None:
            with suppress(sqlite3.Error):
                conn.close()


def cleanup_memorias_backups(memorias_db_path: str | Path) -> int:
    """Remove stale cleanup backup DBs and manifests sibling to *memorias_db_path*."""
    if not memorias_db_path:
        return 0
    try:
        backups_dir = Path(memorias_db_path).resolve().parent / "backups"
    except OSError:
        return 0
    if not backups_dir.is_dir():
        return 0
    removed = 0
    try:
        for item in list(backups_dir.iterdir()):
            if not item.is_file():
                continue
            low = item.name.lower()
            is_backup_artifact = (
                item.suffix.lower() == ".db"
                or low.endswith(".db-wal")
                or low.endswith(".db-shm")
                or low.endswith(".db-journal")
                or low == _CLEANUP_MANIFEST_NAME
            )
            if is_backup_artifact:
                with suppress(OSError):
                    item.unlink()
                    removed += 1
    except OSError:
        pass
    return removed


def _table_exists(conn: sqlite3.Connection, table_name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table_name,),
    ).fetchone()
    return row is not None


def purge_memorias_keys(
    db_path: str | Path,
    keys: Iterable[str | None],
    timeout: float = _WRITE_TIMEOUT_SECONDS,
) -> int:
    """Delete all rows in `memorias` matching *keys*, clean attempts, and VACUUM."""
    clean_keys = _normalize_keys(keys)
    if not db_path or not clean_keys:
        return 0
    p = Path(db_path)
    if not p.is_file():
        return 0

    deleted = 0
    attempts_deleted = 0
    try:
        with closing(sqlite3.connect(str(p), timeout=timeout)) as conn, conn:
            conn.execute("PRAGMA foreign_keys = ON")
            enable_secure_delete(conn)
            if not _table_exists(conn, "memorias"):
                return 0
            for idx in range(0, len(clean_keys), 400):
                chunk = clean_keys[idx : idx + 400]
                marks = ",".join("?" for _ in chunk)
                cur = conn.execute(
                    f"DELETE FROM memorias WHERE profile_id IN ({marks})",
                    chunk,
                )
                deleted += max(0, cur.rowcount)
            if _table_exists(conn, "memoria_promotion_attempts"):
                cur_att = conn.execute(
                    "DELETE FROM memoria_promotion_attempts "
                    "WHERE memoria_id NOT IN (SELECT id FROM memorias)"
                )
                attempts_deleted += max(0, cur_att.rowcount)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("memorias purge failed (fail-open): %s", type(exc).__name__)
        return 0

    if deleted > 0 or attempts_deleted > 0:
        sanitize_sqlite_db(p, timeout=timeout)
    return deleted


def purge_backoff_keys(
    memorias_db_path: str | Path,
    keys: Iterable[str | None],
    timeout: float = _WRITE_TIMEOUT_SECONDS,
) -> int:
    """Delete matching *keys* from sibling `memoria_promotion_backoff.db` and VACUUM."""
    clean_keys = _normalize_keys(keys)
    if not memorias_db_path or not clean_keys:
        return 0
    try:
        backoff_path = Path(memorias_db_path).resolve().with_name("memoria_promotion_backoff.db")
    except OSError:
        return 0
    if not backoff_path.is_file():
        return 0

    deleted = 0
    try:
        with closing(sqlite3.connect(str(backoff_path), timeout=timeout)) as conn, conn:
            enable_secure_delete(conn)
            if not _table_exists(conn, "promotion_backoff"):
                return 0
            for idx in range(0, len(clean_keys), 400):
                chunk = clean_keys[idx : idx + 400]
                marks = ",".join("?" for _ in chunk)
                cur = conn.execute(
                    f"DELETE FROM promotion_backoff WHERE profile_id IN ({marks})",
                    chunk,
                )
                deleted += max(0, cur.rowcount)
    except (sqlite3.Error, OSError) as exc:
        logger.warning("backoff purge failed (fail-open): %s", type(exc).__name__)
        return 0

    if deleted > 0:
        sanitize_sqlite_db(backoff_path, timeout=timeout)
    return deleted


def purge_v5_profile_keys(
    v5_subsystem: Any,
    keys: Iterable[str | None],
    *,
    drop_privacy_fence: bool = False,
) -> bool | None:
    """Purge *keys* from Memory v5 and optionally drop their privacy fences."""
    if v5_subsystem is None:
        return None
    clean_keys = _normalize_keys(keys)
    if not clean_keys:
        return True
    all_ok = True
    for key in clean_keys:
        try:
            ok = bool(v5_subsystem.purge_profile(key))
            if not ok:
                all_ok = False
            if drop_privacy_fence and hasattr(v5_subsystem, "drop_profile_fence"):
                v5_subsystem.drop_profile_fence(key)
        except Exception as exc:
            logger.warning("memory_v5 purge failed exc=%s", type(exc).__name__)
            all_ok = False
    return all_ok


def purge_profile_cascade(
    *,
    profile_id: str,
    legacy_name: str | None = None,
    memorias_db: str | None = None,
    v5_subsystem: Any = None,
    memorias_enabled: bool = True,
    force_memorias_when_disabled: bool = False,
    drop_v5_privacy_fence: bool = False,
) -> dict[str, Any]:
    """Purge a profile (UUID + legacy name) across all memory stores and backups."""
    keys = _normalize_keys((profile_id, legacy_name))
    v5_purged = purge_v5_profile_keys(
        v5_subsystem,
        keys,
        drop_privacy_fence=drop_v5_privacy_fence,
    )
    deleted = 0
    if memorias_db:
        purge_backoff_keys(memorias_db, keys)
        cleanup_memorias_backups(memorias_db)
        if memorias_enabled or force_memorias_when_disabled:
            deleted = purge_memorias_keys(memorias_db, keys)
    return {"deleted": deleted, "v5_purged": v5_purged}


def _distinct_profile_ids_from_db(db_path: Path, table_name: str) -> set[str]:
    if not db_path.is_file():
        return set()
    try:
        with closing(sqlite3.connect(str(db_path), timeout=_WRITE_TIMEOUT_SECONDS)) as conn:
            if not _table_exists(conn, table_name):
                return set()
            rows = conn.execute(
                f"SELECT DISTINCT profile_id FROM {table_name} WHERE profile_id IS NOT NULL"
            ).fetchall()
            return {str(r[0]).strip() for r in rows if r and r[0] and str(r[0]).strip()}
    except (sqlite3.Error, OSError):
        return set()


def gc_orphan_profiles(
    *,
    active_profiles: dict[str, Any],
    memorias_db: str | None = None,
    v5_subsystem: Any = None,
) -> dict[str, int]:
    """Purge rows belonging to deleted/unknown profiles across all memory stores."""
    active_keys: set[str] = set()
    if isinstance(active_profiles, dict):
        for name, data in active_profiles.items():
            if isinstance(name, str) and name.strip():
                active_keys.add(name.strip())
            if isinstance(data, dict):
                pid = str(data.get("id") or "").strip()
                if pid:
                    active_keys.add(pid)

    # Fail-safe: never wipe stores when perfiles.json is empty or failed to load.
    if not active_keys:
        return {"memorias_deleted": 0, "backoff_deleted": 0, "v5_profiles_purged": 0}

    memorias_deleted = 0
    backoff_deleted = 0
    v5_profiles_purged = 0

    if memorias_db:
        mem_path = Path(memorias_db)
        if mem_path.is_file():
            mem_ids = _distinct_profile_ids_from_db(mem_path, "memorias")
            orphan_mem = [pid for pid in mem_ids if pid not in active_keys]
            if orphan_mem:
                memorias_deleted = purge_memorias_keys(mem_path, orphan_mem)
        try:
            backoff_path = mem_path.resolve().with_name("memoria_promotion_backoff.db")
        except OSError:
            backoff_path = None
        if backoff_path is not None and backoff_path.is_file():
            backoff_ids = _distinct_profile_ids_from_db(backoff_path, "promotion_backoff")
            orphan_backoff = [pid for pid in backoff_ids if pid not in active_keys]
            if orphan_backoff:
                backoff_deleted = purge_backoff_keys(memorias_db, orphan_backoff)
        cleanup_memorias_backups(memorias_db)

    if v5_subsystem is not None and hasattr(v5_subsystem, "list_distinct_profile_ids"):
        try:
            v5_ids = v5_subsystem.list_distinct_profile_ids()
            orphan_v5 = [pid for pid in v5_ids if pid and pid not in active_keys]
            if orphan_v5:
                purge_v5_profile_keys(v5_subsystem, orphan_v5, drop_privacy_fence=True)
                v5_profiles_purged = len(orphan_v5)
        except Exception as exc:
            logger.warning("memory_v5 orphan gc failed exc=%s", type(exc).__name__)

    return {
        "memorias_deleted": memorias_deleted,
        "backoff_deleted": backoff_deleted,
        "v5_profiles_purged": v5_profiles_purged,
    }
