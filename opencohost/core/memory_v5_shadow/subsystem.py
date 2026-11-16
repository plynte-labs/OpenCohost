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
from dataclasses import dataclass, field
from typing import Any, Optional, Sequence

from opencohost.core.memory_v5_shadow.evidence import CommittedTurnSnapshot
from opencohost.core.memory_v5_shadow.privacy import ShadowPrivacyController
from opencohost.core.memory_v5_shadow.runtime import MemoryRuntime
from opencohost.core.memory_v5_shadow.semantic_cache import SemanticCacheStore
from opencohost.core.memory_v5_shadow.semantic_worker import SemanticWorkerService
from opencohost.core.memory_v5_shadow.semantic_indexer import IncrementalSemanticIndexer
from opencohost.core.memory_v5_shadow.episodic_recall import EpisodicRecallCoordinator, RecallMode
from opencohost.core.memory_v5_shadow.fact_index import FACT_FALLBACK_DISABLED, FactSemanticIndex

logger = logging.getLogger(__name__)
# The only logger routed to logs/opencohost_*.log: owner-visible lines go here.
_oc_logger = logging.getLogger("OpenCohost")


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
    # Telemetry-only best query/exchange cosine (None: nothing was embedded).
    top_score: Optional[float] = None
    # Structured RetrievedEpisodeContext items behind `block`, for callers
    # that render their own compact form (the merged memorias block, ADR-059).
    episodes: tuple[Any, ...] = ()
    # Stage telemetry (memory-grounding T1): ms + fixed device code only.
    embed_ms: Optional[int] = None
    worker_wait_ms: Optional[int] = None
    retrieval_ms: Optional[int] = None
    device: Optional[str] = None
    # The turn's query embedding from the episodic stage, reused downstream.
    query_vec: Optional[tuple] = field(default=None, repr=False)
    # Decision telemetry (T4): enum/family codes and counts only, never text.
    intent: Optional[str] = None
    trigger: Optional[str] = None
    anchors: Optional[int] = None
    accepted_n: Optional[int] = None
    best_reason: Optional[str] = None

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
        fact_index: Optional[FactSemanticIndex] = None,
    ) -> None:
        self._mode = mode
        self._runtime = runtime
        self._cache_store = cache_store
        self._worker = worker
        self._indexer = indexer
        self._coordinator = coordinator
        self._fact_index = fact_index
        self._status = status or {"requested": mode, "effective": "OFF", "reason_code": "off_default"}
        self._stream_seq: int = 0
        self._run_id: Optional[str] = runtime.run_id if runtime else None
        self._shutdown = False
        # Exception TYPE name only (never the message) when init failed open.
        self.init_exception_type: Optional[str] = None
        self._warned_drop_reasons: set[str] = set()

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
        except Exception as exc:
            failed = cls(mode="OFF", status={"requested": "OFF", "effective": "INIT_FAILED", "reason_code": "init_exception"})
            failed.init_exception_type = type(exc).__name__
            return failed

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

            # Episodes that close inside this process (SHUTDOWN segmentation)
            # are embedded right away instead of waiting for the next
            # startup reconciliation. Runs on the runtime worker thread,
            # fail-open.
            rt.set_episodes_closed_listener(indexer.reconcile_unindexed_episodes)

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
                _oc_logger.info(
                    "memory_v5: startup_recovery episodes=%d closed=%d open=%d pruned=%d",
                    recovery_counts["episodes_materialized"],
                    recovery_counts["closed_episodes"],
                    recovery_counts["open_episodes"],
                    recovery_counts["pruned_orphan_membership"],
                )
            except Exception as rec_exc:
                _oc_logger.warning(
                    "memory_v5: startup_recovery failed exc=%s", type(rec_exc).__name__
                )

            try:
                indexed = indexer.reconcile_unindexed_episodes()
                _oc_logger.info("memory_v5: startup_reconcile indexed=%d", indexed)
            except Exception as rec_exc:
                _oc_logger.warning(
                    "memory_v5: startup_reconcile failed exc=%s", type(rec_exc).__name__
                )

            # ADR-060: fact-notebook vectors share the worker and the cache;
            # they are (re)built in the background on first use per profile.
            fact_index = FactSemanticIndex(cache_store, worker)

            status = {"requested": req_mode, "effective": req_mode, "reason_code": "ok"}
            return cls(
                mode=req_mode,
                runtime=rt,
                cache_store=cache_store,
                worker=worker,
                indexer=indexer,
                coordinator=coord,
                status=status,
                fact_index=fact_index,
            )
        except Exception as exc:
            # Type name only: messages can carry paths; the engine logs the
            # single startup line on the OpenCohost logger.
            logger.warning("Memory v5 initialization failed open: %s", type(exc).__name__)
            failed = cls(
                mode=req_mode,
                status={"requested": req_mode, "effective": "INIT_FAILED", "reason_code": "init_exception"},
            )
            failed.init_exception_type = type(exc).__name__
            return failed

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

    @property
    def fact_index(self) -> Optional[FactSemanticIndex]:
        return self._fact_index

    def fact_query_vector(
        self,
        query_text: str,
        profile_id: Optional[str],
        timeout_s: float,
        precomputed: Optional[Sequence[float]] = None,
    ) -> tuple[Optional[list[float]], str]:
        """Query embedding for fact ranking (ADR-060) -> (vector|None, code).

        Codes: none, disabled (no fact index), cold, timeout, error, skipped.
        Never raises; never embeds a fact row.
        """
        if self._fact_index is None:
            return None, FACT_FALLBACK_DISABLED
        if precomputed is not None:
            return self._fact_index.query_vector(
                profile_id or "", query_text, timeout_s, precomputed=precomputed
            )
        return self._fact_index.query_vector(profile_id or "", query_text, timeout_s)

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
        self,
        query_text: str,
        profile_id: Optional[str],
        reference_time=None,
        budget_ms: Optional[int] = None,
    ) -> RecallDecision:
        """Application-side deterministic retrieval verdict for one query.

        *budget_ms* bounds the worker IPC wait for the query embedding (the
        only blocking cross-process call); None keeps the worker default.
        Reasons: DISABLED (no coordinator / not ACTIVE), NO_RECALL (no
        profile or query), RECALL_EXCEPTION, else the coordinator's code.
        """
        if self._coordinator is None:
            return RecallDecision.empty(reason="DISABLED")
        if not profile_id or not query_text:
            return RecallDecision.empty()
        try:
            kwargs = {"reference_time": reference_time}
            if budget_ms is not None:
                kwargs["embed_timeout_s"] = max(0.0, budget_ms / 1000.0)
            packet = self._coordinator.process_query(
                query_text, profile_id=profile_id, **kwargs
            )
            if packet is None or packet.mode != RecallMode.ACTIVE:
                return RecallDecision.empty(reason="DISABLED")
            scope = packet.scope.value if hasattr(packet.scope, "value") else str(packet.scope)
            hit = bool(packet.formatted_block)
            return RecallDecision(
                block=packet.formatted_block,
                scope=scope,
                hit=hit,
                reason=packet.reason_code,
                token_estimate=packet.token_estimate,
                top_score=getattr(packet, "top_score", None),
                episodes=tuple(packet.retrieved_episodes or ()),
                embed_ms=getattr(packet, "embed_ms", None),
                worker_wait_ms=getattr(packet, "worker_wait_ms", None),
                retrieval_ms=getattr(packet, "retrieval_ms", None),
                device=getattr(packet, "device", None),
                query_vec=getattr(packet, "query_vec", None),
                intent=getattr(packet, "intent", None),
                trigger=getattr(packet, "trigger", None),
                anchors=getattr(packet, "anchors", None),
                accepted_n=getattr(packet, "accepted_n", None),
                best_reason=getattr(packet, "best_reason", None),
            )
        except Exception as e_exc:
            # Type only: exception messages can carry query or corpus text.
            logger.warning("Episodic recall query failed open: %s", type(e_exc).__name__)
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
            except Exception as exc:
                reason = "snapshot_construction_or_enqueue_exception"
                self._warn_drop_once(reason, type(exc).__name__)
                try:
                    rt.record_drop(2, reason)
                except Exception:
                    pass
        return None

    def _warn_drop_once(self, reason: str, exc_type: str) -> None:
        """WARNING on the OpenCohost logger, once per (reason, type). Codes only."""
        key = f"{reason}:{exc_type}"
        if key in self._warned_drop_reasons:
            return
        self._warned_drop_reasons.add(key)
        _oc_logger.warning("memory_v5: drop reason=%s exc=%s", reason, exc_type)

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
                # The runtime exposes no purge of its own (privacy goes through
                # the controller, an ordered barrier on the single writer).
                res = ShadowPrivacyController(self._runtime).purge_profile(
                    profile_id, timeout=timeout_s
                )["status"]
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

    def _drop_episode_embeddings(self, episode_ids) -> None:
        """Remove cached vectors/tokens of episodes that lost or expired content."""
        if self._cache_store is None:
            return
        for eid in episode_ids:
            try:
                self._cache_store.delete_episode_cache(eid)
            except Exception:
                pass

    def forget_user_turns(self, profile_id: str, matcher) -> int:
        """Forget the exchanges whose owner text satisfies *matcher*.

        Used when one memoria row is deleted: v5 keeps no link from a row to the
        turns it was distilled from, but the row's stable key is derived
        deterministically from the owner's words, so the caller passes a
        matcher over the evidence text. Removes the owner turn and Kira's reply,
        the affected episodes' membership and embeddings. Raises on a store
        failure (the caller decides how to report it); returns events deleted.
        """
        rt = self._runtime
        if rt is None:
            return 0
        out = rt._store.delete_user_turns_matching(profile_id, matcher)
        self._drop_episode_embeddings(out["episode_ids"])
        return int(out["events"])

    def run_retention(
        self,
        ttl_days: Optional[int] = None,
        now=None,
        batch_limit: Optional[int] = None,
    ) -> dict:
        """Bounded evidence-journal TTL job. Fail-open: never raises.

        Deletes evidence older than *ttl_days* (setting default), expired
        closed sessions/episodes, and their cached embeddings. ``ttl_days`` of
        0 keeps everything. Returns counts only, never text.
        """
        from datetime import datetime, timedelta, timezone

        rt = self._runtime
        if rt is None:
            return {"events": 0, "episodes": 0}
        try:
            from opencohost.config import settings as _settings

            days = _settings.MEMORY_V5_EVIDENCE_TTL_DAYS if ttl_days is None else int(ttl_days)
            limit = (
                _settings.MEMORY_V5_RETENTION_BATCH_LIMIT if batch_limit is None else int(batch_limit)
            )
            if days <= 0:
                return {"events": 0, "episodes": 0}
            ref = now or datetime.now(timezone.utc)
            cutoff = (ref - timedelta(days=days)).astimezone(timezone.utc)
            cutoff_iso = cutoff.isoformat().replace("+00:00", "Z")
            out = rt._store.purge_older_than(cutoff_iso, limit=limit)
            self._drop_episode_embeddings(out["episode_ids"])
            return {"events": int(out["events"]), "episodes": len(out["episode_ids"])}
        except Exception as exc:
            return {"events": 0, "episodes": 0, "error": type(exc).__name__}

    def forget_all(self, timeout_s: float = 2.0) -> bool:
        """Forget all memory records across runtime and semantic cache store."""
        success = True
        if self._runtime is not None:
            try:
                res = ShadowPrivacyController(self._runtime).forget_all(
                    timeout=timeout_s
                )["status"]
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

        self._fact_index = None
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

    def __del__(self) -> None:
        try:
            self.shutdown(timeout_s=0.5)
        except Exception:
            pass
