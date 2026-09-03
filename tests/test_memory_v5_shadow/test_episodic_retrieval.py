from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path
import pytest

from opencohost.core.memory_v5_shadow.query_analyzer import (
    EpisodicQuery,
    RecallIntent,
    TemporalConstraint,
    TemporalConstraintType,
)
from opencohost.core.memory_v5_shadow.retrieval import (
    CandidateRetriever,
    HybridEpisodicRanker,
    RankedCandidateEpisode,
)
from opencohost.core.memory_v5_shadow.semantic_cache import (
    EpisodeEmbeddingRecord,
    ExchangeEmbeddingRecord,
    SemanticCacheStore,
)


def test_retriever_hard_profile_isolation(tmp_path: Path):
    db_path = tmp_path / "cache.db"
    store = SemanticCacheStore(db_path)
    store.initialize()

    # Two profiles: prof_A and prof_B
    store.insert_exchange_embedding(
        ExchangeEmbeddingRecord("ex1", "prof_A", "s1", "ep1", "h1", "m", "v1", 3, [1.0, 0.0, 0.0], "2026-09-02T10:00:00Z")
    )
    store.insert_episode_embedding(
        EpisodeEmbeddingRecord("ep1", "prof_A", "m", "v1", [1.0, 0.0, 0.0], 0.9, 0.8, "fp1", "2026-09-02T10:00:00Z")
    )

    store.insert_exchange_embedding(
        ExchangeEmbeddingRecord("ex2", "prof_B", "s2", "ep2", "h2", "m", "v1", 3, [1.0, 0.0, 0.0], "2026-09-02T10:00:00Z")
    )
    store.insert_episode_embedding(
        EpisodeEmbeddingRecord("ep2", "prof_B", "m", "v1", [1.0, 0.0, 0.0], 0.9, 0.8, "fp2", "2026-09-02T10:00:00Z")
    )

    retriever = CandidateRetriever(store)
    query = EpisodicQuery(
        raw_text="¿Te acuerdas?",
        normalized_query="te acuerdas",
        recall_intent=RecallIntent.EXPLICIT,
        temporal_constraint=None,
        lexical_anchors=[],
        profile_id="prof_A",
    )

    candidates = retriever.get_candidates(query, query_vector=[1.0, 0.0, 0.0])
    # Must ONLY contain candidates for prof_A
    assert len(candidates) == 1
    assert candidates[0].profile_id == "prof_A"
    assert candidates[0].episode_id == "ep1"

    store.close()


def test_retriever_hard_temporal_filter(tmp_path: Path):
    db_path = tmp_path / "cache.db"
    store = SemanticCacheStore(db_path)
    store.initialize()

    # Episode 1: Last week (2026-08-27)
    store.insert_exchange_embedding(
        ExchangeEmbeddingRecord("ex_lw", "prof_1", "s1", "ep_lw", "h1", "m", "v1", 3, [1.0, 0.0, 0.0], "2026-08-27T10:00:00Z")
    )
    store.insert_episode_embedding(
        EpisodeEmbeddingRecord("ep_lw", "prof_1", "m", "v1", [1.0, 0.0, 0.0], 0.9, 0.8, "fp1", "2026-08-27T10:00:00Z")
    )

    # Episode 2: 3 months ago (2026-05-10) with exact identical vector
    store.insert_exchange_embedding(
        ExchangeEmbeddingRecord("ex_old", "prof_1", "s2", "ep_old", "h2", "m", "v1", 3, [1.0, 0.0, 0.0], "2026-05-10T10:00:00Z")
    )
    store.insert_episode_embedding(
        EpisodeEmbeddingRecord("ep_old", "prof_1", "m", "v1", [1.0, 0.0, 0.0], 0.9, 0.8, "fp2", "2026-05-10T10:00:00Z")
    )

    retriever = CandidateRetriever(store)
    now = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
    query = EpisodicQuery(
        raw_text="¿Qué dijimos la semana pasada?",
        normalized_query="dijimos semana pasada",
        recall_intent=RecallIntent.EXPLICIT,
        temporal_constraint=TemporalConstraint(TemporalConstraintType.LAST_WEEK),
        lexical_anchors=[],
        profile_id="prof_1",
    )

    candidates = retriever.get_candidates(query, query_vector=[1.0, 0.0, 0.0], reference_time=now)
    # The 3-month-old episode MUST BE ELIMINATED by the hard temporal filter despite having cosine 1.0!
    assert len(candidates) == 1
    assert candidates[0].episode_id == "ep_lw"

    store.close()


def test_hybrid_ranker_corroboration_and_rejection():
    ranker = HybridEpisodicRanker()

    # Candidate with high cosine (0.91) but pure technical noise without lexical match for explicit query
    # and low cohesion
    cand_weak = ranker.score_candidate(
        exchange_cosine=0.50,
        episode_cosine=0.45,
        lexical_score=0.0,
        cohesion_mean=0.60,
        time_delta_days=5.0,
        intent=RecallIntent.EXPLICIT,
    )
    assert cand_weak.accepted is False
    assert cand_weak.reason_code == "REJECTED_LOW_SEMANTIC"

    # Candidate with good cosine (0.75) and lexical match
    cand_good = ranker.score_candidate(
        exchange_cosine=0.75,
        episode_cosine=0.70,
        lexical_score=0.5,
        cohesion_mean=0.85,
        time_delta_days=3.0,
        intent=RecallIntent.EXPLICIT,
    )
    assert cand_good.accepted is True
    assert cand_good.reason_code == "ACCEPTED_EXPLICIT_CORROBORATED"


def test_mmr_diversity_deduplication():
    ranker = HybridEpisodicRanker()

    # 3 candidate episodes:
    # Ep A (score 0.90, vector [1, 0, 0])
    # Ep B (score 0.88, vector [0.99, 0.1, 0] -> near duplicate of A!)
    # Ep C (score 0.82, vector [0.1, 0.99, 0] -> distinct aspect of topic!)
    ep_a = RankedCandidateEpisode("ep_A", "s1", "p1", "exA", 0.90, 0.90, 0.5, [1.0, 0.0, 0.0], "ACCEPTED")
    ep_b = RankedCandidateEpisode("ep_B", "s2", "p1", "exB", 0.88, 0.88, 0.5, [0.99, 0.1, 0.0], "ACCEPTED")
    ep_c = RankedCandidateEpisode("ep_C", "s3", "p1", "exC", 0.82, 0.82, 0.5, [0.1, 0.99, 0.0], "ACCEPTED")

    selected = ranker.apply_mmr_diversity([ep_a, ep_b, ep_c], max_episodes=2, lambda_param=0.5)
    # Selected should choose Ep A first, then Ep C because Ep B is a near duplicate of A!
    assert len(selected) == 2
    assert selected[0].episode_id == "ep_A"
    assert selected[1].episode_id == "ep_C"
