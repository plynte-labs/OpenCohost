# WU0 Contract Inventory — Memory v5 Shadow Formation Foundation

**Change**: `memory-v5-shadow-formation-foundation`
**Authorization**: Engram #6327 `WU0_ACQUISITION_AUTHORIZED`
**Baseline**: HEAD `a1b0af5` — `KEEP_LEXICAL` (78/78 GREEN, semantic safety refinement closed)
**Mode**: `openspec` — filesystem `tasks.md` authoritative; Engram progress mirrored if available
**Scope**: WU0 only (Tasks 0.1–0.4). Zero `opencohost/` edits. Produced via CodeGraph-first inspection.

---

## 1. Source Seam Inventory

### 1.1 Commit Seam & Lock Discipline

| Fact | Source Evidence | WU1 Implication |
|------|-----------------|-----------------|
| `MemoriaCaptureMixin._commit_history` is the sole history commit seam | `opencohost/core/engine/llm_engine_memorias.py:217-357` | WU1 snapshot must hook **after** `self._history_lock` release (line 248-347 holds lock; 348-357 is post-lock I/O). |
| `_history_lock` guards eviction capture + dual `historial.append` atomically, preventing interleaving between worker loop and agenda speaker daemon | `llm_engine_memorias.py:257` `with self._history_lock:` + comment `Hold _history_lock around the eviction-capture + both appends` | WU1 must copy snapshot fields **inside** lock, zero hashing/JSON/SQLite/queue inside lock. |
| Under lock, v4 performs deque work **and CPU-heavy draft derivation** (`significant_token_count`, `derive_stable_key`, title/signature building); SQLite starts only after release | `llm_engine_memorias.py:40-123,248-357` | WU1 may only add bounded field copies and sequence allocation under this already-busy lock; hashing, JSON, queueing, and SQLite remain post-lock. |
| `historial` is `deque(maxlen=HISTORY_MAX_TURNS*2)` with `HISTORY_MAX_TURNS=3` (6 messages) | `opencohost/core/llm_engine.py:747` `deque(maxlen=HISTORY_MAX_TURNS * 2)` + `opencohost/config/settings.py:78` `HISTORY_MAX_TURNS = 3` | Rolling window is 3 turns, not 10 — WU1 must not assume larger window. |
| Privacy tag is snapshotted once per commit: `priv = self._memorias_private` then tagged onto both entries | `llm_engine_memorias.py:324-333` | Snapshot must capture `is_private=priv` atomically to avoid split-pair bug (B-S1). |
| Profile id is read inside lock: `profile_id = self._current_profile_id` in `_build_memoria_draft` | `llm_engine_memorias.py:73` | Snapshot must copy `profile_id` under lock (state-at-event), never current state at eviction. |

### 1.2 Stream Coordination — Does NOT Exist (Gap)

| Fact | Source Evidence | WU1 Fix |
|------|-----------------|---------|
| No `committed_turn_id`, `run_id`, timestamp, or `stream_sequence` exists in history | `llm_engine_memorias.py:325-333`: each exchange appends two dicts `{role, content, source, private[, mem_captured]}` | WU1 introduces a process `run_id` and one shared monotonic allocator. The user and assistant entries receive consecutive sequences while the same `_history_lock` is held. |
| No deterministic `event_id` derivation exists | `memoria_store.derive_stable_key` uses profile+sorted-tokens, not `sha256(committed_turn_id)` | WU1 introduces `event_id = sha256(committed_turn_id.encode("utf-8")).hexdigest()[:24]` in worker, never under lock. |
| Profile switch is already atomic with history clear, but has no lifecycle marker | `llm_engine.py:1381-1405` | Allocate consecutive `PROFILE_SWITCH_OUT`/`PROFILE_SWITCH_IN` sequences in this critical section; persist after release. Startup profile uses the same command path (`engine_host.py:776-809`). |

### 1.3 Source Eligibility Allowlist

**Actual allowlist** (fail-closed, verified):

```python
# opencohost/core/llm_engine.py:195
_DIGEST_CAPTURE_SOURCES = frozenset({"direct", "ptt", OWNER_BUNDLE_SOURCE})
# where OWNER_BUNDLE_SOURCE = "owner-bundle" (opencohost/config/settings.py:218)
```

