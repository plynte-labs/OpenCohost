"""Persistent per-profile infrastructure backoff for memoria promotion."""

from __future__ import annotations

import logging
import re
import sqlite3
import threading
import time
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger(__name__)

PROFILE_BACKOFF_DELAYS_S = (60, 300, 1800, 7200, 21600)
_MAX_FAILURE_CLASS_CHARS = 48
_SAFE_FAILURE_CLASS_RE = re.compile(r"^[a-z0-9_]+$")
_SIDECAR_TIMEOUT_SECONDS = 0.25


@dataclass(frozen=True)
class PromotionBackoffState:
    """One profile's current infrastructure retry deadline."""

    failure_count: int
    last_failure_at_s: int
    next_attempt_at_s: int
    last_failure_class: str
    persisted: bool


class PromotionBackoffStore:
    """SQLite sidecar with a conservative process-local failure fallback."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self._fallback_lock = threading.Lock()
        self._fallback: dict[str, PromotionBackoffState] = {}
        try:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()
        except (OSError, sqlite3.Error) as exc:
            logger.warning(
                "memoria promotion backoff init failed: %s",
                type(exc).__name__,
            )

    def get_state(self, profile_id: str) -> PromotionBackoffState | None:
        """Return persisted state, conservatively merged with local fallback."""
        profile_id = self._profile_id(profile_id)
        try:
            persisted = self._read_persisted(profile_id)
        except sqlite3.Error as exc:
            self._warn("read", exc)
            persisted = None
        with self._fallback_lock:
            fallback = self._fallback.get(profile_id)
        if persisted is None:
            return fallback
        if fallback is None:
            return persisted
        if fallback.next_attempt_at_s >= persisted.next_attempt_at_s:
            return fallback
        return persisted

    def is_active(self, profile_id: str, now_s: int) -> bool:
        """Check whether the profile deadline is active without incrementing."""
        profile_id = self._profile_id(profile_id)
        now_s = self._epoch(now_s)
        read_failed = False
        try:
            persisted = self._read_persisted(profile_id)
        except sqlite3.Error as exc:
            self._warn("read", exc)
            persisted = None
            read_failed = True

        with self._fallback_lock:
            fallback = self._fallback.get(profile_id)
            if read_failed and fallback is None:
                fallback = self._fallback_state(
                    now_s, "sidecar_unavailable",
                )
                self._fallback[profile_id] = fallback

            states = [state for state in (persisted, fallback) if state]
            state = max(
                states,
                key=lambda item: item.next_attempt_at_s,
                default=None,
            )
            active = bool(
                state
                and state.last_failure_at_s <= now_s
                and now_s < state.next_attempt_at_s
            )
            if not active and not read_failed and fallback is not None:
                self._fallback.pop(profile_id, None)
        return active

    def record_failure(
        self,
        profile_id: str,
        failure_class: str,
        now_s: int,
    ) -> PromotionBackoffState:
        """Increment one profile and persist its next exact retry deadline."""
        profile_id = self._profile_id(profile_id)
        failure_class = self._failure_class(failure_class)
        now_s = self._epoch(now_s)
        try:
            with closing(self._connect()) as conn, conn:
                row = conn.execute(
                    "SELECT failure_count FROM promotion_backoff "
                    "WHERE profile_id = ?",
                    (profile_id,),
                ).fetchone()
                prior_count = int(row[0]) if row is not None else 0
                failure_count = min(prior_count + 1, 5)
                next_attempt_at_s = (
                    now_s + PROFILE_BACKOFF_DELAYS_S[failure_count - 1]
                )
                conn.execute(
                    """
                    INSERT INTO promotion_backoff (
                        profile_id, failure_count, last_failure_at_s,
                        next_attempt_at_s, last_failure_class
                    ) VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(profile_id) DO UPDATE SET
                        failure_count = excluded.failure_count,
                        last_failure_at_s = excluded.last_failure_at_s,
                        next_attempt_at_s = excluded.next_attempt_at_s,
                        last_failure_class = excluded.last_failure_class
                    """,
                    (
                        profile_id,
                        failure_count,
                        now_s,
                        next_attempt_at_s,
                        failure_class,
                    ),
                )
        except sqlite3.Error as exc:
            self._warn("write", exc)
            state = self._fallback_state(now_s, failure_class)
            with self._fallback_lock:
                self._fallback[profile_id] = state
            return state

        state = PromotionBackoffState(
            failure_count=failure_count,
            last_failure_at_s=now_s,
            next_attempt_at_s=next_attempt_at_s,
            last_failure_class=failure_class,
            persisted=True,
        )
        with self._fallback_lock:
            self._fallback.pop(profile_id, None)
        return state

    def reset(self, profile_id: str, *, now_s: int | None = None) -> bool:
        """Clear a healthy profile.

        Retain a conservative fallback when persistence fails.
        """
        profile_id = self._profile_id(profile_id)
        try:
            with closing(self._connect()) as conn, conn:
                conn.execute(
                    "DELETE FROM promotion_backoff WHERE profile_id = ?",
                    (profile_id,),
                )
        except sqlite3.Error as exc:
            self._warn("reset", exc)
            fallback_now = self._epoch(
                int(time.time()) if now_s is None else now_s
            )
            with self._fallback_lock:
                fallback = self._fallback.get(profile_id)
                if (
                    fallback is None
                    or fallback.next_attempt_at_s <= fallback_now
                ):
                    self._fallback[profile_id] = self._fallback_state(
                        fallback_now, "sidecar_unavailable",
                    )
            return False

        with self._fallback_lock:
            self._fallback.pop(profile_id, None)
        return True

    def _init_db(self) -> None:
        with closing(self._connect()) as conn, conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS promotion_backoff (
                    profile_id TEXT PRIMARY KEY,
                    failure_count INTEGER NOT NULL
                        CHECK(failure_count BETWEEN 1 AND 5),
                    last_failure_at_s INTEGER NOT NULL
                        CHECK(last_failure_at_s >= 0),
                    next_attempt_at_s INTEGER NOT NULL
                        CHECK(next_attempt_at_s > last_failure_at_s),
                    last_failure_class TEXT NOT NULL
                        CHECK(length(last_failure_class) BETWEEN 1 AND 48)
                        CHECK(last_failure_class NOT GLOB '*[^a-z0-9_]*')
                )
                """
            )
            columns = {
                row[1]
                for row in conn.execute(
                    "PRAGMA table_info(promotion_backoff)"
                )
            }
            expected = {
                "profile_id",
                "failure_count",
                "last_failure_at_s",
                "next_attempt_at_s",
                "last_failure_class",
            }
            if columns != expected:
                raise sqlite3.DatabaseError(
                    "invalid promotion backoff schema"
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS "
                "idx_promotion_backoff_due "
                "ON promotion_backoff(next_attempt_at_s)"
            )
            conn.execute("PRAGMA user_version = 1")

    def _read_persisted(
        self, profile_id: str,
    ) -> PromotionBackoffState | None:
        with closing(self._connect()) as conn:
            row = conn.execute(
                "SELECT failure_count, last_failure_at_s, "
                "next_attempt_at_s, last_failure_class "
                "FROM promotion_backoff WHERE profile_id = ?",
                (profile_id,),
            ).fetchone()
        if row is None:
            return None
        return PromotionBackoffState(
            failure_count=int(row[0]),
            last_failure_at_s=int(row[1]),
            next_attempt_at_s=int(row[2]),
            last_failure_class=str(row[3]),
            persisted=True,
        )

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(
            self.db_path,
            timeout=_SIDECAR_TIMEOUT_SECONDS,
        )

    @staticmethod
    def _profile_id(profile_id: str) -> str:
        value = (profile_id or "").strip()
        if not value:
            raise ValueError("profile_id is required")
        return value

    @staticmethod
    def _epoch(value: int) -> int:
        value = int(value)
        if value < 0:
            raise ValueError("epoch seconds must be non-negative")
        return value

    @staticmethod
    def _failure_class(value: str) -> str:
        value = (value or "").strip()
        if (
            len(value) > _MAX_FAILURE_CLASS_CHARS
            or _SAFE_FAILURE_CLASS_RE.fullmatch(value) is None
        ):
            return "unexpected_precommit"
        return value

    @staticmethod
    def _fallback_state(
        now_s: int, failure_class: str,
    ) -> PromotionBackoffState:
        return PromotionBackoffState(
            failure_count=5,
            last_failure_at_s=now_s,
            next_attempt_at_s=now_s + PROFILE_BACKOFF_DELAYS_S[-1],
            last_failure_class=failure_class,
            persisted=False,
        )

    @staticmethod
    def _warn(operation: str, exc: sqlite3.Error) -> None:
        logger.warning(
            "memoria promotion backoff %s failed: %s",
            operation,
            type(exc).__name__,
        )
