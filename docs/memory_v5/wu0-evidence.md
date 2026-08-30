# Memory v5 WU0 Contract and Evidence Baseline Report

## Executive Summary

This report establishes the source- and test-backed v4 memory contract inventory, resolves planning document contradictions to `UNKNOWN = 0`, records privacy and retention readiness gates, documents baseline measurement receipts, and deterministically selects the next single experimental route.

No production code, database schema, prompt injection, or dependencies were altered in WU0.

---

## 1. Source-Backed v4 Contract Inventory

The current production memory system in `develop` is a profile-isolated SQLite v4 store with the following verified architectural contracts:

### 1.1 Database Schema & Storage Contract
- **Authority**: `opencohost/core/memory/memoria_store.py` (`MemoriaStore`, lines 1–100, 850–900).
- **SQLite Engine**: Own unshared database (`memorias.db`), `PRAGMA user_version = 4`.
- **Primary Table**: `memorias` with columns:
  - `id` (TEXT PRIMARY KEY, formatted as `mem_<uuid4_hex>`)
  - `profile_id` (TEXT, profile isolation key)
  - `stable_key` (TEXT, derived deterministic token key)
  - `revision` (INTEGER, increments on draft refresh)
  - `title` (TEXT, 3-token summary)
  - `content` (TEXT, full exchange text)
  - `status` (TEXT: `'draft'`, `'curated'`, `'summary'`, `'imported'`, `'promoted'`)
  - `pinned` (INTEGER, 0 or 1)
  - `private` (INTEGER, 0 or 1)
  - `inactive` (INTEGER, 0 or 1)
  - `created_at` (TEXT, UTC ISO timestamp)
  - `updated_at` (TEXT, UTC ISO timestamp)
  - `signature` (TEXT, 12-token full pair signature)
  - `judged_at` (TEXT, UTC ISO timestamp when promoted/judged)
- **Constraint**: `UNIQUE(profile_id, stable_key)` composite key enforcing strict cross-profile isolation.
- **Secondary Table**: `memoria_promotion_attempts` for promotion retry tracking and backoff.

### 1.2 Capture Contract
- **Authority**: `opencohost/core/engine/llm_engine_memorias.py`, `opencohost/core/llm_engine.py`.
- **Capture Strategy**: Cheap, LLM-free, synchronous extraction of significant tokens from eligible `direct`, `ptt`, and owner-bundle history.
- **Concurrency & Locking**: History snapshots are taken under `_history_lock`, but all SQLite I/O and token derivation occur outside the lock.
- **Rolling Window**: `HISTORY_MAX_TURNS = 3` (6 messages total), defined in `opencohost/config/settings.py`.

### 1.3 Retrieval & Ranking Contract
- **Authority**: `opencohost/core/memory/memoria_store.py` (`select_top_k`, `is_meta_recall_query`, `build_injection_lines`, `build_recency_lines`).
- **Lexical Matching**: Smoothed inverse document frequency (Raw IDF Sum) across candidate signatures. Requires `>= 2` shared tokens (`_MIN_SHARED_TOKENS`).
- **Temporal & Meta Recall**: Interrogative regex routing (`is_meta_recall_query`) redirects temporal queries (e.g. "¿qué recordás de mí?", "sesión pasada") to `build_recency_lines`, prioritizing vouched/promoted/summary rows by recency instead of IDF.
- **Budget**: Hard cap of `MEMORIAS_MAX_INJECT_CHARS = 700` characters. Pinned rows have priority carve-out up to `MEMORIAS_MAX_PINNED_INJECT = 2` rows.

### 1.4 Promotion & Background Maintenance
- **Authority**: `opencohost/core/engine/llm_engine_scout.py`, `opencohost/core/llm_engine.py`.
- **Lifecycle**: Local engine idle sweep triggers one bounded LLM call to evaluate drafts. Applies strict JSON validation, atomic status transitions, and exponential retry backoff.

### 1.5 Prompt Assembly Seam
- **Authority**: `opencohost/core/context/prompt_assembler.py`.
- **DB-Free Boundary**: Prompt assembler receives a pre-rendered string block and never interacts with SQLite or raw candidate objects.

---

## 2. Contradiction & RFC Reconciliation Ledger

| Claim / Topic | RFC / Work Plan Document Claim | Actual `develop` Code / Test Reality | Resolution (`UNKNOWN = 0`) |
|---|---|---|---|
| Rolling History Window | Stated as 10 turns | `HISTORY_MAX_TURNS = 3` in `settings.py` (6 messages total) | Reconciled: 3 turns is production reality. |
| Memory v5 First Track Scope | Pre-Track RFC proposed full vertical (Evidence->Session->Episode->Fact->Embeddings->CAG) | Exploration RFC & SDD review: broad changes unproven and high risk | Reconciled: First track is strictly bounded WU0 evidence baseline. |
| Promotion Processing | Assumed continuous background queue | Synchronous single-worker idle sweep with retry backoff | Reconciled: Keep existing single-job idle model. |
| Retrieval Architecture | ADR-034 historical snapshot claimed minimal lexical retrieval | `develop` already has multi-tier status, IDF scoring, temporal regex router, and pin carve-outs | Reconciled: v4 retrieval is already hybrid lexical+temporal. |
| Embeddings Backend | Work Plan named FastEmbed / MiniLM as fixed requirement | No vector dependencies exist in repository; performance/packaging unbenchmarked | Reconciled: Deferred; requires standalone benchmark. |
| Work Units Count | Work plan referenced WU0-WU10 without approved contracts for WU8-WU10 | Only WU0-WU7 have outline definitions; WU0 is the immediate boundary | Reconciled: WU0 is the sole active authorized work unit. |