- **Eligible**: `"direct"` (typed owner turn), `"ptt"` (push-to-talk voice), `"owner-bundle"` (bundled burst of owner questions) — see `llm_engine_memorias.py:65` `source not in _eng._DIGEST_CAPTURE_SOURCES: return None`.
- **Explicitly ineligible** (gated to `None` via same check or sentinel logic):
  - `"chat"` — viewer-derived turns can reach `_commit_history`, but `_build_memoria_draft` rejects them because they are absent from the allowlist (`llm_engine_memorias.py:65-66`)
  - `"accumulated"` — verbatim viewer chat bundle, comment at `llm_engine.py:188-194` forbids adding it
  - `"kira-agenda"` / `"kira-agenda-stop"` — masked to `"[agenda segura: prompt interno omitido]"` at `llm_engine_memorias.py:238-239` and rejected by `user_content.startswith("[agenda segura")`
  - Missing/unknown `source` — fail-closed (`not in` allowlist) + `evicted_source not in _DIGEST_CAPTURE_SOURCES` at `llm_engine_memorias.py:276`
- **WU1 responsibility**: Enumerate exactly these three eligible sources; forbid synthetic agenda, viewer, or internal system events from autobiographical journal (spec TASK 0.1 last bullet). Shadow `source` column stores the raw eligible source; no viewer chat ever enters `evidence_journal`.

---

## 2. Closing the 10 UNKNOWNs

Each item maps to spec `REQ-5 §2.5 cl.7`; evidence is file:line, not assumption.

