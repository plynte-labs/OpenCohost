"""web-search-honesty-20261018 T4: the recall preflight must not decode vectors.

Live 2026-10-17: ``memory_recall ms`` ran ~210 ms over ``embed_ms + retrieval_ms``
on turns that took the NONE-intent preflight. That preflight loaded every
exchange row with its vector (``np.frombuffer(...).tolist()``) only to read the
lexical tokens, then ``get_candidates`` loaded the same table again, and the
first load sat before ``embed_ms`` so no stage timer covered it."""
from __future__ import annotations

from pathlib import Path

from opencohost.core.memory_v5_shadow.episodic_recall import EpisodicRecallCoordinator, RecallMode
from opencohost.core.memory_v5_shadow.query_analyzer import EpisodicQueryAnalyzer
from opencohost.core.memory_v5_shadow.semantic_cache import ExchangeEmbeddingRecord, SemanticCacheStore


def _record(i: int, tokens: str) -> ExchangeEmbeddingRecord:
    return ExchangeEmbeddingRecord(
        exchange_key=f"k{i}", profile_id="p", session_id="s", episode_id="e",
        content_hash=f"h{i}", model_id="m", model_version="1", dimensions=4,
        vector=[0.1, 0.2, 0.3, 0.4], created_at="2026-10-01T00:00:00Z", lexical_tokens=tokens,
    )


def test_store_lexical_tokens_query_returns_tokens_without_vectors(tmp_path: Path):
    cache = SemanticCacheStore(tmp_path / "c.db")
    cache.initialize()
    cache.insert_exchange_embedding(_record(1, "audifonos grave"))
    cache.insert_exchange_embedding(_record(2, "heap stack"))
    assert sorted(cache.get_exchange_lexical_tokens_by_profile("p")) == ["audifonos grave", "heap stack"]
    assert cache.get_exchange_lexical_tokens_by_profile("other") == []


def test_preflight_reads_tokens_only_never_the_vector_table(tmp_path: Path):
    cache = SemanticCacheStore(tmp_path / "c.db")
    cache.initialize()
    cache.insert_exchange_embedding(_record(1, "audifonos grave"))

    def boom(profile_id):  # the heavy load must stay out of the preflight
        raise AssertionError("preflight decoded exchange vectors")

    cache.get_exchange_embeddings_by_profile = boom
    coord = EpisodicRecallCoordinator(None, cache, object(), mode=RecallMode.ACTIVE)
    query = EpisodicQueryAnalyzer().analyze("mis audifonos suenan raro", profile_id="p")
    assert coord._has_historical_anchor_overlap(query) is True
    other = EpisodicQueryAnalyzer().analyze("cuentame un chiste de programadores", profile_id="p")
    assert coord._has_historical_anchor_overlap(other) is False
