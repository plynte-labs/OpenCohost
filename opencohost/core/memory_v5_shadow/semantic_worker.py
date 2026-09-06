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

    def _call(self, method: str, args: tuple, timeout_s: float) -> Any:
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

    def embed_query(self, text: str, timeout_s: float = 0.5) -> Optional[list[float]]:
        return self._call("embed_query", (text,), timeout_s)

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
