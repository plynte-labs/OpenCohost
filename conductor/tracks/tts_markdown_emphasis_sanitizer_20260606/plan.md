# Implementation Plan - TTS Markdown Emphasis Sanitizer

## Phase 1 - TDD Contract

- [x] Task: Add red tests for Markdown emphasis cleanup
    - [x] Preserve text inside `*...*`, `**...**`, and `***...***`
    - [x] Preserve math/code-like expressions such as `5*10=50`, `a*b`, and `2 ** 8`

## Phase 2 - Minimal Implementation

- [x] Task: Implement conservative TTS emphasis sanitizer
    - [x] Add fast path when no asterisk exists
    - [x] Strip emphasis markers only when content looks like natural text
    - [x] Wire sanitizer before TTS sentence chunking

## Phase 3 - Verification

- [x] Task: Run targeted LLM/TTS tests
    - [x] Run `tests/test_llm_engine_timeouts.py`
    - [x] Run compile check for `core/llm_engine.py`
