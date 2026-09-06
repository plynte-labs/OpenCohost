"""
Incremental Semantic Indexer for Memory v5.

Triggered upon Episode closure or during startup reconciliation.
Resolves Evidence strictly via episode_membership.event_id ordered by sequence_index.
Extracts ConversationalExchanges, embeds them via the isolated SemanticWorkerService,
and populates the derived SemanticCacheStore.
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from opencohost.core.memory.memoria_store import (
    _SCORING_STOPWORDS,
    _significant_tokens,
)
from opencohost.core.memory_v5_shadow.semantic import (
    build_episode_semantic_representation,
    extract_exchanges_from_events,
    load_episode_evidence_events,
)
from opencohost.core.memory_v5_shadow.semantic_cache import (
    EpisodeEmbeddingRecord,
    ExchangeEmbeddingRecord,
    SemanticCacheStore,
)
from opencohost.core.memory_v5_shadow.semantic_worker import SemanticWorkerService

logger = logging.getLogger(__name__)


class IncrementalSemanticIndexer:
    def __init__(
        self,
        shadow_conn: Any,
        cache_store: SemanticCacheStore,
        worker: SemanticWorkerService,
        model_id: str = "minilm-l12",
        model_version: str = "v1",
    ) -> None:
        self.shadow_conn = shadow_conn
        self.cache_store = cache_store
        self.worker = worker
        self.model_id = model_id
        self.model_version = model_version

    def index_episode(self, episode_id: str) -> bool:
        """
        Index a single Episode into the derived semantic cache.
        Returns True if indexed, False if skipped/failed.
        """
        # 1. Fetch Episode metadata
        cur = self.shadow_conn.execute(
            "SELECT episode_id, profile_id, session_id, state, started_at FROM episodes WHERE episode_id = ?",
            (episode_id,),
        )
        ep_row = cur.fetchone()
        if not ep_row:
            logger.warning("Episode %s not found in shadow store.", episode_id)
            return False

        profile_id = ep_row[1] if isinstance(ep_row, tuple) else ep_row["profile_id"]
        session_id = ep_row[2] if isinstance(ep_row, tuple) else ep_row["session_id"]
        started_at = ep_row[4] if isinstance(ep_row, tuple) else ep_row["started_at"]

        # 2. Resolve Evidence strictly via episode_membership.event_id ordered by sequence_index
        raw_events = load_episode_evidence_events(self.shadow_conn, episode_id)
        if not raw_events:
            return False

        # 3. Extract complete ConversationalExchanges
        exchanges, _ = extract_exchanges_from_events(raw_events, session_id=session_id)
        if not exchanges:
            return False

        # 4. Batch embed unique exchange texts via isolated worker
        unique_texts: list[str] = []
        text_to_keys: dict[str, list[str]] = {}
        for ex in exchanges:
            key = f"{ex.user_event_id}:{ex.assistant_event_id}"
            t = ex.combined_text
            if t not in text_to_keys:
                text_to_keys[t] = []
                unique_texts.append(t)
            text_to_keys[t].append(key)

        vectors = self.worker.embed_batch(unique_texts)
        if vectors is None or len(vectors) != len(unique_texts):
            logger.warning("Semantic worker failed to embed exchanges for episode %s.", episode_id)
            return False

        key_to_vec: dict[str, list[float]] = {}
        for text, vec in zip(unique_texts, vectors):
            for k in text_to_keys[text]:
                key_to_vec[k] = vec

        now_iso = datetime.now(timezone.utc).isoformat()

        # 5. Persist ExchangeEmbeddingRecords with real lexical tokens
        ex_vecs: list[list[float]] = []
        for ex in exchanges:
            key = f"{ex.user_event_id}:{ex.assistant_event_id}"
            vec = key_to_vec[key]
            ex_vecs.append(vec)
            content_h = hashlib.sha256(ex.combined_text.encode("utf-8")).hexdigest()[:16]

            # Extract normalized tokens minus domain & scoring stopwords
            comb_tokens = set(_significant_tokens(ex.combined_text)) - _SCORING_STOPWORDS
            lexical_tokens_str = " ".join(sorted(comb_tokens))

            rec = ExchangeEmbeddingRecord(
                exchange_key=key,
                profile_id=profile_id,
                session_id=session_id,
                episode_id=episode_id,
                content_hash=content_h,
                model_id=self.model_id,
                model_version=self.model_version,
                dimensions=len(vec),
                vector=vec,
                created_at=started_at,
                lexical_tokens=lexical_tokens_str,
            )
            self.cache_store.insert_exchange_embedding(rec)

        # 6. Compute aggregate Episode representation (normalized centroid + cohesion)
        rep = build_episode_semantic_representation(
            episode_id=episode_id,
            session_id=session_id,
            profile_id=profile_id,
            exchange_vectors=ex_vecs,
        )
        if rep is not None:
            all_texts = [ex.combined_text for ex in exchanges]
            combined_hash = hashlib.sha256("".join(all_texts).encode("utf-8")).hexdigest()[:16]
            ep_rec = EpisodeEmbeddingRecord(
                episode_id=episode_id,
                profile_id=profile_id,
                model_id=self.model_id,
                model_version=self.model_version,
                vector=rep.vector,
                cohesion_mean=rep.cohesion.mean_exchange_to_centroid,
                cohesion_min=rep.cohesion.min_exchange_to_centroid,
                content_fingerprint=combined_hash,
                created_at=started_at,
            )
            self.cache_store.insert_episode_embedding(ep_rec)

        return True

    def reconcile_unindexed_episodes(self) -> int:
        """
        Detect closed episodes missing in episode_embeddings or stale due to
        model_id, model_version, or content_fingerprint mismatch, and rebuild them.
        Returns the number of newly or re-indexed episodes.
        """
        cur = self.shadow_conn.execute(
            "SELECT episode_id, session_id FROM episodes WHERE state = 'CLOSED'"
        )
        closed_rows = cur.fetchall()

        indexed_count = 0
        for r in closed_rows:
            eid = r[0] if isinstance(r, tuple) else r["episode_id"]
            sid = r[1] if isinstance(r, tuple) else r["session_id"]

            cached_ep = self.cache_store.get_episode_by_id(eid)
            if cached_ep is None:
                if self.index_episode(eid):
                    indexed_count += 1
                continue

            # Staleness validation: check model identity
            if cached_ep.model_id != self.model_id or cached_ep.model_version != self.model_version:
                logger.info("Episode %s cache is stale (model version mismatch). Rebuilding.", eid)
                self.cache_store.delete_episode_cache(eid)
                if self.index_episode(eid):
                    indexed_count += 1
                continue

            # Staleness validation: check content fingerprint
            raw_events = load_episode_evidence_events(self.shadow_conn, eid)
            exs, _ = extract_exchanges_from_events(raw_events, session_id=sid)
            texts = [ex.combined_text for ex in exs]
            current_fp = hashlib.sha256("".join(texts).encode("utf-8")).hexdigest()[:16]
            if cached_ep.content_fingerprint != current_fp:
                logger.info("Episode %s cache is stale (content fingerprint mismatch). Rebuilding.", eid)
                self.cache_store.delete_episode_cache(eid)
                if self.index_episode(eid):
                    indexed_count += 1

        return indexed_count
