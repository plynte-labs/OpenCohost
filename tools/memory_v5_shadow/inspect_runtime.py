#!/usr/bin/env python3
"""OpenCohost Memory v5 Shadow - Runtime Inspector CLI

Read-only, metadata-only inspector for operator verification of shadow DB.
Prints zero content, zero queries, and zero PII.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

# Add project root to sys.path
ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from opencohost.core.memory_v5_shadow.episodes import (
    EpisodeSegmentationEngine,
    compute_canonical_episodes_hash,
)
from opencohost.core.memory_v5_shadow.sessions import (
    SessionFormationReducer,
    compute_canonical_sessions_hash,
)


def get_db_path(custom_path: str | None = None) -> Path:
    if custom_path:
        return Path(custom_path)
    env_db = os.environ.get("OPENCOHOST_MEMORY_V5_SHADOW_DB")
    if env_db:
        return Path(env_db)
    try:
        from opencohost.config.settings import MEMORY_V5_SHADOW_DB
        return Path(MEMORY_V5_SHADOW_DB)
    except Exception:
        return Path(ROOT) / "data" / "memory_v5_shadow" / "memory_v5_shadow.db"


def inspect(db_path: Path, verify_rebuild: bool = False) -> int:
    mode = os.environ.get("OPENCOHOST_MEMORY_V5_MODE", "OFF").upper()
    print("==================================================")
    print(" OpenCohost Memory v5 Shadow — Runtime Inspector  ")
    print("==================================================")
    print(f"Target DB:        {db_path}")
    print(f"Configured Mode:  {mode}")

    if not db_path.exists():
        print("\n[STATUS] Database file does not exist yet.")
        print("Run OpenCohost with OPENCOHOST_MEMORY_V5_MODE='SHADOW' to initialize.")
        return 0

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        print(f"Tables present:   {', '.join(sorted(tables))}")

        runs = conn.execute("SELECT * FROM shadow_runs ORDER BY rowid DESC LIMIT 5").fetchall()
        print(f"\n--- Shadow Runs (Total: {len(runs)}) ---")
        for r in runs:
            print(f"  Run ID:           {r['run_id']}")
            print(f"  Started:          {r['started_at']}")
            print(f"  Capture Enabled:  {bool(r['capture_enabled'])}")
            print(f"  Degraded:         {bool(r['degraded'])}")
            print(f"  Dropped Evidence: {r['dropped_evidence_total']}")
            print(f"  Control Failures: {r['control_failures_total']}")

        ev_count = conn.execute("SELECT COUNT(*) FROM evidence_journal").fetchone()[0]
        ev_profiles = [r[0] for r in conn.execute("SELECT DISTINCT profile_id FROM evidence_journal").fetchall()]
        last_seq = conn.execute("SELECT MAX(stream_sequence) FROM evidence_journal").fetchone()[0] or 0

        print("\n--- Evidence Journal ---")
        print(f"  Evidence Rows:    {ev_count}")
        print(f"  Last Sequence:    {last_seq}")
        print(f"  Profiles Active:  {len(ev_profiles)} {ev_profiles}")

        if ev_count > 0:
            roles = conn.execute("SELECT role, COUNT(*) FROM evidence_journal GROUP BY role").fetchall()
            role_summary = ", ".join(f"{r[0]}: {r[1]}" for r in roles)
            print(f"  Role Breakdown:   {role_summary}")
            sources = conn.execute("SELECT source, COUNT(*) FROM evidence_journal GROUP BY source").fetchall()
            src_summary = ", ".join(f"{r[0]}: {r[1]}" for r in sources)
            print(f"  Source Breakdown: {src_summary}")

        lc_count = conn.execute("SELECT COUNT(*) FROM lifecycle_events").fetchone()[0]
        print("\n--- Lifecycle Events ---")
        print(f"  Event Rows:       {lc_count}")
        if lc_count > 0:
            kinds = conn.execute("SELECT kind, COUNT(*) FROM lifecycle_events GROUP BY kind").fetchall()
            kind_summary = ", ".join(f"{r[0]}: {r[1]}" for r in kinds)
            print(f"  Kind Breakdown:   {kind_summary}")

        sess_rows = conn.execute("SELECT * FROM sessions ORDER BY started_at, session_id").fetchall()
        live_sessions = [dict(r) for r in sess_rows]
        print(f"\n--- Sessions Projection (Total: {len(live_sessions)}) ---")
        for s in live_sessions:
            print(
                f"  [{s['session_id'][:8]}..] Profile: {s['profile_id']} | State: {s['state']} | "
                f"Events: {s['event_count']} | Open: {s['opened_reason']} | Close: {s['closure_reason'] or 'N/A'}"
            )

        ep_rows = conn.execute("SELECT * FROM episodes ORDER BY started_at, episode_id").fetchall()
        live_episodes = [dict(r) for r in ep_rows]
        print(f"\n--- Episodes Projection (Total: {len(live_episodes)}) ---")
        for ep in live_episodes:
            print(
                f"  [{ep['episode_id'][:8]}..] Sess: {ep['session_id'][:8]}.. | Events: {ep['event_count']} | "
                f"Open: {ep['opened_reason']} | Close: {ep['closure_reason']}"
            )

        if verify_rebuild:
            reducer = SessionFormationReducer()
            rebuilt_sessions = reducer.rebuild_from_db(db_path)
            live_hash = compute_canonical_sessions_hash(live_sessions)
            rebuilt_hash = compute_canonical_sessions_hash(rebuilt_sessions)
            print("\n--- In-Memory Reconstruction Verification ---")
            print(f"  Live Sessions Hash:    {live_hash}")
            print(f"  Rebuilt Sessions Hash: {rebuilt_hash}")
            if live_hash == rebuilt_hash:
                print("  Sessions Replay:       PASS (100% Deterministic Match)")
            else:
                print("  Sessions Replay:       FAIL (Hash Mismatch)")
                return 1

            engine = EpisodeSegmentationEngine()
            rebuilt_eps, _ = engine.segment_all_from_db(db_path, persist=False)
            live_ep_hash = compute_canonical_episodes_hash(live_episodes)
            rebuilt_ep_hash = compute_canonical_episodes_hash(rebuilt_eps)
            print(f"  Live Episodes Hash:    {live_ep_hash}")
            print(f"  Rebuilt Episodes Hash: {rebuilt_ep_hash}")
            if live_ep_hash == rebuilt_ep_hash:
                print("  Episodes Replay:       PASS (100% Deterministic Match)")
            else:
                print("  Episodes Replay:       FAIL (Hash Mismatch)")
                return 1

        print("\n==================================================")
        print(" [METADATA-ONLY AUDIT: OK — ZERO PAYLOAD EXPOSED] ")
        print("==================================================")
        return 0
    except Exception as exc:
        print(f"\n[ERROR] Inspection failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Inspect OpenCohost Memory v5 Shadow DB metadata")
    parser.add_argument("--db", type=str, default=None, help="Custom shadow.db path")
    parser.add_argument(
        "--verify-rebuild",
        action="store_true",
        help="Run in-memory deterministic replay and verify SHA-256 parity against live sessions & episodes",
    )
    args = parser.parse_args()
    db_p = get_db_path(args.db)
    return inspect(db_p, verify_rebuild=args.verify_rebuild)


if __name__ == "__main__":
    sys.exit(main())
