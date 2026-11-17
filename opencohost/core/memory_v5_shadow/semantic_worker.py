"""
Failure-Isolated Semantic Worker Process for Memory v5.

Runs MiniLM ONNX embedding inside a dedicated child process to completely isolate
the ~700–900 MB memory footprint and model-loading latency from the main engine.
Enforces non-blocking IPC with bounded timeouts and fail-open resilience.
"""
from __future__ import annotations

import hashlib
import logging
import math
import multiprocessing as mp
import queue
import sys
import threading
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _deterministic_pseudo_vector(text: str, dim: int = 384) -> list[float]:
    raw = [
        int.from_bytes(hashlib.sha256(f"{text}:{i}".encode("utf-8")).digest()[:4], "big") / (2**32)
        for i in range(dim)
    ]
    norm = math.sqrt(sum(x * x for x in raw))
    if norm > 1e-9:
        return [x / norm for x in raw]
    return [0.0] * dim


def _worker_process_loop(
    req_queue: mp.Queue,
    resp_queue: mp.Queue,
    use_dummy: bool,
    simulated_delay_s: float,
    ready_event: Any = None,
) -> None:
    backend: Any = None
    if not use_dummy:
        try:
            from opencohost.core.memory_v5_shadow.semantic import MiniLMEmbeddingBackend

            backend = MiniLMEmbeddingBackend()
            backend.initialize()
        except Exception as exc:
            logger.error("Failed to initialize MiniLM in semantic worker process: %s", exc)

    if ready_event is not None:
        try:
            ready_event.set()
        except Exception:
            pass

    while True:
        try:
            item = req_queue.get()
            if item is None or item[1] == "SHUTDOWN":
                break

            req_id, method, args = item

            if simulated_delay_s > 0:
                time.sleep(simulated_delay_s)

            if method == "embed_query":
                text = args[0]
                if use_dummy:
                    vec = _deterministic_pseudo_vector(text)
                elif backend is None:
                    vec = None
                else:
                    vec = backend.embed(text)
                resp_queue.put((req_id, vec))

            elif method == "embed_batch":
                texts = args[0]
                if use_dummy:
                    vecs = [_deterministic_pseudo_vector(t) for t in texts]
                elif backend is None:
                    vecs = None
                else:
                    vecs = backend.embed_batch(texts)
                resp_queue.put((req_id, vecs))

            else:
                resp_queue.put((req_id, None))

        except Exception as exc:
            print(f"Error in semantic worker loop: {exc}", file=sys.stderr)
            break


