# Implementation Plan - UI Thread Event Ownership Hardening

## Phase 1 - Exploration and Design

- [ ] Task: Map UI callbacks by thread source
    - [ ] MotorVocalIA event callbacks
    - [ ] recording/audio callbacks
    - [ ] OBS retry callbacks
    - [ ] Stream/chat callbacks
- [ ] Task: Define ownership rule
    - [ ] Identify widget/timer mutations that must run on Tk mainloop
    - [ ] Identify pure state updates that can remain thread-safe
    - [ ] Choose a minimal routing pattern
- [ ] Task: Conductor - User Manual Verification 'Phase 1 - Exploration and Design' (Protocol in workflow.md)

## Phase 2 - Red Tests

- [ ] Task: Add focused tests for event routing
    - [ ] Motor event dispatch schedules UI mutation
    - [ ] speaking timer setup does not run directly from worker thread
    - [ ] behavior remains stable when callbacks occur before/after mainloop
- [ ] Task: Conductor - User Manual Verification 'Phase 2 - Red Tests' (Protocol in workflow.md)

## Phase 3 - Minimal Hardening

- [ ] Task: Apply selected routing pattern
    - [ ] Keep changes local to UI event ownership
    - [ ] Preserve cohost agenda behavior
    - [ ] Preserve logging and diagnostics
- [ ] Task: Conductor - User Manual Verification 'Phase 3 - Minimal Hardening' (Protocol in workflow.md)

## Phase 4 - Verification

- [ ] Task: Run focused verification
    - [ ] AppShell/UI resilience tests
    - [ ] cohost orchestration tests
    - [ ] runtime smoke deterministic harness
    - [ ] `py_compile`
    - [ ] `git diff --check`
- [ ] Task: Conductor - User Manual Verification 'Phase 4 - Verification' (Protocol in workflow.md)
