# ADR-051: Engine Slimming Batch 1 (PromptContextAssembler, GenerationEvaluator, Pure Helpers)

## Status
Accepted

## Date
2026-08-28

## Context
Following the initial three-seam decomposition in ADR-050 (`CohostEngine`, `TurnScheduler`, and `LLMInferenceService`), the core monolith `MotorVocalIA` in `opencohost/core/llm_engine.py` remained at 4,262 lines of code. It continued to house several tangled, non-orchestration responsibilities:
1. **Prompt Compilation & Context Budgeting**: History projection, personalization filtering, memorias RAG block injection, background digest delimiting, evicted-turn callbacks, and sampling options generation.
2. **Post-Generation Evaluation & Guardrails**: Text/unicode normalization, telemetry derivation, model trace mismatch diagnosis, agenda output transforms, intra-sentence clause repetition inspection/repair, two-pass TTS output guards, and chat repetition checks.
3. **Pure String, Memory & Rate Helpers**: Memory judge promotion parsing, prompt injection marker neutralization, Edge-TTS vs. Piper rate conversions, and contiguous owner turn bundle rendering.

Track `llm_engine_slimming_batch_1_20260827` was executed across three sequential Work Units (WU1, WU2, WU3) on branch `refactor/v2-lean-engine-decomposition` to extract these concerns into pure, decoupled components.

---

## Decision

We extracted the non-orchestration domains from `MotorVocalIA` into pure, stateless modules while keeping 100% of stateful side effects inside `MotorVocalIA`:

### 1. `PromptContextAssembler` (`opencohost/core/context/prompt_assembler.py`) — WU1 (`2b363ea`)
- **Purity**: Zero locks, zero threads, zero disk I/O, zero network calls.
- **Ordered Assembly Pipeline**:
  1. System prompt framing and active grounding rules.
  2. Streamer personalization block (`<perfil_streamer>`).
  3. History window projection with chat assistant-only filtering and last-agenda monologue collapse.
  4. Memorias RAG injection block (`<memorias_guardadas>`).
  5. Editorial cue card block (`<editorial_context>`).
  6. Memory digest block (`<memoria_de_fondo>`) wrapped in standard delimiters.
  7. Token budget enforcement (`apply_char_budget_pure`) and evicted turn callback routing.
  8. Model sampling options synthesis (temperature, top_p, gemma overrides, reasoning token uncap, presence/frequency penalties).
- **Concurrency Invariant**: `MotorVocalIA._build_generation_request` acquires `self._history_lock` exclusively to snapshot history and active profile identity, releasing it *before* running prompt assembly.

### 2. `GenerationEvaluator` (`opencohost/core/context/generation_evaluator.py`) — WU2 (`719061c`)
- **Purity**: Zero mutations to engine state; returns structured evaluation results (`EvaluationResult`).
- **Ordered Post-Generation Pipeline**:
  1. Null byte, BOM (`\x00\ufeff`), and whitespace normalization.
  2. Context telemetry derivation (`CtxTelemetryData`: prompt eval count, eval count, prefill/decode latencies, token utilization ratio).
  3. Audit trace logging & model mismatch diagnosis (`ModelTraceData`).
  4. Agenda output sanitization and transformer hooks.
  5. Intra-sentence clause repetition inspection and repair (`SanitizeVerdict`).
  6. Output guard with TTS check (`output_guard_with_tts_check`), bypassed for streamed turns (`is_streamed`).
  7. Chat history repetition detection against assistant-only history window.
- **Side-Effect Boundary**: `MotorVocalIA._finalize_generation` retains full ownership of all side effects: telemetry ring buffer append, UI error event emissions, local model success marking, epoch invalidations, guardrail retry loops, and history persistence.

### 3. Pure Helpers & Owner Turn Bundling — WU3 (`30e81fa`)
- **`promotion_parser.py` (`opencohost/core/memory/promotion_parser.py`)**:
  - `_PROMOTION_JUDGE_PROMPT`, fence stripping regex, `PromotionParseDiagnostics` dataclass.
  - Fail-silent pure parsers: `_parse_promotion_decisions` and `_parse_promotion_diagnostics`.