| # | UNKNOWN | Verdict (FROZEN) | Source Evidence | WU1 Contract |
|---|---------|------------------|-----------------|--------------|
| 1 | Committed-turn stable identity & total stream sequence (`run_id`, `stream_sequence`) | **Gap closed by minimal seam**. No identity or timestamp exists in v4. One `_commit_history` call atomically appends a user message and an assistant message. | `llm_engine_memorias.py:325-333`; profile switch lock at `llm_engine.py:1389-1400` | Generate UUIDv4 `run_id` once per engine process. A single counter under `_history_lock` allocates **two consecutive values per eligible exchange**, one per persisted role, and two consecutive values for switch OUT/IN. `committed_turn_id=f"{run_id}:{stream_sequence}"`; `event_id=sha256(committed_turn_id)[:24]` post-lock. Capture one UTC `occurred_at` immediately before lock acquisition and copy it to both exchange records; `(run_id, stream_sequence)`, not wall clock, is authoritative order. |
| 2 | Privacy state transition handling for pre-existing evidence (policy mutability vs content immutability) | **FROZEN: capture pause is a prohibition; retained policy is mutable** | `set_memorias_private` is explicitly forward-only (`llm_engine_memorias.py:624-630`); `_build_memoria_draft` rejects any `private is not False` (`:65-75`); API calls it directly (`api/routers/memoria.py:546-554`) | WU1 MUST NOT persist turns committed while `_memorias_private=True`; storing them with `is_private=1` would regress the existing privacy promise. For evidence already retained, content/provenance remain immutable; only `is_private` and `retention_disposition` may change. No transition retro-captures omitted turns. This is a required reconciliation against spec REQ-5.2 before WU1 authorization. |
| 3 | Concrete retention policy (duration, size bounds) | **FROZEN** (see §4.1) | v4 caps: `MEMORIAS_PROFILE_CAP=200` (`settings.py:517`), `MEMORIAS_SUMMARY_CAP=10` (`:534`); no TTL exists (`memoria_store.py:1763` prune is count-only) | Shadow retention: **30-day time TTL** + **per-profile cap 10 000 evidence rows** + **DB size cap 256 MB** (whichever triggers first). Nightly `retention_purge` deletes `evidence_journal` where `created_at < now-30d` and cascades via FK; logs counts only. Configurable via `MEMORY_V5_RETENTION_DAYS`/`MEMORY_V5_RETENTION_MAX_ROWS`. |
| 4 | Backup semantics (explicit exclusion of `memory_v5_shadow.db` from standard archives) | **FROZEN: excluded** | `storage.py:30-44` resolves user data; `.gitignore:39` already ignores the entire `data/` tree; no product backup/archive implementation was found | Place the DB at `USER_DATA_DIR/data/memory_v5_shadow/memory_v5_shadow.db`. It is outside every current archive because no such archive exists; any future backup allowlist must explicitly exclude this path. No `.gitignore` edit is needed because `data/` already covers it. |
| 5 | Export semantics | **FROZEN: no export in this track** | No bulk export API exists for memorias (only per-row `GET /api/memoria/list` + per-row `GET /api/memoria/row/{id}`); `tools/memory_v5*` are offline-only | Shadow mode offers **no export endpoint**; `ShadowStore` has no `export()` method. Future export (if approved) is opt-in, profile-scoped, requires explicit consent, and redacts `content` by default. Out of scope for `memory-v5-shadow-formation-foundation`. |
| 6 | Purge-safe hard-boundary provenance (profile-owned transition pairs without cross-profile purge corruption) | **FROZEN: decoupled pair design** | No `lifecycle_events` table exists in v4; v4 `purge_profile` is single-table `DELETE FROM memorias WHERE profile_id=?` (`memoria_store.py:1467-1477`) | Profile switch emits **two rows sharing `transition_id`**: `PROFILE_SWITCH_OUT` owned by `owner_profile_id=A` and `PROFILE_SWITCH_IN` owned by `owner_profile_id=B`. `purge_profile(B)` deletes only `WHERE owner_profile_id=B`, preserving `A`'s `OUT` marker. `lifecycle_id = sha256(f"{run_id}:{stream_sequence}:{kind}:{owner_profile_id or ''}:{transition_id or ''}")[:24]`. |
| 7 | Control command admission failure contract (product fail-open, scientific degradation, non-silent failure) | **FROZEN** (see §4.2) | v4 `command_queue` is unbounded `queue.Queue()` (`llm_engine.py:547`); v4 has no shadow queue; no `control_failures` telemetry exists | `TurnCommand` droppable fail-open (`put_nowait` + `DroppedEvidenceCounter`); `ProfileSwitchCommand`/`ShutdownCommand` have reserved capacity (queue `maxsize=1000`, control slots 10% reserved or separate control queue); if control admission fails: (a) conversation continues fail-open, (b) increment `control_failures` in `shadow_diagnostics`, (c) flag run `degraded=true`, (d) disable further capture for session to prevent cross-profile contamination (§REQ-9). |
| 8 | Purge execution: atomic purge transaction + FK cascade strategy for dependent derived rows | **FROZEN** | v4 purge is one SQL delete (`memoria_store.py:1467-1477`); `/api/perfiles/{name}` deletion only rewrites profile JSON and does **not** purge memory (`api/routers/perfiles.py:157-173`); no `forget_all` exists | Shadow `purge_profile` uses `BEGIN IMMEDIATE`, deletes owned lifecycle rows, sessions (cascading episodes/membership), evidence (cascading membership), and retention state, then commits. `forget_all` deletes all nine shadow tables in one transaction. A future profile-delete integration must purge shadow by stable UUID **before** removing profile metadata; purge failure preserves the profile for retry rather than falsely reporting deletion. |
| 9 | Crash consistency boundaries (deriving CLEAN vs UNCLEAN from durable shutdown markers) | **FROZEN** | v4 clean shutdown flushes then enqueues the engine stop sentinel (`engine_host.py:1181-1194`) but persists no shutdown marker | Shadow shutdown must be inserted after prior commands drain and committed before worker close. A run with that durable marker is CLEAN; a `shadow_runs.run_id` without it is UNCLEAN. Startup closes that run's `sessions(state='OPEN')` using its last durable evidence timestamp (or run start if none), marks `CRASH_RECOVERY_CLOSED`, and writes profile-owned `STARTUP_RECOVERY`. RAM-queued events are never treated as durable. |
| 10 | Exact turn source eligibility allowlist from actual `llm_engine_memorias.py` seam | **FROZEN** (see §1.3) | `llm_engine.py:195` `_DIGEST_CAPTURE_SOURCES`, `llm_engine_memorias.py:65,276` gates | Allowlist frozen as `{"direct","ptt","owner-bundle"}`; all other sources (`chat`, `accumulated`, `kira-agenda*`, missing/unknown) are **rejected fail-closed** and never enter `evidence_journal`. WU1 must assert this allowlist in seam tests. |

Status: **10/10 UNKNOWN → RESOLVED, 0 UNKNOWN remaining.**

---

## 3. DDL v1 Freeze

**Artifact**: `docs/memory_v5/memory_v5_shadow_ddl_v1.sql` (executable, `PRAGMA user_version=1`)

Covers **all frozen contracts**: evidence + lifecycle + sessions + episodes + membership + diagnostics/control failure state + retention metadata + crash consistency + deterministic ordering + FK cascades + CHECK constraints.

