from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from opencohost.core.memory_v5_shadow.evidence import CommittedTurnSnapshot
from opencohost.core.memory_v5_shadow.store import ShadowStore

logger = logging.getLogger("OpenCohost")


class MemoryRuntime:
    def __init__(self, db_path: Path | str | None = None, queue_maxsize: int = 1000) -> None:
        from opencohost.config.settings import MEMORY_V5_SHADOW_CONTROL_RESERVED, MEMORY_V5_SHADOW_QUEUE_MAXSIZE

        self.run_id = uuid.uuid4().hex
        self.db_path = Path(db_path) if db_path is not None else None
        maxsize = int(queue_maxsize) if queue_maxsize is not None else int(MEMORY_V5_SHADOW_QUEUE_MAXSIZE)
        reserved = int(MEMORY_V5_SHADOW_CONTROL_RESERVED)
        if maxsize <= reserved:
            reserved = max(0, maxsize // 2)
        self._maxsize = maxsize
        self._reserved = reserved
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._store = ShadowStore(db_path=self.db_path) if self.db_path is not None else ShadowStore()
        if self.db_path is None:
            self.db_path = self._store.db_path
        self._running = True
        self._admission_closed = False
        self._capture_enabled = True
        self._degraded = False
        self._dropped = 0
        self._control_failures = 0
        self._lock = threading.Lock()
        self._adm_lock = threading.Lock()
        self._purge_epochs: dict[str, int] = {}
        self._global_epoch = 0
        self._enqueue_epoch = 0
        self._worker_epochs: dict[str, int] = {}
        self._worker_global = 0
        try:
            with self._store._lock:
                self._store._conn.execute(
                    "INSERT OR IGNORE INTO shadow_runs (run_id, started_at, degraded, capture_enabled, dropped_evidence_total, control_failures_total, created_at) VALUES (?,?,?,?,?,?,?)",
                    (self.run_id, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), 0, 1, 0, 0, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
                )
                self._store._conn.commit()
        except Exception:
            pass
        self._worker = threading.Thread(target=self._worker_loop, daemon=True, name="memory-v5-shadow")
        self._worker.start()
        self._bg_latencies: list[float] = []
        self._bg_lock = threading.Lock()

    def record_turn(self, snapshot: CommittedTurnSnapshot) -> None:
        try:
            if snapshot.is_private:
                return
            with self._adm_lock:
                if self._admission_closed or not self._capture_enabled:
                    with self._lock:
                        self._dropped += 1
                    return
                prof_epoch = self._purge_epochs.get(snapshot.profile_id, 0)
                glob = self._global_epoch
                enq_ep = self._enqueue_epoch
                self._enqueue_epoch += 1
                cap = max(1, self._maxsize - self._reserved)
                if self._queue.qsize() >= cap:
                    with self._lock:
                        self._dropped += 1
                    return
                self._queue.put_nowait((snapshot, prof_epoch, glob, enq_ep))
        except queue.Full:
            with self._lock:
                self._dropped += 1
        except Exception:
            logger.debug("shadow record_turn fail-open", exc_info=True)

    def on_profile_switch(self, old_profile_id: Optional[str], new_profile_id: str) -> None:
        cmd = ("profile_switch", old_profile_id, new_profile_id, time.time())
        try:
            with self._adm_lock:
                if self._admission_closed:
                    with self._lock:
                        self._control_failures += 1
                        self._degraded = True
                        self._capture_enabled = False
                    logger.warning("shadow control admission failed kind=PROFILE_SWITCH reason=admission_closed")
                    return
                self._queue.put_nowait(cmd)
        except queue.Full:
            with self._lock:
                self._control_failures += 1
                self._degraded = True
                self._capture_enabled = False
            logger.warning("shadow control admission failed kind=PROFILE_SWITCH reason=queue_full")
        except Exception:
            logger.debug("shadow on_profile_switch fail-open", exc_info=True)

    def _ordered_profile_switch(self, old_profile_id: Optional[str], new_profile_id: str, run_id: str, seq_out: int, seq_in: int) -> None:
        cmd = ("profile_switch_ordered", old_profile_id, new_profile_id, run_id, seq_out, seq_in, time.time())
        try:
            with self._adm_lock:
                if self._admission_closed:
                    with self._lock:
                        self._control_failures += 1
                        self._degraded = True
                        self._capture_enabled = False
                    logger.warning("shadow control admission failed kind=PROFILE_SWITCH reason=admission_closed")
                    return
                self._queue.put_nowait(cmd)
        except queue.Full:
            with self._lock:
                self._control_failures += 1
                self._degraded = True
                self._capture_enabled = False
            logger.warning("shadow control admission failed kind=PROFILE_SWITCH reason=queue_full")
        except Exception:
            logger.debug("shadow ordered profile switch fail-open", exc_info=True)

    def shutdown(self, timeout: float = 2.0) -> None:
        with self._adm_lock:
            self._admission_closed = True
            self._running = False
        try:
            self._worker.join(timeout=timeout)
        except Exception:
            pass
        if self._worker.is_alive():
            with self._lock:
                self._degraded = True
                self._control_failures += 1
            logger.warning("shadow shutdown timeout worker still alive")
            return
        try:
            self._store.close()
        except Exception:
            pass

    @property
    def diagnostics(self) -> dict:
        with self._lock:
            dropped = self._dropped
            cf = self._control_failures
            deg = self._degraded
            cap = self._capture_enabled
        qd = self._queue.qsize()
        with self._bg_lock:
            lat = list(self._bg_latencies[-100:])
        p50 = p95 = p99 = 0.0
        if lat:
            s = sorted(lat)
            p50 = s[int(len(s) * 0.5)]
            p95 = s[int(len(s) * 0.95)] if len(s) > 1 else s[-1]
            p99 = s[int(len(s) * 0.99)] if len(s) > 1 else s[-1]
        alive = self._worker.is_alive()
        return {
            "run_id": self.run_id,
            "dropped_evidence_total": dropped,
            "control_failures_total": cf,
            "degraded": deg,
            "capture_enabled": cap,
            "queue_depth": qd,
            "bg_p50_ms": p50,
            "bg_p95_ms": p95,
            "bg_p99_ms": p99,
            "worker_alive": alive,
            "admission_closed": self._admission_closed,
        }

    def _dispatch_barrier(self, kind: str, profile_id: str | None, timeout: float = 2.0):
        ev = threading.Event()
        holder = {"state": "QUEUED", "lock": threading.Lock()}
        with self._adm_lock:
            if self._admission_closed:
                with self._lock:
                    self._control_failures += 1
                    self._degraded = True
                    self._capture_enabled = False
                return {"status": "FAILED", "reason": "admission_closed"}
            if kind == "purge":
                ep = self._purge_epochs.get(profile_id or "", 0) + 1
                self._purge_epochs[profile_id or ""] = ep
                glob = self._global_epoch
            else:
                self._global_epoch += 1
                glob = self._global_epoch
                ep = 0
            enq_ep = self._enqueue_epoch
            self._enqueue_epoch += 1
            cmd = (kind, profile_id, ep, glob, enq_ep, ev, holder)
            try:
                self._queue.put(cmd, timeout=1.0)
            except queue.Full:
                with self._lock:
                    self._control_failures += 1
                    self._degraded = True
                    self._capture_enabled = False
                logger.warning("shadow control admission failed kind=%s reason=queue_full", kind.upper())
                return {"status": "FAILED", "reason": "queue_full"}
        ok = ev.wait(timeout=timeout)
        if not ok:
            with holder["lock"]:
                state = holder["state"]
                if state == "QUEUED":
                    holder["state"] = "CANCELLED"
            with self._lock:
                self._control_failures += 1
                self._degraded = True
                self._capture_enabled = False
            logger.warning("shadow barrier timeout kind=%s", kind)
            if state == "QUEUED":
                return {"status": "FAILED", "reason": "barrier_timeout_cancelled"}
            if state == "STARTED":
                return {"status": "INDETERMINATE", "reason": "execution_started"}
        if holder.get("error"):
            with self._lock:
                self._control_failures += 1
                self._degraded = True
                self._capture_enabled = False
            return {"status": "FAILED", "reason": holder.get("error")}
        return {"status": "COMMITTED", "reason": "committed"}

    def _worker_loop(self) -> None:
        while self._running or not self._queue.empty():
            try:
                item = self._queue.get(timeout=0.1)
            except queue.Empty:
                continue
            if isinstance(item, tuple) and item and item[0] in ("purge", "forget_all"):
                kind, profile_id, ep, glob, enq_ep, ev, holder = item
                with holder["lock"]:
                    if holder["state"] == "CANCELLED":
                        ev.set()
                        self._queue.task_done()
                        continue
                    holder["state"] = "STARTED"
                try:
                    if kind == "purge":
                        self._store.purge_profile(profile_id or "")
                        self._worker_epochs[profile_id or ""] = ep
                        self._worker_global = glob
                    else:
                        self._store.forget_all()
                        self._worker_global = glob
                except Exception as exc:
                    holder["error"] = type(exc).__name__
                    with self._lock:
                        self._degraded = True
                        self._control_failures += 1
                        self._capture_enabled = False
                finally:
                    with holder["lock"]:
                        holder["state"] = "FAILED" if holder.get("error") else "COMMITTED"
                    ev.set()
                    self._queue.task_done()
                continue
            if isinstance(item, tuple) and item and item[0] in ("profile_switch", "profile_switch_ordered"):
                self._queue.task_done()
                continue
            try:
                snap, prof_ep, glob_ep, enq_ep = item  # type: ignore
                cur_prof = self._worker_epochs.get(snap.profile_id, 0)
                cur_glob = self._worker_global
                if prof_ep < cur_prof or glob_ep < cur_glob:
                    with self._lock:
                        self._dropped += 1
                    self._queue.task_done()
                    continue
                t0 = time.perf_counter()
                out = self._store.insert_snapshot(snap)
                dt = (time.perf_counter() - t0) * 1000
                with self._bg_lock:
                    self._bg_latencies.append(dt)
                    if len(self._bg_latencies) > 1000:
                        self._bg_latencies = self._bg_latencies[-500:]
                if out.outcome == "INTEGRITY_COLLISION":
                    with self._lock:
                        self._degraded = True
                        self._capture_enabled = False
                        self._control_failures += 1
                elif out.outcome == "STORE_FAILURE":
                    with self._lock:
                        self._degraded = True
                        self._capture_enabled = False
                        self._dropped += 1
                elif out.outcome == "EXACT_DUPLICATE":
                    pass
            except Exception:
                with self._lock:
                    self._dropped += 1
                    self._degraded = True
                logger.debug("shadow worker insert fail-open", exc_info=True)
            finally:
                self._queue.task_done()
