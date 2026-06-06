# Implementation Plan ? Cohost Audio Arbitration Crash

## Phase 1 ? TDD Contract

- [x] Task: Write red test for direct commands during agenda speech
    - [x] Confirm direct `process_context` does not run inference immediately while agenda audio is active
    - [x] Confirm direct work is queued with higher priority than agenda
- [x] Task: Write red test for agenda prefetch while direct processing is active
    - [x] Confirm agenda prefetch does not call `play_prefetched_agenda()`
    - [x] Confirm stale agenda prefetch is cleared

## Phase 2 ? Minimal Guard

- [x] Task: Implement motor-level direct deferral while any speech is active
    - [x] Track current processing source for arbitration
    - [x] Queue direct work instead of overlapping with agenda speech
- [x] Task: Implement UI-level agenda prefetch guard
    - [x] Allow agenda-to-agenda chaining
    - [x] Block and clear prefetch when non-agenda/direct processing or speech owns the motor

## Phase 3 ? Stress and Verify

- [x] Task: Add everyday stress edge case
    - [x] Rapid direct commands during agenda speech must queue without inference re-entry
- [x] Task: Run targeted automated verification
    - [x] `tests/test_kira_orchestration_gaps.py` passes
- [x] Task: Manual runtime verification in cohost mode
    - [x] Start cohost mode with agenda speaking
    - [x] Send a direct/operator interaction while Kira is speaking
    - [x] Verify Kira does not advance agenda-prefetch over the direct response
    - [x] Verify app remains alive after the interaction


## Manual verification note

User ran a short 3-turn cohost test. Logs showed agenda prefetch paused for higher-priority direct work and the app remained alive. This is accepted as a short positive runtime validation; latency/TTL tradeoff is documented for future review.