- **`injection_markers.py` (`opencohost/core/context/injection_markers.py`)**:
  - `INJECTION_MARKERS` keyword list and `_strip_injection_markers` neutralizer.
- **`speech_rate.py` (`opencohost/core/speech/speech_rate.py`)**:
  - `edge_rate_for_length_scale` phoneme duration converter and top-level non-blocking `_is_connection_error`.
- **`TurnScheduler` Bundling & Lock Inversion Fix (`opencohost/core/turn_scheduler.py`)**:
  - Canonical `TurnStamp` provenance model integration (`opencohost/core/scheduling/turn_stamp.py`).
  - Added pure `compose_owner_bundle` and `TurnScheduler.compose_bundle`.
  - Resolved lock inversion in `TurnScheduler.expire()` by evaluating dynamic TTL closures *before* acquiring `_sched_lock`.
- **Re-export Compatibility**:
  - Explicit re-exports (`import symbol as symbol`) in `opencohost/core/llm_engine.py` to preserve 100% backward compatibility for test harnesses, monkeypatches, and static analysis (mypy/pyright).

---

## Metrics & Reductions

| Milestone / Work Unit | Scope | Monolith (`llm_engine.py`) LOC | Delta |
| :--- | :--- | :--- | :--- |
| **Baseline (Post-ADR-050)** | Commit `821cb9a` | 4,262 LOC | Baseline |
| **WU1 (`2b363ea`)** | `PromptContextAssembler` | 4,070 LOC | -192 LOC |
| **WU2 (`719061c`)** | `GenerationEvaluator` | 3,909 LOC | -161 LOC |
| **WU3 (`30e81fa`)** | Pure Helpers & Turn Bundling | 3,629 LOC | -280 LOC |
| **Cumulative Batch 1** | **Total Across 3 Work Units** | **3,629 LOC** | **-633 LOC (-14.85%)** |

### Codebase Additions
- **Created Modules (9 files)**: 1,311 lines of focused, testable code across `context/`, `memory/`, `speech/`, `scheduling/`, and `tests/`.
- **New Dedicated Tests (3 test suites)**: 25 new unit tests with zero external I/O or network mocks.

---

## Verification & Validation Matrix

1. **Automated Test Regressions**:
   - **Backend Pytest**: **5,830 passed, 15 skipped, 0 failed** in 337s.
   - **Frontend Vitest**: **1,206 passed, 0 failed** in 67s.
   - **Characterization Suites**: 209 unit tests across `test_prompt_context_assembler.py`, `test_generation_evaluator.py`, `test_turn_scheduler.py`, `test_owner_question_bundling.py`, `test_memoria_promotion_parse.py`, and `test_tts_speed_presets.py` passed 100%.
2. **Dual Blind Adversarial Reviews (Judgment Day)**:
   - **Judge A (Concurrency & Memory Lifecycle)**: **APPROVED** (verified zero I/O under `_history_lock`, pre-resolved TTLs in `TurnScheduler.expire`, and module-level import discipline).
   - **Judge B (Contracts & Behavioral Parity)**: **APPROVED** (verified canonical `TurnStamp` parity, prompt ordering, fallback lines, and clean re-exports).
3. **Runtime Smoke Session**:
   - Live session verified on Tauri UI + local Ollama:
     - Real-time PTT speech interruption preemption over active agenda loops.
     - Context assembly and memory recall without hallucination or token overflow.
     - Multi-turn agenda execution (5 consecutive topics) without clause repetition or false-positive guardrail triggers.

---

## References
- ADR-019: Thread-Safe Telemetry Collector and Judgment Day
- ADR-049: Provider Readiness Reconciliation and PTT Speech Safety
- ADR-050: Lean Engine Decomposition (CohostEngine, TurnScheduler, LLMInferenceService)
- Conductor Track: `llm_engine_slimming_batch_1_20260827`
