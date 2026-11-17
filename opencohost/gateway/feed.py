"""In-memory feed of accepted phone turns, shown in the desktop conversation.

remote-vrm-avatar T9. The phone's text is operator-owned input that the owner
wants to see in their own app, so it is kept here, in memory only, and served by
an operator-only endpoint on the main API (never by the gateway).

PRIVACY (R8 boundary): the text is never logged, never persisted to disk, never
placed in the event log, metrics or prompts. The feed dies with the process.
"""

import collections
import threading
import time

MAX_ENTRIES = 50
MAX_AGE_S = 30 * 60


class RemoteTurnFeed:
    def __init__(self, clock=time.time, max_entries: int = MAX_ENTRIES, max_age_s: float = MAX_AGE_S):
        self._clock = clock
        self._max_age_s = max_age_s
        self._lock = threading.Lock()
        self._items: collections.deque = collections.deque(maxlen=max_entries)
        self._seq = 0

    def _expire(self, now: float) -> None:
        while self._items and now - self._items[0]["accepted_at"] >= self._max_age_s:
            self._items.popleft()

    def add(self, text: str, device_id: str, device_label: str) -> int:
        """Record an accepted turn and return its sequence number."""
        now = self._clock()
        with self._lock:
            self._expire(now)
            self._seq += 1
            self._items.append(
                {
                    "seq": self._seq,
                    "text": text,
                    "accepted_at": now,
                    "device_id": device_id,
                    "device_label": device_label,
                }
            )
            return self._seq

    def since(self, after: int = 0) -> dict:
        with self._lock:
            self._expire(self._clock())
            return {
                "items": [dict(item) for item in self._items if item["seq"] > after],
                "latest_seq": self._seq,
            }
