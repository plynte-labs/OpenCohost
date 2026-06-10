# Implementation Plan - Speech Source Lifecycle Hardening

## Phase 1 - Design

- [x] Task: Produce focused lifecycle design
    - [x] Map all `_hablar()` exit paths
    - [x] Define source clearing semantics before/after `speaking_end`
    - [x] Confirm interactions with cohost/direct arbitration

## Phase 2 - Apply

Red phase confirmed: `test_tts_none_text_balances_events_and_clears_speech_source`
failed before implementation with `TypeError: argument of type 'NoneType' is not
iterable`.

- [x] Task: Add lifecycle tests
    - [x] Normal TTS completion clears speech source
    - [x] Empty/invalid TTS text clears speech source
    - [x] Missing heavy TTS reference clears speech source
    - [x] Playback exception clears speech source before `speaking_end`
- [x] Task: Apply minimal lifecycle hardening
    - [x] Clear source ownership on every speech end path
    - [x] Normalize `None` TTS text before Markdown emphasis sanitization
    - [x] Preserve useful callback diagnostics

## Phase 3 - Verify

- [x] Task: Run focused verification
    - [x] Runtime smoke harness
    - [x] Cohost orchestration tests
    - [x] LLM/TTS lifecycle tests

Verification completed:

- `pytest tests/test_runtime_smoke_harness.py tests/test_kira_orchestration_gaps.py tests/test_llm_engine_timeouts.py -q`: 66 passed.
- `tools/runtime_smoke_harness.py --mode deterministic --json temp/runtime-smoke-deterministic.json`: passed with `no_stale_speech_source: true`.
- `py_compile`: passed for touched runtime/test files.
- `git diff --check`: passed.
