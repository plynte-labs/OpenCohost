from __future__ import annotations

import math
import sqlite3
from pathlib import Path
import pytest
import numpy as np

from opencohost.core.memory_v5_shadow.semantic_cache import (
    SemanticCacheStore,
    ExchangeEmbeddingRecord,
    EpisodeEmbeddingRecord,
)


def test_semantic_cache_store_schema_and_vector_roundtrip(tmp_path: Path):
    db_path = tmp_path / "semantic_cache.db"
    store = SemanticCacheStore(db_path)
    store.initialize()

    # Create dummy 384-d vector with known values
    vec = [float(i) / 1000.0 for i in range(384)]
    # L2 normalize
    norm = math.sqrt(sum(x * x for x in vec))
    vec = [x / norm for x in vec]

    ex_rec = ExchangeEmbeddingRecord(
        exchange_key="usr_01:asst_01",
        profile_id="prof_A",
        session_id="sess_01",
        episode_id="ep_01",
        content_hash="abc123hash",
        model_id="minilm-l12",
        model_version="v1",
        dimensions=384,
        vector=vec,
        created_at="2026-09-02T12:00:00Z",
    )

    store.insert_exchange_embedding(ex_rec)

    # Read back
    retrieved = store.get_exchange_embeddings_by_profile("prof_A")
    assert len(retrieved) == 1
    r = retrieved[0]
    assert r.exchange_key == "usr_01:asst_01"
    assert r.profile_id == "prof_A"
    assert r.dimensions == 384
    assert len(r.vector) == 384
    # Bit-exact check on float32 representation
    assert np.allclose(r.vector, vec, atol=1e-6)

    store.close()


def test_semantic_cache_profile_isolation_and_purge(tmp_path: Path):
    db_path = tmp_path / "semantic_cache.db"
    store = SemanticCacheStore(db_path)
    store.initialize()

    vec_a = [1.0, 0.0, 0.0]
    vec_b = [0.0, 1.0, 0.0]

    # Insert for profile A
    store.insert_exchange_embedding(
        ExchangeEmbeddingRecord(
            exchange_key="ex_A",
            profile_id="prof_A",
            session_id="s1",
            episode_id="ep1",
            content_hash="hA",
            model_id="m1",
            model_version="v1",
            dimensions=3,
            vector=vec_a,
            created_at="2026-09-02T12:00:00Z",
        )
    )
    store.insert_episode_embedding(
        EpisodeEmbeddingRecord(
            episode_id="ep1",
            profile_id="prof_A",
            model_id="m1",
            model_version="v1",
            vector=vec_a,
            cohesion_mean=0.92,
            cohesion_min=0.85,
            content_fingerprint="fpA",
            created_at="2026-09-02T12:00:00Z",
        )
    )

    # Insert for profile B
    store.insert_exchange_embedding(
        ExchangeEmbeddingRecord(
            exchange_key="ex_B",
            profile_id="prof_B",
            session_id="s2",
            episode_id="ep2",
            content_hash="hB",
            model_id="m1",
            model_version="v1",
            dimensions=3,
            vector=vec_b,
            created_at="2026-09-02T12:05:00Z",
        )
    )
    store.insert_episode_embedding(
        EpisodeEmbeddingRecord(
            episode_id="ep2",
            profile_id="prof_B",
            model_id="m1",
            model_version="v1",
            vector=vec_b,
            cohesion_mean=0.88,
            cohesion_min=0.80,
            content_fingerprint="fpB",
            created_at="2026-09-02T12:05:00Z",
        )
    )

    # Profile isolation: querying prof_A returns ONLY prof_A data
    exs_a = store.get_exchange_embeddings_by_profile("prof_A")
    eps_a = store.get_episode_embeddings_by_profile("prof_A")
    assert len(exs_a) == 1 and exs_a[0].profile_id == "prof_A"
    assert len(eps_a) == 1 and eps_a[0].profile_id == "prof_A"

    exs_b = store.get_exchange_embeddings_by_profile("prof_B")
    eps_b = store.get_episode_embeddings_by_profile("prof_B")
    assert len(exs_b) == 1 and exs_b[0].profile_id == "prof_B"
    assert len(eps_b) == 1 and eps_b[0].profile_id == "prof_B"

    # Purge Profile A
    store.purge_profile_cache("prof_A")

    assert len(store.get_exchange_embeddings_by_profile("prof_A")) == 0
    assert len(store.get_episode_embeddings_by_profile("prof_A")) == 0
    # Profile B remains 100% intact
    assert len(store.get_exchange_embeddings_by_profile("prof_B")) == 1
    assert len(store.get_episode_embeddings_by_profile("prof_B")) == 1

    # Forget All
    store.forget_all_cache()
    assert len(store.get_exchange_embeddings_by_profile("prof_B")) == 0
    assert len(store.get_episode_embeddings_by_profile("prof_B")) == 0

    store.close()
