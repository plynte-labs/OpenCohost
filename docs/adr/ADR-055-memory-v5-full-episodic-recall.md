# ADR-055 - Memory v5 Full Episodic Recall Architecture

**Date:** 2026-09-02  
**Status:** Accepted  
**Decision Scope:** Memory v5 Production-Ready Episodic Recall, Semantic Cache, Isolated Embedding Worker, Query Understanding, Hybrid Ranking, Context Assembly, and Runtime Modes.

---

## 1. Context & Problem Statement

ADR-054 established the foundational shadow formation architecture:
`Evidence Journal -> Sessions -> Episodes -> episode_membership`.
Subsequent empirical validation demonstrated that:
1. Deterministic temporal episode formation operates stably in real runs.
2. Local exchange-to-exchange cosine similarity alone is insufficient as an Episode boundary sensor.
3. MiniLM embeddings provide strong semantic signal for pairwise proximity and within-episode cohesion diagnostics.

The remaining gap in Memory v5 is the operational path from a user query to retrieved episodic memories utilized naturally in Kira's responses:
`Query -> Candidate Generation -> Hybrid Ranking -> Context Expansion -> Recall Packet -> PromptContextAssembler -> Kira`.

Without this path, memories are formed on disk but inaccessible to the assistant. Furthermore, running MiniLM inside the main engine process introduces a ~700–900 MB RSS footprint and a ~1.3s blocking cold-start latency.

---

## 2. Core Architecture Decisions

### 2.1 Preserved Foundation Authority
The formation layer remains strictly authoritative:
`evidence_journal` and `lifecycle_events` are ground truth.
`sessions`, `episodes`, and `episode_membership` are pure, deterministic projections.
`episode_membership` is the sole authority for Episode Evidence.

### 2.2 Rebuildable Derived Semantic Cache (`memory_v5_semantic_cache.db`)
Derived semantic vectors are persisted in a separate, 100% rebuildable SQLite database:
- `exchange_embeddings`: `(exchange_id, profile_id, session_id, episode_id, content_hash, model_id, model_version, dimensions, vector_blob, created_at)`.
- `episode_embeddings`: `(episode_id, profile_id, model_id, model_version, vector_blob, cohesion_mean, cohesion_min, content_fingerprint)`.
Any privacy operation (profile purge, forget-all) must purge both authoritative and derived cache rows.

### 2.3 Semantic Worker Process Isolation
To protect the main engine process from memory bloat and model-loading freezes:
- MiniLM runs in a dedicated background worker process (`SemanticWorkerService`).
- Communication occurs via non-blocking IPC with a bounded timeout (e.g. 500ms).
- If the worker crashes, times out, or fails, the system fails open: semantic retrieval returns `NO_RECALL` and Kira continues without disruption.

### 2.4 Incremental Semantic Indexing
When an Episode reaches `CLOSED` state:
1. Evidence is resolved strictly via `episode_membership.event_id` ordered by `sequence_index`.
2. Ordered `ConversationalExchange` units are extracted.
3. Unique exchanges are batch-embedded via the semantic worker.
4. Exchange vectors and aggregate Episode representation (normalized centroid + cohesion) are saved to the derived semantic cache.
Startup routines detect missing or stale cache rows and rebuild them asynchronously.

### 2.5 Query Understanding (`EpisodicQueryAnalyzer`)
Analyzes current user input to extract:
- Normalized query text.
- Recall intent: `EXPLICIT` (e.g. "¿te acuerdas?", "¿qué habíamos dicho?") vs `IMPLICIT` (contextual reference without recall keywords).
- Explicit temporal constraints: `today`, `yesterday`, `last_week`, `N_days_ago`, `named_range`, `last_time`, or `none`.
Explicit temporal constraints act as **hard pre-ranking filters**.

### 2.6 Hard Profile Isolation at Candidate Generation
Candidates are filtered strictly by `profile_id == current_profile` at query generation time. Cross-profile candidate retrieval followed by post-filtering is strictly forbidden.

### 2.7 Exchange-First Candidate Retrieval & Hybrid Ranking
- Retrieval evaluates similarity against historical `ConversationalExchange` vectors first.
- Winning exchanges map to their parent Episodes (supporting centroids provide coarse alignment).
- `HybridEpisodicRanker` combines:
  - Lexical score (preserving ADR-034 corroboration);
  - Semantic exchange score;
  - Episode score;
  - Temporal filter compliance;
  - Recency prior;
  - Episode cohesion.
- Raw cosine similarity alone never authorizes prompt injection.

### 2.8 Diversity & Bounded Context Expansion
- When multiple Episodes match, bounded semantic deduplication (MMR) suppresses redundant copies.
- Selected Episodes expand only with adjacent exchanges ($\pm 1$ exchange) within exact membership order.
- `EpisodicRecallPacket` enforces strict limits: max 3 Episodes, max 3 exchanges per Episode, hard token budget (~1200 tokens).

### 2.9 Prompt Contract (`PromptContextAssembler`)
Injected memories are wrapped in an explicit `<episodic_memory>` block:
- Clearly designated as past conversations, not current verified facts.
- Current user statements take precedence over conflicting memories.
- Model uses the context naturally when relevant and ignores it when irrelevant.

### 2.10 Runtime Modes
- `OFF`: Zero retrieval execution.
- `SHADOW`: Full retrieval, ranking, and packet construction; prompt left byte-identical.
- `ACTIVE`: Approved `EpisodicRecallPacket` injected into PromptContext.

---

## 3. Consequences & Non-Goals

- Zero external vector databases (pure SQLite + NumPy).
- Zero automated unsupervised clustering or entity/knowledge graph generation in this track.
- 100% fail-open resilience: Kira's conversation loop is never blocked by semantic retrieval failures.
