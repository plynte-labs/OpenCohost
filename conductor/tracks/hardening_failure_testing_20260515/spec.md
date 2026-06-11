# Specification: Hardening & Failure Testing

## Overview

VoiceAI is approaching installer/distribution work. Before packaging, the app must survive realistic abusive live-stream conditions and external-service failures without freezing, leaking VRAM, corrupting config, or leaving Kira/agenda state inconsistent.

This track focuses on destructive testing and recovery hardening, especially for Kira Agenda Mode, user interruptions, reactive chat load, TTS/LLM lifecycle, local configs, and OBS-adjacent runtime behavior.

## Goals

- Prove Kira can sustain long autonomous agenda sessions without repetition, stale UI state, or state-machine stalls.
- Prove Kira can be interrupted repeatedly while speaking/generating without corrupting agenda counters, prefetch cache, TTS playback, or avatar/UI state.
- Simulate reactive/bad chat traffic at streamer scale and verify SmartAggregator backpressure/fallback behavior.
- Build smoke/failure scripts that can later become installer diagnostics.
- Identify packaging blockers: local absolute paths, mutable tracked configs, missing assets, external service assumptions, and unclear recovery messages.

## Functional Requirements

### FR1 — Long Agenda Stress

The system SHALL support a test scenario where Kira creates and processes at least 7 approved agenda topics with extended responses.

The test SHALL verify:
- no exact or near-repeat agenda outputs across recent turns;
- topic/turn counters remain consistent;
- prefetch cache is cleared or consumed correctly;
- `last_outputs` anti-loop memory records direct and prefetched speech;
- UI response panel shows the currently spoken Kira text, not the previous turn;
- the app remains responsive while TTS and LLM are active.

### FR2 — Repeated Interruptions

The system SHALL support repeated interruption scenarios during:
- active TTS playback;
- background agenda prefetch;
- agenda generation;
- topic transition/closing;
- reactive chat handling.

The test SHALL verify:
- current speech stops or yields predictably;
- stale prefetched responses are discarded;
- agenda state returns to a valid state;
- avatar/UI/audio bed do not remain stuck in speaking/listening/error state;
- VRAM cleanup paths still run on close/crash.

### FR3 — Reactive Chat Load Simulation

The system SHALL include a deterministic synthetic chat-load test that simulates noisy high-volume chat bursts.

Initial load targets:
- low: 100 messages/minute;
- medium: 500 messages/minute;
- high: 2,000 viewers/messages burst profile, with spam/repetition/garbage content.

The test SHALL verify:
- SmartAggregator sampling/backoff activates as configured;
- vibe analysis does not call the LLM excessively under high traffic;
- chat triggers do not starve agenda or user interruptions;
- memory/queue sizes stay bounded;
- UI remains responsive and logs stay useful.

### FR4 — Failure Matrix

The system SHALL define and automate a failure matrix covering:
- Ollama unavailable;
- Ollama model not loaded or slow;
- Qwen TTS unavailable;
- Qwen port occupied;
- GPU/VRAM pressure;
- Edge-TTS/network unavailable;
- OBS disconnected;
- YouTube chat disconnected or spammy;
- missing/corrupt avatar config;
- missing/corrupt music library config;
- missing local assets;
- app close/crash during TTS.

Each scenario SHALL result in one of:
- graceful fallback;
- clear user-facing recovery message;
- safe disabled state;
- captured diagnostic failure with no resource leak.

### FR5 — Installer Readiness Diagnostics

The system SHALL provide smoke checks that can later be run after installation:
- startup/config load;
- Ollama availability/model status;
- Qwen health/cleanup;
- avatar/music asset sanity;
- agenda controller state-machine sanity;
- SmartAggregator load/backpressure sanity.

## Non-Functional Requirements

- Tests MUST be deterministic and not require real YouTube, real OBS, or real GPU unless explicitly marked manual/live.
- High-load simulations MUST avoid unbounded CPU, RAM, or disk growth.
- Test utilities MUST not write user configs unless they use isolated temp config paths or patch persistence.
- Manual live tests MUST include exact steps, expected results, and cleanup instructions.
- No installer work begins until hardening blockers are triaged.

## Acceptance Criteria

- Focused automated tests pass in `python`.
- A manual 7-topic Kira agenda stress test plan exists and has been executed or explicitly marked pending.
- Repeated interruption tests cover at least three interruption timings.
- Synthetic chat load tests cover low, medium, and high/noisy traffic profiles.
- Failure matrix is documented with status per scenario: pass, warning, fail, or manual-only.
- No tracked config contains pytest temp paths or unintended local private metadata except explicitly accepted local-state files.
- A packaging-readiness report lists remaining blockers before installer work resumes.

## Out of Scope

- Integrating LiveAudio/Whisper into VoiceAI.
- Building the installer.
- Rewriting the UI architecture.
- Real YouTube OAuth/live-stream E2E tests unless performed manually by the operator.
