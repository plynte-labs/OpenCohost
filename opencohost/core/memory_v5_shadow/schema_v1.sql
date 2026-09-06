-- memory_v5_shadow.db — DDL v1 (WU0 Freeze)
-- Track: memory-v5-shadow-formation-foundation | Auth: Engram #6327 WU0_ACQUISITION_AUTHORIZED
-- Baseline: HEAD a1b0af5 KEEP_LEXICAL (78/78 GREEN)
-- This file is the sole authoritative schema for Shadow Mode. Legacy memorias.db (v4, user_version=4) is never attached or migrated.
-- Verify: python -c "import sqlite3; conn=sqlite3.connect(':memory:'); conn.executescript(open('docs/memory_v5/memory_v5_shadow_ddl_v1.sql').read()); print(conn.execute('PRAGMA user_version').fetchone())"  -- must be (1,)

PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;
PRAGMA user_version = 1;

-- ---------------------------------------------------------------------------
-- 1. shadow_runs — Per-process scientific state; CLEAN/UNCLEAN remains derived
-- from durable SHUTDOWN markers rather than stored as an authoritative flag.
CREATE TABLE IF NOT EXISTS shadow_runs (
    run_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    degraded INTEGER NOT NULL DEFAULT 0 CHECK(degraded IN (0,1)),
    capture_enabled INTEGER NOT NULL DEFAULT 1 CHECK(capture_enabled IN (0,1)),
    dropped_evidence_total INTEGER NOT NULL DEFAULT 0 CHECK(dropped_evidence_total >= 0),
    control_failures_total INTEGER NOT NULL DEFAULT 0 CHECK(control_failures_total >= 0),
    created_at TEXT NOT NULL
);

-- 2. evidence_journal — Authoritative shadow input (immutable content while retained)
-- REQ-3: committed_turn_id stable identity, event_id = sha256(committed_turn_id)[:24],
--       total ordering (run_id, stream_sequence), content_hash, is_private mutable.
-- Purge: DELETE FROM evidence_journal WHERE profile_id=?  (profile-scoped, FK parent for membership)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS evidence_journal (
    event_id TEXT PRIMARY KEY,                          -- sha256(committed_turn_id)[:24], worker-computed after lock
    committed_turn_id TEXT NOT NULL UNIQUE,             -- stable message identity: f"{run_id}:{stream_sequence}"
    profile_id TEXT NOT NULL,
    run_id TEXT NOT NULL,                               -- process run UUID
    stream_sequence INTEGER NOT NULL,                    -- monotonic per run, shared with lifecycle_events
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    source TEXT NOT NULL CHECK(source IN ('direct','ptt','owner-bundle')),
    occurred_at TEXT NOT NULL,                          -- ISO8601 UTC, turn commit time
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,                         -- sha256(content)
    is_private INTEGER NOT NULL DEFAULT 0 CHECK(is_private IN (0,1)),  -- mutable policy; content is not
    retention_disposition TEXT NOT NULL DEFAULT 'ACTIVE'
        CHECK(retention_disposition IN ('ACTIVE','PURGE_PENDING')),
    created_at TEXT NOT NULL                            -- ISO8601 UTC, persist time
    ,UNIQUE(run_id, stream_sequence)
);
CREATE INDEX IF NOT EXISTS idx_evidence_profile_seq   ON evidence_journal(profile_id, stream_sequence);
CREATE INDEX IF NOT EXISTS idx_evidence_stream_order  ON evidence_journal(run_id, stream_sequence);
CREATE INDEX IF NOT EXISTS idx_evidence_profile_created ON evidence_journal(profile_id, created_at);
CREATE INDEX IF NOT EXISTS idx_evidence_content_hash ON evidence_journal(content_hash);