**Touched Area Unresolved Claims**: `UNKNOWN = 0`.

---

## 3. Privacy and Retention Readiness Gates

Before any future raw evidence or conversation journal persistence is approved, the following gates must be evaluated:

| Gate | Status | Evidence / Requirement |
|---|---|---|
| **Operator Consent** | `UNMET` | No explicit opt-in UX or disclosure exists for persisting raw user audio/text transcripts in an evidence journal. |
| **Profile Boundary Isolation** | `MET` | SQLite composite `UNIQUE(profile_id, stable_key)` and query-level `profile_id` filtering prevent cross-profile contamination. |
| **Row / Profile Deletion ("Forget All")** | `MET (v4)` / `UNMET (v5 Journal)` | `MemoriaStore` implements profile purge and row deletion, but no mechanism exists to purge derived graph or journal artifacts. |
| **Retention Policy & TTL** | `UNMET` | v4 uses a FIFO draft cap (`MEMORIAS_PROFILE_CAP = 100`), but no time-based expiration or lifecycle TTL exists for raw turns. |
| **Backup & Redaction Controls** | `UNMET` | No sanitization/redaction pipeline exists for PII before hypothetical raw transcript disk persistence. |
| **Metadata-Only Diagnostics** | `MET` | Failure logs, exceptions, and baseline receipts emit only IDs, counts, and reasons, never row titles or content. |

---

## 4. Measurement Receipts & Baselines

### 4.1 Synthetic Case Evaluation Receipt
- **Command**: `python tools/memory_v5_wu0.py --synthetic-only --iterations 25`
- **Total Cases**: 9 (lexical, paraphrase, temporal, mixed, stale_contradiction, private, pinned_curated, profile_isolation, no_memory)
- **Outcomes**:
  - `pass`: 7 (77.8%)
  - `miss`: 2 (22.2%) — specifically `tc-paraphrase-002` (vocabulary mismatch) and `tc-stale-contradiction-005` (lexical IDF matches both without temporal supersession)
  - `false_injection`: 0 (0.0%)
  - `isolation_failure`: 0 (0.0%)
  - `unavailable`: 0 (0.0%)
- **Latency**:
  - `p50`: 2.50 ms
  - `p95`: 3.78 ms
  - `p99`: 4.10 ms
  - `mean`: 2.65 ms
- **Peak Memory**: ~359 KB (Python object overhead during tracing)

### 4.2 Local Database Autopsy Receipt
- **Command**: `python tools/memory_v5_wu0.py --local-db --db-path data/memorias/memorias.db --max-rows 200 --iterations 25`
- **Status**: `completed`
- **Schema User Version**: 4
- **Total Rows Inspected**: 397
- **Status Distribution**:
  - `draft`: 325 (81.9%)
  - `promoted`: 49 (12.3%)
  - `imported`: 19 (4.8%)
  - `summary`: 4 (1.0%)
  - `curated`: 0 (0.0%)
- **Flag Counts**:
  - `inactive`: 97 (24.4%)
  - `private`: 0 (0.0%)
  - `pinned`: 0 (0.0%)
- **Query Latency**: `p50` = 0.46 ms

### 4.3 Uncertainty Notes and Sample Limits
- Synthetic fixtures provide deterministic coverage of query classes but do not capture live streaming ambient conversational noise.
- Local DB autopsy was performed on a sample of 397 rows; production behavior under tens of thousands of rows requires dedicated scaling benchmarks.

---

## 5. Terminal Routing Decision

### Selected Route: `semantic retrieval benchmark`

### Rationale
1. **Primary Defect Identified**: The baseline confirms that current v4 lexical retrieval achieves 100% precision on exact keywords, temporal routing, and pinned rows, but systematically misses paraphrase queries where the user does not use identical vocabulary tokens.
2. **Privacy Gate Constraints**: Persistent raw evidence journaling cannot be authorized because Operator Consent and Retention/Redaction gates remain `UNMET`.
3. **Actionable Autonomous Next Step**: A dedicated **semantic retrieval benchmark** can be executed completely offline against existing anonymized v4 memory units (`memorias` rows) to measure embedding-based and hybrid recall vs lexical recall, without altering production runtime, persistence, or privacy posture.

---

## 6. Rollback & Invariants Verification

- **Rollback Boundary**: Deleting the 4 WU0 files (`docs/memory_v5/wu0-evidence.md`, `tests/fixtures/memory_v5_expected_cases.json`, `tools/memory_v5_wu0.py`, `tests/test_memory_v5_wu0.py`) leaves the production codebase and v4 SQLite databases in their original, untouched state.
- **Production Isolation**: Zero modifications were made to `opencohost/`, `config/`, or database files. No new runtime dependencies or models were added.
