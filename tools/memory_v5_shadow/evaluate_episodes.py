#!/usr/bin/env python3
"""OpenCohost Memory v5 Shadow - Episode Evaluator & Consolidation Diagnostics

Computes truthful, metadata-only diagnostics on segmented episodes.
Zero synthetic proxy inflation, zero PII, zero prompt exposure.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict

# Add project root to sys.path
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opencohost.core.memory_v5_shadow.episodes import EpisodeSegmentationEngine


def compute_consolidation_metrics(db_path: Path | str) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        episodes = conn.execute(
            "SELECT * FROM episodes ORDER BY started_at, episode_id"
        ).fetchall()
        if not episodes:
            return {
                "total_episodes": 0,
                "mean_evidence_per_episode": 0.0,
                "episode_size_distribution": {},
                "exact_content_repetition_rate": 0.0,
                "human_evaluation_status": "PENDING_REAL_CORPUS",
                "consolidation_pressure": "INSUFFICIENT_EVIDENCE",
            }

        total_eps = len(episodes)
        turn_counts = [e["event_count"] for e in episodes]
        mean_turns = sum(turn_counts) / max(1, total_eps)

        density_counts = Counter(
            [
                "1-4"
                if t <= 4
                else "5-10"
                if t <= 10
                else "11-20"
                if t <= 20
                else "21-30"
                if t <= 30
                else "30+"
                for t in turn_counts
            ]
        )

        cur = conn.execute(
            """
            SELECT DISTINCT m.episode_id, e.content_hash
            FROM episode_membership m
            JOIN evidence_journal e ON m.event_id = e.event_id
            """
        )
        members = cur.fetchall()
        hash_to_eps = Counter([r["content_hash"] for r in members])
        duplicate_hashes = sum(1 for h, c in hash_to_eps.items() if c > 1)
        unique_hashes = max(1, len(hash_to_eps))
        exact_content_repetition = duplicate_hashes / unique_hashes

        return {
            "total_episodes": total_eps,
            "mean_evidence_per_episode": round(mean_turns, 2),
            "episode_size_distribution": dict(density_counts),
            "exact_content_repetition_rate": round(exact_content_repetition, 4),
            "human_evaluation_status": "PENDING_REAL_CORPUS",
            "consolidation_pressure": "INSUFFICIENT_EVIDENCE",
        }
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(
        description="Evaluate Memory v5 Shadow Episodes and Diagnostics"
    )
    parser.add_argument("--db", type=str, required=True, help="Path to shadow DB")
    parser.add_argument(
        "--segment",
        action="store_true",
        help="Run episode segmentation before evaluating",
    )
    args = parser.parse_args()
    db_p = Path(args.db)

    if args.segment:
        engine = EpisodeSegmentationEngine()
        engine.segment_all_from_db(db_p, persist=True)

    metrics = compute_consolidation_metrics(db_p)
    print("==================================================")
    print(" Memory v5 Shadow — Episode Diagnostics & Metrics ")
    print("==================================================")
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
