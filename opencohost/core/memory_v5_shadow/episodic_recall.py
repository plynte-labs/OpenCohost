"""
Episodic Recall Coordinator and Packet Builder for Memory v5.

Coordinates:
Query Analysis -> Worker Embedding -> Candidate Retrieval -> Hybrid Ranking -> MMR Diversity
-> Context Expansion -> EpisodicRecallPacket -> Prompt Context Integration.
Supports OFF, SHADOW (prompt unchanged), and ACTIVE (injected into prompt) modes.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

import numpy as np

from opencohost.core.memory_v5_shadow.query_analyzer import (
    EpisodicQuery,
    EpisodicQueryAnalyzer,
    RecallIntent,
    TemporalConstraintType,
)
from opencohost.core.memory_v5_shadow.retrieval import (
    CandidateRetriever,
    HybridEpisodicRanker,
    RankedCandidateEpisode,
    _cosine,
)
from opencohost.core.memory_v5_shadow.semantic import (
    ConversationalExchange,
    extract_exchanges_from_events,
    load_episode_evidence_events,
)
from opencohost.core.memory_v5_shadow.semantic_cache import SemanticCacheStore
from opencohost.core.memory_v5_shadow.semantic_worker import SemanticWorkerService

logger = logging.getLogger(__name__)


class RecallMode(str, Enum):
    OFF = "OFF"
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"


@dataclass(frozen=True)
class RetrievedEpisodeContext:
    episode_id: str
    session_id: str
    started_at: str
    composite_score: float
    exchanges: list[ConversationalExchange]


@dataclass(frozen=True)
class EpisodicRecallPacket:
    query_hash: str
    profile_id: str
    mode: RecallMode
    retrieved_episodes: list[RetrievedEpisodeContext]
    formatted_block: str
    token_estimate: int
    reason_code: str


_EPISODIC_PREAMBLE = (
    "<episodic_memory>\n"
    "These excerpts are from previous conversations with this same profile.\n\n"
    "They describe what was previously discussed, believed, planned or experienced.\n"
    "They are not automatically current factual truth.\n"
    "Prefer the current user message over old memory when they conflict.\n"
)


def _format_episodic_block(episodes: list[RetrievedEpisodeContext]) -> str:
    if not episodes:
        return ""

    header = _EPISODIC_PREAMBLE

    body_parts = []
    for ep in episodes:
        ep_header = f"\n[Episode: {ep.started_at}]"
        turns = []
        for ex in ep.exchanges:
            turns.append(f"User: {ex.user_text}\nKira: {ex.assistant_text}")
        body_parts.append(ep_header + "\n" + "\n\n".join(turns))

    footer = "\n</episodic_memory>"
    return header + "".join(body_parts) + footer


class EpisodicRecallCoordinator:
    def __init__(
        self,
        shadow_conn: Any,
        cache_store: SemanticCacheStore,
        worker: SemanticWorkerService,
        mode: RecallMode = RecallMode.SHADOW,
        query_analyzer: Optional[EpisodicQueryAnalyzer] = None,
        ranker: Optional[HybridEpisodicRanker] = None,
        max_episodes: int = 3,
        max_exchanges_per_ep: int = 3,
        max_token_budget: int = 1200,
    ) -> None:
        self.shadow_conn = shadow_conn
        self.cache_store = cache_store
        self.worker = worker
        self.mode = mode
        self.query_analyzer = query_analyzer or EpisodicQueryAnalyzer()
        self.ranker = ranker or HybridEpisodicRanker()
        self.retriever = CandidateRetriever(cache_store)
        self.max_episodes = max_episodes
        self.max_exchanges_per_ep = max_exchanges_per_ep
        self.max_token_budget = max_token_budget

    def process_query(
        self,
        query_text: str,
        profile_id: str,
        reference_time: Optional[datetime] = None,
    ) -> Optional[EpisodicRecallPacket]:
        if self.mode == RecallMode.OFF:
            return None

        q_hash = hashlib.sha256(query_text.encode("utf-8")).hexdigest()[:16]

        # 1. Query understanding
        query = self.query_analyzer.analyze(query_text, profile_id=profile_id, reference_time=reference_time)
        if query.recall_intent == RecallIntent.NONE:
            return EpisodicRecallPacket(
                query_hash=q_hash,
                profile_id=profile_id,
                mode=self.mode,
                retrieved_episodes=[],
                formatted_block="",
                token_estimate=0,
                reason_code="NO_RECALL_INTENT",
            )

        # 2. Query embedding via isolated worker
        query_vec = self.worker.embed_query(query.normalized_query)
        if query_vec is None:
            logger.warning("Worker embedding failed or timed out; fail-open to NO_RECALL.")
            return EpisodicRecallPacket(
                query_hash=q_hash,
                profile_id=profile_id,
                mode=self.mode,
                retrieved_episodes=[],
                formatted_block="",
                token_estimate=0,
                reason_code="WORKER_UNAVAILABLE",
            )

        # 3. Candidate retrieval (same-profile + temporal hard filter)
        cand_exchanges = self.retriever.get_candidates(query, query_vector=query_vec, reference_time=reference_time)
        if not cand_exchanges:
            return EpisodicRecallPacket(
                query_hash=q_hash,
                profile_id=profile_id,
                mode=self.mode,
                retrieved_episodes=[],
                formatted_block="",
                token_estimate=0,
                reason_code="NO_CANDIDATES",
            )

        # Pre-fetch episode metadata & cohesion
        ep_records = {
            ep.episode_id: ep for ep in self.cache_store.get_episode_embeddings_by_profile(profile_id)
        }

        # 4. Exchange scoring and grouping by Episode
        ep_to_best: dict[str, tuple[float, str, CandidateScoreResult, float]] = {}

        q_arr = np.array(query_vec, dtype=np.float32)
        q_norm = np.linalg.norm(q_arr)

        from opencohost.core.memory.memoria_store import (
            _SCORING_STOPWORDS,
            _significant_tokens,
        )

        q_tokens = set(_significant_tokens(query.normalized_query)) - _SCORING_STOPWORDS

        ref_dt = reference_time or datetime.now(timezone.utc)
        if ref_dt.tzinfo is None:
            ref_dt = ref_dt.replace(tzinfo=timezone.utc)

        for cand in cand_exchanges:
            v_arr = np.array(cand.vector, dtype=np.float32)
            ex_cos = float(np.dot(q_arr, v_arr) / (q_norm * np.linalg.norm(v_arr) + 1e-9))

            ep_rec = ep_records.get(cand.episode_id)
            ep_cos = _cosine(query_vec, ep_rec.vector) if ep_rec else ex_cos
            cohesion = ep_rec.cohesion_mean if ep_rec else 0.8

            # Real candidate-specific lexical score
            lex_score = 0.0
            if q_tokens and cand.lexical_tokens:
                cand_tokens = set(cand.lexical_tokens.split())
                shared = q_tokens & cand_tokens
                lex_score = round(len(shared) / max(1, len(q_tokens)), 4)

            # Candidate recency
            cand_dt = None
            if cand.created_at:
                try:
                    cand_dt = datetime.fromisoformat(cand.created_at.replace("Z", "+00:00"))
                except Exception:
                    pass
            if cand_dt is None:
                cand_dt = ref_dt

            time_delta_days = max(0.0, (ref_dt - cand_dt).total_seconds() / 86400.0)

            score_res = self.ranker.score_candidate(
                exchange_cosine=ex_cos,
                episode_cosine=ep_cos,
                lexical_score=lex_score,
                cohesion_mean=cohesion,
                time_delta_days=time_delta_days,
                intent=query.recall_intent,
                has_lexical_anchors=bool(query.lexical_anchors),
            )

            if score_res.accepted:
                curr = ep_to_best.get(cand.episode_id)
                if curr is None or score_res.score > curr[0]:
                    ep_to_best[cand.episode_id] = (score_res.score, cand.exchange_key, score_res, lex_score)
            else:
                last_rejected_reason = score_res.reason_code

        if not ep_to_best:
            return EpisodicRecallPacket(
                query_hash=q_hash,
                profile_id=profile_id,
                mode=self.mode,
                retrieved_episodes=[],
                formatted_block="",
                token_estimate=0,
                reason_code=last_rejected_reason,
            )

        # 5. Build RankedCandidateEpisode list with actual best lexical score
        ranked_eps: list[RankedCandidateEpisode] = []
        for eid, (comp_score, best_k, s_res, best_lex) in ep_to_best.items():
            ep_rec = ep_records.get(eid)
            vec = ep_rec.vector if ep_rec else query_vec
            ranked_eps.append(
                RankedCandidateEpisode(
                    episode_id=eid,
                    session_id="",
                    profile_id=profile_id,
                    best_exchange_key=best_k,
                    composite_score=comp_score,
                    exchange_semantic_score=s_res.score,
                    lexical_score=best_lex,
                    vector=vec,
                    reason_code=s_res.reason_code,
                )
            )

        # 6. Candidate Selection: LAST_TIME selects most recent compatible, otherwise MMR Diversity
        if query.temporal_constraint and query.temporal_constraint.constraint_type == TemporalConstraintType.LAST_TIME:
            placeholders = ",".join("?" for _ in ranked_eps)
            cur = self.shadow_conn.execute(
                f"SELECT episode_id, started_at FROM episodes WHERE episode_id IN ({placeholders})",
                [r.episode_id for r in ranked_eps],
            )
            ep_starts = {
                (r[0] if isinstance(r, tuple) else r["episode_id"]): (r[1] if isinstance(r, tuple) else r["started_at"])
                for r in cur.fetchall()
            }
            selected_eps = sorted(ranked_eps, key=lambda r: ep_starts.get(r.episode_id, ""), reverse=True)[:1]
        else:
            selected_eps = self.ranker.apply_mmr_diversity(ranked_eps, max_episodes=self.max_episodes)

        # 7. Context Expansion: load actual exchanges from Episode evidence
        retrieved_contexts: list[RetrievedEpisodeContext] = []
        for sel in selected_eps:
            cur = self.shadow_conn.execute(
                "SELECT session_id, started_at FROM episodes WHERE episode_id = ?",
                (sel.episode_id,),
            )
            row = cur.fetchone()
            if row is None:
                continue
            sid = row[0] if isinstance(row, tuple) else row["session_id"]
            started_at = row[1] if isinstance(row, tuple) else row["started_at"]

            raw_events = load_episode_evidence_events(self.shadow_conn, sel.episode_id)
            exs, _ = extract_exchanges_from_events(raw_events, session_id=sid)

            # Find target exchange
            target_idx = -1
            for idx, ex in enumerate(exs):
                key = f"{ex.user_event_id}:{ex.assistant_event_id}"
                if key == sel.best_exchange_key:
                    target_idx = idx
                    break

            if target_idx == -1:
                logger.warning(
                    "Target exchange key %s not found in episode %s. Dropping stale candidate.",
                    sel.best_exchange_key,
                    sel.episode_id,
                )
                continue

            # Bounded window: best exchange ± 1 adjacent
            w_start = max(0, target_idx - 1)
            w_end = min(len(exs), target_idx + 2)
            window_exs = exs[w_start:w_end][: self.max_exchanges_per_ep]

            retrieved_contexts.append(
                RetrievedEpisodeContext(
                    episode_id=sel.episode_id,
                    session_id=sid,
                    started_at=started_at,
                    composite_score=sel.composite_score,
                    exchanges=window_exs,
                )
            )

        formatted = _format_episodic_block(retrieved_contexts)
        tokens = len(formatted) // 4

        # Enforce hard token budget across episodes
        while tokens > self.max_token_budget and len(retrieved_contexts) > 1:
            retrieved_contexts.pop()
            formatted = _format_episodic_block(retrieved_contexts)
            tokens = len(formatted) // 4

        # Enforce hard token budget within single remaining episode
        if tokens > self.max_token_budget and len(retrieved_contexts) == 1:
            single_ep = retrieved_contexts[0]
            while tokens > self.max_token_budget and len(single_ep.exchanges) > 1:
                single_ep = RetrievedEpisodeContext(
                    episode_id=single_ep.episode_id,
                    session_id=single_ep.session_id,
                    started_at=single_ep.started_at,
                    composite_score=single_ep.composite_score,
                    exchanges=single_ep.exchanges[: len(single_ep.exchanges) - 1],
                )
                retrieved_contexts = [single_ep]
                formatted = _format_episodic_block(retrieved_contexts)
                tokens = len(formatted) // 4

            if tokens > self.max_token_budget and len(single_ep.exchanges) == 1:
                max_chars = self.max_token_budget * 4
                overhead = len(_EPISODIC_PREAMBLE) + 80
                avail = max_chars - overhead
                if avail < 60:
                    retrieved_contexts = []
                    formatted = ""
                    tokens = 0
                else:
                    ex0 = single_ep.exchanges[0]
                    trimmed_ex = ConversationalExchange(
                        user_event_id=ex0.user_event_id,
                        assistant_event_id=ex0.assistant_event_id,
                        user_text=ex0.user_text[: avail // 2],
                        assistant_text=ex0.assistant_text[: avail // 2] + "...",
                        stream_sequence=ex0.stream_sequence,
                        occurred_at=ex0.occurred_at,
                    )
                    single_ep = RetrievedEpisodeContext(
                        episode_id=single_ep.episode_id,
                        session_id=single_ep.session_id,
                        started_at=single_ep.started_at,
                        composite_score=single_ep.composite_score,
                        exchanges=[trimmed_ex],
                    )
                    retrieved_contexts = [single_ep]
                    formatted = _format_episodic_block(retrieved_contexts)
                    tokens = len(formatted) // 4

        if not retrieved_contexts:
            final_reason = "DROPPED_TOKEN_BUDGET" if selected_eps else "NO_CANDIDATES"
        else:
            final_reason = selected_eps[0].reason_code if selected_eps else "NO_CANDIDATES"

        return EpisodicRecallPacket(
            query_hash=q_hash,
            profile_id=profile_id,
            mode=self.mode,
            retrieved_episodes=retrieved_contexts,
            formatted_block=formatted,
            token_estimate=tokens,
            reason_code=final_reason,
        )
