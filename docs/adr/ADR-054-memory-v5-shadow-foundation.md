# ADR-054 - Memory v5 Shadow Formation Foundation: Evidence Journals, Session Projections, and Deterministic Episode Segmentation

**Date:** 2026-09-02  
**Status:** Accepted - Shadow Formation Architecture & Zero-Retrieval Isolation  
**Decision Scope:** Memory v5 Formation Layer Architecture, Public Façade, Stream Linearization, Session/Episode Projections, and Replay Parity.

## 1. Decision Summary

We are establishing the foundational architecture for the Memory v5 Shadow Formation layer in OpenCohost. This decision enforces a strict 4-layer separation of memory concerns:
* **Layer 1: Continuity** (in-memory history for active turns under `_history_lock`).
* **Layer 2: Production Persistence v4** (`memorias.db` — lexical authority).
* **Layer 3: Formation Shadow v5** (`memory_v5_shadow.db` — journals, sessions, episodes).
* **Layer 4: Retrieval v5 / Semantic Embeddings** (Strictly OUT OF SCOPE for this foundation phase).

The Shadow Formation layer presents a minimal public façade (`record_turn`, `on_profile_switch`, `shutdown`) and deliberately exposes **zero `retrieve()` API**. Its sole purpose is to safely observe, record, and linearly project the raw continuous interaction stream into higher-order structural units (Sessions and Episodes) without polluting the active production pipeline or prematurely introducing semantic retrieval.

## 2. Context & Problem Statement

Historically, OpenCohost has operated on single-turn conversational units (Memory v4). While sufficient for immediate continuity and simple state management, v4's architecture is fundamentally insufficient for long-term multi-topic streams. As sessions grow, the absence of robust structural segmentation limits the system's ability to contextualize, consolidate, and meaningfully reflect on past interactions.

Introducing semantic retrieval and prompt injection mechanisms prematurely—before establishing a rock-solid, segmented representation of memory—carries significant risks (as demonstrated by the evaluation in ADR-053 and subsequent safety refinement benchmarks). Without rigorous boundaries, context leaks, temporal distortion, false memory injection, and hallucinated associations become critical failure modes.

Building on the historical context set by ADR-028, ADR-034, and ADR-053, we recognize the necessity of a dedicated layer responsible purely for the observation and segmentation of memory streams. This layer must operate as a "shadow," collecting evidence and forming structures (episodes/sessions) completely isolated from the live conversational loop until the structural models are proven reliable.

## 3. Core Architecture Decisions

### Authority vs Derived Projections
The architecture distinguishes strictly between immutable ground truth and derived structures:
- The **Evidence Journal** (`evidence_journal`) and **Lifecycle Events** (`lifecycle_events`) constitute the sole authoritative truth.
- **Sessions** (`sessions`) and **Episodes** (`episodes`, `episode_membership`) are derived projections, designed to be 100% rebuildable and deterministic based on the underlying evidence journals.

### Synchronous Minimal Seam & Lock Discipline
Integration with the live system imposes near-zero overhead:
- The `_history_lock` capture executes synchronously in memory in `< 15µs`.
- Processing is immediately deferred to a non-blocking background worker queue (`MemoryRuntime`).
- This design ensures **fail-open** behavior for the core product (conversational continuity is never disrupted) and **fail-closed** behavior for shadow metrics.

### Monotonic Linearization & Purge Cutoff Fencing (JD-WU1-004)
To guarantee data integrity and correct temporal ordering under arbitrary thread preemption:
- We enforce a strict `(run_id, stream_sequence)` total ordering across turns and lifecycle transitions.
- The intake pipeline utilizes an atomic exchange mechanism (`record_turn_exchange`).
- Pre-purge cutoff fencing captures the sequence barrier at dispatch time, ensuring no pre-purge turn evidence leaks across destructive operations such as profile deletion or `forget_all` even under arbitrary thread scheduling delays.

### Session Formation Pure Reducer
Session boundaries are computed via a single, deterministic pure reducer function (`SessionFormationReducer`), shared 100% across the background worker, historical replay engine, and CLI inspection tools. The reducer enforces:
* **Exact Hard Boundaries:** `STARTUP`, `PROFILE_SWITCH_OUT/IN`, `SHUTDOWN`, `CRASH_RECOVERY_CLOSED`.
* **Soft Boundaries:** `IDLE_GAP` ($\ge 30\,\text{min}$), `MAX_DURATION` ($\ge 8\,\text{h}$), `MAX_TURNS` ($\ge 500$ events).

