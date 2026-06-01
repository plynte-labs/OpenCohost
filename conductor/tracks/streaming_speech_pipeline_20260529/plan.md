# Implementation Plan — Track B Streaming Speech Pipeline

## Phase 0 — Planning and Safety

- [ ] Task: Confirm clean/safe Git boundary before code
    - [ ] Run `git status --short`.
    - [ ] Keep `data/` untracked unless the user explicitly chooses otherwise.
    - [ ] Use `python tools/safe_stage_check.py` before staging.
- [ ] Task: Conductor - User Manual Verification 'Planning and Safety' (Protocol in workflow.md)

## Phase 1 — Red Tests for Strangler Boundary

- [ ] Task: Add legacy-preservation red test
    - [ ] Prove streaming-disabled `_ejecutar_inferencia()` calls `_generar_dialogo()` then `_hablar()`.
    - [ ] Prove `_hablar()` remains the preservation boundary and is not bypassed by default.
- [ ] Task: Add TTFA red test with fakes
    - [ ] Fake LLM yields one complete sentence, then delayed trailing text.
    - [ ] Assert first TTS/playback starts before full LLM completion.
    - [ ] Avoid real sleeps, network, Ollama, TTS, or pygame.
- [ ] Task: Add cancellation red tests
    - [ ] Cancel before first complete sentence: no TTS call and state resets.
    - [ ] Cancel after playback starts: no further chunks and no duplicate end signal.
    - [ ] Prove playback stop/unload happens before temporary file deletion on cancellation.
- [ ] Task: Add fallback red tests
    - [ ] Failure before first audio falls back to legacy exactly once.
    - [ ] Failure after first audio does not replay full legacy speech.
- [ ] Task: Add Spanish sentence-splitter red tests
    - [ ] Do not split opening punctuation `¿` / `¡` as sentence endings.
    - [ ] Do not split common abbreviations such as `Dr.`, `Dra.`, `Sr.`, `Sra.`, `ej.`, `vs.`, and `pág.`.
    - [ ] Emit complete Spanish questions/exclamations only once the closing punctuation arrives.
- [ ] Task: Add heavy-TTS ordering/backpressure red test
    - [ ] Prove Qwen/heavy synthesis is sequential and ordered.
    - [ ] Prove no unbounded parallel heavy synthesis is started for multiple sentence chunks.
- [ ] Task: Conductor - User Manual Verification 'Red Tests for Strangler Boundary' (Protocol in workflow.md)

## Phase 2 — Minimal Streaming Components

- [ ] Task: Implement standalone `SentenceSplitter`
    - [ ] Emit stable sentence chunks from incremental deltas.
    - [ ] Handle Spanish opening marks and common abbreviations.
    - [ ] Buffer incomplete trailing text.
    - [ ] Filter tiny/noisy chunks consistently with TTS UX.
- [ ] Task: Implement minimal streaming pipeline orchestration
    - [ ] Consume fake/abstract LLM deltas.
    - [ ] Send emitted sentences to fake/abstract TTS/playback ports.
    - [ ] Track whether audio has started for fallback decisions.
    - [ ] Preserve sentence order from synthesis through playback.
- [ ] Task: Add cancellation token semantics
    - [ ] Stop LLM consumption.
    - [ ] Stop future synthesis/playback.
    - [ ] Stop and unload active playback before deleting temp files.
    - [ ] Reset state deterministically.
- [ ] Task: Conductor - User Manual Verification 'Minimal Streaming Components' (Protocol in workflow.md)

## Phase 3 — LLM Engine Integration Behind Flag

- [ ] Task: Add streaming-disabled default integration
    - [ ] Ensure no streaming components initialize when disabled.
    - [ ] Preserve current `_generar_dialogo()` → `_hablar()` behavior.
- [ ] Task: Add streaming-enabled integration seam
    - [ ] Route through `StreamingSpeechPipeline.run(...)` only when explicitly enabled.
    - [ ] Keep fallback to legacy path before first audio.
    - [ ] Preserve history/success semantics under failure.
    - [ ] Emit UI events through the existing motor callback contract; do not mutate CustomTkinter widgets from worker threads.
- [ ] Task: Conductor - User Manual Verification 'LLM Engine Integration Behind Flag' (Protocol in workflow.md)

## Phase 4 — Focused Regression and Work Unit Commit

- [ ] Task: Run focused regression
    - [ ] Run new streaming/splitter tests.
    - [ ] Run relevant legacy tests such as `tests/test_llm_engine_model_trace.py`, `tests/test_llm_engine_timeouts.py`, and `tests/test_validation.py`.
- [ ] Task: Stress-first self-review
    - [ ] Attempt invalid states, timeouts, partial failures, retries exhausted, rollback/persistence, false success, and unnecessary warmup cases.
    - [ ] Add only high-value red tests discovered during stress review.
- [ ] Task: Prepare reviewable work unit
    - [ ] Use `python tools/safe_stage_check.py`.
    - [ ] Stage explicit paths only.
    - [ ] Commit only after focused regression passes.
- [ ] Task: Conductor - User Manual Verification 'Focused Regression and Work Unit Commit' (Protocol in workflow.md)