Key deltas vs spec draft (explicit, not silent copy):

| Draft Gap | Resolution in v1 |
|-----------|------------------|
| No run-scoped degradation/crash metadata | Added `shadow_runs`; CLEAN/UNCLEAN is still derived from durable lifecycle markers |
| No diagnostics/control failure table for REQ-1/REQ-9 | Added global `shadow_diagnostics` + append-only `control_failures` |
| No retention metadata table | Added `retention_state` (last purge watermark); retention enforcement is app-level but watermark is durable |
| `lifecycle_events.owner_profile_id` nullable handling for `SHUTDOWN` | Explicit `NULL` for global shutdown plus kind/owner/transition CHECK constraints |
| Ambiguous purge transaction boundaries | Documented `BEGIN IMMEDIATE` + four-statement atomic purge in DDL header |
| Missing `content_hash` index for integrity checks | Added `idx_evidence_content_hash` |
| Missing deterministic ordering support | Unique coordinates inside each journal plus one application allocator shared by both journals; SQLite cannot enforce cross-table uniqueness |

Full DDL is in `memory_v5_shadow_ddl_v1.sql`. Key properties:

- `PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL; PRAGMA foreign_keys=ON; PRAGMA busy_timeout=5000;`
- `evidence_journal.event_id PK, committed_turn_id UNIQUE, (run_id, stream_sequence) indexed, CHECK(role IN ('user','assistant'))`
- `lifecycle_events` with `owner_profile_id`, `transition_id`, closed `kind` enum, purge-safe via `owner_profile_id`
- `sessions` carries `run_id`, enforces coherent OPEN/CLOSED fields, and indexes run/profile state
- `episodes` `REFERENCES sessions(session_id) ON DELETE CASCADE`, `state OPEN/CLOSED`, `formation_policy_id/version`
- `episode_membership` `PRIMARY KEY(episode_id, event_id)` with dual FK cascades
- `control_failures` + `shadow_diagnostics` for REQ-9 non-silent failure

---

## 4. Retention / Backup / Export Policy Freeze

### 4.1 Retention

- **Time TTL**: 30 days (frozen default). WU1 may expose `MEMORY_V5_RETENTION_DAYS`; purge runs at startup when the durable watermark is older than 24 hours and then every 24 hours while running.
- **Per-profile row cap**: 10 000 evidence rows; oldest rows evicted FIFO when cap exceeded (after nightly TTL pass).
- **DB size cap**: 256 MB across the main DB plus `-wal`; when exceeded, the worker deletes oldest evidence regardless of age and checkpoints WAL. This is worker-only and never blocks turn commit.
- **Diagnostics retention**: `control_failures` retained 90 days independently.
- **No derived leakage**: each retention transaction deletes evidence (membership cascades), removes empty episodes, recomputes affected session/episode counts, and then commits.

### 4.2 Backup & Export

- **Backup**: `memory_v5_shadow.db` is **explicitly excluded** from any backup/archive. No backup implementation currently exists; the existing `data/` ignore already covers the shadow subtree.
- **Export**: No export surface in this track. No `ShadowStore.export()`, no `/api/memory_v5/*` export route. Any future export requires explicit operator consent + PII redaction review, and is a separate SDD change.

---

## 5. Measure-First Baseline Benchmark

### 5.1 Method (no production edits)

The benchmark invokes the **real** inherited `MemoriaCaptureMixin._commit_history` method. A minimal harness supplies its locks/deques and a real `MemoriaStore` backed by a temporary database outside the repository. It warms 25 commits, then measures 1,000 steady-state eligible `source="direct"` commits; this exercises the lock section, draft derivation, post-lock SQLite conflict update, and title bookkeeping without touching user data.

```bash
uv run python docs/memory_v5/wu0_benchmark_v4_commit.py
```

The reproducible harness is versioned beside this report. It writes only to an OS temporary directory; no `opencohost/` or user database is written.

### 5.2 Environment

| Field | Value |
|-------|-------|
| OS | Windows-11 10.0.26200 SP0 (AMD64 Family 25 Model 80, AuthenticAMD) |
| Python | 3.12.11 (`uv run python`) |
| `HISTORY_MAX_TURNS` | 3 (6 deque slots) |
| Sample count | 1 000 measured commits after 25 warmups |
| Wall clock | `time.perf_counter()` → microseconds |
| Persistence | Real v4 `MemoriaStore` temporary DB, steady-state conflict-update path |

