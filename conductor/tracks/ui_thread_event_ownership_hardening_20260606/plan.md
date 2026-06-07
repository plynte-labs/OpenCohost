# Implementation Plan - UI Thread Event Ownership Hardening

## Phase 1 - Exploration and Design

- [x] Task: Map UI callbacks by thread source
    - [x] MotorVocalIA event callbacks
    - [x] recording/audio callbacks
    - [x] OBS retry callbacks
    - [x] Stream/chat callbacks
- [x] Task: Define ownership rule
    - [x] Identify widget/timer mutations that must run on Tk mainloop for MotorVocalIA events
    - [x] Identify pure state updates that can remain thread-safe
    - [x] Choose a minimal routing pattern for MotorVocalIA events
- [x] Task: Conductor - User Manual Verification 'Phase 1 - Exploration and Design' (Protocol in workflow.md)

## Phase 2 - Red Tests

- [x] Task: Add focused tests for event routing
    - [x] Motor event dispatch schedules UI mutation
    - [x] delayed/timer UI callbacks are preserved until main-thread pump runs
    - [x] behavior remains stable when callbacks occur on main thread
- [x] Task: Conductor - User Manual Verification 'Phase 2 - Red Tests' (Protocol in workflow.md)

## Phase 3 - Minimal Hardening

- [x] Task: Apply selected routing pattern
    - [x] Keep changes local to UI event ownership for MotorVocalIA events
    - [x] Preserve cohost agenda behavior
    - [x] Preserve logging and diagnostics
- [x] Task: Conductor - User Manual Verification 'Phase 3 - Minimal Hardening' (Protocol in workflow.md)

## Phase 4 - Verification

- [x] Task: Run focused verification
    - [x] AppShell/UI resilience tests
    - [x] cohost orchestration tests
    - [x] runtime smoke deterministic harness
    - [x] `py_compile`
    - [x] `git diff --check`
    - [x] Focused AppShell/cohost/crash/runtime/LLM suite: 103 passed
    - [x] Deterministic runtime smoke: passed with `no_stale_speech_source: true`
- [x] Task: Conductor - User Manual Verification 'Phase 4 - Verification' (Protocol in workflow.md)
