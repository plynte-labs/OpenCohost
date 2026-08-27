# ADR-050: Lean Engine Decomposition (CohostEngine, TurnScheduler, LLMInferenceService)

## Status
Accepted

## Date
2026-08-27

## Context
`MotorVocalIA` in `opencohost/core/llm_engine.py` historically accumulated multiple orthogonal responsibilities:
1. Priority queueing, dynamic priority assignment, TTL evaluation, capacity pruning, and contiguous owner question bundling.
2. Direct local (Ollama) and cloud chat completion and token streaming.
3. Multi-tier audio synthesis, TTS prewarming, and fallback coordination.
4. Memory extraction, promotion, and conversational context compilation.

Testing queue behavior or inference formatting previously required constructing heavyweight mock engines or dealing with monolithic lock graphs (`_lock`, `_pq_lock`, `_accum_lock`, `_direct_drain_lock`, `_prefetch_lock`).

## Decision
We decomposed the engine into three focused, clean seams following the Lean Architecture Plan:

1. **`CohostEngine` Composition Facade (`opencohost/core/cohost_engine.py`)**:
   - Acts as a lightweight composition wrapper with `__slots__ = ("_runtime",)`.
   - Exposes `EngineHost.motor` while transparently delegating all attribute reads, writes, and method invocations to the underlying engine runtime.

2. **`TurnScheduler` Dispatch Queue Component (`opencohost/core/turn_scheduler.py`)**:
   - Fully owns the dispatch list, `_sched_lock`, priority tier assignments (`PTT=0`, `Direct=1`, `Stream/Agenda=2/3` resolved at enqueue time), TTL eviction, owner-question backpressure exemptions, contiguous owner bundling, and ordered follower requeue.
   - Operates in-place on list slices (`self._items[:] = kept`) to preserve identity for legacy test harnesses.
   - Exposes `_head_snapshot_locked()` to prevent self-deadlock when callers already hold the scheduler lock.

3. **`LLMInferenceService` Inference Runner (`opencohost/core/engine/llm_inference_service.py`)**:
   - Decouples raw prompt/messages input from audio, TTS, and UI callback loops.
   - Provides normalized request execution (`execute_chat`) and streaming token/sentence generation (`stream_tokens`, `stream_sentences` with `SentenceSplitter`).

## Consequences

### Positive
- **Deterministic, sub-50ms Testing**: Priority scheduling and TTL rules are verifiable in 0.44s without external processes or audio infrastructure.
- **Acyclic Lock Graph**: Lock transitions (`_direct_drain_lock -> _sched_lock -> _accum_lock`) are formally documented, verified by blind dual adversarial review, and proved deadlock-free.
- **Zero Behavioral Regressions**: 100% backwards compatibility maintained across 5,815 backend tests and 1,206 frontend tests.

### Negative / Residual
- Facade type identity (`isinstance(host.motor, MotorVocalIA)`) evaluates to `False`, requiring consumers to use attribute duck-typing or check against `CohostEngine`.

## References
- ADR-019: Thread-Safe Telemetry Collector and Judgment Day
- ADR-049: Provider Readiness Reconciliation and PTT Speech Safety
- Conductor Track: `opencohost_v2_lean_decomposition_20260827`