### 5.3 Results (focused seam — NOT end-to-end turn latency)

| Seam | N | p50 | p95 | p99 | max | mean |
|------|---|-----|-----|-----|-----|------|
| Real v4 `_commit_history` + real temporary `MemoriaStore` | 1000 | **4,920.2 µs** | **6,129.0 µs** | **7,462.0 µs** | **60,549.9 µs** | **5,195.6 µs** |

**Notes/limitations**:

- Focused commit seam only: it excludes LLM generation, TTS, and dispatch. It is not end-to-end turn latency.
- The repeated text intentionally measures the steady-state v4 conflict-update path. Fresh inserts and prune-at-cap behavior can differ and remain WU1 parity-test concerns.
- The maximum is an OS/filesystem scheduling outlier; p95/p99 are the useful baseline. Raw content was synthetic and never persisted outside the temporary directory.

### 5.4 Conservative WU1 Headroom Targets (derived from baseline, not claimed latency)

WU1 must stay well below the noise floor of the turn commit seam:

| Budget | Target (p95, focused seam) | Rationale |
|--------|----------------------------|-----------|
| In-lock allocation of two identities + field copies | **≤ 20 µs p95 incremental** | Keeps added lock work below 0.33% of the measured 6.13 ms v4 p95; hashing/JSON remain forbidden. |
| Post-lock non-blocking enqueue | **≤ 30 µs p95 incremental** | Combined hot-path target stays ≤50 µs, below 0.82% of baseline p95. |
| Combined WU1 hot-path overhead | **≤ 50 µs p95 incremental** | Must be measured as a paired OFF-vs-SHADOW delta in WU1, not inferred from this v4-only run. |
| Background SQLite write | **No synchronous commit budget** | It is off the conversational path; acceptance is queue stability, zero control loss, and bounded data-drop rate. |
| Queue saturation drops (data turns) | **≤ 1%** under sustained 10 turns/s burst | Fail-open counter `dropped_evidence_total` must increment; control drops must be 0. |

Fail-open invariant: exceeding any budget never blocks turn commit — data turns drop, control failures are logged to `control_failures`, run marked degraded.

---

## 6. Deviations & Explicit Decisions

- **No production edits**: as required, `opencohost/` diff is zero (verified via `git diff --name-only -- opencohost`).
- **DDL additions beyond spec draft**: `shadow_runs`, `shadow_diagnostics`, `control_failures`, and `retention_state` are explicit additions listed in §3.
- **Source allowlist narrowed to three**: spec tasks listed placeholder `"chat-ptt"/"chat-twitch"/"user-direct"` but actual code is `direct/ptt/owner-bundle`; frozen to actual code to avoid spec drift (§1.3).
- **Privacy reconciliation is explicit**: the sealed spec's `is_private=True` persistence cannot be implemented using the existing capture-pause switch; maintainer approval must resolve this before WU1.
- **Retention numbers are frozen defaults, not tuning**: 30 days / 10 k rows / 256 MB are conservative local-only defaults proposed for maintainer approval.

---

## 7. Work Unit Evidence

| Evidence | Value |
|----------|-------|
| Focused test command and exact result | `uv run python docs/memory_v5/wu0_benchmark_v4_commit.py` — **PASS**, N=1,000, p50=4,920.2 µs, p95=6,129.0 µs, p99=7,462.0 µs, max=60,549.9 µs. |
| Runtime harness command/scenario and exact result | **N/A — justified**: WU0 is doc/spec/bench only (`opencohost/` forbidden). No runtime boundary exists until WU1 `MemoryRuntime` is implemented. The next harness is WU1's `OFF` vs `SHADOW` mode lifecycle test. |
| Rollback boundary | Delete the report, DDL, and benchmark harness under `docs/memory_v5/`, then revert `openspec/changes/memory-v5-shadow-formation-foundation/tasks.md` WU0 checkboxes → repo returns to `a1b0af5`, zero runtime effect. |
| Opencohost mutations | **Zero** — `git diff --name-only -- opencohost` → `(no output)` (see §8). |

---

## 8. Verification Commands

```
git diff --name-only -- opencohost  # → (empty) — zero production changes
git status --porcelain              # → only the three untracked WU0 artifacts (OpenSpec artifacts are ignored here)
uv run python docs/memory_v5/wu0_benchmark_v4_commit.py
```

DDL validation:

