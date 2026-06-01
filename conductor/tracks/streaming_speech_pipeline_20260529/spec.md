# Specification — Track B Streaming Speech Pipeline

## Overview

Introduce an optional streaming speech pipeline that can consume streaming LLM deltas, emit complete sentence chunks, synthesize/play audio as soon as possible, and preserve the existing `_hablar()` legacy path through a Strangler Pattern.

## Goals

- Reduce time-to-first-audio (TTFA) by starting TTS/playback after the first stable sentence instead of waiting for the full LLM response.
- Preserve the current non-streaming behavior when streaming is disabled or unavailable.
- Provide safe cancellation semantics for streaming generation, synthesis, and playback.
- Provide deterministic fallback behavior when streaming fails before speech starts.
- Keep changes small, test-first, and reversible.

## Functional Requirements

### FR1 — Legacy Preservation

- When streaming is disabled, `MotorVocalIA._ejecutar_inferencia()` MUST call `_generar_dialogo()` and then `_hablar()` as it does today.
- `_hablar()` MUST remain callable with the same signature and behavior.
- Existing TTS chunking, state reset, callbacks, and validation behavior MUST NOT regress.

### FR2 — Streaming Pipeline Gate

- Streaming MUST be behind an explicit feature flag/configuration or constructor-injected strategy.
- If the flag is off, no streaming components should be initialized for the request.

### FR3 — Sentence Splitting

- The new sentence splitter MUST accept incremental text deltas.
- It MUST emit only stable sentence chunks, primarily ending in `.`, `!`, `?`, or equivalent safe delimiters.
- It MUST support Spanish punctuation without treating opening marks `¿` and `¡` as sentence boundaries.
- It MUST avoid splitting common Spanish abbreviations such as `Dr.`, `Dra.`, `Sr.`, `Sra.`, `ej.`, `vs.`, and `pág.`.
- It MUST buffer incomplete trailing text until completion, flush, cancellation, or failure policy decides otherwise.
- It SHOULD avoid emitting tiny/noisy chunks that would produce bad TTS UX.

### FR4 — TTFA Behavior

- When the LLM yields a complete first sentence, TTS/playback MUST be allowed to begin before the full LLM response completes.
- Tests MUST prove this with fakes/events, not real sleeps or real network/TTS calls.

### FR5 — Cancellation

- Cancellation before the first complete sentence MUST avoid TTS calls and reset speaking state.
- Cancellation after playback starts MUST stop future synthesis/playback and avoid duplicate `speaking_end` callbacks.
- Cancellation MUST NOT fall back to legacy full speech unless explicitly requested by policy.

### FR6 — Fallback

- Streaming failure before first audio starts MUST call the legacy path exactly once.
- Streaming failure after audio starts MUST NOT call legacy `_hablar()` for the full response.
- Fallback MUST avoid false success reporting.

### FR7 — History / Success Semantics

- Conversation history or completion state MUST NOT be committed as successful if streaming fails before producing valid output.
- If partial audio is played, the implementation MUST make success/failure semantics explicit and test-covered.

### FR8 — UI Thread Safety

- Streaming pipeline workers MUST NOT mutate CustomTkinter widgets directly.
- UI-facing events MUST use the existing motor event callback contract and be scheduled on the UI thread by the UI layer, e.g. through `AppShell._safe_after()` / `.after()`.
- Tests should verify emitted event order where practical, not direct widget mutation.

### FR9 — TTS Backpressure and Ordering

- Playback order MUST match sentence order.
- Qwen3-TTS/heavy synthesis MUST be sequential by default to protect local GPU/VRAM and avoid CUDA/OOM-style failures.
- Any Edge-TTS/light parallelism is out of scope for the first implementation slice unless guarded by explicit ordering/backpressure tests.

### FR10 — Windows Audio File Lifecycle

- Cancellation/interrupt paths MUST stop playback and unload the currently loaded `pygame.mixer.music` file before deleting temporary audio files.
- Temporary file cleanup MUST tolerate Windows file locks without crashing the pipeline.

## Non-Functional Requirements

- Local-first behavior and Qwen3-TTS cached/offline startup behavior must remain untouched.
- Heavy TTS timeouts must remain realistic for production but mocked/faked in tests.
- Thread/queue cleanup must be deterministic under cancellation and partial failures.
- UI notifications must preserve Tkinter main-thread safety.
- Streaming TTS must apply backpressure instead of unbounded queue growth.
- The first implementation slice should be small enough for a reviewable work unit.

## Acceptance Criteria

- Red tests exist before implementation for TTFA, cancellation, fallback, and legacy preservation.
- Focused tests pass without real Ollama, real Qwen3-TTS, real audio playback, or network access.
- With streaming disabled, existing legacy inference-to-speech behavior is preserved.
- With streaming enabled and fake streaming LLM, first TTS/playback starts before LLM completion.
- Sentence splitter tests cover Spanish opening punctuation and common abbreviations.
- Heavy/Qwen synthesis is proven sequential in the first slice.
- Cancellation tests prove stop/unload-before-delete behavior using fakes/mocks.
- Broad destructive staging remains avoided; `data/` remains untracked unless explicitly decided otherwise.

## Out of Scope

- Rewriting `_hablar()` wholesale.
- Replacing all TTS internals in one change.
- Parallelizing Qwen3-TTS/heavy synthesis.
- Changing SmartAggregator raw-chat policy.
- Changing LiveVoice continuous or PTT behavior directly.
- Real end-to-end model/TTS benchmarking in the first implementation slice.

## Stress-First TDD Rule

Use this loop for each implementation unit:

1. Write a red test that captures exact behavior.
2. Implement the minimum change to pass.
3. Run the focused test until green.
4. Review the diff; it must be small and explainable.
5. Try to break the test with invalid state, timeouts, partial failures, retries exhausted, rollback/persistence errors, false success, and unnecessary resource preparation.
6. Add another red test only when it changes a meaningful technical decision.
7. Stop when additional tests are overengineering, low value, or worse tradeoff than benefit.

Principle: the goal is not to test everything; it is to find ghost bugs before commit until adding more tests has worse tradeoff than benefit.
