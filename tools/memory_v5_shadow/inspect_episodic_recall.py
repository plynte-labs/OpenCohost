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
        default=None,
        help="Query text to evaluate recall on (not needed with --replay-six)",
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
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Strict privacy: print metadata only (IDs, states, counts, "
        "dates, reason codes, scores, block lengths, hashes) — never raw "
        "corpus/chat text, exchanges, or context blocks.",
    )
    parser.add_argument(
        "--temp-copy",
        action="store_true",
        help="Work on temporary backup copies of the authoritative DBs "
        "(via the SQLite backup API, safe on live locked files) and report "
        "authoritative snapshot hashes before/after to prove them unchanged.",
    )
    parser.add_argument(
        "--replay-six",
        action="store_true",
        help="Replay the six fixed acceptance phrases plus the negative "
        "control against temporary copies (implies --temp-copy and "
        "--metadata-only). Runs startup recovery on the copies first.",
    )

    args = parser.parse_args()

    if args.replay_six:
        args.temp_copy = True
        args.metadata_only = True
        if not args.reference_time:
            args.reference_time = "2026-09-03T12:00:00+00:00"

    if args.replay_six:
        return _run_replay_six(args)

    if not args.query:
        print("Error: --query is required without --replay-six", file=sys.stderr)
        return 2

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
        print(f"Scope             : {packet.scope.value if hasattr(packet.scope, 'value') else packet.scope}")
        print(f"Candidates        : {packet.candidate_count}")
        print(f"Episodes Retained : {len(packet.retrieved_episodes)}")
        print(f"Token Estimate    : {packet.token_estimate} tokens")
        print(f"Block Length      : {len(packet.formatted_block)} chars")

        if args.metadata_only:
            # Metadata only: IDs, dates, counts, scores — never corpus text.
            for idx, ep in enumerate(packet.retrieved_episodes, start=1):
                print(f"  [Episode #{idx}] id={ep.episode_id} "
                      f"session={ep.session_id} started={ep.started_at} "
                      f"score={ep.composite_score:.4f} exchanges={len(ep.exchanges)}")
            print("================================================================================")
            return 0

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
        if args.metadata_only:
            print(f"[Withheld in metadata-only mode: {len(packet.formatted_block)} chars]")
        elif packet.formatted_block:
            print(packet.formatted_block)
        else:
            print("[Empty Context Block - No memory injected into prompt]")

        print("================================================================================")
        return 0

    finally:
        worker.shutdown()
        conn.close()
        cache_store.close()


# ---------------------------------------------------------------------------
# Metadata-only six-phrase replay on temporary copies (acceptance harness)
# ---------------------------------------------------------------------------

REPLAY_PHRASES = [
    "que opino de la IA segun mis memorias?",
    "A que me dedico, cuales son mis hobbies y que pienso o tengo interes, que practico",
    "que discutimos la sesion pasada?",
    "las sesiones del 2 de septiembre que estudiamos o discutimos?",
    "hablamos sobre el heap o stack?",
    "que opinamos de la IA y el test de turing?",
]
REPLAY_NEGATIVE = "¿Cuál es la capital de Francia?"


