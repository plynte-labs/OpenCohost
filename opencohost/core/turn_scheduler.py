"""Thread-safe dispatch scheduling extracted from ``MotorVocalIA``.

This component owns queue state, ordering, TTL classification, capacity and
owner-prefix operations. It deliberately does not know about inference, TTS,
UI callbacks, prefetch, accumulation policy or speech playback.
"""

from __future__ import annotations

from dataclasses import dataclass
import threading
import time
from typing import Any, Callable, Iterable

from opencohost.config.settings import (
    DIRECT_ANSWER_MAX_WAIT_SECONDS,
    OWNER_BUNDLE_MAX_CHARS,
    OWNER_BUNDLE_MAX_ITEMS,
)
from opencohost.core import turn_priority


OWNER_QUESTION_SOURCES = frozenset({"direct", "ptt"})
TurnItem = tuple
HeadSnapshot = tuple[int, str, str, str | None]


@dataclass(frozen=True)
class EnqueueResult:
    item: TurnItem
    dropped: tuple[TurnItem, ...]
    head_snapshot: HeadSnapshot | None


@dataclass(frozen=True)
class ExpiredTurn:
    item: TurnItem
    age_seconds: float
    ttl_seconds: float


@dataclass(frozen=True)
class DequeueResult:
    item: TurnItem | None = None
    blocked: bool = False
    empty: bool = False
    empty_value: Any = None


