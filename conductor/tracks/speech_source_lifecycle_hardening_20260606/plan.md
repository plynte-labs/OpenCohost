# Implementation Plan - Speech Source Lifecycle Hardening

## Phase 1 - Design Pending

- [ ] Task: Produce focused lifecycle design
    - [ ] Map all `_hablar()` exit paths
    - [ ] Define source clearing semantics before/after `speaking_end`
    - [ ] Confirm interactions with cohost/direct arbitration

## Phase 2 - Apply Pending

- [ ] Task: Add lifecycle tests
    - [ ] Normal TTS completion clears speech source
    - [ ] Empty/invalid TTS text clears speech source
    - [ ] Missing heavy TTS reference clears speech source
- [ ] Task: Apply minimal lifecycle hardening
    - [ ] Clear source ownership on every speech end path
    - [ ] Preserve useful callback diagnostics

## Phase 3 - Verify Pending

- [ ] Task: Run focused verification
    - [ ] Runtime smoke harness
    - [ ] Cohost orchestration tests
    - [ ] LLM/TTS lifecycle tests
