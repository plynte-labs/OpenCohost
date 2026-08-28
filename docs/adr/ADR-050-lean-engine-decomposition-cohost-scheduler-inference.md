# ADR-050: Lean Engine Decomposition (CohostEngine, TurnScheduler, LLMInferenceService)

## Status
Accepted

## Date
2026-08-27

## Context
`MotorVocalIA` in `opencohost/core/llm_engine.py` historically accumulated multiple orthogonal responsibilities:
1. Priority queueing, dynamic priority assignment, TTL evaluation, capacity pruning, and contiguous owner question bundling.
2. Main conversational local (Ollama) and cloud chat completion, token streaming, client management, and watchdog supervision.
3. Multi-tier audio synthesis, TTS prewarming, and fallback coordination.
4. Memory extraction, promotion, and conversational context compilation.

## Decision
We decomposed the engine into three focused, clean seams following the Lean Architecture Plan:

1. **`CohostEngine` Composition Facade (`opencohost/core/cohost_engine.py`) - WRAPPER ONLY**:
   - Lightweight composition wrapper exposing `EngineHost.motor` while transparently delegating all attribute reads, writes, and method invocations to the underlying engine runtime.

2. **`TurnScheduler` Dispatch Queue Component (`opencohost/core/turn_scheduler.py`) - REAL EXTRACTION**:
   - Fully owns the dispatch list, `_sched_lock`, priority tier assignments (`PTT=0`, `Direct=1`, `Stream/Agenda=2/3` resolved at enqueue time), TTL eviction, owner-question backpressure exemptions, contiguous owner bundling, and ordered follower requeue.
   - Operates in-place on list slices (`self._items[:] = kept`) to preserve identity for legacy test harnesses.
   - Exposes `_head_snapshot_locked()` to prevent self-deadlock when callers already hold the scheduler lock.

3. **`LLMInferenceService` Inference Runner (`opencohost/core/engine/llm_inference_service.py`) - REAL EXTRACTION (Main Conversational)**:
   - Fully owns execution of main conversational local (Ollama) and cloud (OpenAI-compatible) LLM invocations.
   - Fully owns watchdog timeouts (`call_with_watchdog`, translating `httpx.TimeoutException` to `TimeoutError(f"watchdog_timeout:{timeout:.2f}s")`), streaming token loops (`chat_streaming`, idle-probe watchdog, total wall-clock ceiling), and cloud profile dispatch.
   - Production conversational path (`MotorVocalIA._cloud_attempt_loop`, `_run_streaming_attempt`, `_cloud_chat`, `_ollama_chat_with_watchdog`) delegates to `LLMInferenceService`.
   - Stripped duplicate low-level HTTP loops and client instantiation from `llm_engine_models.py` and `llm_engine_cloud.py`.

### Scope Boundaries & Explicit Non-Goals
- **Main conversational generation paths using service:** 100%
- **Direct main Local generation owners outside service:** 0
- **Direct main Cloud generation owners outside service:** 0
- **Out-of-scope direct Ollama consumers (intentionally deferred):**
  - Topic Scout (`_ollama_scout_chat`)
  - Memoria Promotion Judge (`_ollama_judge_chat`)
- **Residual coupling:**
  - `model/client resolver (getattr(engine, "ollama", ...))`
  - `cloud API-key resolver (getattr(engine, "_cloud_api_key", ...))`
  - `clients timeout cache resolver (getattr(engine, "_ollama_chat_clients", ...))`
  - This coupling remains intentionally bounded to allow legacy model/provider lifecycle to manage hardware/daemon state.

## Validation & Verification Matrix
- **LIVE / REALENV**: Real manual smoke session on Tauri + `gemma4:e4b` / `llama3` (streaming TTFA ~8.4s, Piper 1st audio ~0.3s-0.4s, zero memory/UI freezes).
- **AUTOMATED**:
  - Full backend pytest suite: **5,815 passed, 15 skipped, 0 failed** in 330.79s.
  - Full frontend Vitest suite: **1,206 passed, 0 failed** in 100.51s.
  - Characterization & timeout suites (`test_llm_engine_timeouts.py`, `test_llm_streaming_loop.py`, `test_llm_cloud_client.py`, `test_llm_is_local_gating.py`, `test_llm_inference_service.py`): 170 passed.
- **DUAL ADVERSARIAL REVIEW**:
  - Judge A (Concurrency, Threads & Watchdogs): **APPROVED** (0 blockers).
  - Judge B (Contracts & Local/Cloud Parity): **APPROVED** (0 blockers).

## References
- ADR-019: Thread-Safe Telemetry Collector and Judgment Day
- ADR-049: Provider Readiness Reconciliation and PTT Speech Safety
- Conductor Track: `opencohost_v2_lean_decomposition_20260827`