-- ---------------------------------------------------------------------------
-- 3. lifecycle_events — Purge-safe hard boundary provenance (REQ-4)
-- Profile switch is a decoupled pair sharing transition_id:
--   PROFILE_SWITCH_OUT owned by A, PROFILE_SWITCH_IN owned by B, same transition_id.
-- Purging B deletes only B's IN marker, preserving A's OUT. SHUTDOWN is global (owner_profile_id NULL).
-- lifecycle_id = sha256(f"{run_id}:{stream_sequence}:{kind}:{owner_profile_id or ''}:{transition_id or ''}")[:24]
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lifecycle_events (
    lifecycle_id TEXT PRIMARY KEY,
    owner_profile_id TEXT,  -- NULL for global SHUTDOWN; FK-free on purpose (purge-safe decoupling)
    kind TEXT NOT NULL CHECK(kind IN ('PROFILE_SWITCH_OUT','PROFILE_SWITCH_IN','SHUTDOWN','EXPLICIT_SESSION_END','STARTUP_RECOVERY')),
    transition_id TEXT,     -- correlation for paired PROFILE_SWITCH_OUT/IN; NULL otherwise
    run_id TEXT NOT NULL,
    stream_sequence INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, stream_sequence),
    CHECK(
        (kind IN ('PROFILE_SWITCH_OUT','PROFILE_SWITCH_IN') AND owner_profile_id IS NOT NULL AND transition_id IS NOT NULL)
        OR (kind = 'SHUTDOWN' AND owner_profile_id IS NULL AND transition_id IS NULL)
        OR (kind IN ('EXPLICIT_SESSION_END','STARTUP_RECOVERY') AND owner_profile_id IS NOT NULL AND transition_id IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_lifecycle_owner_seq   ON lifecycle_events(owner_profile_id, stream_sequence);
CREATE INDEX IF NOT EXISTS idx_lifecycle_stream_order ON lifecycle_events(run_id, stream_sequence);
CREATE INDEX IF NOT EXISTS idx_lifecycle_kind         ON lifecycle_events(kind);
CREATE INDEX IF NOT EXISTS idx_lifecycle_transition   ON lifecycle_events(transition_id) WHERE transition_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 4. sessions — Derived, rebuildable projection (REQ-7)
-- State OPEN/CLOSED; hard boundaries from lifecycle_events, soft from policy.
-- Purge: DELETE FROM sessions WHERE profile_id=? cascades to episodes + membership.
-- Crash recovery: OPEN rows with no SHUTDOWN for run_id → CRASH_RECOVERY_CLOSED.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    profile_id TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('OPEN','CLOSED')),
    started_at TEXT NOT NULL,
    ended_at TEXT,  -- NULL while OPEN
    opened_reason TEXT NOT NULL CHECK(opened_reason IN ('STARTUP','PROFILE_SWITCH_IN','IDLE_GAP','EXPLICIT')),
    closure_reason TEXT CHECK(closure_reason IN ('PROFILE_SWITCH_OUT','SHUTDOWN','EXPLICIT_END','IDLE_GAP','MAX_DURATION','MAX_TURNS','CRASH_RECOVERY_CLOSED')),
    event_count INTEGER NOT NULL DEFAULT 0 CHECK(event_count >= 0),
    CHECK(
        (state = 'OPEN' AND ended_at IS NULL AND closure_reason IS NULL)
        OR (state = 'CLOSED' AND ended_at IS NOT NULL AND closure_reason IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_sessions_profile_state ON sessions(profile_id, state);
CREATE INDEX IF NOT EXISTS idx_sessions_run_state       ON sessions(run_id, state);
CREATE INDEX IF NOT EXISTS idx_sessions_started_at     ON sessions(started_at);

-- ---------------------------------------------------------------------------
-- 5. episodes — Deterministic, pure-function projection within a session (REQ-8)
-- episode_id = sha256(f"{session_id}:{start_event_id}:{policy_id}:{policy_version}")[:24]
-- FK to sessions ON DELETE CASCADE; retention and purge cascade transitively.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS episodes (
    episode_id TEXT PRIMARY KEY,
    profile_id TEXT NOT NULL,
    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    state TEXT NOT NULL CHECK(state IN ('OPEN','CLOSED')),
    formation_policy_id TEXT NOT NULL,          -- e.g. 'deterministic-temporal'
    formation_policy_version TEXT NOT NULL,     -- e.g. 'v1'
    started_at TEXT NOT NULL,
    ended_at TEXT,  -- NULL while OPEN
    opened_reason TEXT NOT NULL CHECK(opened_reason IN ('SESSION_START','IDLE_GAP_EPISODE','MAX_EPISODE_TURNS','MAX_EPISODE_DURATION')),
    closure_reason TEXT CHECK(closure_reason IN ('IDLE_GAP_EPISODE','MAX_EPISODE_TURNS','MAX_EPISODE_DURATION','SESSION_CLOSED','CRASH_RECOVERY_CLOSED')),
    event_count INTEGER NOT NULL DEFAULT 0 CHECK(event_count >= 0),
    CHECK(
        (state = 'OPEN' AND ended_at IS NULL AND closure_reason IS NULL)
        OR (state = 'CLOSED' AND ended_at IS NOT NULL AND closure_reason IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_episodes_session        ON episodes(session_id);
CREATE INDEX IF NOT EXISTS idx_episodes_profile_state  ON episodes(profile_id, state);
CREATE INDEX IF NOT EXISTS idx_episodes_policy         ON episodes(formation_policy_id, formation_policy_version);

-- ---------------------------------------------------------------------------
-- 6. episode_membership — Ordered event→episode mapping (REQ-8)
-- Dual FK cascades: episode delete → membership deleted; evidence delete → membership deleted.
-- Guarantees no derived leakage when evidence is purged for privacy/retention.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS episode_membership (
    episode_id TEXT NOT NULL REFERENCES episodes(episode_id) ON DELETE CASCADE,
    event_id TEXT NOT NULL REFERENCES evidence_journal(event_id) ON DELETE CASCADE,
    sequence_index INTEGER NOT NULL CHECK(sequence_index >= 0),
    PRIMARY KEY (episode_id, event_id)
);
CREATE INDEX IF NOT EXISTS idx_membership_event   ON episode_membership(event_id);
CREATE INDEX IF NOT EXISTS idx_membership_episode_seq ON episode_membership(episode_id, sequence_index);

-- ---------------------------------------------------------------------------
-- 7. control_failures — Non-silent admission log for REQ-9
-- Data turns may be dropped fail-open; control drops must be recorded.
-- Each failure flags the run as degraded (shadow_diagnostics degraded=true)
-- and disables further capture for the session.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS control_failures (
    failure_id TEXT PRIMARY KEY,  -- uuid hex
    run_id TEXT NOT NULL,
    stream_sequence INTEGER NOT NULL,  -- allocated before admission; gaps remain replay-visible
    kind TEXT NOT NULL CHECK(kind IN ('TURN_DROP','PROFILE_SWITCH_DROP','SHUTDOWN_DROP')),
    reason TEXT NOT NULL,         -- e.g. 'queue_full', 'worker_deadlock'
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(run_id, stream_sequence)
);
CREATE INDEX IF NOT EXISTS idx_control_failures_run ON control_failures(run_id, occurred_at);

-- ---------------------------------------------------------------------------
-- 8. shadow_diagnostics — Global KV metadata (REQ-1, retention watermark)
-- Run-scoped degraded/counter state belongs to shadow_runs, not this table.
-- Keys here are global metadata such as last_retention_purge_at and last_run_id.
-- Raw conversation text is NEVER stored here (counts/reasons only).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shadow_diagnostics (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- 9. retention_state — Durable watermark for retention job (REQ-5.3)
-- Separate from shadow_diagnostics to keep retention state isolated and queryable.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS retention_state (
    profile_id TEXT PRIMARY KEY,
    last_purge_at TEXT NOT NULL,
    last_purge_deleted_evidence INTEGER NOT NULL DEFAULT 0,
    last_purge_reason TEXT NOT NULL CHECK(last_purge_reason IN ('TTL','ROW_CAP','SIZE_CAP','MANUAL','INIT'))
);

-- ---------------------------------------------------------------------------
-- ATOMIC PURGE CONTRACT (documented, enforced at application layer):
-- BEGIN IMMEDIATE;
--   DELETE FROM lifecycle_events WHERE owner_profile_id = ?;
--   DELETE FROM sessions        WHERE profile_id = ?;  -- cascades episodes + membership
--   DELETE FROM evidence_journal WHERE profile_id = ?;  -- cascades orphan membership
--   DELETE FROM retention_state  WHERE profile_id = ?;
-- COMMIT;
-- forget_all: BEGIN IMMEDIATE; DELETE FROM episode_membership; DELETE FROM episodes; DELETE FROM sessions;
--            DELETE FROM lifecycle_events; DELETE FROM evidence_journal; DELETE FROM control_failures;
--            DELETE FROM shadow_runs; DELETE FROM shadow_diagnostics; DELETE FROM retention_state; COMMIT;
-- ---------------------------------------------------------------------------
