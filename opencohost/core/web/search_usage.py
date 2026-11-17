"""Local monthly usage guard for web search APIs (web-search-api-fallback-20261010 Phase 1, A1/A2/A3/A5).

SQLite-backed transactional ledger:
- Short BEGIN IMMEDIATE transactions with PRAGMA synchronous=FULL.
- Capped busy_timeout at min(50ms, budget).
- Admission checks consumed + reserved < cap atomically.
- Reservation lifecycle: RESERVED -> DISPATCHED (consumed + 1) or RELEASED (consumed 0).
- Idempotent dispatches and releases.
- Clock rollback protection fails closed with "clock_rollback".
- Forward billing period transitions preserve historical counts and start at 0.
- Legacy JSON migration imports once, validates schema strictly, preserves file,
  and fails closed with "usage_state" on malformed legacy files or corrupt DBs.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import uuid
from datetime import datetime
from typing import Callable, Optional

from opencohost.config import settings

logger = logging.getLogger("OpenCohost")

# Closed reasons
REASON_LOCAL_CAP = "local_cap"
REASON_USAGE_STATE = "usage_state"
REASON_CLOCK_ROLLBACK = "clock_rollback"
REASON_BUSY = "busy"
REASON_WORKER_UNAVAILABLE = "worker_unavailable"

CLOSED_REASONS = frozenset({
    REASON_LOCAL_CAP,
    REASON_USAGE_STATE,
    REASON_CLOCK_ROLLBACK,
    REASON_BUSY,
    REASON_WORKER_UNAVAILABLE,
})

# Reservation states
STATE_RESERVED = "RESERVED"
STATE_DISPATCHED = "DISPATCHED"
STATE_RELEASED = "RELEASED"

_PERIOD_REGEX = re.compile(r"^\d{4}-(?:0[1-9]|1[0-2])$")


class UsageStateError(Exception):
    """Raised when ledger or legacy usage state is corrupt, invalid, or malformed."""


class ClockRollbackError(Exception):
    """Raised when system period is earlier than the validated high-water mark."""


class ReservationResult:
    """Result of an atomic reservation admission attempt."""

    def __init__(self, ok: bool, reason: str = "", request_id: str = "") -> None:
        self.ok = ok
        self.reason = reason
        self.request_id = request_id

    def __bool__(self) -> bool:
        return self.ok

    def __iter__(self):
        yield self.ok
        yield self.reason

    def __repr__(self) -> str:
        return f"ReservationResult(ok={self.ok}, reason={self.reason!r}, request_id={self.request_id!r})"

    def __eq__(self, other: object) -> bool:
        if isinstance(other, tuple):
            return (self.ok, self.reason) == other
        if isinstance(other, ReservationResult):
            return (self.ok, self.reason, self.request_id) == (other.ok, other.reason, other.request_id)
        return False


def _current_month_str() -> str:
    return datetime.now().strftime("%Y-%m")


def _validate_legacy_json(file_path: str) -> tuple[bool, Optional[dict]]:
    """Validate legacy JSON format. Returns (is_valid, data_or_None).

    Valid JSON rules:
    - Must be a JSON object (dict).
    - Must contain 'month' matching 'YYYY-MM'.
    - Provider counts must be nonnegative integers (not booleans, not strings, not negatives).
    """
    if not file_path or not os.path.exists(file_path):
        return True, None

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False, None

    if not isinstance(data, dict):
        return False, None

    month = data.get("month")
    if not isinstance(month, str) or not _PERIOD_REGEX.match(month):
        return False, None

    for k, v in data.items():
        if k == "month":
            continue
        # In Python, bool is a subclass of int: reject bool explicitly
        if type(v) is not int or v < 0:
            return False, None

    return True, data


class SearchUsageTracker:
    """Transactional SQLite ledger for web search quota tracking and admission."""

    def __init__(
        self,
        *,
        db_path: Optional[str] = None,
        legacy_file_path: Optional[str] = None,
        file_path: Optional[str] = None,
        current_month: Optional[str] = None,
        month_resolver: Optional[Callable[[], str]] = None,
        owner_id: Optional[str] = None,
    ) -> None:
        if db_path is not None:
            self._db_path = db_path
            if legacy_file_path is not None:
                self._legacy_file_path = legacy_file_path
            else:
                self._legacy_file_path = os.path.splitext(db_path)[0] + ".json"
        elif file_path is not None:
            if file_path.endswith(".sqlite3") or file_path.endswith(".db"):
                self._db_path = file_path
                self._legacy_file_path = legacy_file_path or (os.path.splitext(file_path)[0] + ".json")
            elif file_path.endswith(".json"):
                self._legacy_file_path = legacy_file_path or file_path
                self._db_path = os.path.splitext(file_path)[0] + ".sqlite3"
            else:
                self._db_path = file_path
                self._legacy_file_path = legacy_file_path or ""
        else:
            self._db_path = getattr(settings, "WEB_SEARCH_USAGE_DB", "") or os.path.join(
                getattr(settings, "CONFIG_DIR", "."), "web_search_usage.sqlite3"
            )
            self._legacy_file_path = legacy_file_path if legacy_file_path is not None else getattr(
                settings, "WEB_SEARCH_USAGE_FILE", ""
            )

        self._explicit_month = current_month
        self._month_resolver = month_resolver
        self._owner_id = owner_id or str(os.getpid())
        self._lock = threading.Lock()

    def _get_month(self) -> str:
        if self._explicit_month is not None:
            return self._explicit_month
        if self._month_resolver is not None:
            return self._month_resolver()
        return _current_month_str()

    @staticmethod
    def _resolve_busy_timeout_ms(budget_ms: Optional[float] = None) -> int:
        if budget_ms is None:
            return 50
        if budget_ms <= 0:
            return 0
        return min(50, int(budget_ms))

    def _connect(self, timeout_ms: int = 50) -> sqlite3.Connection:
        if not self._db_path:
            raise UsageStateError("No database path configured")
        if self._db_path != ":memory:":
            target_dir = os.path.dirname(os.path.abspath(self._db_path))
            if target_dir:
                os.makedirs(target_dir, exist_ok=True)

        timeout_sec = max(0.0, timeout_ms / 1000.0)
        conn = sqlite3.connect(self._db_path, timeout=timeout_sec, isolation_level=None)
        conn.execute("PRAGMA synchronous = FULL;")
        conn.execute(f"PRAGMA busy_timeout = {timeout_ms};")
        return conn

    def _ensure_initialized(self, conn: sqlite3.Connection) -> None:
        """Create schema, run legacy migration, and check clock rollback within active transaction."""
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS period_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                high_water_period TEXT NOT NULL,
                legacy_migrated INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS period_usage (
                period TEXT NOT NULL,
                provider TEXT NOT NULL,
                consumed INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (period, provider)
            );
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reservations (
                request_id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                provider TEXT NOT NULL,
                period TEXT NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_reservations_admission ON reservations (period, provider, state);"
        )

        cur = conn.execute("SELECT high_water_period, legacy_migrated FROM period_state WHERE id = 1;")
        row = cur.fetchone()
        cur_period = self._get_month()
        now_iso = datetime.now().isoformat()

        if row is None:
            # First initialization: check migration from legacy JSON
            if not self._legacy_file_path or not os.path.exists(self._legacy_file_path):
                # Fresh install: start clean
                conn.execute(
                    "INSERT INTO period_state (id, high_water_period, legacy_migrated, updated_at) VALUES (1, ?, 1, ?);",
                    (cur_period, now_iso),
                )
            else:
                is_valid, data = _validate_legacy_json(self._legacy_file_path)
                if not is_valid:
                    logger.warning(
                        "[WEB] corrupt or malformed legacy usage file %s, failing closed with usage_state",
                        os.path.basename(self._legacy_file_path),
                    )
                    raise UsageStateError("Malformed legacy JSON")

                month = data.get("month", cur_period)
                for provider, count in data.items():
                    if provider != "month":
                        conn.execute(
                            "INSERT OR REPLACE INTO period_usage (period, provider, consumed) VALUES (?, ?, ?);",
                            (month, provider, count),
                        )
                if month > cur_period:
                    raise ClockRollbackError(
                        f"Clock rollback: current period {cur_period} is before legacy month {month}"
                    )
                high_water = cur_period
                conn.execute(
                    "INSERT INTO period_state (id, high_water_period, legacy_migrated, updated_at) VALUES (1, ?, 1, ?);",
                    (high_water, now_iso),
                )
        else:
            high_water = row[0]
            # Check clock rollback vs forward transition
            if cur_period < high_water:
                raise ClockRollbackError(
                    f"Clock rollback: current period {cur_period} is before high-water {high_water}"
                )
            elif cur_period > high_water:
                conn.execute(
                    "UPDATE period_state SET high_water_period = ?, updated_at = ? WHERE id = 1;",
                    (cur_period, now_iso),
                )

    def reserve(
        self,
        provider: str,
        cap: Optional[int] = None,
        request_id: Optional[str] = None,
        *,
        budget_ms: Optional[float] = None,
    ) -> ReservationResult:
        """Atomically check consumed + reserved < cap and record a RESERVED slot."""
        if cap is None:
            if provider == "tavily":
                cap = getattr(settings, "TAVILY_MONTHLY_CAP", 900)
            elif provider == "brave":
                cap = getattr(settings, "BRAVE_MONTHLY_CAP", 900)
            else:
                cap = 900

        req_id = request_id or uuid.uuid4().hex
        timeout_ms = self._resolve_busy_timeout_ms(budget_ms)

        with self._lock:
            try:
                conn = self._connect(timeout_ms=timeout_ms)
            except (UsageStateError, sqlite3.Error, OSError) as exc:
                logger.warning("[WEB] failed to connect to usage db %s (%s)", os.path.basename(self._db_path), type(exc).__name__)
                return ReservationResult(ok=False, reason=REASON_USAGE_STATE, request_id=req_id)

            try:
                conn.execute("BEGIN IMMEDIATE;")
                self._ensure_initialized(conn)

                # Check idempotency for existing request_id
                cur = conn.execute("SELECT state FROM reservations WHERE request_id = ?;", (req_id,))
                existing = cur.fetchone()
                if existing is not None:
                    state = existing[0]
                    if state == STATE_RESERVED:
                        conn.execute("COMMIT;")
                        return ReservationResult(ok=True, reason="", request_id=req_id)
                    conn.execute("ROLLBACK;")
                    return ReservationResult(ok=False, reason=REASON_USAGE_STATE, request_id=req_id)

                period = self._get_month()
                # Admission calculation: consumed + reserved < cap
                cur = conn.execute(
                    "SELECT consumed FROM period_usage WHERE period = ? AND provider = ?;",
                    (period, provider),
                )
                p_row = cur.fetchone()
                consumed = p_row[0] if p_row else 0

                cur = conn.execute(
                    "SELECT COUNT(*) FROM reservations WHERE period = ? AND provider = ? AND state = ?;",
                    (period, provider, STATE_RESERVED),
                )
                reserved = cur.fetchone()[0]

                if consumed + reserved >= cap:
                    conn.execute("COMMIT;")
                    return ReservationResult(ok=False, reason=REASON_LOCAL_CAP, request_id=req_id)

                now_iso = datetime.now().isoformat()
                conn.execute(
                    """
                    INSERT INTO reservations (request_id, owner_id, provider, period, state, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?);
                    """,
                    (req_id, self._owner_id, provider, period, STATE_RESERVED, now_iso, now_iso),
                )
                conn.execute("COMMIT;")
                return ReservationResult(ok=True, reason="", request_id=req_id)
            except ClockRollbackError as exc:
                logger.warning("[WEB] clock rollback denied API admission: %s", exc)
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                return ReservationResult(ok=False, reason=REASON_CLOCK_ROLLBACK, request_id=req_id)
            except (UsageStateError, sqlite3.Error, OSError) as exc:
                logger.warning("[WEB] search_usage error during reserve: %s (%s)", exc, type(exc).__name__)
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                return ReservationResult(ok=False, reason=REASON_USAGE_STATE, request_id=req_id)
            finally:
                conn.close()

    def mark_dispatched(self, request_id: str, *, budget_ms: Optional[float] = None) -> bool:
        """Transition reservation RESERVED -> DISPATCHED, incrementing consumed once.

        Idempotent: calling multiple times on an already dispatched reservation returns True without double-incrementing.
        Fails if reservation was already released or not found.
        """
        timeout_ms = self._resolve_busy_timeout_ms(budget_ms)
        with self._lock:
            try:
                conn = self._connect(timeout_ms=timeout_ms)
            except (UsageStateError, sqlite3.Error, OSError):
                return False

            try:
                conn.execute("BEGIN IMMEDIATE;")
                self._ensure_initialized(conn)

                cur = conn.execute(
                    "SELECT provider, period, state FROM reservations WHERE request_id = ?;",
                    (request_id,),
                )
                row = cur.fetchone()
                if not row:
                    conn.execute("ROLLBACK;")
                    return False

                provider, period, state = row
                if state == STATE_RELEASED:
                    conn.execute("ROLLBACK;")
                    return False
                if state == STATE_DISPATCHED:
                    conn.execute("COMMIT;")
                    return True
                if state == STATE_RESERVED:
                    now_iso = datetime.now().isoformat()
                    conn.execute(
                        "UPDATE reservations SET state = ?, updated_at = ? WHERE request_id = ?;",
                        (STATE_DISPATCHED, now_iso, request_id),
                    )
                    conn.execute(
                        """
                        INSERT INTO period_usage (period, provider, consumed) VALUES (?, ?, 1)
                        ON CONFLICT(period, provider) DO UPDATE SET consumed = consumed + 1;
                        """,
                        (period, provider),
                    )
                    conn.execute("COMMIT;")
                    return True

                conn.execute("ROLLBACK;")
                return False
            except Exception as exc:
                logger.warning("[WEB] error during mark_dispatched: %s (%s)", exc, type(exc).__name__)
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                return False
            finally:
                conn.close()

    def release(self, request_id: str, *, budget_ms: Optional[float] = None) -> bool:
        """Transition reservation RESERVED -> RELEASED, consuming zero calls.

        Idempotent: duplicate release on RELEASED returns True.
        Fails if reservation was already dispatched.
        """
        timeout_ms = self._resolve_busy_timeout_ms(budget_ms)
        with self._lock:
            try:
                conn = self._connect(timeout_ms=timeout_ms)
            except (UsageStateError, sqlite3.Error, OSError):
                return False

            try:
                conn.execute("BEGIN IMMEDIATE;")
                self._ensure_initialized(conn)

                cur = conn.execute(
                    "SELECT provider, period, state FROM reservations WHERE request_id = ?;",
                    (request_id,),
                )
                row = cur.fetchone()
                if not row:
                    conn.execute("ROLLBACK;")
                    return False

                provider, period, state = row
                if state == STATE_DISPATCHED:
                    conn.execute("ROLLBACK;")
                    return False
                if state == STATE_RELEASED:
                    conn.execute("COMMIT;")
                    return True
                if state == STATE_RESERVED:
                    now_iso = datetime.now().isoformat()
                    conn.execute(
                        "UPDATE reservations SET state = ?, updated_at = ? WHERE request_id = ?;",
                        (STATE_RELEASED, now_iso, request_id),
                    )
                    conn.execute("COMMIT;")
                    return True

                conn.execute("ROLLBACK;")
                return False
            except Exception as exc:
                logger.warning("[WEB] error during release: %s (%s)", exc, type(exc).__name__)
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                return False
            finally:
                conn.close()

    def get_count(self, provider: str) -> int:
        """Return consumed calls for the current period."""
        with self._lock:
            try:
                conn = self._connect()
            except (UsageStateError, sqlite3.Error, OSError):
                return 0

            try:
                conn.execute("BEGIN IMMEDIATE;")
                self._ensure_initialized(conn)
                period = self._get_month()
                cur = conn.execute(
                    "SELECT consumed FROM period_usage WHERE period = ? AND provider = ?;",
                    (period, provider),
                )
                row = cur.fetchone()
                conn.execute("COMMIT;")
                return row[0] if row else 0
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                return 0
            finally:
                conn.close()

    def get_consumed_for_period(self, provider: str, period: str) -> int:
        """Return consumed calls for an arbitrary calendar period."""
        with self._lock:
            try:
                conn = self._connect()
            except (UsageStateError, sqlite3.Error, OSError):
                return 0

            try:
                conn.execute("BEGIN IMMEDIATE;")
                self._ensure_initialized(conn)
                cur = conn.execute(
                    "SELECT consumed FROM period_usage WHERE period = ? AND provider = ?;",
                    (period, provider),
                )
                row = cur.fetchone()
                conn.execute("COMMIT;")
                return row[0] if row else 0
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                return 0
            finally:
                conn.close()

    def get_high_water_period(self) -> str:
        """Return current validated high-water period."""
        with self._lock:
            try:
                conn = self._connect()
            except (UsageStateError, sqlite3.Error, OSError):
                return ""

            try:
                conn.execute("BEGIN IMMEDIATE;")
                self._ensure_initialized(conn)
                cur = conn.execute("SELECT high_water_period FROM period_state WHERE id = 1;")
                row = cur.fetchone()
                conn.execute("COMMIT;")
                return row[0] if row else ""
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                return ""
            finally:
                conn.close()

    def is_legacy_migrated(self) -> bool:
        """Return True if legacy migration has succeeded."""
        with self._lock:
            try:
                conn = self._connect()
            except (UsageStateError, sqlite3.Error, OSError):
                return False

            try:
                conn.execute("BEGIN IMMEDIATE;")
                self._ensure_initialized(conn)
                cur = conn.execute("SELECT legacy_migrated FROM period_state WHERE id = 1;")
                row = cur.fetchone()
                conn.execute("COMMIT;")
                return bool(row[0]) if row else False
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                return False
            finally:
                conn.close()

    def is_capped(self, provider: str, cap: Optional[int] = None) -> bool:
        """Check whether provider is at or above cap (or fails closed)."""
        if cap is None:
            if provider == "tavily":
                cap = getattr(settings, "TAVILY_MONTHLY_CAP", 900)
            elif provider == "brave":
                cap = getattr(settings, "BRAVE_MONTHLY_CAP", 900)
            else:
                cap = 900

        with self._lock:
            try:
                conn = self._connect()
            except (UsageStateError, sqlite3.Error, OSError):
                return True

            try:
                conn.execute("BEGIN IMMEDIATE;")
                self._ensure_initialized(conn)
                period = self._get_month()
                cur = conn.execute(
                    "SELECT consumed FROM period_usage WHERE period = ? AND provider = ?;",
                    (period, provider),
                )
                p_row = cur.fetchone()
                consumed = p_row[0] if p_row else 0

                cur = conn.execute(
                    "SELECT COUNT(*) FROM reservations WHERE period = ? AND provider = ? AND state = ?;",
                    (period, provider, STATE_RESERVED),
                )
                reserved = cur.fetchone()[0]
                conn.execute("COMMIT;")
                return (consumed + reserved) >= cap
            except (ClockRollbackError, UsageStateError, sqlite3.Error):
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                return True
            except Exception:
                try:
                    conn.execute("ROLLBACK;")
                except Exception:
                    pass
                return True
            finally:
                conn.close()

    def record_call(self, provider: str) -> int:
        """Backwards compatibility helper: reserve and mark dispatched."""
        res = self.reserve(provider)
        if res.ok:
            self.mark_dispatched(res.request_id)
        return self.get_count(provider)

    def _set_consumed(self, provider: str, period: str, count: int) -> None:
        """Internal helper for test setups to seed consumed count."""
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            self._ensure_initialized(conn)
            conn.execute(
                """
                INSERT INTO period_usage (period, provider, consumed) VALUES (?, ?, ?)
                ON CONFLICT(period, provider) DO UPDATE SET consumed = ?;
                """,
                (period, provider, count, count),
            )
            conn.execute("COMMIT;")
        except Exception:
            try:
                conn.execute("ROLLBACK;")
            except Exception:
                pass
            raise
        finally:
            conn.close()


_DEFAULT_TRACKER: Optional[SearchUsageTracker] = None
_TRACKER_LOCK = threading.Lock()


def get_tracker() -> SearchUsageTracker:
    global _DEFAULT_TRACKER
    with _TRACKER_LOCK:
        if _DEFAULT_TRACKER is None:
            _DEFAULT_TRACKER = SearchUsageTracker()
        return _DEFAULT_TRACKER
