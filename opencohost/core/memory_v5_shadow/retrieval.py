"""
Candidate Retrieval, Hybrid Ranking, and MMR Diversity for Memory v5.

Enforces same-profile candidate generation from the beginning.
Evaluates similarity against ConversationalExchange vectors first, mapped to parent Episodes.
Applies hybrid scoring (semantic + lexical + temporal + recency + cohesion) and MMR deduplication.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np

from opencohost.core.memory_v5_shadow.query_analyzer import (
    EpisodicQuery,
    RecallIntent,
    TemporalConstraintType,
)
from opencohost.core.memory_v5_shadow.semantic_cache import (
    SemanticCacheStore,
)


@dataclass(frozen=True)
class CandidateExchange:
    exchange_key: str
    profile_id: str
    session_id: str
    episode_id: str
    created_at: str
    vector: list[float]
    lexical_tokens: str = ""


class CandidateRetriever:
    def __init__(self, cache_store: SemanticCacheStore) -> None:
        self.cache_store = cache_store

    def get_candidates(
        self,
        query: EpisodicQuery,
        query_vector: list[float],
        reference_time: Optional[datetime] = None,
    ) -> list[CandidateExchange]:
        """
        Retrieve candidate exchanges strictly filtered by profile_id and temporal constraint.
        Never retrieves cross-profile.
        """
        # Hard profile isolation: only query the cache for query.profile_id
        ex_records = self.cache_store.get_exchange_embeddings_by_profile(query.profile_id)
        if not ex_records:
            return []

        # If fixed-window temporal constraint is present, filter allowed episodes first
        if (
            query.temporal_constraint is not None
            and query.temporal_constraint.constraint_type != TemporalConstraintType.LAST_TIME
        ):
            ep_records = self.cache_store.get_episode_embeddings_by_profile(query.profile_id)
            allowed_episode_ids = {
                ep.episode_id
                for ep in ep_records
                if query.temporal_constraint.matches(ep.created_at, reference_time)
            }
            ex_records = [ex for ex in ex_records if ex.episode_id in allowed_episode_ids]

        return [
            CandidateExchange(
                exchange_key=r.exchange_key,
                profile_id=r.profile_id,
                session_id=r.session_id,
                episode_id=r.episode_id,
                created_at=r.created_at,
                vector=r.vector,
                lexical_tokens=r.lexical_tokens,
            )
            for r in ex_records
        ]


@dataclass(frozen=True)
class CandidateScoreResult:
    accepted: bool
    score: float
    reason_code: str


@dataclass(frozen=True)
class RankedCandidateEpisode:
    episode_id: str
    session_id: str
    profile_id: str
    best_exchange_key: str
    composite_score: float
    exchange_semantic_score: float
    lexical_score: float
    vector: list[float]
    reason_code: str


def _cosine(v1: list[float], v2: list[float]) -> float:
    a1 = np.array(v1, dtype=np.float32)
    a2 = np.array(v2, dtype=np.float32)
    dot = float(np.dot(a1, a2))
    n1 = float(np.linalg.norm(a1))
    n2 = float(np.linalg.norm(a2))
    if n1 > 1e-9 and n2 > 1e-9:
        return dot / (n1 * n2)
    return 0.0


class HybridEpisodicRanker:
    """
    Ranks candidate episodes combining:
    - exchange_cosine (highest exchange match within Episode)
    - episode_cosine (Episode aggregate centroid match)
    - lexical_score (significant token overlap with candidate exchange)
    - cohesion_mean (internal cohesion of the Episode)
    - recency_prior (exponential decay based on time_delta_days)
    """

    def __init__(self) -> None:
        pass

    def score_candidate(
        self,
        exchange_cosine: float,
        episode_cosine: float,
        lexical_score: float,
        cohesion_mean: float,
        time_delta_days: float,
        intent: RecallIntent,
        has_lexical_anchors: bool = False,
    ) -> CandidateScoreResult:
        recency_prior = math.exp(-0.01 * max(0.0, time_delta_days))

        if intent == RecallIntent.EXPLICIT:
            if exchange_cosine < 0.35:
                return CandidateScoreResult(False, exchange_cosine, "REJECTED_LOW_SEMANTIC")
            if has_lexical_anchors and lexical_score <= 0.0:
                return CandidateScoreResult(False, exchange_cosine, "REJECTED_LEXICAL_UNSUBSTANTIATED")
            if exchange_cosine < 0.55 and lexical_score <= 0.0:
                return CandidateScoreResult(False, exchange_cosine, "REJECTED_LOW_SEMANTIC")

            score = (
                0.40 * exchange_cosine
                + 0.25 * lexical_score
                + 0.15 * episode_cosine
                + 0.10 * cohesion_mean
                + 0.10 * recency_prior
            )
            return CandidateScoreResult(True, round(score, 4), "ACCEPTED_EXPLICIT_CORROBORATED")

        else:  # IMPLICIT or NONE (opportunistic)
            # NONE reuses the IMPLICIT strict cutoffs verbatim — no numeric
            # threshold tuning. Its extra strictness comes from the
            # deterministic lexical/cache preflight in the coordinator, which
            # only lets anchored queries reach this scorer at all.
            if exchange_cosine < 0.78:
                return CandidateScoreResult(False, exchange_cosine, "REJECTED_LOW_SEMANTIC")
            if exchange_cosine < 0.85 and lexical_score <= 0.0:
                return CandidateScoreResult(False, exchange_cosine, "REJECTED_LEXICAL_UNSUBSTANTIATED")

            score = (
                0.45 * exchange_cosine
                + 0.25 * lexical_score
                + 0.10 * episode_cosine
                + 0.10 * cohesion_mean
                + 0.10 * recency_prior
            )
            accepted_code = (
                "ACCEPTED_OPPORTUNISTIC_CORROBORATED"
                if intent == RecallIntent.NONE
                else "ACCEPTED_IMPLICIT_HIGH_CONFIDENCE"
            )
            return CandidateScoreResult(True, round(score, 4), accepted_code)

    def apply_mmr_diversity(
        self,
        candidates: list[RankedCandidateEpisode],
        max_episodes: int = 3,
        lambda_param: float = 0.7,
    ) -> list[RankedCandidateEpisode]:
        if not candidates:
            return []

        remaining = list(candidates)
        # Sort initial by composite score
        remaining.sort(key=lambda x: x.composite_score, reverse=True)

        selected: list[RankedCandidateEpisode] = [remaining.pop(0)]

        while remaining and len(selected) < max_episodes:
            best_mmr = -float("inf")
            best_idx = 0

            for idx, cand in enumerate(remaining):
                max_sim = max(_cosine(cand.vector, s.vector) for s in selected)
                mmr_score = lambda_param * cand.composite_score - (1.0 - lambda_param) * max_sim

                if mmr_score > best_mmr:
                    best_mmr = mmr_score
                    best_idx = idx

            selected.append(remaining.pop(best_idx))

        return selected