def _sha256_file(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _backup_live_db(src: Path, dst: Path) -> bool:
    """Copy a (possibly live-locked) SQLite DB via the backup API."""
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            dst.unlink()
        src_con = sqlite3.connect(f"file:{src.resolve()}?mode=ro", uri=True, timeout=10)
        try:
            dst_con = sqlite3.connect(str(dst))
            try:
                src_con.backup(dst_con)
            finally:
                dst_con.close()
        finally:
            src_con.close()
        return True
    except Exception as exc:
        print(f"[replay] backup skipped for {src}: {type(exc).__name__}", file=sys.stderr)
        return False


def _v4_block_len(v4_copy: Path, profile_id: str, query_text: str) -> str:
    """Length of the v4 injection block on a temp copy (replicates the
    engine's routing: meta-recall -> recency, else lexical). Length only —
    never content."""
    try:
        from opencohost.config.settings import MEMORIAS_MAX_INJECT_CHARS
        from opencohost.core.memory.memoria_store import (
            MemoriaStore,
            build_injection_lines,
            build_recency_lines,
            is_meta_recall_query,
        )
        from opencohost.i18n import active as i18n_active

        store = MemoriaStore(v4_copy)
        rows = store.list_injection_candidates(profile_id)
        if not rows:
            return "0"
        if is_meta_recall_query(query_text):
            lines = build_recency_lines(rows)
        else:
            lines = build_injection_lines(rows, query_text)
        if not lines:
            return "0"
        block = (
            i18n_active.memorias_block_open() + "\n"
            + "\n".join(lines)
            + "\n" + i18n_active.memorias_block_close()
        )
        # Same char budget the engine enforces.
        _ = MEMORIAS_MAX_INJECT_CHARS
        return str(len(block))
    except Exception as exc:
        return f"n/a ({type(exc).__name__})"


def _run_replay_six(args) -> int:
    import tempfile

    from opencohost.core.memory_v5_shadow.subsystem import MemorySubsystem

    work = Path(tempfile.mkdtemp(prefix="v5replay_"))
    snap0 = work / "snap0"
    snap1 = work / "snap1"
    snap0.mkdir()
    snap1.mkdir()

    shadow_src = Path(args.db)
    cache_src = Path(args.cache_db)
    try:
        from opencohost.config.settings import MEMORIAS_DB
    except Exception:
        MEMORIAS_DB = None
    v4_src = Path(MEMORIAS_DB) if MEMORIAS_DB else None

    ref_time = None
    if args.reference_time:
        ref_time = datetime.fromisoformat(args.reference_time.replace("Z", "+00:00"))

    # H0: content snapshots of the authoritative DBs (backup API works on
    # live-locked files; direct file hashing is unavailable while locked).
    sources = {"shadow": shadow_src}
    if cache_src.exists():
        sources["cache"] = cache_src
    if v4_src is not None and v4_src.exists():
        sources["v4"] = v4_src
    hashes0: dict[str, str] = {}
    copies: dict[str, Path] = {}
    for name, src in sources.items():
        dst = work / f"{name}_copy.db"
        if _backup_live_db(src, dst):
            copies[name] = dst
            snap = snap0 / f"{name}.db"
            _backup_live_db(src, snap)
            hashes0[name] = _sha256_file(snap)

    if "shadow" not in copies:
        print("[replay] cannot copy authoritative shadow DB; aborting", file=sys.stderr)
        return 1

    print("================================================================================")
    print("MEMORY V5 SIX-PHRASE METADATA-ONLY REPLAY (temporary copies)")
    print("================================================================================")
    print(f"Work dir        : {work}")
    print(f"Profile ID      : {args.profile_id}")
    print(f"Reference Time  : {ref_time or 'NOW (UTC)'}")
    print(f"Worker          : {'dummy pseudo-vectors' if args.use_dummy else 'local MiniLM'}")

    # Startup recovery on the COPY (never on the authoritative DB).
    recovery = MemorySubsystem.startup_episode_recovery(str(copies["shadow"]))
    print(f"Recovery (copy) : {recovery}")

    conn = sqlite3.connect(str(copies["shadow"]), timeout=10.0)
    cache_store = SemanticCacheStore(copies.get("cache", work / "fresh_cache.db"))
    cache_store.initialize()
    worker = SemanticWorkerService(use_dummy=args.use_dummy)
    worker.start(timeout=120.0)
    try:
        indexer = IncrementalSemanticIndexer(conn, cache_store, worker)
        newly = indexer.reconcile_unindexed_episodes()
        print(f"Reconciled (copy): {newly} episode(s) newly indexed")

        analyzer = EpisodicQueryAnalyzer()
        coord = EpisodicRecallCoordinator(
            shadow_conn=conn,
            cache_store=cache_store,
            worker=worker,
            mode=RecallMode.ACTIVE,
            query_analyzer=analyzer,
        )

        rows = []
        for idx, phrase in enumerate(REPLAY_PHRASES + [REPLAY_NEGATIVE], start=1):
            label = f"Q{idx}" if idx <= len(REPLAY_PHRASES) else "NEG"
            analysis = analyzer.analyze(phrase, profile_id=args.profile_id,
                                        reference_time=ref_time)
            packet = coord.process_query(phrase, profile_id=args.profile_id,
                                         reference_time=ref_time)
            if packet is None:
                rows.append((label, "OFF", "-", "-", "0", "-", "0", "n/a", "RECALL_OFF"))
                continue
            tc = analysis.temporal_constraint
            temporal = tc.constraint_type.value if tc else "-"
            ep_ids = ",".join(e.episode_id[:8] for e in packet.retrieved_episodes) or "-"
            v5len = str(len(packet.formatted_block))
            v4len = _v4_block_len(copies["v4"], args.profile_id, phrase) if "v4" in copies else "n/a"
            scope = analysis.scope.value if hasattr(analysis.scope, "value") else str(analysis.scope)
            rows.append((label, f"{analysis.recall_intent.value}/{scope}",
                         temporal, str(packet.candidate_count), ep_ids,
                         v5len, v4len, packet.reason_code))

        print("--------------------------------------------------------------------------------")
        print(f"{'q':<4}{'intent/scope':<32}{'temporal':<14}{'cand':<6}{'episodes':<28}{'v5len':<8}{'v4len':<8}reason")
        for label, intent_scope, temporal, cand, ep_ids, v5len, v4len, reason in rows:
            print(f"{label:<4}{intent_scope:<32}{temporal:<14}{cand:<6}{ep_ids:<28}{v5len:<8}{v4len:<8}{reason}")
        print("--------------------------------------------------------------------------------")
        print("Phrase inputs are the six fixed acceptance queries (Q1..Q6) plus the")
        print("negative control (NEG). No corpus/chat text is printed in this mode.")
    finally:
        worker.shutdown()
        conn.close()
        cache_store.close()

    # H1: fresh snapshots of the authoritative DBs — must equal H0.
    print("--------------------------------------------------------------------------------")
    ok = True
    for name, src in sources.items():
        snap = snap1 / f"{name}.db"
        if _backup_live_db(src, snap):
            h1 = _sha256_file(snap)
            match = hashes0.get(name) == h1
            ok = ok and match
            print(f"Authoritative {name}: sha256 match={match} "
                  f"(before={hashes0.get(name, '?')[:16]}.. after={h1[:16]}..)")
        else:
            print(f"Authoritative {name}: could not re-snapshot (see warning above)")
    print("================================================================================")
    print("REPLAY_UNCHANGED" if ok else "REPLAY_HASH_MISMATCH")
    return 0 if ok else 3


if __name__ == "__main__":
    sys.exit(main())