### Episode Segmentation Engine
Within the scope of a defined session, the Episode Segmentation Engine (`EpisodeSegmentationEngine`) groups evidence chronologically:
- Deterministic ID calculation: $\text{sha256}\left(\text{session\_id} + \text{":"} + \text{start\_event\_id} + \text{":"} + \text{policy\_id} + \text{":"} + \text{policy\_version}\right)[:24]$.
- Pure temporal boundaries (Policy v1):
  - `IDLE_GAP_EPISODE` $\ge 10\,\text{min}$ ($600\,\text{s}$)
  - `MAX_EPISODE_TURNS` $\ge 30$ events
  - `MAX_EPISODE_DURATION` $\ge 45\,\text{min}$ ($2700\,\text{s}$)
  - `SESSION_CLOSED`
- Crucially, this phase utilizes **zero LLM summarization** and **zero semantic models**, relying exclusively on deterministic temporal and mechanical boundaries.

### Automatic Runtime Formation vs Read-Only Operator Inspection
The background worker automatically projects sessions and episodes upon session closure or graceful shutdown. To ensure integrity and correctness, operators can run:
```powershell
python tools/memory_v5_shadow/inspect_runtime.py --db <db_path> --verify-rebuild
```
which performs a 100% read-only in-memory replay of the evidence journal. Parity is verified via canonical SHA-256 hashing of the projected structures against the persisted state.

### Crash Recovery & Self-Healing
If the OpenCohost process terminates unexpectedly between an evidence journal commit and a session upsert, the system self-heals:
1. Startup crash recovery transitions orphan sessions to `CLOSED` (`CRASH_RECOVERY_CLOSED`) and emits `STARTUP_RECOVERY` events.
2. Startup journal replay (`reconcile_sessions_from_journals()`) reconstructs and reconciles all session projections from the authoritative journals before accepting new live turns.

### Truthful Metrics vs Human Consolidation Gate
We explicitly reject the use of synthetic mathematical heuristics (e.g. `dup_rate * 1.5`) to simulate or measure consolidation pressure. The architecture defers entirely to human evaluation. Progression to subsequent consolidation phases requires a formal human review over a targeted corpus of 50–100 real-world episodes.

## 4. Schema & DDL Specification

The Shadow Formation database (`memory_v5_shadow.db`) strictly adheres to DDL v1 schema conventions (`schema_v1.sql`). The primary tables include:

* **`shadow_runs`**: Tracks application execution instances for sequence linearization.
* **`evidence_journal`**: Immutable append-only log of raw conversational turns.
* **`lifecycle_events`**: Immutable log of system state changes (startup, shutdown, profile switches).
* **`sessions`**: Derived projections of continuous interaction blocks.
* **`episodes`**: Derived sub-session granular projections based on temporal segmentation.
* **`episode_membership`**: Mapping table linking `evidence_journal` entries to specific `episodes`.
* **`retention_state`**: Tracks status of purges, ensuring cutoff fences are respected.
* **`shadow_diagnostics`**: Records internal performance metrics and timing data.
* **`control_failures`**: Logs any non-fatal failures within the shadow pipeline for offline analysis.

## 5. Testing, Verification & Certification

The robustness of the v5 Shadow Formation foundation has been rigorously validated:
* **46/46 unit/integration tests** passing in `tests/test_memory_v5_shadow/`.
* **64/64 pipeline memory regression tests** ensuring zero degradation to existing v4 continuity.
* **Blind Dual-Judge Judgment Day Certification** successfully achieved across Work Units 1, 2, and 3 (WU1, WU2, WU3), confirming adherence to architectural constraints, lock discipline, and deterministic behavior.

## 6. Consequences & Future Trajectory

**Positive Consequences:**
* Provides completely transparent shadow capture in daily OpenCohost usage.
* Introduces zero production risk to the active conversational layer.
* Establishes verifiable, deterministic replay capabilities essential for rigorous offline evaluation.

**Future Trajectory:**
1. Operate this shadow architecture during daily OpenCohost usage to collect 50–100 natural episodes.
2. Build a local human review tool (`review_episodes.py`) to evaluate segmentation coherence, over-segmentation, under-segmentation, and boundary precision.
3. Once human validation is completed, proceed to **Structured Consolidation** (Subjects/Claims/Provenance) and downstream multilingual MiniLM shadow indexing (Layer 4).
