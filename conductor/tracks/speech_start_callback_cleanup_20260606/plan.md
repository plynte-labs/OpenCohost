# Implementation Plan - Speech Start Callback Cleanup

## Phase 1 - Focused Design

- [x] Task: Decide cleanup semantics for failed `speaking_start`
    - [x] Confirm whether to emit `speaking_end` after a failed `speaking_start`
    - [x] Confirm logging/diagnostic behavior for callback exceptions
    - [x] Choose minimal guard vs lifecycle helper vs broader `try/finally`
- [x] Task: Conductor - User Manual Verification 'Phase 1 - Focused Design' (Protocol in workflow.md)
    - [x] User approved documenting and applying the narrow design.

## Phase 2 - Red Tests

- [x] Task: Add callback failure tests
    - [x] Verify `speaking_start` sees the active source
    - [x] Verify `speaking_start` exception clears `_speaking`
    - [x] Verify `speaking_start` exception clears `_current_speech_source`
    - [x] Verify no regression in `speaking_end` ordering for existing paths
    - [x] Red result confirmed: `test_speaking_start_callback_failure_clears_speech_source` failed because `motor.is_speaking` stayed `True`.
- [x] Task: Conductor - User Manual Verification 'Phase 2 - Red Tests' (Protocol in workflow.md)

## Phase 3 - Minimal Hardening

- [x] Task: Apply selected cleanup strategy
    - [x] Keep the change local to speech lifecycle ownership
    - [x] Preserve useful diagnostics for callback failures
    - [x] Avoid changing cohost agenda policy or TTS generation behavior
    - [x] Guarded only the `speaking_start` callback window; on failure, clear `_speaking` and `_current_speech_source`, log traceback, and re-raise.
- [x] Task: Conductor - User Manual Verification 'Phase 3 - Minimal Hardening' (Protocol in workflow.md)

## Phase 4 - Verification

- [x] Task: Run focused verification
    - [x] LLM/TTS lifecycle tests
    - [x] Cohost orchestration tests
    - [x] Deterministic runtime smoke harness
    - [x] `py_compile`
    - [x] `git diff --check`
    - [x] Focused pytest: `67 passed`
    - [x] Deterministic runtime smoke: passed with `no_stale_speech_source: true`
- [x] Task: Conductor - User Manual Verification 'Phase 4 - Verification' (Protocol in workflow.md)
