#!/usr/bin/env python3
"""
CLI tool for operator inspection of Memory v5 Full Episodic Recall.

Demonstrates the complete episodic recall pipeline on real or test shadow databases:
Query Analysis -> Isolated Semantic Embedding -> Candidate Retrieval ->
Hybrid Ranking -> MMR Diversity -> Context Expansion -> EpisodicRecallPacket.

Usage:
    python tools/memory_v5_shadow/inspect_episodic_recall.py \\
        --db .artifacts/memory_v5_shadow/manual/francisco-trial.db \\
        --profile-id 30ea444e-99c7-4369-95bc-ff9215314aa3 \\
        --query "¿Te acuerdas qué hablamos de soberanía de datos y SQLite?"
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# Force UTF-8 on Windows stdout if possible
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from opencohost.core.memory_v5_shadow.episodic_recall import (
    EpisodicRecallCoordinator,
    RecallMode,
)
from opencohost.core.memory_v5_shadow.query_analyzer import (
    EpisodicQueryAnalyzer,
)
from opencohost.core.memory_v5_shadow.retrieval import (
    CandidateRetriever,
    HybridEpisodicRanker,
)
from opencohost.core.memory_v5_shadow.semantic_cache import (
    SemanticCacheStore,
)
from opencohost.core.memory_v5_shadow.semantic_indexer import (
    IncrementalSemanticIndexer,
)
from opencohost.core.memory_v5_shadow.semantic_worker import (
    SemanticWorkerService,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect Memory v5 episodic recall pipeline on a shadow database."
    )
    parser.add_argument(
        "--db",
        type=str,
        default=".artifacts/memory_v5_shadow/manual/francisco-trial.db",
        help="Path to shadow SQLite database",
    )
    parser.add_argument(
        "--cache-db",
        type=str,
        default=".artifacts/memory_v5_shadow/semantic_cache.db",
        help="Path to derived semantic cache SQLite database",
    )
    parser.add_argument(
        "--profile-id",
        type=str,
        default="30ea444e-99c7-4369-95bc-ff9215314aa3",
        help="Profile ID to query",
    )
    parser.add_argument(
        "--query",
        type=str,
        required=True,
        help="Query text to evaluate recall on",
    )
    parser.add_argument(
        "--reference-time",
        type=str,
        default=None,
        help="Reference time in ISO format (default: now)",
    )
    parser.add_argument(
        "--mode",
        type=str,
        choices=["ACTIVE", "SHADOW"],
        default="ACTIVE",
        help="Recall mode",
    )
    parser.add_argument(
        "--use-dummy",
        action="store_true",
        help="Use deterministic pseudo-embeddings instead of local MiniLM model",
    )
    parser.add_argument(
        "--reindex",
        action="store_true",
        help="Force re-indexing of all closed episodes into cache before query",
    )

    args = parser.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"Error: Shadow database not found at {db_path}", file=sys.stderr)
        return 1

    cache_path = Path(args.cache_db)
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    ref_time = None
    if args.reference_time:
        ref_time = datetime.fromisoformat(args.reference_time.replace("Z", "+00:00"))

    print("================================================================================")
    print("MEMORY V5 EPISODIC RECALL INSPECTION")
    print("================================================================================")
    print(f"Shadow DB     : {db_path}")
    print(f"Cache DB      : {cache_path}")
    print(f"Profile ID    : {args.profile_id}")
    print(f"Query         : {args.query}")
    print(f"Reference Time: {ref_time or 'NOW (UTC)'}")
    print(f"Mode          : {args.mode}")
    print("--------------------------------------------------------------------------------")

    # Connect to shadow store and cache
    conn = sqlite3.connect(f"file:{db_path.resolve()}?mode=ro", uri=True)
    cache_store = SemanticCacheStore(cache_path)
    cache_store.initialize()

    # Start isolated semantic worker
    worker = SemanticWorkerService(use_dummy=args.use_dummy)
    worker.start()

    try:
        # Reconcile / index
        indexer = IncrementalSemanticIndexer(
            shadow_conn=conn,
            cache_store=cache_store,
            worker=worker,
        )
        if args.reindex:
            cache_store.purge_profile_cache(args.profile_id)
        indexed_count = indexer.reconcile_unindexed_episodes()
        if indexed_count > 0:
            print(f"[Indexer] Indexed {indexed_count} new closed episodes into semantic cache.")

        # Query Analysis
        analyzer = EpisodicQueryAnalyzer()
        query_analysis = analyzer.analyze(args.query, profile_id=args.profile_id, reference_time=ref_time)
        print("\n--- 1. QUERY UNDERSTANDING ---")
        print(f"Recall Intent     : {query_analysis.recall_intent.value}")
        print(f"Normalized Query  : {query_analysis.normalized_query}")
        print(f"Lexical Anchors   : {query_analysis.lexical_anchors}")
        if query_analysis.temporal_constraint:
            tc = query_analysis.temporal_constraint
            print(f"Temporal Filter   : {tc.constraint_type.value} (n_val={tc.n_value})")
        else:
            print("Temporal Filter   : None (Open Window)")

        # Recall Coordinator
        coord = EpisodicRecallCoordinator(
            shadow_conn=conn,
            cache_store=cache_store,
            worker=worker,
            mode=RecallMode(args.mode),
            query_analyzer=analyzer,
        )

        packet = coord.process_query(args.query, profile_id=args.profile_id, reference_time=ref_time)

        print("\n--- 2. RETRIEVAL & RECALL OUTCOME ---")
        if packet is None:
            print("Outcome: RECALL_OFF")
            return 0

        print(f"Reason Code       : {packet.reason_code}")
        print(f"Episodes Retained : {len(packet.retrieved_episodes)}")
        print(f"Token Estimate    : {packet.token_estimate} tokens")

        for idx, ep in enumerate(packet.retrieved_episodes, start=1):
            print(f"\n  [Episode #{idx}]")
            print(f"  ID             : {ep.episode_id}")
            print(f"  Session ID     : {ep.session_id}")
            print(f"  Started At     : {ep.started_at}")
            print(f"  Composite Score: {ep.composite_score:.4f}")
            print(f"  Exchanges Count: {len(ep.exchanges)}")
            for e_idx, ex in enumerate(ep.exchanges, start=1):
                print(f"    Turn {e_idx}:")
                print(f"      User: {ex.user_text}")
                print(f"      Kira: {ex.assistant_text[:80]}...")

        print("\n--- 3. ASSEMBLED EPISODIC CONTEXT BLOCK ---")
        if packet.formatted_block:
            print(packet.formatted_block)
        else:
            print("[Empty Context Block - No memory injected into prompt]")

        print("================================================================================")
        return 0

    finally:
        worker.shutdown()
        conn.close()
        cache_store.close()


if __name__ == "__main__":
    sys.exit(main())
