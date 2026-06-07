# Implementation Plan - Runtime Smoke Harness

## Phase 1 - Approved Design

- [x] Task: Produce focused design for this track
    - [x] Confirm boundaries and non-goals
    - [x] Define test/verification strategy
    - [x] Decide implementation belongs in this track, limited to harness/reporting only
    - [x] Document approved design in `spec.md`

## Phase 2 - Deterministic Smoke Harness

- [x] Task: Red phase - define deterministic smoke runner expectations
    - [x] Specify command contract, timeout behavior, exit codes, and report schema
    - [x] Add focused tests for report parsing and pass/fail classification
    - [x] Confirm tests fail before implementation
- [x] Task: Green phase - implement deterministic smoke runner
    - [x] Add an explicit smoke runner entrypoint
    - [x] Add deterministic fake LLM/TTS/audio controls
    - [x] Emit structured report and concise console summary
    - [x] Keep real audio/device smoke out of default pytest execution
- [x] Task: Scenario - cohost direct-interruption safety
    - [x] Start controlled runtime/motor path
    - [x] Simulate agenda speech and direct/operator interaction
    - [x] Verify no agenda-prefetch overlap with direct processing/speech
    - [x] Verify balanced `speaking_start` / `speaking_end`
    - [x] Verify empty/invalid TTS text balances speech events and clears source ownership
- [x] Task: Conductor - User Manual Verification 'Phase 2 - Deterministic Smoke Harness' (Protocol in workflow.md)

## Phase 3 - Semi-Real Runtime Smoke

Deferred note: keep this phase pending for now. The deterministic harness is useful
for controlled regression checks, but semi-real pygame/audio validation must remain
opt-in because it touches local audio/runtime behavior.

- [ ] Task: Add opt-in real/semi-real mode design guardrails
    - [ ] Require explicit operator opt-in for real audio/device behavior
    - [ ] Define safe timeouts and cleanup behavior
    - [ ] Define report fields for pygame/audio availability and failures
- [ ] Task: Implement semi-real mode only if deterministic mode proves insufficient
    - [ ] Exercise pygame mixer initialization/play/unload when opted in
    - [ ] Avoid OBS, YouTube, OAuth, and production chat dependencies
    - [ ] Preserve local-first/offline constraints where possible
- [ ] Task: Conductor - User Manual Verification 'Phase 3 - Semi-Real Runtime Smoke' (Protocol in workflow.md)

## Phase 4 - Release Validation Documentation

- [x] Task: Document how to run smoke validation
    - [x] Add command examples
    - [x] Document expected pass/fail output
    - [x] Document when to use deterministic vs semi-real mode
- [x] Task: Verify release-readiness usage
    - [x] Run focused unit tests related to cohost/audio/motor contracts
    - [x] Run deterministic smoke harness
    - [x] Keep semi-real smoke opt-in only; not run without explicit operator request
- [ ] Task: Conductor - User Manual Verification 'Phase 4 - Release Validation Documentation' (Protocol in workflow.md)
