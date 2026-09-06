#!/usr/bin/env python3
"""
Inspect Semantic Signals Tool for Memory v5 Shadow

Reads a SQLite shadow database in read-only mode, reconstructs authoritative
Session membership via SessionFormationReducer, and extracts ConversationalExchanges
strictly scoped to each Session. Computes batch MiniLM semantic signals.
Outputs metadata-only diagnostics without exposing raw conversation text.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add project root to path
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from opencohost.core.memory_v5_shadow.semantic import (
    MiniLMEmbeddingBackend,
    SemanticSignalProvider,
    extract_exchanges_from_events,
)
from opencohost.core.memory_v5_shadow.sessions import SessionFormationReducer


def inspect_semantic_signals(db_path: Path | str, session_id: str | None = None) -> int:
    path = Path(db_path)
    if not path.exists():
        print(f"[ERROR] Database file not found: {path}", file=sys.stderr)
        return 1

    # Authoritative replay of session membership
    reducer = SessionFormationReducer()
    all_sessions = reducer.rebuild_from_db(path, include_evidence_payload=True)

    if session_id:
        target_sessions = [s for s in all_sessions if s["session_id"] == session_id]
        if not target_sessions:
            print(f"[ERROR] Session {session_id} not found in {path}", file=sys.stderr)
            return 1
    else:
        target_sessions = all_sessions

    if not target_sessions:
        print(f"[INFO] No sessions found in {path}")
        return 0

    print("==================================================")
    print("  Memory v5 Shadow — Semantic Signal Inspector   ")
    print("==================================================")
    print(f"Target DB:        {path}")
    print(f"Total Sessions:   {len(target_sessions)}")

    backend = MiniLMEmbeddingBackend()
    backend.initialize()
    provider = SemanticSignalProvider(backend=backend)

    for s in target_sessions:
        sid = s["session_id"]
        st = s["state"]
        pid = s["profile_id"]
        start_t = s["started_at"]
        end_t = s["ended_at"] or "Active"
        ev_count = s["event_count"]

        print(f"\n--- Session [{sid[:10]}..] ---")
        print(f"  Profile:        {pid}")
        print(f"  State:          {st} | Total Assigned Events: {ev_count}")
        print(f"  Time Window:    {start_t} -> {end_t}")

        # Fetch authoritative events strictly assigned to this session by the reducer
        session_events = reducer.session_events.get(sid, [])
        exchanges, diag = extract_exchanges_from_events(session_events, session_id=sid)

        print(
            f"  Exchanges:      {diag.complete_exchanges} complete | "
            f"Orphan Users: {diag.orphan_user_events} | "
            f"Orphan Assistants: {diag.orphan_assistant_events}"
        )

        if not exchanges:
            print("  [WARN] No complete conversational exchanges found in this session.")
            continue

        signals = provider.analyze_session(exchanges)

        print("\n  Ex # | Time (UTC) | Sim to Prev | Sim to Context (3-win) | Trajectory")
        print("  ----+------------+-------------+------------------------+------------")
        for sig in signals:
            ord_str = f"#{sig.exchange_ordinal:02d}"
            t_str = sig.occurred_at[11:19] if len(sig.occurred_at) >= 19 else sig.occurred_at
            prev_s = f"{sig.similarity_to_previous:0.4f}" if sig.similarity_to_previous is not None else "  N/A "
            ctx_s = f"{sig.similarity_to_recent_context:0.4f}" if sig.similarity_to_recent_context is not None else "  N/A "

            # Visual sparkline indicator
            if sig.similarity_to_recent_context is None:
                bar = "[START]"
            else:
                bars_n = int(max(0.0, sig.similarity_to_recent_context) * 10)
                bar = "#" * bars_n + "-" * (10 - bars_n)

            print(f"  {ord_str} |  {t_str}  |   {prev_s}    |         {ctx_s}         | {bar}")

        valid_ctx_sims = [sig.similarity_to_recent_context for sig in signals if sig.similarity_to_recent_context is not None]
        if valid_ctx_sims:
            mean_sim = sum(valid_ctx_sims) / len(valid_ctx_sims)
            min_sim = min(valid_ctx_sims)
            min_idx = [sig.exchange_ordinal for sig in signals if sig.similarity_to_recent_context == min_sim][0]
            print(
                f"\n  [Signals Summary]: Mean Context Sim = {mean_sim:0.4f} | "
                f"Min Context Sim = {min_sim:0.4f} (at Ex #{min_idx:02d})"
            )

    print("\n==================================================")
    print(" [METADATA-ONLY AUDIT: OK — ZERO PAYLOAD EXPOSED] ")
    print("==================================================")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect semantic signals for Memory v5 Shadow sessions.")
    parser.add_argument("--db", required=True, help="Path to SQLite shadow database")
    parser.add_argument("--session-id", default=None, help="Filter by session ID")
    args = parser.parse_args()

    ret = inspect_semantic_signals(args.db, session_id=args.session_id)
    sys.exit(ret)


if __name__ == "__main__":
    main()
