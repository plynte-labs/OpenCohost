"""MemorySubsystem — Unified Domain Facade for Memory v5 Shadow and Episodic Recall.

Encapsulates:
- Formation runtime (MemoryRuntime)
- Semantic cache store (SemanticCacheStore)
- MiniLM background worker (SemanticWorkerService)
- Incremental indexer (IncrementalSemanticIndexer)
- Episodic recall coordinator (EpisodicRecallCoordinator)

Direction of dependency:
llm_engine -> memory domain
NEVER: memory domain -> llm_engine
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Optional

from opencohost.core.memory_v5_shadow.evidence import CommittedTurnSnapshot
from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime
from opencohost.core.memory_v5_shadow.semantic_cache import SemanticCacheStore
from opencohost.core.memory_v5_shadow.semantic_worker import SemanticWorkerService
from opencohost.core.memory_v5_shadow.semantic_indexer import IncrementalSemanticIndexer
from opencohost.core.memory_v5_shadow.episodic_recall import EpisodicRecallCoordinator, RecallMode

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RecallDecision:
    """Deterministic application-side verdict: the local LLM never decides
    whether to call memory — this policy retrieves evidence and supplies a
    grounded context block (or an empty one with a reason)."""

    block: str
    scope: str
    hit: bool
    reason: str
    token_estimate: int

    @classmethod
    def empty(cls, scope: str = "EPISODIC_TOPIC", reason: str = "NO_RECALL") -> "RecallDecision":
        return cls(block="", scope=scope, hit=False, reason=reason, token_estimate=0)


class MemorySubsystem:
    """Unified domain facade for Memory v5 lifecycle, storage, semantic processing, and retrieval."""

    def __init__(
        self,
        mode: str = "OFF",
        runtime: Optional[MemoryRuntime] = None,
        cache_store: Optional[SemanticCacheStore] = None,
        worker: Optional[SemanticWorkerService] = None,
        indexer: Optional[IncrementalSemanticIndexer] = None,
        coordinator: Optional[EpisodicRecallCoordinator] = None,
        status: Optional[dict[str, str]] = None,
    ) -> None:
        self._mode = mode
        self._runtime = runtime
        self._cache_store = cache_store
        self._worker = worker
        self._indexer = indexer
        self._coordinator = coordinator
        self._status = status or {"requested": mode, "effective": "OFF", "reason_code": "off_default"}
        self._stream_seq: int = 0
        self._run_id: Optional[str] = runtime.run_id if runtime else None
        self._shutdown = False

    @classmethod
    def from_settings(
        cls,
        mode: Optional[str] = None,
        shadow_db: Optional[str] = None,
        cache_db: Optional[str] = None,
    ) -> "MemorySubsystem":
        """Factory creating and initializing MemorySubsystem from configuration."""
        try:
            from opencohost.config.settings import (
                MEMORY_V5_MODE,
                MEMORY_V5_SHADOW_DB,
                MEMORY_V5_SEMANTIC_CACHE_DB,
            )

            req_mode = str(mode if mode is not None else MEMORY_V5_MODE).upper()
            s_db = shadow_db or MEMORY_V5_SHADOW_DB
            c_db = cache_db or MEMORY_V5_SEMANTIC_CACHE_DB
        except Exception:
            return cls(mode="OFF", status={"requested": "OFF", "effective": "INIT_FAILED", "reason_code": "init_exception"})

        if req_mode not in ("SHADOW", "ACTIVE"):
            return cls(mode="OFF", status={"requested": req_mode, "effective": "OFF", "reason_code": "off_default"})

        try:
            import opencohost.core.memory_v5_shadow.runtime as rt_module

            cache_store = SemanticCacheStore(db_path=c_db)
            cache_store.initialize()

            rt = rt_module.MemoryRuntime(db_path=s_db, semantic_cache=cache_store)

            worker = SemanticWorkerService()
            worker.start()

            indexer = IncrementalSemanticIndexer(
                shadow_conn=rt._store._conn,
                cache_store=cache_store,
                worker=worker,
            )

            recall_mode = RecallMode.ACTIVE if req_mode == "ACTIVE" else RecallMode.SHADOW
            coord = EpisodicRecallCoordinator(
                shadow_conn=rt._store._conn,
                cache_store=cache_store,
                worker=worker,
                mode=recall_mode,
            )

            # Startup crash recovery runs AFTER recovered sessions are CLOSED
            # (MemoryRuntime.__init__ above) and BEFORE semantic
            # reconciliation below: stale eligible episodes are deterministically
            # materialized/closed, orphan membership pruned, and the newly
            # closed episodes are then picked up by the reconciler. Live
            # CURRENT open episodes are materialized OPEN and stay unindexed
            # (the indexer only ever takes CLOSED) — never indexed to mask
            # lifecycle defects.
            try:
                recovery_counts = cls.startup_episode_recovery(s_db)
                logger.info("Memory v5 startup episode recovery: %s", recovery_counts)
            except Exception as rec_exc:
                logger.warning("Startup episode recovery warning: %s", rec_exc)

            try:
                indexer.reconcile_unindexed_episodes()
            except Exception as rec_exc:
                logger.warning("Startup semantic reconciliation warning: %s", rec_exc)

            status = {"requested": req_mode, "effective": req_mode, "reason_code": "ok"}
            return cls(
                mode=req_mode,
                runtime=rt,
                cache_store=cache_store,
                worker=worker,
                indexer=indexer,
                coordinator=coord,
                status=status,
            )
        except Exception as exc:
            logger.warning("Memory v5 initialization failed: %s; failing open.", exc)
            return cls(
                mode=req_mode,
                status={"requested": req_mode, "effective": "INIT_FAILED", "reason_code": "init_exception"},
            )

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def status(self) -> dict[str, str]:
        return dict(self._status)

    @property
    def runtime(self) -> Optional[MemoryRuntime]:
        return self._runtime

    @property
    def run_id(self) -> Optional[str]:
        if self._run_id is not None:
            return self._run_id
        return self._runtime.run_id if self._runtime else None

    @property
    def stream_seq(self) -> int:
        return self._stream_seq

    @property
    def cache_store(self) -> Optional[SemanticCacheStore]:
        return self._cache_store

    @property
    def worker(self) -> Optional[SemanticWorkerService]:
        return self._worker

    @property
    def indexer(self) -> Optional[IncrementalSemanticIndexer]:
        return self._indexer

    @property
    def coordinator(self) -> Optional[EpisodicRecallCoordinator]:
        return self._coordinator

    @staticmethod
    def startup_episode_recovery(shadow_db_path: object) -> dict[str, int]:
        """Deterministic crash/startup recovery on the authoritative shadow DB.

        Materializes/closes stale eligible episodes from the (already
        recovered and CLOSED) sessions and prunes orphan episode_membership
        rows. Returns metadata-only counts — never corpus text.
        """
        from opencohost.core.memory_v5_shadow.episodes import (
            EpisodeSegmentationEngine,
        )

        engine = EpisodeSegmentationEngine()
        episodes, _memberships = engine.segment_all_from_db(
            shadow_db_path, persist=True
        )
        closed = sum(1 for e in episodes if e.get("state") == "CLOSED")
        open_n = sum(1 for e in episodes if e.get("state") == "OPEN")

        pruned = 0
        conn = sqlite3.connect(str(shadow_db_path), timeout=5.0)
        try:
            cur = conn.execute(
                "DELETE FROM episode_membership "
                "WHERE episode_id NOT IN (SELECT episode_id FROM episodes)"
            )
            pruned = int(cur.rowcount or 0)
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass
        return {
            "episodes_materialized": len(episodes),
            "closed_episodes": closed,
            "open_episodes": open_n,
            "pruned_orphan_membership": pruned,
        }

    def recall_decision(
        self, query_text: str, profile_id: Optional[str], reference_time=None
    ) -> RecallDecision:
        """Application-side deterministic retrieval verdict for one query."""
        if self._coordinator is None or not profile_id or not query_text:
            return RecallDecision.empty()
        try:
            packet = self._coordinator.process_query(
                query_text, profile_id=profile_id, reference_time=reference_time
            )
            if packet is None or packet.mode != RecallMode.ACTIVE:
                return RecallDecision.empty()
            scope = packet.scope.value if hasattr(packet.scope, "value") else str(packet.scope)
            hit = bool(packet.formatted_block)
            return RecallDecision(
                block=packet.formatted_block,
                scope=scope,
                hit=hit,
                reason=packet.reason_code,
                token_estimate=packet.token_estimate,
            )
        except Exception as e_exc:
            logger.warning("Episodic recall query failed open: %s", e_exc)
            return RecallDecision.empty(reason="RECALL_EXCEPTION")

    def recall_block(self, query_text: str, profile_id: Optional[str]) -> str:
        """Query episodic memory and return formatted prompt block for ACTIVE mode."""
        return self.recall_decision(query_text, profile_id).block

    def on_profile_switch(
        self,
        departing_profile_id: Optional[str],
        arriving_profile_id: Optional[str],
    ) -> Optional[tuple[int, int]]:
        """Order profile switch event and allocate stream sequences."""
        rt = self._runtime
        run_id = self.run_id
        if rt is not None and run_id is not None:
            try:
                assigned = rt._ordered_profile_switch(departing_profile_id, arriving_profile_id, run_id=run_id)
                if assigned:
                    self._stream_seq = assigned[1]
                    return assigned
            except Exception:
                pass
        return None

    def record_turn_exchange(
        self,
        user_snap: CommittedTurnSnapshot,
        asst_snap: CommittedTurnSnapshot,
    ) -> Optional[tuple[int, int]]:
        """Atomically record user + assistant turn exchange and advance stream sequence."""
        rt = self._runtime
        if rt is not None:
            try:
                assigned = rt.record_turn_exchange(user_snap, asst_snap)
                if assigned:
                    self._stream_seq = assigned[1]
                    return assigned
            except Exception:
                try:
                    rt.record_drop(2, "snapshot_construction_or_enqueue_exception")
                except Exception:
                    pass
        return None

    def record_turn(self, snapshot: CommittedTurnSnapshot) -> None:
        """Record a single turn snapshot."""
        if self._runtime is not None:
            try:
                self._runtime.record_turn(snapshot)
            except Exception:
                pass

    def record_drop(self, count: int, reason: str) -> None:
        """Record dropped evidence events."""
        if self._runtime is not None:
            try:
                self._runtime.record_drop(count, reason)
            except Exception:
                pass

    def purge_profile(self, profile_id: str, timeout_s: float = 2.0) -> bool:
        """Purge all profile records across runtime and semantic cache store."""
        success = True
        if self._runtime is not None:
            try:
                res = self._runtime.purge_profile(profile_id, timeout_s=timeout_s)
                if res != "COMMITTED":
                    success = False
            except Exception:
                success = False
        if self._cache_store is not None:
            try:
                self._cache_store.purge_profile_cache(profile_id)
            except Exception:
                success = False
        return success

    def forget_all(self, timeout_s: float = 2.0) -> bool:
        """Forget all memory records across runtime and semantic cache store."""
        success = True
        if self._runtime is not None:
            try:
                res = self._runtime.forget_all(timeout_s=timeout_s)
                if res != "COMMITTED":
                    success = False
            except Exception:
                success = False
        if self._cache_store is not None:
            try:
                self._cache_store.forget_all_cache()
            except Exception:
                success = False
        return success

    def shutdown(self, timeout_s: float = 2.0) -> None:
        """Gracefully shut down runtime, workers, and caches. Idempotent.

        Ownership order (correct lifecycle): FIRST stop new memory admission
        and quiesce the runtime — its SHUTDOWN write closes the session and
        segments episodes — THEN shut down the semantic worker, THEN close
        the cache. Uses only real public APIs (runtime.shutdown,
        worker.shutdown, cache.close).
        """
        if self._shutdown:
            return
        self._shutdown = True

        if self._runtime is not None:
            try:
                # Positional: MemoryRuntime.shutdown(timeout).
                self._runtime.shutdown(timeout_s)
            except Exception:
                pass
            self._runtime = None

        if self._worker is not None:
            try:
                self._worker.shutdown()
            except Exception:
                pass
            self._worker = None

        if self._cache_store is not None:
            try:
                self._cache_store.close()
            except Exception:
                pass
            self._cache_store = None
