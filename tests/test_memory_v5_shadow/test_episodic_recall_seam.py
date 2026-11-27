"""
Comprehensive 16-Seam Acceptance Test Suite for Memory v5 Full Episodic Recall.
Validates end-to-end integration across query understanding, semantic worker isolation,
cache persistence, candidate retrieval, hybrid ranking, MMR diversity, context expansion,
privacy cascade, and fail-open resilience.
"""
from __future__ import annotations

import queue
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock
import pytest

import opencohost.core.llm_engine as llm_engine
from opencohost.core.context.prompt_assembler import PromptContextAssembler
from opencohost.core.memory_v5_shadow.episodic_recall import (
    EpisodicRecallCoordinator,
    RecallMode,
)
from opencohost.core.memory_v5_shadow.query_analyzer import (
    EpisodicQueryAnalyzer,
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
from opencohost.core.memory_v5_shadow.semantic_indexer import (
    IncrementalSemanticIndexer,
)
from opencohost.core.memory_v5_shadow.semantic_worker import (
    SemanticWorkerService,
)


@pytest.fixture
def mock_worker():
    class DeterministicMockWorker:
        def embed_query(self, text: str, timeout_s: float = 0.5):
            t = text.lower()
            if "audifono" in t or "audífono" in t or "sonos" in t or "grave" in t:
                return [1.0] + [0.0] * 383
            if "arquitectura" in t or "soberanía" in t:
                return [0.0, 1.0] + [0.0] * 382
            if "stack" in t or "heap" in t:
                return [0.0, 0.0, 1.0] + [0.0] * 381
            return [0.05] * 384

        def embed_batch(self, texts: list[str], timeout_s: float = 1.5):
            return [self.embed_query(t) for t in texts]

    return DeterministicMockWorker()


def _setup_seeded_shadow_db(shadow_path: Path):
    conn = sqlite3.connect(shadow_path)
    conn.executescript("""
        CREATE TABLE evidence_journal (
            event_id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            stream_sequence INTEGER NOT NULL,
            role TEXT NOT NULL,
            source TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            content TEXT NOT NULL,
            is_private INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL,
            profile_id TEXT NOT NULL,
            state TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            opened_reason TEXT NOT NULL,
            closure_reason TEXT,
            event_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE episodes (
            episode_id TEXT PRIMARY KEY,
            profile_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            state TEXT NOT NULL,
            formation_policy_id TEXT NOT NULL,
            formation_policy_version TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            opened_reason TEXT NOT NULL,
            closure_reason TEXT,
            event_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE episode_membership (
            episode_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            sequence_index INTEGER NOT NULL,
            PRIMARY KEY (episode_id, event_id)
        );
    """)

    # Episode 1: Sonos Ace discussion (Aug 26, 2026) for profile_A
    evs_ep1 = [
        ("e1", "prof_A", "r1", 1, "user", "PTT", "2026-08-26T10:00:00Z", "¿Probaste los audífonos Sonos?", "2026-08-26T10:00:00Z"),
        ("e2", "prof_A", "r1", 2, "assistant", "PTT", "2026-08-26T10:00:05Z", "Sí, los probé. Son muy cómodos.", "2026-08-26T10:00:05Z"),
        ("e3", "prof_A", "r1", 3, "user", "PTT", "2026-08-26T10:01:00Z", "El problema es que les falta golpe en graves.", "2026-08-26T10:01:00Z"),
        ("e4", "prof_A", "r1", 4, "assistant", "PTT", "2026-08-26T10:01:05Z", "Efectivamente, los graves son contenidos.", "2026-08-26T10:01:05Z"),
    ]
    for ev in evs_ep1:
        conn.execute("INSERT INTO evidence_journal VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)", ev)

    conn.execute(
        "INSERT INTO episodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("ep_sonos", "prof_A", "sess_1", "CLOSED", "policy_v1", "v1", "2026-08-26T10:00:00Z", "2026-08-26T10:01:05Z", "START", "CLOSE", 4),
    )
    for idx, (eid, *_) in enumerate(evs_ep1):
        conn.execute("INSERT INTO episode_membership VALUES (?, ?, ?)", ("ep_sonos", eid, idx))

    # Episode 2: Architecture discussion (Aug 19, 2026, 2 weeks ago) for profile_A
    evs_ep2 = [
        ("e5", "prof_A", "r1", 5, "user", "PTT", "2026-08-19T10:00:00Z", "¿Qué arquitectura decidimos para soberanía local?", "2026-08-19T10:00:00Z"),
        ("e6", "prof_A", "r1", 6, "assistant", "PTT", "2026-08-19T10:00:05Z", "Decidimos edge-first con SQLite sin nube.", "2026-08-19T10:00:05Z"),
    ]
    for ev in evs_ep2:
        conn.execute("INSERT INTO evidence_journal VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)", ev)

    conn.execute(
        "INSERT INTO episodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("ep_arch", "prof_A", "sess_2", "CLOSED", "policy_v1", "v1", "2026-08-19T10:00:00Z", "2026-08-19T10:00:05Z", "START", "CLOSE", 2),
    )
    for idx, (eid, *_) in enumerate(evs_ep2):
        conn.execute("INSERT INTO episode_membership VALUES (?, ?, ?)", ("ep_arch", eid, idx))

    # Episode 3: Secret discussion for profile_B
    evs_ep3 = [
        ("e7", "prof_B", "r2", 7, "user", "PTT", "2026-08-26T12:00:00Z", "Mi clave secreta es 12345.", "2026-08-26T12:00:00Z"),
        ("e8", "prof_B", "r2", 8, "assistant", "PTT", "2026-08-26T12:00:05Z", "Entendido, no la revelaré.", "2026-08-26T12:00:05Z"),
    ]
    for ev in evs_ep3:
        conn.execute("INSERT INTO evidence_journal VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)", ev)

    conn.execute(
        "INSERT INTO episodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("ep_secret", "prof_B", "sess_3", "CLOSED", "policy_v1", "v1", "2026-08-26T12:00:00Z", "2026-08-26T12:00:05Z", "START", "CLOSE", 2),
    )
    for idx, (eid, *_) in enumerate(evs_ep3):
        conn.execute("INSERT INTO episode_membership VALUES (?, ?, ?)", ("ep_secret", eid, idx))

    conn.commit()
    return conn


def test_seam_01_direct_explicit_recall(tmp_path: Path, mock_worker):
    conn = _setup_seeded_shadow_db(tmp_path / "shadow.db")
    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()

    indexer = IncrementalSemanticIndexer(conn, cache, mock_worker)
    assert indexer.reconcile_unindexed_episodes() == 3

    coord = EpisodicRecallCoordinator(conn, cache, mock_worker, mode=RecallMode.ACTIVE)
    packet = coord.process_query("¿Te acuerdas qué problema tenían los audífonos?", profile_id="prof_A")

    assert packet is not None
    assert len(packet.retrieved_episodes) == 1
    assert packet.retrieved_episodes[0].episode_id == "ep_sonos"
    assert "<episodic_memory>" in packet.formatted_block
    assert "graves" in packet.formatted_block
    conn.close()
    cache.close()


def test_seam_02_indirect_implicit_recall(tmp_path: Path, mock_worker):
    conn = _setup_seeded_shadow_db(tmp_path / "shadow.db")
    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()

    indexer = IncrementalSemanticIndexer(conn, cache, mock_worker)
    indexer.reconcile_unindexed_episodes()

    coord = EpisodicRecallCoordinator(conn, cache, mock_worker, mode=RecallMode.ACTIVE)
    # Indirect phrase without "¿te acuerdas?"
    packet = coord.process_query("Volví a probar los audífonos y siguen con el mismo defecto.", profile_id="prof_A")

    assert packet is not None
    assert len(packet.retrieved_episodes) == 1
    assert packet.retrieved_episodes[0].episode_id == "ep_sonos"
    conn.close()
    cache.close()


def test_seam_03_temporal_last_week(tmp_path: Path, mock_worker):
    conn = _setup_seeded_shadow_db(tmp_path / "shadow.db")
    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()

    indexer = IncrementalSemanticIndexer(conn, cache, mock_worker)
    indexer.reconcile_unindexed_episodes()

    coord = EpisodicRecallCoordinator(conn, cache, mock_worker, mode=RecallMode.ACTIVE)
    # Reference date: Wednesday Sep 2, 2026. Last week is Aug 24 - Aug 30.
    now = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
    packet = coord.process_query("¿Qué dijimos la semana pasada de los audífonos?", profile_id="prof_A", reference_time=now)

    assert packet is not None
    assert len(packet.retrieved_episodes) == 1
    assert packet.retrieved_episodes[0].episode_id == "ep_sonos"
    conn.close()
    cache.close()


def test_seam_04_temporal_two_weeks_ago(tmp_path: Path, mock_worker):
    conn = _setup_seeded_shadow_db(tmp_path / "shadow.db")
    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()

    indexer = IncrementalSemanticIndexer(conn, cache, mock_worker)
    indexer.reconcile_unindexed_episodes()

    coord = EpisodicRecallCoordinator(conn, cache, mock_worker, mode=RecallMode.ACTIVE)
    now = datetime(2026, 9, 2, 12, 0, 0, tzinfo=timezone.utc)
    packet = coord.process_query("¿De qué hablamos hace dos semanas sobre arquitectura?", profile_id="prof_A", reference_time=now)

    assert packet is not None
    assert len(packet.retrieved_episodes) == 1
    assert packet.retrieved_episodes[0].episode_id == "ep_arch"
    conn.close()
    cache.close()


def test_seam_05_lexical_corroboration_rejects_hallucinated_similarity(tmp_path: Path):
    ranker = HybridEpisodicRanker()
    # High cosine but completely unsubstantiated for implicit intent
    res = ranker.score_candidate(
        exchange_cosine=0.80,
        episode_cosine=0.75,
        lexical_score=0.0,
        cohesion_mean=0.85,
        time_delta_days=5.0,
        intent=RecallIntent.IMPLICIT,
    )
    assert res.accepted is False
    assert res.reason_code == "REJECTED_LEXICAL_UNSUBSTANTIATED"


def test_seam_06_unrelated_query_no_recall(tmp_path: Path, mock_worker):
    conn = _setup_seeded_shadow_db(tmp_path / "shadow.db")
    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()

    coord = EpisodicRecallCoordinator(conn, cache, mock_worker, mode=RecallMode.ACTIVE)
    packet = coord.process_query("Cuéntame un chiste de programadores.", profile_id="prof_A")

    assert packet is not None
    # NONE is a very strict opportunistic policy, not a hard gate: an
    # unrelated turn with no historical anchor overlap returns NO_RECALL via
    # the deterministic preflight, before any embedding work.
    assert packet.reason_code == "NO_HISTORICAL_ANCHOR"
    assert packet.formatted_block == ""
    assert packet.token_estimate == 0
    conn.close()
    cache.close()


def test_seam_07_under_segmented_episode_exchange_recovery(tmp_path: Path, mock_worker):
    # Tests that query targets the relevant exchange and bounds window to +-1 adjacent
    conn = _setup_seeded_shadow_db(tmp_path / "shadow.db")
    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()

    indexer = IncrementalSemanticIndexer(conn, cache, mock_worker)
    indexer.reconcile_unindexed_episodes()

    coord = EpisodicRecallCoordinator(conn, cache, mock_worker, mode=RecallMode.ACTIVE)
    packet = coord.process_query("¿Te acuerdas qué problema tenía el golpe en graves?", profile_id="prof_A")

    assert packet is not None
    assert len(packet.retrieved_episodes) == 1
    # Bounded exchanges must contain e3:e4 and at most 3 exchanges total
    retrieved = packet.retrieved_episodes[0]
    assert len(retrieved.exchanges) <= 3
    assert any("graves" in ex.user_text for ex in retrieved.exchanges)
    conn.close()
    cache.close()


def test_seam_08_hard_profile_isolation(tmp_path: Path, mock_worker):
    conn = _setup_seeded_shadow_db(tmp_path / "shadow.db")
    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()

    indexer = IncrementalSemanticIndexer(conn, cache, mock_worker)
    indexer.reconcile_unindexed_episodes()

    coord = EpisodicRecallCoordinator(conn, cache, mock_worker, mode=RecallMode.ACTIVE)
    # Profile A queries for secrets -> must NOT find profile B's episode
    packet = coord.process_query("¿Te acuerdas de mi clave secreta?", profile_id="prof_A")
    assert packet is not None
    # Cannot retrieve ep_secret
    assert not any(ep.episode_id == "ep_secret" for ep in packet.retrieved_episodes)
    assert "12345" not in packet.formatted_block
    conn.close()
    cache.close()


def test_seam_09_shadow_mode_prompt_byte_identical(tmp_path: Path, mock_worker):
    conn = _setup_seeded_shadow_db(tmp_path / "shadow.db")
    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()

    indexer = IncrementalSemanticIndexer(conn, cache, mock_worker)
    indexer.reconcile_unindexed_episodes()

    coord = EpisodicRecallCoordinator(conn, cache, mock_worker, mode=RecallMode.SHADOW)
    packet = coord.process_query("¿Te acuerdas qué problema tenían los audífonos?", profile_id="prof_A")

    assert packet is not None
    assert packet.mode == RecallMode.SHADOW
    # Prompt assembler in SHADOW mode passes empty episodic_memory_block
    assembler = PromptContextAssembler()
    setup1 = assembler.assemble("Hola", "ptt", system_prompt="Kira", use_system_role=True, is_local=True, provider_cfg={}, request_model="m", history_snapshot=[], episodic_memory_block="")
    setup2 = assembler.assemble("Hola", "ptt", system_prompt="Kira", use_system_role=True, is_local=True, provider_cfg={}, request_model="m", history_snapshot=[], episodic_memory_block="")
    assert setup1.messages == setup2.messages
    conn.close()
    cache.close()


def test_seam_10_active_mode_prompt_injection(tmp_path: Path, mock_worker):
    conn = _setup_seeded_shadow_db(tmp_path / "shadow.db")
    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()

    indexer = IncrementalSemanticIndexer(conn, cache, mock_worker)
    indexer.reconcile_unindexed_episodes()

    coord = EpisodicRecallCoordinator(conn, cache, mock_worker, mode=RecallMode.ACTIVE)
    packet = coord.process_query("¿Te acuerdas de los audífonos?", profile_id="prof_A")

    assembler = PromptContextAssembler()
    setup = assembler.assemble(
        "¿Qué hacemos con ellos?",
        "ptt",
        system_prompt="Kira",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="m",
        history_snapshot=[],
        episodic_memory_block=packet.formatted_block,
    )
    from opencohost.i18n import active as i18n_active
    assert len(setup.messages) == 2
    user_content = setup.messages[-1]["content"]
    assert setup.messages[-1]["role"] == "user"
    assert "<episodic_memory>" in user_content
    assert "Prefer the current user message over old memory" in user_content
    assert "</episodic_memory>" in user_content
    # fb7e1b6: a v2 owner turn ("ptt") carries the generic owner role label.
    label = f"[{i18n_active.owner_role_label()}]:"
    assert label in user_content
    idx_ep = user_content.index("<episodic_memory>")
    idx_lbl = user_content.index(label)
    assert idx_ep < idx_lbl
    after_label = user_content[idx_lbl + len(label):].strip()
    assert after_label == "¿Qué hacemos con ellos?"
    conn.close()
    cache.close()


def test_seam_11_token_budget_bound_strict(tmp_path: Path, mock_worker):
    conn = _setup_seeded_shadow_db(tmp_path / "shadow.db")
    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()
    indexer = IncrementalSemanticIndexer(conn, cache, mock_worker)
    indexer.reconcile_unindexed_episodes()

    # Budget of 50 tokens
    coord = EpisodicRecallCoordinator(conn, cache, mock_worker, mode=RecallMode.ACTIVE, max_token_budget=50)
    packet = coord.process_query("¿Te acuerdas qué problema tenían los audífonos?", profile_id="prof_A")

    assert packet is not None
    assert packet.token_estimate <= 50
    if packet.formatted_block:
        assert len(packet.formatted_block) // 4 <= 50
    conn.close()
    cache.close()


def test_seam_11b_large_episode_budget_trim(tmp_path: Path, mock_worker):
    """Even when a single Episode has very verbose exchanges, budget must be strictly enforced."""
    conn = sqlite3.connect(tmp_path / "shadow.db")
    conn.executescript("""
        CREATE TABLE evidence_journal (
            event_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, run_id TEXT NOT NULL,
            stream_sequence INTEGER NOT NULL, role TEXT NOT NULL, source TEXT NOT NULL,
            occurred_at TEXT NOT NULL, content TEXT NOT NULL, is_private INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE episodes (
            episode_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, session_id TEXT NOT NULL,
            state TEXT NOT NULL, formation_policy_id TEXT NOT NULL, formation_policy_version TEXT NOT NULL,
            started_at TEXT NOT NULL, ended_at TEXT, opened_reason TEXT NOT NULL,
            closure_reason TEXT, event_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE episode_membership (
            episode_id TEXT NOT NULL, event_id TEXT NOT NULL, sequence_index INTEGER NOT NULL,
            PRIMARY KEY (episode_id, event_id)
        );
    """)
    # Insert 1 huge episode (~2000 chars)
    big_user = "¿Qué opinas de los audífonos Sonos Ace con cancelación de ruido activa y perfil acústico?"
    big_asst = "Los Sonos Ace son audífonos con drivers dinámicos de 40mm. " * 30
    conn.execute("INSERT INTO evidence_journal VALUES ('e1', 'p1', 'r1', 1, 'user', 'PTT', '2026-08-26T10:00:00Z', ?, 0, '2026-08-26T10:00:00Z')", (big_user,))
    conn.execute("INSERT INTO evidence_journal VALUES ('e2', 'p1', 'r1', 2, 'assistant', 'PTT', '2026-08-26T10:00:05Z', ?, 0, '2026-08-26T10:00:05Z')", (big_asst,))
    conn.execute("INSERT INTO episodes VALUES ('ep_huge', 'p1', 's1', 'CLOSED', 'v1', 'v1', '2026-08-26T10:00:00Z', '2026-08-26T10:00:05Z', 'START', 'CLOSE', 2)")
    conn.execute("INSERT INTO episode_membership VALUES ('ep_huge', 'e1', 0)")
    conn.execute("INSERT INTO episode_membership VALUES ('ep_huge', 'e2', 1)")
    conn.commit()

    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()
    indexer = IncrementalSemanticIndexer(conn, cache, mock_worker)
    indexer.reconcile_unindexed_episodes()

    # Strict token budget = 100 tokens (~400 chars)
    coord = EpisodicRecallCoordinator(conn, cache, mock_worker, mode=RecallMode.ACTIVE, max_token_budget=100)
    packet = coord.process_query("¿Te acuerdas de los audífonos Sonos?", profile_id="p1")

    assert packet is not None
    assert packet.token_estimate <= 100
    if packet.formatted_block:
        assert len(packet.formatted_block) // 4 <= 100
    conn.close()
    cache.close()


def test_seam_12_worker_timeout_fail_open(tmp_path: Path):
    class HangingWorker:
        def embed_query(self, text: str, timeout_s: float = 0.5):
            return None  # simulates timeout

    conn = _setup_seeded_shadow_db(tmp_path / "shadow.db")
    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()

    coord = EpisodicRecallCoordinator(conn, cache, HangingWorker(), mode=RecallMode.ACTIVE)
    packet = coord.process_query("¿Te acuerdas?", profile_id="prof_A")
    assert packet is not None
    assert packet.reason_code == "WORKER_UNAVAILABLE"
    assert packet.formatted_block == ""
    conn.close()
    cache.close()


def test_seam_13_worker_crash_resilience(tmp_path: Path):
    worker = SemanticWorkerService(use_dummy=True)
    worker.start()
    assert worker.is_alive()

    # Kill child process
    worker._process.kill()
    worker._process.join(timeout=1.0)
    assert not worker.is_alive()

    # Calling embed_query on crashed worker should return None cleanly without raising
    res = worker.embed_query("testing crash", timeout_s=0.2)
    assert res is None
    worker.shutdown()


def test_seam_14_purge_profile_cascade(tmp_path: Path, mock_worker):
    conn = _setup_seeded_shadow_db(tmp_path / "shadow.db")
    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()

    indexer = IncrementalSemanticIndexer(conn, cache, mock_worker)
    indexer.reconcile_unindexed_episodes()

    # Pre-check: cache has records for prof_A
    assert len(cache.get_episode_embeddings_by_profile("prof_A")) == 2

    # Purge profile A cache
    cache.purge_profile_cache("prof_A")

    # Post-check: cache has 0 records for prof_A, but prof_B is intact
    assert len(cache.get_episode_embeddings_by_profile("prof_A")) == 0
    assert len(cache.get_episode_embeddings_by_profile("prof_B")) == 1
    conn.close()
    cache.close()


def test_seam_15_startup_reconciliation(tmp_path: Path, mock_worker):
    conn = _setup_seeded_shadow_db(tmp_path / "shadow.db")
    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()

    indexer = IncrementalSemanticIndexer(conn, cache, mock_worker)
    # First reconciliation: indexes 3 episodes
    c1 = indexer.reconcile_unindexed_episodes()
    assert c1 == 3

    # Second reconciliation: 0 unindexed
    c2 = indexer.reconcile_unindexed_episodes()
    assert c2 == 0
    conn.close()
    cache.close()


def test_seam_16_mmr_diversity():
    ranker = HybridEpisodicRanker()
    ep1 = RankedCandidateEpisode("ep1", "s1", "p1", "ex1", 0.95, 0.95, 0.5, [1.0, 0.0, 0.0], "ACCEPTED")
    ep2 = RankedCandidateEpisode("ep2", "s2", "p1", "ex2", 0.94, 0.94, 0.5, [0.99, 0.1, 0.0], "ACCEPTED")
    ep3 = RankedCandidateEpisode("ep3", "s3", "p1", "ex3", 0.85, 0.85, 0.5, [0.0, 1.0, 0.0], "ACCEPTED")

    selected = ranker.apply_mmr_diversity([ep1, ep2, ep3], max_episodes=2, lambda_param=0.6)
    assert len(selected) == 2
    assert selected[0].episode_id == "ep1"
    assert selected[1].episode_id == "ep3"


def test_seam_17_real_lexical_corroboration_rejects_unanchored(tmp_path: Path):
    """
    BLOCKER 1: If query has lexical anchors (e.g. 'psicometría'), but candidate has high semantic cosine
    yet zero shared anchors, explicit recall MUST reject it (NO_RECALL).
    """
    conn = _setup_seeded_shadow_db(tmp_path / "shadow.db")
    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()

    class HighCosineUnanchoredWorker:
        def embed_query(self, text: str, timeout_s: float = 0.5):
            # Artificially return high cosine match with the Sonos Ace vector
            return [0.75] + [0.0] * 383

        def embed_batch(self, texts: list[str], timeout_s: float = 1.5):
            # Seed vector for sonos
            return [[1.0] + [0.0] * 383 for _ in texts]

    indexer = IncrementalSemanticIndexer(conn, cache, HighCosineUnanchoredWorker())
    indexer.reconcile_unindexed_episodes()

    coord = EpisodicRecallCoordinator(conn, cache, HighCosineUnanchoredWorker(), mode=RecallMode.ACTIVE)
    # Query has lexical anchors for psicometría, but candidate in cache only talks about audífonos
    packet = coord.process_query("¿Te acuerdas qué hablamos sobre psicometría?", profile_id="prof_A")

    # With real lexical scoring, lex_score is 0.0! For cosine < 0.55 or without corroboration, it's rejected.
    # Even with cosine 0.75, uncorroborated explicit query is evaluated against high threshold or rejected.
    assert packet is not None
    assert len(packet.retrieved_episodes) == 0
    assert packet.formatted_block == ""
    assert packet.reason_code in ("REJECTED_LOW_CONFIDENCE", "REJECTED_LOW_SEMANTIC", "REJECTED_LEXICAL_UNSUBSTANTIATED")
    conn.close()
    cache.close()


def test_seam_18_worker_backend_failure_no_pseudo_recall():
    """
    BLOCKER 2: When use_dummy=False and backend is None (e.g. init failure),
    worker returns None, NOT pseudo-vectors, resulting in clean fail-open NO_RECALL.
    """
    worker = SemanticWorkerService(use_dummy=False)
    # Do not start process or simulate backend=None
    assert worker._process is None
    # embed_query when worker not started or dead returns None
    vec = worker.embed_query("¿Te acuerdas?")
    assert vec is None


def test_seam_19_motor_vocalia_runtime_seam_active_and_shadow(tmp_path: Path, mock_worker):
    """
    BLOCKER 5: Test through the actual MotorVocalIA._prepare_generation_setup request path.
    ACTIVE: <episodic_memory> is present in the actual inference messages.
    SHADOW: same request produces messages byte-identical to baseline.
    """
    conn = _setup_seeded_shadow_db(tmp_path / "shadow.db")
    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()
    indexer = IncrementalSemanticIndexer(conn, cache, mock_worker)
    indexer.reconcile_unindexed_episodes()

    # 1. Baseline setup without coordinator
    motor = llm_engine.MotorVocalIA(queue.Queue(), lambda e: None)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor._current_profile_id = "prof_A"
    motor._episodic_recall_coordinator = None

    query_text = "¿Te acuerdas qué problema tenían los audífonos Sonos?"
    setup_baseline = motor._build_generation_request(
        query_text,
        source="ptt",
        is_local=True,
        provider_cfg={},
        request_model="qwen3",
        watchdog_timeout=10.0,
    )

    # 2. SHADOW mode setup
    coord_shadow = EpisodicRecallCoordinator(conn, cache, mock_worker, mode=RecallMode.SHADOW)
    motor._episodic_recall_coordinator = coord_shadow
    setup_shadow = motor._build_generation_request(
        query_text,
        source="ptt",
        is_local=True,
        provider_cfg={},
        request_model="qwen3",
        watchdog_timeout=10.0,
    )

    # SHADOW messages MUST be byte-identical to baseline
    assert setup_shadow.messages == setup_baseline.messages
    assert "<episodic_memory>" not in setup_shadow.messages[-1]["content"]

    # 3. ACTIVE mode setup
    coord_active = EpisodicRecallCoordinator(conn, cache, mock_worker, mode=RecallMode.ACTIVE)
    motor._episodic_recall_coordinator = coord_active
    setup_active = motor._build_generation_request(
        query_text,
        source="ptt",
        is_local=True,
        provider_cfg={},
        request_model="qwen3",
        watchdog_timeout=10.0,
    )

    # ACTIVE messages MUST carry the recalled Sonos context. ADR-059: it is
    # merged into the memorias block as one compact "[past chat …]" line
    # (not a standalone <episodic_memory> block) under the shared budget.
    active_prompt = setup_active.messages[-1]["content"]
    assert "<episodic_memory>" not in active_prompt
    episode_lines = [ln for ln in active_prompt.split("\n") if ln.startswith("[past chat ")]
    assert len(episode_lines) == 1
    assert "Sonos" in episode_lines[0]
    conn.close()
    cache.close()


def test_seam_20_privacy_linearizability_purge_vs_index_race(tmp_path: Path, mock_worker):
    """
    BLOCKER 6: Purge-vs-index race fence.
    If indexing starts for profile A before a purge, but purge_profile(A) commits first,
    the late insert MUST be rejected by the fence, ensuring the cache remains empty.
    """
    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()

    # Pre-purge timestamp T1
    t1 = "2026-08-26T10:00:00Z"
    rec = ExchangeEmbeddingRecord(
        exchange_key="e1:e2",
        profile_id="prof_race",
        session_id="s1",
        episode_id="ep1",
        content_hash="h1",
        model_id="minilm-l12",
        model_version="v1",
        dimensions=384,
        vector=[0.1] * 384,
        created_at=t1,
        lexical_tokens="audifonos",
    )

    # Purge commits at T2 (now)
    cache.purge_profile_cache("prof_race")

    # Late insert attempt with created_at <= purged_at
    accepted = cache.insert_exchange_embedding(rec)
    assert accepted is False

    # Cache for prof_race MUST remain completely empty
    assert len(cache.get_exchange_embeddings_by_profile("prof_race")) == 0
    cache.close()


def test_seam_21_cache_staleness_rebuild(tmp_path: Path, mock_worker):
    """
    BLOCKER 7: Reconciliation must detect stale model_version and rebuild cache.
    """
    conn = _setup_seeded_shadow_db(tmp_path / "shadow.db")
    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()

    # Index with model_version="v0"
    indexer_v0 = IncrementalSemanticIndexer(conn, cache, mock_worker, model_version="v0")
    assert indexer_v0.reconcile_unindexed_episodes() == 3

    # Check cached records have v0
    ep = cache.get_episode_by_id("ep_sonos")
    assert ep is not None
    assert ep.model_version == "v0"

    # Now run indexer with current model_version="v1"
    indexer_v1 = IncrementalSemanticIndexer(conn, cache, mock_worker, model_version="v1")
    rebuilt = indexer_v1.reconcile_unindexed_episodes()
    assert rebuilt == 3

    # Check cached records are now updated to v1
    ep_updated = cache.get_episode_by_id("ep_sonos")
    assert ep_updated is not None
    assert ep_updated.model_version == "v1"
    conn.close()
    cache.close()


def test_seam_22_last_time_temporal_selection(tmp_path: Path, mock_worker):
    """
    BLOCKER 4: LAST_TIME selects strictly the most recent compatible historical Episode.
    """
    conn = _setup_seeded_shadow_db(tmp_path / "shadow.db")
    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()
    indexer = IncrementalSemanticIndexer(conn, cache, mock_worker)
    indexer.reconcile_unindexed_episodes()

    # prof_A has ep_arch (Aug 19) and ep_sonos (Aug 26).
    # "la última vez" should strictly select ep_sonos (the most recent one).
    coord = EpisodicRecallCoordinator(conn, cache, mock_worker, mode=RecallMode.ACTIVE)
    packet = coord.process_query("¿Qué hablamos de los audífonos la última vez?", profile_id="prof_A")

    assert packet is not None
    assert len(packet.retrieved_episodes) == 1
    assert packet.retrieved_episodes[0].episode_id == "ep_sonos"
    conn.close()
    cache.close()


def test_unit2_english_query_analyzer_parity_and_stopword_filtering():
    from opencohost.core.memory_v5_shadow.query_analyzer import RecallScope

    analyzer = EpisodicQueryAnalyzer()
    ref = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

    q_exp = analyzer.analyze("Do you remember what we talked about regarding Rust?", "p1", reference_time=ref)
    assert q_exp.recall_intent == RecallIntent.EXPLICIT
    assert q_exp.lexical_anchors == ["rust"]

    q_sess = analyzer.analyze("What did we discuss last session?", "p1", reference_time=ref)
    assert q_sess.recall_intent == RecallIntent.EXPLICIT
    assert q_sess.scope == RecallScope.SESSION_RECALL
    assert q_sess.lexical_anchors == []

    q_prof = analyzer.analyze("What do you know about me and my hobbies?", "p1", reference_time=ref)
    assert q_prof.recall_intent == RecallIntent.EXPLICIT
    assert q_prof.scope == RecallScope.PROFILE_SYNTHESIS

    q_imp = analyzer.analyze("Still having the same latency glitch today", "p1", reference_time=ref)
    assert q_imp.recall_intent == RecallIntent.IMPLICIT

    q_none = analyzer.analyze("Tell me a joke about programming", "p1", reference_time=ref)
    assert q_none.recall_intent == RecallIntent.NONE

    q_day = analyzer.analyze("What did we decide on September 2nd about builds?", "p1", reference_time=ref)
    assert q_day.temporal_constraint is not None
    assert q_day.temporal_constraint.constraint_type == TemporalConstraintType.ABSOLUTE_DAY
    assert q_day.temporal_constraint.month == 9
    assert q_day.temporal_constraint.day == 2

    q_mon = analyzer.analyze("Notes from in August about shaders", "p1", reference_time=ref)
    assert q_mon.temporal_constraint is not None
    assert q_mon.temporal_constraint.constraint_type == TemporalConstraintType.MONTH
    assert q_mon.temporal_constraint.month == 8


def test_unit2_corroborated_implicit_relaxes_while_none_and_uncorroborated_stay_strict():
    ranker = HybridEpisodicRanker()

    imp_ok = ranker.score_candidate(
        exchange_cosine=0.58,
        episode_cosine=0.55,
        lexical_score=0.5,
        cohesion_mean=0.8,
        time_delta_days=2.0,
        intent=RecallIntent.IMPLICIT,
    )
    assert imp_ok.accepted is True
    assert imp_ok.reason_code == "ACCEPTED_IMPLICIT_HIGH_CONFIDENCE"

    imp_low = ranker.score_candidate(
        exchange_cosine=0.54,
        episode_cosine=0.55,
        lexical_score=0.5,
        cohesion_mean=0.8,
        time_delta_days=2.0,
        intent=RecallIntent.IMPLICIT,
    )
    assert imp_low.accepted is False
    assert imp_low.reason_code == "REJECTED_LOW_SEMANTIC"

    imp_uncorr = ranker.score_candidate(
        exchange_cosine=0.80,
        episode_cosine=0.75,
        lexical_score=0.0,
        cohesion_mean=0.8,
        time_delta_days=2.0,
        intent=RecallIntent.IMPLICIT,
    )
    assert imp_uncorr.accepted is False
    assert imp_uncorr.reason_code == "REJECTED_LEXICAL_UNSUBSTANTIATED"

    none_strict = ranker.score_candidate(
        exchange_cosine=0.60,
        episode_cosine=0.60,
        lexical_score=0.5,
        cohesion_mean=0.8,
        time_delta_days=2.0,
        intent=RecallIntent.NONE,
    )
    assert none_strict.accepted is False
    assert none_strict.reason_code == "REJECTED_LOW_SEMANTIC"


def test_unit2_semantic_indexer_uses_user_text_only_and_v2_version(tmp_path: Path):
    conn = sqlite3.connect(tmp_path / "shadow.db")
    conn.executescript("""
        CREATE TABLE evidence_journal (
            event_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, run_id TEXT NOT NULL,
            stream_sequence INTEGER NOT NULL, role TEXT NOT NULL, source TEXT NOT NULL,
            occurred_at TEXT NOT NULL, content TEXT NOT NULL,
            is_private INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL
        );
        CREATE TABLE episodes (
            episode_id TEXT PRIMARY KEY, profile_id TEXT NOT NULL, session_id TEXT NOT NULL,
            state TEXT NOT NULL, formation_policy_id TEXT NOT NULL,
            formation_policy_version TEXT NOT NULL, started_at TEXT NOT NULL,
            ended_at TEXT, opened_reason TEXT NOT NULL, closure_reason TEXT,
            event_count INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE episode_membership (
            episode_id TEXT NOT NULL, event_id TEXT NOT NULL, sequence_index INTEGER NOT NULL,
            PRIMARY KEY (episode_id, event_id)
        );
    """)
    conn.execute(
        "INSERT INTO episodes VALUES ('ep_u', 'p1', 's1', 'CLOSED', 'p', 'v1', "
        "'2026-09-01T10:00:00Z', '2026-09-01T10:05:00Z', 'S', 'C', 2)"
    )
    conn.execute(
        "INSERT INTO evidence_journal VALUES ('u1', 'p1', 'r1', 1, 'user', 'direct', "
        "'2026-09-01T10:00:00Z', 'I love analog modular synthesizers', 0, '2026-09-01T10:00:00Z')"
    )
    conn.execute(
        "INSERT INTO evidence_journal VALUES ('a1', 'p1', 'r1', 2, 'assistant', 'direct', "
        "'2026-09-01T10:00:05Z', 'Nice! Quantum entanglement is wild too.', 0, '2026-09-01T10:00:05Z')"
    )
    conn.execute("INSERT INTO episode_membership VALUES ('ep_u', 'u1', 0)")
    conn.execute("INSERT INTO episode_membership VALUES ('ep_u', 'a1', 1)")
    conn.commit()

    cache = SemanticCacheStore(tmp_path / "cache.db")
    cache.initialize()

    embedded_texts = []

    class SpyWorker:
        def embed_batch(self, texts, timeout_s=1.5):
            embedded_texts.extend(texts)
            return [[1.0] + [0.0] * 383 for _ in texts]

    indexer = IncrementalSemanticIndexer(conn, cache, SpyWorker())
    assert indexer.model_version == "v2"
    assert indexer.index_episode("ep_u") is True
    assert embedded_texts == ["I love analog modular synthesizers"]

    ex_recs = cache.get_exchange_embeddings_by_profile("p1")
    assert len(ex_recs) == 1
    assert ex_recs[0].model_version == "v2"
    tokens = set(ex_recs[0].lexical_tokens.split())
    assert {"analog", "modular", "synthesizers"}.issubset(tokens)
    assert "quantum" not in tokens
    assert "entanglement" not in tokens
    # Closed-class EN words stripped from exchange lexical tokens too.
    assert "about" not in tokens
    conn.close()
    cache.close()


def test_query_analyzer_chitchat_counterexamples_stay_none():
    from opencohost.core.memory_v5_shadow.query_analyzer import RecallScope

    analyzer = EpisodicQueryAnalyzer()
    ref = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)

    chitchat = (
        "My work was super busy today",
        "What do I do now in this game?",
        "Ese juego es muy interesante",
        "Me dedico a disfrutar el fin de semana",
        "Last time I played Dark Souls I fell off a cliff",
        "This boss hits harder than like before",
    )
    for text in chitchat:
        res = analyzer.analyze(text, "p1", reference_time=ref)
        assert res.recall_intent == RecallIntent.NONE, (text, res.scope, res.trigger)

    # Real EN+ES profile synthesis & framed implicit queries keep working.
    for text in (
        "What do you know about me?",
        "What are my hobbies?",
        "What are my interests?",
        "What do I do for a living?",
        "Who am I?",
        "A qué me dedico?",
        "Cuáles son mis intereses?",
        "Qué sabes de mí?",
    ):
        res = analyzer.analyze(text, "p1", reference_time=ref)
        assert res.scope == RecallScope.PROFILE_SYNTHESIS, text
        assert res.recall_intent == RecallIntent.EXPLICIT, text

    imp = analyzer.analyze("Using these shaders again; the last time we chatted I picked ACES", "p1", reference_time=ref)
    assert imp.recall_intent == RecallIntent.IMPLICIT
    imp2 = analyzer.analyze("It behaves like I said earlier on stream", "p1", reference_time=ref)
    assert imp2.recall_intent == RecallIntent.IMPLICIT

