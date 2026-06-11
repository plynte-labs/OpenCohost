# Implementation Plan: Hardening & Failure Testing

## Phase 1 — Test Harness and Safety Boundaries

- [ ] Task: Create isolated hardening test helpers
    - [ ] Add deterministic fake agenda topics and long-form prompt fixtures.
    - [ ] Add fake chat message generator with spam/garbage/profanity/repetition profiles.
    - [ ] Add isolated temp config helpers so tests never write real user config.
    - [ ] Add bounded queue/memory assertions for load tests.
- [ ] Task: Define hardening scenario registry
    - [ ] Create a machine-readable list of scenarios with ID, subsystem, mode, expected recovery, and manual/automated flag.
    - [ ] Include agenda, interruptions, chat load, service failures, config/assets, and cleanup scenarios.
- [ ] Task: Conductor - User Manual Verification 'Phase 1 — Test Harness and Safety Boundaries' (Protocol in workflow.md)

## Phase 2 — Kira Agenda Long-Run Stress

- [ ] Task: Add automated agenda anti-loop stress tests
    - [ ] Simulate at least 7 approved agenda topics with extended output candidates.
    - [ ] Verify direct and prefetched outputs update repetition memory.
    - [ ] Verify near-repeats are rejected without corrupting state.
    - [ ] Verify topic and turn counters stay consistent after prefetch playback.
- [ ] Task: Add UI/log timing assertions for agenda playback
    - [ ] Verify background prefetch does not update the visible Kira response early.
    - [ ] Verify cached playback emits the current Kira response when speech starts.
- [ ] Task: Create manual 7-topic live run checklist
    - [ ] Include expected logs, UI states, avatar states, and cleanup steps.
    - [ ] Include failure capture template for repeated/stale output.
- [ ] Task: Conductor - User Manual Verification 'Phase 2 — Kira Agenda Long-Run Stress' (Protocol in workflow.md)

## Phase 3 — Interruption and Recovery Hardening

- [ ] Task: Test repeated interruption during active TTS
    - [ ] Interrupt while a chunk is playing.
    - [ ] Interrupt during chunk queueing.
    - [ ] Verify audio bed/avatar/UI state recovers.
- [ ] Task: Test repeated interruption during generation/prefetch
    - [ ] Interrupt while agenda generation is active.
    - [ ] Interrupt while background prefetch is active.
    - [ ] Verify stale prefetch cache is discarded.
    - [ ] Verify agenda controller returns to a valid state.
- [ ] Task: Test emergency close cleanup
    - [ ] Simulate app close while TTS/Qwen/Ollama are active.
    - [ ] Verify Qwen managed process stop path and Ollama unload are called.
- [ ] Task: Conductor - User Manual Verification 'Phase 3 — Interruption and Recovery Hardening' (Protocol in workflow.md)

## Phase 4 — Reactive Chat Load and Backpressure

- [ ] Task: Add synthetic chat load tests
    - [ ] Generate low, medium, and high/noisy chat bursts.
    - [ ] Include spam, repeated emojis, short garbage, and hostile/no-signal messages.
    - [ ] Verify filters reduce noise before expensive processing.
- [ ] Task: Verify SmartAggregator backpressure and LLM-call limits
    - [ ] Assert high-traffic sampling/backoff activates at configured threshold.
    - [ ] Assert vibe/trigger processing does not starve agenda or interruptions.
    - [ ] Assert queues/log buffers stay bounded.
- [ ] Task: Create manual chat stress checklist
    - [ ] Include 2k-viewer equivalent simulation instructions.
    - [ ] Include expected UI/log signals for high-traffic mode.
- [ ] Task: Conductor - User Manual Verification 'Phase 4 — Reactive Chat Load and Backpressure' (Protocol in workflow.md)

## Phase 5 — Service/Config Failure Matrix

- [ ] Task: Add service failure smoke tests
    - [ ] Ollama unavailable / model unavailable.
    - [ ] Qwen unavailable / port occupied / slow health response.
    - [ ] Edge-TTS/network failure.
    - [ ] OBS disconnected.
- [ ] Task: Add config and asset failure tests
    - [ ] Missing/corrupt avatar config.
    - [ ] Missing/corrupt music library config.
    - [ ] Missing avatar/music assets.
    - [ ] Ensure tests do not persist temp paths to tracked config.
- [ ] Task: Produce packaging-readiness diagnostics report
    - [ ] List pass/warning/fail per scenario.
    - [ ] Identify installer blockers and first-run wizard requirements.
- [ ] Task: Conductor - User Manual Verification 'Phase 5 — Service/Config Failure Matrix' (Protocol in workflow.md)

## Phase 6 — Final Verification and Installer Gate

- [ ] Task: Run focused hardening suite
    - [ ] Run agenda/interruption/chat/failure tests in `python`.
    - [ ] Record failing scenarios and triage severity.
- [ ] Task: Run full relevant regression suite
    - [ ] Run core, UI, smart_aggregator, health monitor, avatar, and music tests.
    - [ ] Verify no config pollution after test execution.
- [ ] Task: Decide installer readiness
    - [ ] Create final report: ready, ready with warnings, or blocked.
    - [ ] List exact next tasks for packaging track.
- [ ] Task: Conductor - User Manual Verification 'Phase 6 — Final Verification and Installer Gate' (Protocol in workflow.md)
