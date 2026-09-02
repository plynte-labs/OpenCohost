from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Sequence

from opencohost.core.memory_v5_shadow.evidence import CommittedTurnSnapshot
from opencohost.core.memory_v5_shadow.store import ShadowStore

logger = logging.getLogger("OpenCohost")


class MemoryRuntime:
    def __init__(self, db_path: Path | str | None = None, queue_maxsize: int = 1000) -> None:
        from opencohost.config.settings import (
            MEMORY_V5_SHADOW_CONTROL_RESERVED,
            MEMORY_V5_SHADOW_QUEUE_MAXSIZE,
        )

        self.run_id = uuid.uuid4().hex
        self.db_path = Path(db_path) if db_path is not None else None
        maxsize = (
            int(queue_maxsize)
            if queue_maxsize is not None
            else int(MEMORY_V5_SHADOW_QUEUE_MAXSIZE)
        )
        reserved = int(MEMORY_V5_SHADOW_CONTROL_RESERVED)
        if maxsize <= reserved:
            reserved = max(0, maxsize // 2)
        self._maxsize = maxsize
        self._reserved = reserved
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._store = (
            ShadowStore(db_path=self.db_path)
            if self.db_path is not None
            else ShadowStore()
        )
        if self.db_path is None:
            self.db_path = self._store.db_path
        self._running = True
        self._admission_closed = False
        self._capture_enabled = True
        self._degraded = False
        self._dropped = 0
        self._control_failures = 0
        self._stream_sequence = 0
        self._lock = threading.Lock()
        self._adm_lock = threading.Lock()
        self._purged_cutoffs: dict[str, int] = {}
        self._global_purged_cutoff: int = -1
        started_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        self._store.start_run(self.run_id, started_at)
        self._worker = threading.Thread(
            target=self._worker_loop, daemon=True, name="memory-v5-shadow"
        )
        self._worker.start()
        self._bg_latencies: list[float] = []
        self._bg_lock = threading.Lock()

    def allocate_sequences(self, count: int = 1) -> tuple[int, ...]:
        with self._adm_lock:
            start = self._stream_sequence + 1
            self._stream_sequence += count
            return tuple(range(start, start + count))

    def record_turn(self, snapshot: CommittedTurnSnapshot) -> None:
        try:
            if snapshot.is_private:
                return
            with self._adm_lock:
                if self._admission_closed or not self._capture_enabled:
                    with self._lock:
                        self._dropped += 1
                    return
                cap = max(1, self._maxsize - self._reserved)
                if self._queue.qsize() >= cap:
                    with self._lock:
                        self._dropped += 1
                    return
                if not snapshot.stream_sequence or snapshot.stream_sequence <= 0:
                    seq = self._stream_sequence + 1
                    self._stream_sequence += 1
                    snapshot = CommittedTurnSnapshot(
                        committed_turn_id=f"{self.run_id}:{seq}",
                        profile_id=snapshot.profile_id,
                        run_id=self.run_id,
                        stream_sequence=seq,
                        role=snapshot.role,
                        source=snapshot.source,
                        occurred_at=snapshot.occurred_at,
                        content=snapshot.content,
                        is_private=False,
                    )
                self._queue.put_nowait(("single", snapshot))
        except queue.Full:
            with self._lock:
                self._dropped += 1
        except Exception:
            logger.debug("shadow record_turn fail-open", exc_info=True)

    def record_turn_exchange(
        self, user_snap: CommittedTurnSnapshot, asst_snap: CommittedTurnSnapshot
    ) -> Optional[tuple[int, int]]:
        try:
            if user_snap.is_private or asst_snap.is_private:
                return None
            with self._adm_lock:
                if self._admission_closed or not self._capture_enabled:
                    with self._lock:
                        self._dropped += 2
                    return None
                cap = max(1, self._maxsize - self._reserved)
                if self._queue.qsize() >= cap:
                    with self._lock:
                        self._dropped += 2
                    return None
                if (
                    not user_snap.stream_sequence
                    or user_snap.stream_sequence <= 0
                    or not asst_snap.stream_sequence
                    or asst_snap.stream_sequence <= 0
                ):
                    seq_u = self._stream_sequence + 1
                    seq_a = self._stream_sequence + 2
                    self._stream_sequence += 2
                    user_snap = CommittedTurnSnapshot(
                        committed_turn_id=f"{self.run_id}:{seq_u}",
                        profile_id=user_snap.profile_id,
                        run_id=self.run_id,
                        stream_sequence=seq_u,
                        role=user_snap.role,
                        source=user_snap.source,
                        occurred_at=user_snap.occurred_at,
                        content=user_snap.content,
                        is_private=False,
                    )
                    asst_snap = CommittedTurnSnapshot(
                        committed_turn_id=f"{self.run_id}:{seq_a}",
                        profile_id=asst_snap.profile_id,
                        run_id=self.run_id,
                        stream_sequence=seq_a,
                        role=asst_snap.role,
                        source=asst_snap.source,
                        occurred_at=asst_snap.occurred_at,
                        content=asst_snap.content,
                        is_private=False,
                    )
                else:
                    seq_u = user_snap.stream_sequence
                    seq_a = asst_snap.stream_sequence
                self._queue.put_nowait(("exchange", (user_snap, asst_snap)))
                return (seq_u, seq_a)
        except queue.Full:
            with self._lock:
                self._dropped += 2
            return None
        except Exception:
            logger.debug("shadow record_turn_exchange fail-open", exc_info=True)
            return None

    def record_drop(self, count: int = 1, reason: str = "") -> None:
        with self._lock:
            self._dropped += count
            self._degraded = True
        logger.debug("shadow turn dropped count=%d reason=%s", count, reason)

    def on_profile_switch(
        self, old_profile_id: Optional[str], new_profile_id: str
    ) -> None:
        self._ordered_profile_switch(old_profile_id, new_profile_id)

    def _ordered_profile_switch(
        self,
        old_profile_id: Optional[str],
        new_profile_id: str,
        run_id: str | None = None,
        seq_out: int | None = None,
        seq_in: int | None = None,
    ) -> Optional[tuple[int, int]]:
        transition_id = uuid.uuid4().hex
        occurred_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        try:
            with self._adm_lock:
                if self._admission_closed:
                    with self._lock:
                        self._control_failures += 1
                        self._degraded = True
                        self._capture_enabled = False
                    logger.warning(
                        "shadow control admission failed kind=PROFILE_SWITCH reason=admission_closed"
                    )
                    return None
                s_out = seq_out if seq_out is not None else self._stream_sequence + 1
                s_in = seq_in if seq_in is not None else self._stream_sequence + 2
                if seq_out is None or seq_in is None:
                    self._stream_sequence += 2
                r_id = run_id or self.run_id
                cmd = (
                    "profile_switch_ordered",
                    old_profile_id,
                    new_profile_id,
                    r_id,
                    s_out,
                    s_in,
                    transition_id,
                    occurred_at,
                )
                self._queue.put_nowait(cmd)
                return (s_out, s_in)
        except queue.Full:
            with self._lock:
                self._control_failures += 1
                self._degraded = True
                self._capture_enabled = False
            logger.warning(
                "shadow control admission failed kind=PROFILE_SWITCH reason=queue_full"
            )
            return None
        except Exception:
            logger.debug("shadow ordered profile switch fail-open", exc_info=True)
            return None

    def shutdown(self, timeout: float = 2.0) -> None:
        with self._adm_lock:
            if not self._admission_closed:
                self._admission_closed = True
                self._running = False
                try:
                    seq_shut = self._stream_sequence + 1
                    self._stream_sequence += 1
                    occ = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                    self._queue.put_nowait(("shutdown", seq_shut, occ))
                except Exception:
                    pass
        try:
            self._worker.join(timeout=timeout)
        except Exception:
            pass
        if self._worker.is_alive():
            with self._lock:
                self._degraded = True
                self._control_failures += 1
            logger.warning("shadow shutdown timeout worker still alive")

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

    def _dispatch_barrier(
        self, kind: str, profile_id: str | None, timeout: float = 2.0
    ):
        ev = threading.Event()
        holder = {"state": "QUEUED", "lock": threading.Lock()}
        with self._adm_lock:
            if self._admission_closed:
                with self._lock:
                    self._control_failures += 1
                    self._degraded = True
                    self._capture_enabled = False
                return {"status": "FAILED", "reason": "admission_closed"}
            cutoff_seq = self._stream_sequence
            cmd = (kind, profile_id, cutoff_seq, ev, holder)
            try:
                self._queue.put_nowait(cmd)
            except queue.Full:
                with self._lock:
                    self._control_failures += 1
                    self._degraded = True
                    self._capture_enabled = False
                logger.warning(
                    "shadow control admission failed kind=%s reason=queue_full",
                    kind.upper(),
                )
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
            return {"status": "FAILED", "reason": holder.get("error")}
        return {"status": "COMMITTED", "reason": "committed"}

    def _worker_loop(self) -> None:
        try:
            while self._running or not self._queue.empty():
                try:
                    item = self._queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if isinstance(item, tuple) and item and item[0] in ("purge", "forget_all"):
                    kind, profile_id, cutoff_seq, ev, holder = item
                    with holder["lock"]:
                        if holder["state"] == "CANCELLED":
                            ev.set()
                            self._queue.task_done()
                            continue
                        holder["state"] = "STARTED"
                    try:
                        if kind == "purge":
                            self._store.purge_profile(profile_id or "")
                            self._purged_cutoffs[profile_id or ""] = max(
                                self._purged_cutoffs.get(profile_id or "", -1),
                                cutoff_seq,
                            )
                        else:
                            self._store.forget_all()
                            self._global_purged_cutoff = max(
                                self._global_purged_cutoff, cutoff_seq
                            )
                    except Exception as exc:
                        holder["error"] = type(exc).__name__
                        with self._lock:
                            self._degraded = True
                            self._control_failures += 1
                            self._capture_enabled = False
                    finally:
                        with holder["lock"]:
                            holder["state"] = (
                                "FAILED" if holder.get("error") else "COMMITTED"
                            )
                        ev.set()
                        self._queue.task_done()
                    continue
                if (
                    isinstance(item, tuple)
                    and item
                    and item[0] == "profile_switch_ordered"
                ):
                    try:
                        (
                            _,
                            old_pid,
                            new_pid,
                            r_id,
                            s_out,
                            s_in,
                            t_id,
                            occ_at,
                        ) = item
                        if old_pid:
                            self._store.insert_lifecycle_event(
                                kind="PROFILE_SWITCH_OUT",
                                owner_profile_id=old_pid,
                                transition_id=t_id,
                                run_id=r_id,
                                stream_sequence=s_out,
                                occurred_at=occ_at,
                            )
                        if new_pid:
                            self._store.insert_lifecycle_event(
                                kind="PROFILE_SWITCH_IN",
                                owner_profile_id=new_pid,
                                transition_id=t_id,
                                run_id=r_id,
                                stream_sequence=s_in,
                                occurred_at=occ_at,
                            )
                    except Exception:
                        with self._lock:
                            self._control_failures += 1
                            self._degraded = True
                    finally:
                        self._queue.task_done()
                    continue
                if isinstance(item, tuple) and item and item[0] == "shutdown":
                    try:
                        _, s_shut, occ_at = item
                        self._store.insert_lifecycle_event(
                            kind="SHUTDOWN",
                            owner_profile_id=None,
                            transition_id=None,
                            run_id=self.run_id,
                            stream_sequence=s_shut,
                            occurred_at=occ_at,
                        )
                    except Exception:
                        pass
                    finally:
                        self._queue.task_done()
                    continue
                try:
                    if isinstance(item, tuple) and item and item[0] == "exchange":
                        _, pair = item
                        snaps_to_process: Sequence[CommittedTurnSnapshot] = pair
                    elif isinstance(item, tuple) and item and item[0] == "single":
                        _, single_snap = item
                        snaps_to_process = [single_snap]
                    else:
                        snaps_to_process = [item[0]] if isinstance(item, tuple) else [item]

                    admitted: list[CommittedTurnSnapshot] = []
                    for snap in snaps_to_process:
                        if snap.is_private:
                            continue
                        if snap.stream_sequence <= self._global_purged_cutoff:
                            with self._lock:
                                self._dropped += 1
                            continue
                        p_cutoff = self._purged_cutoffs.get(snap.profile_id, -1)
                        if snap.stream_sequence <= p_cutoff:
                            with self._lock:
                                self._dropped += 1
                            continue
                        admitted.append(snap)

                    if admitted:
                        t0 = time.perf_counter()
                        outcomes = self._store.insert_snapshots(admitted)
                        dt = (time.perf_counter() - t0) * 1000
                        with self._bg_lock:
                            self._bg_latencies.append(dt)
                            if len(self._bg_latencies) > 1000:
                                self._bg_latencies = self._bg_latencies[-500:]
                        for out in outcomes:
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
                except Exception:
                    with self._lock:
                        self._dropped += 1
                        self._degraded = True
                    logger.debug("shadow worker insert fail-open", exc_info=True)
                finally:
                    self._queue.task_done()
        finally:
            try:
                self._store.close()
            except Exception:
                pass
