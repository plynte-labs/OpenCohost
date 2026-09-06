#!/usr/bin/env python3
"""
Inspect Episode Semantic Similarity Tool for Memory v5 Shadow

Reads a SQLite shadow database in read-only mode.
Resolves Episode evidence strictly through episode_membership.event_id ordered by sequence_index.
Embeds all unique exchanges once and computes aggregate episode representations.
Outputs metadata-only diagnostics:
- within-episode exchange cohesion (mean and minimum exchange->centroid cosine)
- nearest episodes overall
- nearest episodes excluding the same session
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

# Add project root to path
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from opencohost.core.memory_v5_shadow.semantic import (
    MiniLMEmbeddingBackend,
    batch_embed_unique_exchanges,
    build_episode_semantic_representation,
    compute_episode_similarities,
    extract_exchanges_from_events,
    load_episode_evidence_events,
)


def format_delta_time(seconds: float) -> str:
    abs_s = abs(seconds)
    sign = "+" if seconds >= 0 else "-"
    if abs_s < 60:
        return f"{sign}{abs_s:.0f}s"
    elif abs_s < 3600:
        return f"{sign}{abs_s / 60:.1f}m"
    else:
        return f"{sign}{abs_s / 3600:.1f}h"


def inspect_episode_similarity(
    db_path: Path | str,
    episode_id: str | None = None,
    profile_id: str | None = None,
) -> int:
    path = Path(db_path)
    if not path.exists():
        print(f"[ERROR] Database file not found: {path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        query = "SELECT * FROM episodes WHERE 1=1"
        params: list[str] = []
        if episode_id:
            query += " AND episode_id = ?"
            params.append(episode_id)
        if profile_id:
            query += " AND profile_id = ?"
            params.append(profile_id)
        query += " ORDER BY started_at ASC"

        episodes = conn.execute(query, params).fetchall()
        if not episodes:
            print(f"[INFO] No matching episodes found in {path}")
            return 0

        print("============================================================")
        print("  Memory v5 Shadow — Episode Semantic Similarity Inspector  ")
        print("============================================================")
        print(f"Target DB:        {path}")
        print(f"Total Episodes:   {len(episodes)}")

        # Step 1: Resolve Evidence strictly via episode_membership.event_id ordered by sequence_index
        all_exchanges: list = []
        ep_exchanges_map: dict[str, list] = {}
        ep_timestamps: dict[str, str] = {}

        for ep in episodes:
            eid = ep["episode_id"]
            sid = ep["session_id"]
            ep_timestamps[eid] = ep["started_at"]

            raw_events = load_episode_evidence_events(conn, eid)
            exs, diag = extract_exchanges_from_events(raw_events, session_id=sid)
            ep_exchanges_map[eid] = exs
            all_exchanges.extend(exs)

        # Step 2: Embed unique exchanges ONCE across all episodes
        backend = MiniLMEmbeddingBackend()
        backend.initialize()
        exchange_vectors = batch_embed_unique_exchanges(all_exchanges, backend=backend)

        # Step 3: Build aggregate EpisodeSemanticRepresentation with cohesion diagnostics
        representations = []
        for ep in episodes:
            eid = ep["episode_id"]
            sid = ep["session_id"]
            pid = ep["profile_id"]
            exs = ep_exchanges_map[eid]
            vecs = [
                exchange_vectors[f"{ex.user_event_id}:{ex.assistant_event_id}"]
                for ex in exs
                if f"{ex.user_event_id}:{ex.assistant_event_id}" in exchange_vectors
            ]
            rep = build_episode_semantic_representation(
                episode_id=eid,
                session_id=sid,
                profile_id=pid,
                exchange_vectors=vecs,
            )
            if rep is not None:
                representations.append(rep)

        if not representations:
            print("[WARN] No episodes could be represented semantically (no complete exchanges).")
            return 0

        # Step 4: Compute pairwise similarities
        reports = compute_episode_similarities(representations, episode_timestamps=ep_timestamps)

        # Step 5: Format metadata-only diagnostic report
        for r in reports:
            ep_row = [ep for ep in episodes if ep["episode_id"] == r.episode_id][0]
            st = ep_row["state"]
            ev_count = ep_row["event_count"]
            start_t = ep_row["started_at"]
            end_t = ep_row["ended_at"] or "Active"
            co = r.cohesion

            print(f"\n--- Episode [{r.episode_id[:10]}..] ---")
            print(f"  Session ID:     [{r.session_id[:10]}..] | Profile: {r.profile_id}")
            print(f"  State:          {st} | Events: {ev_count} | Exchanges: {co.exchange_count}")
            print(f"  Time Window:    {start_t} -> {end_t}")
            print(
                f"  Cohesion:       Mean Exchange->Centroid = {co.mean_exchange_to_centroid:0.4f} | "
                f"Min = {co.min_exchange_to_centroid:0.4f}"
            )

            # Nearest Overall
            print("\n  Nearest Episodes (Overall):")
            if not r.nearest_overall:
                print("    (No other episodes to compare)")
            else:
                for idx, n in enumerate(r.nearest_overall[:5], 1):
                    dt_str = format_delta_time(n.time_delta_seconds)
                    tag = "(same session)" if n.is_same_session else "(cross session)"
                    print(
                        f"    {idx}. Episode [{n.neighbor_episode_id[:10]}..] | "
                        f"Cosine: {n.cosine_similarity:0.4f} | dt: {dt_str:>7s} | {tag}"
                    )

            # Nearest Cross-Session
            print("\n  Nearest Episodes (Cross-Session Only):")
            if not r.nearest_cross_session:
                print("    (No cross-session episodes)")
            else:
                for idx, n in enumerate(r.nearest_cross_session[:5], 1):
                    dt_str = format_delta_time(n.time_delta_seconds)
                    print(
                        f"    {idx}. Episode [{n.neighbor_episode_id[:10]}..] | "
                        f"Cosine: {n.cosine_similarity:0.4f} | dt: {dt_str:>7s}"
                    )

        print("\n============================================================")
        print(" [METADATA-ONLY DIAGNOSTIC: PROXIMITY ONLY — NO LABELS/MERGE]")
        print("============================================================")
        return 0

    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect Episode semantic similarity and cohesion for Memory v5 Shadow."
    )
    parser.add_argument("--db", required=True, help="Path to SQLite shadow database")
    parser.add_argument("--episode-id", default=None, help="Filter by Episode ID")
    parser.add_argument("--profile-id", default=None, help="Filter by Profile ID")
    args = parser.parse_args()

    ret = inspect_episode_similarity(args.db, episode_id=args.episode_id, profile_id=args.profile_id)
    sys.exit(ret)


if __name__ == "__main__":
    main()