```bash
uv run python -c "
import sqlite3, pathlib, tempfile
sql = pathlib.Path('docs/memory_v5/memory_v5_shadow_ddl_v1.sql').read_text()
tmp = tempfile.mktemp(suffix='.db')
conn = sqlite3.connect(tmp)
conn.executescript(sql)
# verify FKs, user_version, cascades, run/session linkage and state constraints
print('user_version', conn.execute('PRAGMA user_version').fetchone())
print('foreign_keys', conn.execute('PRAGMA foreign_keys').fetchone())
print('tables', [r[0] for r in conn.execute(\"SELECT name FROM sqlite_master WHERE type='table'\").fetchall()])
# verify purge FK cascades listed
for row in conn.execute('PRAGMA foreign_key_list(episodes)').fetchall(): print('ep FK', row)
for row in conn.execute('PRAGMA foreign_key_list(episode_membership)').fetchall(): print('memb FK', row)
# verify idempotency: insert same committed_turn_id twice → second is no-op
import hashlib
conn.execute(\"INSERT OR IGNORE INTO evidence_journal (event_id, committed_turn_id, profile_id, run_id, stream_sequence, role, source, occurred_at, content, content_hash, is_private, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)\", (hashlib.sha256(b'turn-1').hexdigest()[:24], 'turn-1', 'p1','run1',1,'user','direct','2026-09-01T00:00:00Z','hello', hashlib.sha256(b'hello').hexdigest(),0,'2026-09-01T00:00:00Z'))
conn.commit()
c = conn.execute('SELECT count(*) FROM evidence_journal').fetchone()[0]
conn.execute(\"INSERT OR IGNORE INTO evidence_journal (event_id, committed_turn_id, profile_id, run_id, stream_sequence, role, source, occurred_at, content, content_hash, is_private, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)\", (hashlib.sha256(b'turn-1').hexdigest()[:24], 'turn-1', 'p1','run1',1,'user','direct','2026-09-01T00:00:00Z','hello', hashlib.sha256(b'hello').hexdigest(),0,'2026-09-01T00:00:00Z'))
conn.commit()
print('idempotent count after duplicate', conn.execute('SELECT count(*) FROM evidence_journal').fetchone()[0], 'expected', c)
"
# → user_version (1,), foreign_keys (1,), tables [...], episode FK CASCADE verified, idempotent count unchanged
```

---

## 9. Remaining Work (WU1 Blocked)

WU0 is complete. **Maintainer review required before WU1 acquisition.**

| Work Unit | Status | Gate |
|-----------|--------|------|
| **WU0** Contract Inventory & Privacy Freeze (0.1–0.4) | **COMPLETE** | This doc + DDL v1 + reproducible benchmark + tasks.md checkboxes |
| **WU1** MemoryRuntime + Evidence Journal (1.1–1.4) | **BLOCKED** | Awaits maintainer approval of WU0 contracts, DDL, and headroom budgets |
| **WU2** Session Lifecycle & Lifecycle Journal | **BLOCKED** | Depends on WU1 |
| **WU3** Deterministic Episode Formation & Evaluation | **BLOCKED** | Depends on WU2 |

Do not acquire or start WU1 until this inventory is reviewed and `#6327` WU1 authorization is issued.

---

## 10. References

- `opencohost/core/engine/llm_engine_memorias.py:1-708` — sole history commit seam
- `opencohost/core/llm_engine.py:195` `_DIGEST_CAPTURE_SOURCES` + `:252` `_HISTORY_ASSISTANT_ONLY_SOURCES` + `:747` deque init + `:540-760` `MotorVocalIA.__init__` profile/lock init
- `opencohost/config/settings.py:78` `HISTORY_MAX_TURNS=3`, `:514-535` memorias caps, `:218` `OWNER_BUNDLE_SOURCE`
- `opencohost/core/memory/memoria_store.py:1-60` module contract, `:1467-1500` purge/get, `:1819-1944` schema v4
- `opencohost/config/storage.py:31-44` data root, temp, gitignore
- `docs/memory_v5/wu0_benchmark_v4_commit.py` reproducible real-seam benchmark harness
- Track specs: `openspec/changes/memory-v5-shadow-formation-foundation/specs/memory-shadow-formation-foundation/spec.md` + `design.md` + `proposal.md`
- Prior WU0 evidence baseline: `docs/memory_v5/wu0-evidence.md` (superseded by this inventory for shadow track; retained for v4 lexical context)
