"""Semantic index over the `memorias` fact notebook (ADR-060).

Durable fact rows (promoted, curated, imported, summary) are embedded with
the same MiniLM worker process as the v5 episodes and stored in the same
derived semantic cache (`fact_embeddings`). Drafts, private and inactive
rows are never embedded.

A cached vector is valid only while (row id, revision, content hash, model)
all still match the live row: `update_row` rewrites content without bumping
revision, so the hash is what catches operator edits and promotions.

Hot-path contract: the turn never embeds a row. `view()` scores the current
candidates against a query vector using only valid cached vectors and says
whether the cache is out of date; `schedule_reconcile()` then re-embeds and
drops entries in the background (reconcile-on-read). The cache persists
across restarts, so only changed rows are ever re-embedded.

Logs carry counts, ids and codes only — never row or query text.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Optional, Sequence

import numpy as np

from opencohost.core.memory_v5_shadow.semantic_cache import (
    FactEmbeddingRecord,
    SemanticCacheStore,
)

_oc_logger = logging.getLogger("OpenCohost")

SEMANTIC_FACT_STATUSES = frozenset({"promoted", "curated", "imported", "summary"})

# Small batches: the worker is single-threaded and a MiniLM batch costs
# ~90ms per row (256-token padding), so 8 rows hold it ~0.7s at most and a
# turn's query embedding never queues behind a whole-notebook batch.
_BATCH_SIZE = 8
_BATCH_TIMEOUT_S = 30.0

FACT_FALLBACK_NONE = "none"
FACT_FALLBACK_DISABLED = "disabled"
FACT_FALLBACK_COLD = "cold"
FACT_FALLBACK_TIMEOUT = "timeout"
FACT_FALLBACK_ERROR = "error"
FACT_FALLBACK_SKIPPED = "skipped"


def _field(row: Any, name: str, default: Any = None) -> Any:
    try:
        value = row[name]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def content_fingerprint(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def is_semantic_eligible(row: Any) -> bool:
    return (
        str(_field(row, "status", "")) in SEMANTIC_FACT_STATUSES
        and not int(_field(row, "private", 0) or 0)
        and not int(_field(row, "inactive", 0) or 0)
    )


@dataclass(frozen=True)
class _FactKey:
    fact_id: str
    revision: int
    content_hash: str
    content: str = field(repr=False, compare=False)


@dataclass(frozen=True)
class FactReconcileResult:
    indexed: int = 0
    dropped: int = 0
    kept: int = 0
    failed: int = 0


@dataclass(frozen=True)
class FactIndexView:
    """Valid cosines for the current candidates + whether the cache is stale."""

    scores: dict
    needs_reconcile: bool


def _default_scheduler(fn: Callable[[], Any]) -> threading.Thread:
    thread = threading.Thread(target=fn, name="memory-fact-reconcile", daemon=True)
    thread.start()
    return thread


class FactSemanticIndex:
    def __init__(
        self,
        cache_store: SemanticCacheStore,
        worker: Any,
        *,
        scheduler: Optional[Callable[[Callable[[], Any]], Any]] = None,
        model_id: str = "minilm-l12",
        model_version: str = "v1",
        batch_size: int = _BATCH_SIZE,
    ) -> None:
        self.cache_store = cache_store
        self.worker = worker
        self.model_id = model_id
        self.model_version = model_version
        self._scheduler = scheduler or _default_scheduler
        self._batch_size = max(1, int(batch_size))
        self._inflight: set[str] = set()
        self._inflight_lock = threading.Lock()

    # -- keys -------------------------------------------------------------

    def _desired(self, profile_id: str, rows: Iterable[Any]) -> dict[str, _FactKey]:
        desired: dict[str, _FactKey] = {}
        for row in rows:
            if str(_field(row, "profile_id", profile_id)) != profile_id:
                continue
            if not is_semantic_eligible(row):
                continue
            content = str(_field(row, "content", "") or "")
            if not content.strip():
                continue
            fid = str(_field(row, "id", ""))
            if not fid:
                continue
            desired[fid] = _FactKey(
                fid, int(_field(row, "revision", 1) or 1), content_fingerprint(content), content
            )
        return desired

    def _is_valid(self, rec: FactEmbeddingRecord, key: _FactKey) -> bool:
        return (
            rec.revision == key.revision
            and rec.content_hash == key.content_hash
            and rec.model_id == self.model_id
            and rec.model_version == self.model_version
        )

    # -- query time ---------------------------------------------------------

    def query_vector(
        self,
        profile_id: str,
        text: str,
        timeout_s: float,
        *,
        precomputed: Optional[Sequence[float]] = None,
    ) -> tuple[Optional[list[float]], str]:
        """Embed the turn's query, bounded by *timeout_s*. Never raises.

        *precomputed* is the vector the episodic stage already embedded for
        this same turn (memory-grounding T2): no worker call is spent and the
        budget no longer matters; the cold check still applies.

        Returns (vector, "none") or (None, fallback code). A profile with no
        fact embedding at all is "cold": no IPC is spent on it.
        """
        if not profile_id or not (text or "").strip():
            return None, FACT_FALLBACK_SKIPPED
        try:
            if self.cache_store.count_fact_embeddings(profile_id) == 0:
                return None, FACT_FALLBACK_COLD
            if precomputed is not None:
                return list(precomputed), FACT_FALLBACK_NONE
            if timeout_s <= 0:
                return None, FACT_FALLBACK_TIMEOUT
            vec = self.worker.embed_query(text, timeout_s=timeout_s)
        except Exception:
            return None, FACT_FALLBACK_ERROR
        if vec is None:
            return None, FACT_FALLBACK_TIMEOUT
        return list(vec), FACT_FALLBACK_NONE

    def view(
        self,
        profile_id: str,
        rows: Iterable[Any],
        query_vec: Optional[list[float]],
        *,
        ignore_orphans: bool = False,
    ) -> FactIndexView:
        """Cosines for eligible *rows* with a VALID cached vector. Pure read."""
        rows = list(rows)
        desired = self._desired(profile_id, rows)
        cached = {rec.fact_id: rec for rec in self.cache_store.get_fact_embeddings_by_profile(profile_id)}
        needs = False if ignore_orphans else any(fid not in desired for fid in cached)
        valid: dict[str, FactEmbeddingRecord] = {}
        for fid, key in desired.items():
            rec = cached.get(fid)
            if rec is not None and self._is_valid(rec, key):
                valid[fid] = rec
            else:
                needs = True
        scores: dict[str, float] = {}
        if query_vec is not None and valid:
            q = np.asarray(query_vec, dtype=np.float32)
            for fid, rec in valid.items():
                v = np.asarray(rec.vector, dtype=np.float32)
                if v.shape == q.shape:
                    scores[fid] = float(np.dot(q, v))
        return FactIndexView(scores=scores, needs_reconcile=needs)

    def remove_fact_embedding(self, fact_id: str) -> None:
        """Drop one deleted or invalidated fact's cached vector immediately. Fail-open."""
        fid = str(fact_id or "").strip()
        if not fid:
            return
        try:
            self.cache_store.delete_fact_embeddings([fid])
        except Exception as exc:
            _oc_logger.debug(
                "memory_facts: remove_fact_embedding skipped exc=%s", type(exc).__name__
            )

    # -- background -----------------------------------------------------------

    def reconcile(self, profile_id: str, rows: Iterable[Any]) -> FactReconcileResult:
        """Bring the profile's fact vectors in line with *rows*. Blocking.

        Drops vectors of rows that are gone, private, inactive, draft or
        stale; embeds missing/stale ones in small batches through the worker.
        A failed batch stops the pass (retried on the next reconcile).
        """
        desired = self._desired(profile_id, rows)
        cached = {rec.fact_id: rec for rec in self.cache_store.get_fact_embeddings_by_profile(profile_id)}
        gone = [fid for fid in cached if fid not in desired]
        stale = [
            fid for fid, rec in cached.items()
            if fid in desired and not self._is_valid(rec, desired[fid])
        ]
        # A stale vector is deleted BEFORE re-embedding, so a failed batch
        # never leaves an outdated vector behind.
        self.cache_store.delete_fact_embeddings(gone + stale)
        pending = [key for fid, key in desired.items() if fid not in cached or fid in stale]
        kept = len(desired) - len(pending)
        indexed = 0
        failed = 0
        for start in range(0, len(pending), self._batch_size):
            chunk = pending[start:start + self._batch_size]
            try:
                vecs = self.worker.embed_batch([k.content for k in chunk], timeout_s=_BATCH_TIMEOUT_S)
            except Exception:
                vecs = None
            if vecs is None or len(vecs) != len(chunk):
                failed = len(pending) - start
                break
            now_iso = datetime.now(timezone.utc).isoformat()
            for key, vec in zip(chunk, vecs):
                if self.cache_store.upsert_fact_embedding(
                    FactEmbeddingRecord(
                        fact_id=key.fact_id,
                        profile_id=profile_id,
                        revision=key.revision,
                        content_hash=key.content_hash,
                        model_id=self.model_id,
                        model_version=self.model_version,
                        vector=list(vec),
                        created_at=now_iso,
                    )
                ):
                    indexed += 1
        return FactReconcileResult(indexed=indexed, dropped=len(gone), kept=kept, failed=failed)

    def schedule_reconcile(self, profile_id: str, rows: Iterable[Any]) -> Any:
        """Run reconcile() off the turn, single-flight per profile.

        Rows are snapshotted into plain dicts first (the caller's sqlite
        rows are not shared with the background job). Returns the
        scheduler's handle, or None when a pass is already in flight.
        """
        snapshot = [
            {name: _field(row, name) for name in
             ("id", "profile_id", "revision", "status", "private", "inactive", "content")}
            for row in rows
        ]
        with self._inflight_lock:
            if profile_id in self._inflight:
                return None
            self._inflight.add(profile_id)

        def _job() -> None:
            started = time.monotonic()
            try:
                result = self.reconcile(profile_id, snapshot)
                _oc_logger.info(
                    "memory_facts: reconcile profile_id=%s indexed=%d dropped=%d kept=%d failed=%d ms=%d",
                    profile_id, result.indexed, result.dropped, result.kept, result.failed,
                    int((time.monotonic() - started) * 1000),
                )
            except Exception as exc:
                _oc_logger.warning(
                    "memory_facts: reconcile failed profile_id=%s exc=%s", profile_id, type(exc).__name__
                )
            finally:
                with self._inflight_lock:
                    self._inflight.discard(profile_id)

        try:
            return self._scheduler(_job)
        except Exception as exc:
            with self._inflight_lock:
                self._inflight.discard(profile_id)
            _oc_logger.warning("memory_facts: schedule failed exc=%s", type(exc).__name__)
            return None
