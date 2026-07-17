"""Command dispatcher for the Kira FastAPI API layer (Phase 1).

Bounded enqueue gate + capped idempotency TTL cache + command_id/state_version
bookkeeping. Pure stdlib, in-memory only, zero IO — safe to hold `_lock`
across the whole `dispatch()` call. HTTP-code mapping (200/404/409/429)
lives in the PR2 endpoint handler; this module only returns result STATES.
"""

import hashlib
import json
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from threading import Lock
from typing import Optional

_QUEUE_FULL_THRESHOLD = 16
_CACHE_CAP = 1024
# Must exceed OLLAMA_CHAT_TIMEOUT (180s, settings.py) + watchdog recovery +
# operator retry window, or a legitimate retry of a stalled >120s chat turn
# double-fires Kira instead of replaying the cached command_id. 600s = ~3x the
# chat timeout plus margin; the cache stays bounded by _CACHE_CAP (FIFO).
_DEFAULT_TTL_SECONDS = 600.0


@dataclass
class DispatchResult:
    state: str  # "accepted" | "replay" | "conflict" | "queue_full"
    command_id: Optional[str] = None


class Dispatcher:
    """Wraps a `queue.Queue` with a bounded gate + idempotency cache.

    Unit-testable without FastAPI or an engine — `command_queue` is any
    object exposing `qsize()` and `put_nowait()` (a real `queue.Queue` in
    tests and in production, since core's engine queue is unbounded).
    """

    def __init__(self, command_queue, ttl_seconds: float = _DEFAULT_TTL_SECONDS):
        self._queue = command_queue
        self._ttl_seconds = ttl_seconds
        self._lock = Lock()
        self.state_version = 0
        # key -> (command, payload_hash, command_id, expires_at)
        self._cache: "OrderedDict[str, tuple[str, str, str, float]]" = OrderedDict()

    def dispatch(
        self,
        command: str,
        payload: dict,
        key: Optional[str] = None,
        history_text: Optional[str] = None,
    ) -> DispatchResult:
        with self._lock:
            now = time.time()
            self._prune_expired(now)

            if key is not None and key in self._cache:
                cached_command, payload_hash, command_id, _expires_at = self._cache[key]
                if cached_command == command and payload_hash == self._hash_payload(payload):
                    return DispatchResult(state="replay", command_id=command_id)
                return DispatchResult(state="conflict")

            if self._queue.qsize() >= _QUEUE_FULL_THRESHOLD:
                return DispatchResult(state="queue_full")

            command_id = f"cmd_{uuid.uuid4().hex}"
            # history_text (A1, memoria_quality_20260717) rides as an OPTIONAL
            # 3rd tuple element for the F10 PTT path — run()'s tolerant unpack
            # commits it to historial. Omitted -> a 2-tuple, unchanged for every
            # other caller. It is deliberately NOT part of the idempotency
            # hash/cache key (identity is (command, payload) only).
            if history_text is not None:
                self._queue.put_nowait((command, payload, history_text))
            else:
                self._queue.put_nowait((command, payload))
            self.state_version += 1

            if key is not None:
                if len(self._cache) >= _CACHE_CAP:
                    self._cache.popitem(last=False)  # FIFO-evict oldest
                self._cache[key] = (command, self._hash_payload(payload), command_id, now + self._ttl_seconds)

            return DispatchResult(state="accepted", command_id=command_id)

    def _prune_expired(self, now: float) -> None:
        expired = [k for k, (_cmd, _h, _cid, expires_at) in self._cache.items() if expires_at <= now]
        for k in expired:
            del self._cache[k]

    @staticmethod
    def _hash_payload(payload: dict) -> str:
        encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
