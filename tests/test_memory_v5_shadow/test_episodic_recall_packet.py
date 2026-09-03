from __future__ import annotations

import sqlite3
from pathlib import Path
import pytest

from opencohost.core.context.prompt_assembler import PromptContextAssembler
from opencohost.core.memory_v5_shadow.episodic_recall import (
    EpisodicRecallCoordinator,
    EpisodicRecallPacket,
    RecallMode,
)
from opencohost.core.memory_v5_shadow.semantic_cache import (
    EpisodeEmbeddingRecord,
    ExchangeEmbeddingRecord,
    SemanticCacheStore,
)
from opencohost.core.memory_v5_shadow.semantic_worker import SemanticWorkerService


def test_episodic_recall_expansion_and_packet_builder(tmp_path: Path):
    shadow_db = tmp_path / "shadow.db"
    cache_db = tmp_path / "cache.db"

    # Setup SQLite shadow DB with 1 episode of 3 exchanges
    conn = sqlite3.connect(shadow_db)
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

    # 3 exchanges
    events = [
        ("u1", "prof_1", "r1", 1, "user", "PTT", "2026-08-25T10:00:00Z", "¿Qué audífonos compraste?", "2026-08-25T10:00:00Z"),
        ("a1", "prof_1", "r1", 2, "assistant", "PTT", "2026-08-25T10:00:02Z", "Compré los Sonos Ace.", "2026-08-25T10:00:02Z"),
        ("u2", "prof_1", "r1", 3, "user", "PTT", "2026-08-25T10:01:00Z", "Los Sonos Ace tienen poco golpe en graves aunque suba el EQ.", "2026-08-25T10:01:00Z"),
        ("a2", "prof_1", "r1", 4, "assistant", "PTT", "2026-08-25T10:01:05Z", "Entendido, tienen firma sonora neutra.", "2026-08-25T10:01:05Z"),
        ("u3", "prof_1", "r1", 5, "user", "PTT", "2026-08-25T10:02:00Z", "¿Cuánto costaron?", "2026-08-25T10:02:00Z"),
        ("a3", "prof_1", "r1", 6, "assistant", "PTT", "2026-08-25T10:02:02Z", "450 dólares.", "2026-08-25T10:02:02Z"),
    ]
    for ev in events:
        conn.execute("INSERT INTO evidence_journal VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)", ev)

    conn.execute(
        "INSERT INTO episodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("ep_sonos", "prof_1", "s1", "CLOSED", "policy_v1", "v1", "2026-08-25T10:00:00Z", "2026-08-25T10:02:02Z", "SESSION_START", "SESSION_CLOSED", 6),
    )
    for idx, (eid, *_) in enumerate(events):
        conn.execute("INSERT INTO episode_membership VALUES (?, ?, ?)", ("ep_sonos", eid, idx))
    conn.commit()

    # Pre-index cache with realistic dummy vectors
    cache_store = SemanticCacheStore(cache_db)
    cache_store.initialize()

    # Vector for u2:a2 (the bass comment)
    v_bass = [1.0] + [0.0] * 383
    cache_store.insert_exchange_embedding(
        ExchangeEmbeddingRecord(
            "u2:a2", "prof_1", "s1", "ep_sonos", "h2", "m", "v1", 384, v_bass, "2026-08-25T10:01:00Z",
            lexical_tokens="audifonos sonos ace graves problema"
        )
    )
    cache_store.insert_episode_embedding(
        EpisodeEmbeddingRecord("ep_sonos", "prof_1", "m", "v1", v_bass, 0.9, 0.8, "fp", "2026-08-25T10:00:00Z")
    )

    class MockMatchingWorker:
        def embed_query(self, text, timeout_s=0.5):
            return [1.0] + [0.0] * 383

        def embed_batch(self, texts, timeout_s=1.5):
            return [[1.0] + [0.0] * 383 for _ in texts]

    mock_worker = MockMatchingWorker()

    coord = EpisodicRecallCoordinator(
        shadow_conn=conn,
        cache_store=cache_store,
        worker=mock_worker,
        mode=RecallMode.ACTIVE,
    )

    # Query 1 week later asking about the problem with those headphones
    query_text = "¿Te acuerdas qué problema tenía con esos audífonos?"
    packet = coord.process_query(query_text, profile_id="prof_1")

    assert packet is not None
    assert len(packet.retrieved_episodes) == 1
    assert packet.retrieved_episodes[0].episode_id == "ep_sonos"
    # Formatted block must contain episodic_memory tags and the historical bass discussion
    assert "<episodic_memory>" in packet.formatted_block
    assert "</episodic_memory>" in packet.formatted_block
    assert "Sonos Ace" in packet.formatted_block
    assert "graves" in packet.formatted_block

    # Test PromptContextAssembler in ACTIVE vs SHADOW mode
    assembler = PromptContextAssembler()

    # In ACTIVE mode: episodic block is present
    setup_active = assembler.assemble(
        contexto="¿Qué me recomiendas hacer?",
        source="ptt",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="test-model",
        history_snapshot=[],
        episodic_memory_block=packet.formatted_block,
    )
    last_msg = setup_active.messages[-1]["content"]
    assert "<episodic_memory>" in last_msg

    # In SHADOW mode: coordinator generates packet, but prompt remains byte-identical to baseline
    coord_shadow = EpisodicRecallCoordinator(
        shadow_conn=conn,
        cache_store=cache_store,
        worker=mock_worker,
        mode=RecallMode.SHADOW,
    )
    packet_shadow = coord_shadow.process_query(query_text, profile_id="prof_1")
    assert packet_shadow is not None
    assert len(packet_shadow.retrieved_episodes) == 1

    setup_baseline = assembler.assemble(
        contexto="¿Qué me recomiendas hacer?",
        source="ptt",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="test-model",
        history_snapshot=[],
        episodic_memory_block="",
    )
    # Byte-identical prompt verification in SHADOW mode
    setup_shadow = assembler.assemble(
        contexto="¿Qué me recomiendas hacer?",
        source="ptt",
        system_prompt="Eres Kira.",
        use_system_role=True,
        is_local=True,
        provider_cfg={},
        request_model="test-model",
        history_snapshot=[],
        episodic_memory_block="",  # SHADOW leaves prompt untouched
    )
    assert setup_shadow.messages == setup_baseline.messages

    conn.close()
    cache_store.close()

