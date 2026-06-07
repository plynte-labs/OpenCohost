# Implementation Plan - UI Thread Event Ownership Hardening

## Phase 1 - Exploration and Design

- [~] Task: Map UI callbacks by thread source
    - [x] MotorVocalIA event callbacks
    - [ ] recording/audio callbacks
    - [ ] OBS retry callbacks
    - [ ] Stream/chat callbacks
- [~] Task: Define ownership rule
    - [x] Identify widget/timer mutations that must run on Tk mainloop for MotorVocalIA events
    - [ ] Identify pure state updates that can remain thread-safe
    - [x] Choose a minimal routing pattern for MotorVocalIA events
- [ ] Task: Conductor - User Manual Verification 'Phase 1 - Exploration and Design' (Protocol in workflow.md)

## Phase 2 - Red Tests

- [~] Task: Add focused tests for event routing
    - [x] Motor event dispatch schedules UI mutation
    - [ ] speaking timer setup does not run directly from worker thread
    - [x] behavior remains stable when callbacks occur on main thread
- [ ] Task: Conductor - User Manual Verification 'Phase 2 - Red Tests' (Protocol in workflow.md)

## Phase 3 - Minimal Hardening

- [~] Task: Apply selected routing pattern
    - [x] Keep changes local to UI event ownership for MotorVocalIA events
    - [x] Preserve cohost agenda behavior
    - [x] Preserve logging and diagnostics
- [ ] Task: Conductor - User Manual Verification 'Phase 3 - Minimal Hardening' (Protocol in workflow.md)

## Phase 4 - Verification

- [ ] Task: Run focused verification
    - [x] AppShell/UI resilience tests
    - [x] cohost orchestration tests
    - [x] runtime smoke deterministic harness
    - [x] `py_compile`
    - [x] `git diff --check`
    - [x] Focused AppShell/cohost/crash/LLM suite: 96 passed
    - [x] Deterministic runtime smoke: passed with `no_stale_speech_source: true`
- [ ] Task: Conductor - User Manual Verification 'Phase 4 - Verification' (Protocol in workflow.md)