class TurnScheduler:
    """Own and serialize the dispatch queue without runtime side effects."""

    def __init__(
        self,
        *,
        max_items: int = 5,
        default_ttl_seconds: float = 30.0,
        direct_ttl_seconds: float = DIRECT_ANSWER_MAX_WAIT_SECONDS,
        direct_ttl_resolver: Callable[[], float] | None = None,
        priority_resolver: Callable[[str], int] | None = None,
        stream_ttl_resolver: Callable[[], float] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._items: list[TurnItem] = []
        self._sched_lock = threading.Lock()
        self._max_items = max_items
        self._default_ttl_seconds = default_ttl_seconds
        self._direct_ttl_seconds = direct_ttl_seconds
        self._direct_ttl_resolver = direct_ttl_resolver
        self._priority_resolver = (
            priority_resolver or turn_priority.dispatch_priority_for_source
        )
        self._stream_ttl_resolver = (
            stream_ttl_resolver or turn_priority.effective_stream_ttl
        )
        self._clock = clock or time.time

    @property
    def lock(self):
        """Return the scheduler lock for lock-order proofs and migration probes."""
        return self._sched_lock

    @property
    def max_items(self) -> int:
        return self._max_items

    @max_items.setter
    def max_items(self, value: int) -> None:
        self._max_items = value

    @property
    def default_ttl_seconds(self) -> float:
        return self._default_ttl_seconds

    @default_ttl_seconds.setter
    def default_ttl_seconds(self, value: float) -> None:
        self._default_ttl_seconds = value

    def enqueue(
        self,
        payload: str,
        *,
        priority: int | None = None,
        source: str = "chat",
        history_text: str | None = None,
        submitted_at: float | None = None,
        submitted_under_provider: str | None = None,
    ) -> EnqueueResult:
        if priority is None:
            priority = self._priority_resolver(source)
        item = (
            priority,
            self._clock(),
            payload,
            source,
            history_text,
            submitted_at,
            submitted_under_provider,
        )
        with self._sched_lock:
            self._items.append(item)
            self._items.sort(key=lambda queued: (queued[0], queued[1]))
            dropped = []
            while len(self._items) > self._max_items:
                index = next(
                    (
                        position
                        for position in range(len(self._items) - 1, -1, -1)
                        if self._items[position][3] not in OWNER_QUESTION_SOURCES
                    ),
                    None,
                )
                if index is None:
                    break
                dropped.append(self._items.pop(index))
            head_snapshot = self._head_snapshot_locked()
        return EnqueueResult(item, tuple(dropped), head_snapshot)

    def peek(self) -> TurnItem | None:
        with self._sched_lock:
            return self._items[0] if self._items else None

    def snapshot(self) -> tuple[TurnItem, ...]:
        with self._sched_lock:
            return tuple(self._items)

    def head_snapshot(self) -> HeadSnapshot | None:
        with self._sched_lock:
            return self._head_snapshot_locked()

    def _head_snapshot_locked(self) -> HeadSnapshot | None:
        head = self._items[0] if self._items else None
        if head is None:
            return None
        return (head[0], head[2], head[3], head[4] if len(head) > 4 else None)

    def is_head(self, priority: int, payload: str, source: str) -> bool:
        with self._sched_lock:
            head = self._items[0] if self._items else None
            return head is not None and (head[0], head[2], head[3]) == (
                priority,
                payload,
                source,
            )

    def dequeue(self) -> TurnItem | None:
        with self._sched_lock:
            return self._items.pop(0) if self._items else None

    def dequeue_or_else(
        self,
        *,
        on_empty: Callable[[], Any],
        required_priority: int | None = None,
    ) -> DequeueResult:
        """Pop atomically or invoke ``on_empty`` while the scheduler lock is held.

        The callback preserves the legacy ``_pq_lock -> _accum_lock`` edge for
        MotorVocalIA's atomic empty-queue accumulation flush. No other scheduler
        method calls outward while holding this lock.
        """
        with self._sched_lock:
            if required_priority is not None and (
                not self._items or self._items[0][0] != required_priority
            ):
                return DequeueResult(blocked=True)
            if not self._items:
                return DequeueResult(empty=True, empty_value=on_empty())
            return DequeueResult(item=self._items.pop(0))

    def clear(self) -> tuple[TurnItem, ...]:
        with self._sched_lock:
            removed = tuple(self._items)
            self._items.clear()
            return removed

    def drop_source(self, source: str) -> tuple[TurnItem, ...]:
        return self._drop_matching(lambda item: item[3] == source)

    def drop_sources(self, prefixes: tuple[str, ...]) -> tuple[TurnItem, ...]:
        return self._drop_matching(
            lambda item: str(item[3]).startswith(prefixes)
        )

    def _drop_matching(
        self, predicate: Callable[[TurnItem], bool]
    ) -> tuple[TurnItem, ...]:
        with self._sched_lock:
            removed = []
            kept = []
            for item in self._items:
                (removed if predicate(item) else kept).append(item)
            self._items[:] = kept
            return tuple(removed)

    def expire(self, *, now: float | None = None) -> tuple[ExpiredTurn, ...]:
        current = self._clock() if now is None else now
        with self._sched_lock:
            expired = []
            kept = []
            for item in self._items:
                priority, timestamp, _payload, source = item[:4]
                if source == "direct":
                    ttl = (
                        self._direct_ttl_resolver()
                        if self._direct_ttl_resolver is not None
                        else self._direct_ttl_seconds
                    )
                elif source == "chat":
                    ttl = self._stream_ttl_resolver()
                else:
                    ttl = self._default_ttl_seconds
                age = current - timestamp
                if (
                    priority > 0
                    and not source.startswith("kira-agenda")
                    and age > ttl
                ):
                    expired.append(ExpiredTurn(item, age, ttl))
                else:
                    kept.append(item)
            self._items[:] = kept
            return tuple(expired)

    def pending_owner_questions(self) -> int:
        with self._sched_lock:
            return sum(
                1 for item in self._items if item[3] in OWNER_QUESTION_SOURCES
            )

    def has_pending_priority_before(self, priority: int) -> bool:
        with self._sched_lock:
            return any(item[0] < priority for item in self._items)

    def take_owner_prefix(
        self,
        head_item: TurnItem,
        *,
        max_items: int = OWNER_BUNDLE_MAX_ITEMS,
        max_chars: int = OWNER_BUNDLE_MAX_CHARS,
    ) -> list[TurnItem]:
        taken = []
        chars = len(head_item[2])
        with self._sched_lock:
            while (
                self._items
                and self._items[0][3] in OWNER_QUESTION_SOURCES
                and len(taken) + 1 < max_items
                and chars + len(self._items[0][2]) <= max_chars
            ):
                item = self._items.pop(0)
                chars += len(item[2])
                taken.append(item)
        return taken

    def requeue(self, items: Iterable[TurnItem]) -> None:
        with self._sched_lock:
            self._items.extend(items)
            self._items.sort(key=lambda queued: (queued[0], queued[1]))

    # Transitional probes for the existing MotorVocalIA test surface. Production
    # code delegates through the methods above; these keep legacy tests able to
    # seed and inspect exact queue tuples during the extraction phase.
    def _items_for_compat(self) -> list[TurnItem]:
        return self._items

    def _replace_items_for_compat(self, items: list[TurnItem]) -> None:
        self._items = items

    def _replace_lock_for_compat(self, lock) -> None:
        self._sched_lock = lock