class SemanticWorkerService:
    def __init__(
        self,
        use_dummy: bool = False,
        simulated_delay_s: float = 0.0,
    ) -> None:
        self.use_dummy = use_dummy
        self.simulated_delay_s = simulated_delay_s
        self._process: Optional[mp.Process] = None
        self._req_queue: Optional[mp.Queue] = None
        self._resp_queue: Optional[mp.Queue] = None
        self._ready_event: Optional[Any] = None
        self._req_counter = 0
        # One call in flight at a time: calls share one response queue and
        # discard foreign req_ids, so two threads (a turn's query embedding
        # and a background fact reconcile) would otherwise eat each other's
        # replies. A caller that cannot get the lock within its own timeout
        # fails open exactly like an IPC timeout.
        self._call_lock = threading.Lock()
        # Per-thread timing of the last call (memory-grounding T1): the turn
        # thread reads its own embed timing; a background reconcile never
        # overwrites it. Numbers only.
        self._tls = threading.local()

    @property
    def device(self) -> str:
        """Fixed code for where embeddings run. The MiniLM backend is pinned to
        CPUExecutionProvider (semantic.py); the dummy backend is not a model."""
        return "dummy" if self.use_dummy else "cpu"

    def last_call_stats(self) -> Optional[dict]:
        """``{method, wait_ms, call_ms}`` of this thread's last call, or None."""
        return getattr(self._tls, "last", None)

    def _record_call(self, method: str, wait_ms: float, call_ms: float) -> None:
        self._tls.last = {
            "method": method,
            "wait_ms": int(max(0.0, wait_ms)),
            "call_ms": int(max(0.0, call_ms)),
        }

    def start(self, timeout: float = 5.0) -> None:
        if self._process is not None and self._process.is_alive():
            return

        self._req_queue = mp.Queue()
        self._resp_queue = mp.Queue()
        self._ready_event = mp.Event()
        self._process = mp.Process(
            target=_worker_process_loop,
            args=(
                self._req_queue,
                self._resp_queue,
                self.use_dummy,
                self.simulated_delay_s,
                self._ready_event,
            ),
            daemon=True,
        )
        self._process.start()
        self._ready_event.wait(timeout=timeout)

    def is_alive(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def _call(self, method: str, args: tuple, timeout_s: float, block: bool = True) -> Any:
        started = time.monotonic()
        # Never let a skipped/timed-out call read the previous call's stats.
        self._tls.last = None
        if not block:
            # Opt-in try-acquire (session-topic-folder-20261016): a background
            # caller must never queue in front of a turn's own embed. Busy means
            # skip, immediately, with no warning.
            if not self._call_lock.acquire(blocking=False):
                logger.debug("Semantic worker busy; non-blocking call '%s' skipped.", method)
                self._record_call(method, (time.monotonic() - started) * 1000.0, 0.0)
                return None
        elif not self._call_lock.acquire(timeout=max(0.0, timeout_s)):
            logger.warning("Semantic worker busy (limit=%0.3fs); failing open.", timeout_s)
            self._record_call(method, (time.monotonic() - started) * 1000.0, 0.0)
            return None
        try:
            acquired = time.monotonic()
            remaining = max(0.0, timeout_s - (acquired - started))
            result = self._call_locked(method, args, remaining)
            self._record_call(
                method, (acquired - started) * 1000.0, (time.monotonic() - acquired) * 1000.0
            )
            return result
        finally:
            self._call_lock.release()

    def _call_locked(self, method: str, args: tuple, timeout_s: float) -> Any:
        if not self.is_alive():
            logger.warning("SemanticWorkerService is not alive; failing open.")
            return None

        assert self._req_queue is not None
        assert self._resp_queue is not None

        self._req_counter += 1
        req_id = self._req_counter

        try:
            self._req_queue.put_nowait((req_id, method, args))
        except Exception as exc:
            logger.warning("Failed to put item into semantic worker queue: %s", exc)
            return None

        start_time = time.monotonic()
        while time.monotonic() - start_time < timeout_s:
            try:
                # Poll with short wait
                item = self._resp_queue.get(timeout=0.05)
                if item[0] == req_id:
                    return item[1]
                # Stale response from prior timed-out call: discard and continue
            except queue.Empty:
                if not self.is_alive():
                    logger.warning("Semantic worker died during call; failing open.")
                    return None
                continue

        logger.warning("Semantic worker call '%s' timed out (limit=%0.3fs); failing open.", method, timeout_s)
        return None

    def embed_query(
        self, text: str, timeout_s: float = 0.5, *, block: bool = True
    ) -> Optional[list[float]]:
        """``block=False`` skips (returns None at once) when another call holds the worker."""
        return self._call("embed_query", (text,), timeout_s, block=block)

    def embed_batch(self, texts: list[str], timeout_s: float = 10.0) -> Optional[list[list[float]]]:
        return self._call("embed_batch", (texts,), timeout_s)

    def shutdown(self) -> None:
        if self._req_queue is not None and self.is_alive():
            try:
                self._req_queue.put_nowait((None, "SHUTDOWN", None))
            except Exception:
                pass

        if self._process is not None:
            self._process.join(timeout=1.0)
            if self._process.is_alive():
                self._process.terminate()
                self._process.join(timeout=0.5)
            self._process = None

        self._req_queue = None
        self._resp_queue = None

    def __del__(self) -> None:
        try:
            self.shutdown()
        except Exception:
            pass
