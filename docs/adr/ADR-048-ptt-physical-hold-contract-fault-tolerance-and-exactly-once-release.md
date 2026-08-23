# ADR-048: PTT Physical Hold Contract, Transport Fault Tolerance, and Exactly-Once Release

**Status:** Accepted / Implemented (Commit: 628e8c5 / 916bd73)

## Context & Problem Statement

- Commit `fe9932d` regression: keepalive starvation was coupled to `_release_hold()`, so any network stutter or dropped packet unmuted Kira over an open microphone turn.
- Multi-schema live audio ingestion: WhisperLive/LiveAudio WebSocket payloads send `segments` array and `transcript` string in addition to raw `text`.
- TOCTOU race on rapid tap in `start()`.
- Redundant triple-release on normal `stop()` path.
- Bridge stop packet loss vulnerability.

## Decision Drivers

- **Physical hold primacy:** speech router silence must strictly mirror operator physical key state, not socket health.
- **Fault tolerance:** transport drops must not unmute Kira into live turn; client crash must not permanently mute Kira (deadman timer fail-safe 15s).
- **Concurrency guarantees:** atomic precheck under `_lock`, epoch generation tracking, and `_held_epoch` for strictly exactly-once release.
- **Client resiliency:** bridge non-stacked keepalive cadence and retry-on-failure `stop()`.

## Considered Options & Tradeoffs

- **Option 1:** Keep transport and hold tied (rejected: causes unmuting on any network jitter).
- **Option 2:** Pure unbounded physical hold without deadman (rejected: crashes leave Kira muted forever).
- **Option 3:** Decoupled hold with epoch isolation, deadman fail-safe, exactly-once release token, and bridge retry loop (selected).

## Consequences & Invariants

- **Invariant 1:** `auto_stopped` and `error` in `PttSession._begin_grace` never call `on_release()`.
- **Invariant 2:** `PttController.start()` arms speech hold atomically under lock.
- **Invariant 3:** `PttController._release_hold()` executes exactly once per press epoch.
- **Invariant 4:** `PttController._on_session_closed()` on error arms deadman (15s) with matching epoch.
- **Invariant 5:** `ptt_f10_bridge.py` retries `POST /api/ptt/stop` until 200/409.

## Validation

- 39 unit tests in `tests/test_ptt_session.py` (100% green).
- Dual Blind Review (Judgment Day) passed with 2/2 PASS verdicts in Round 2.
- Runtime validation in Tauri UI with multi-minute PTT turns and Agenda interleaving.
