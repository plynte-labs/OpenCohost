# Runtime Smoke Harness - Real App Cohost and Audio Safety Validation

## Status

Design approved on 2026-06-06. Implementation remains pending.

## Purpose

Create a controlled runtime smoke harness for OpenCohost scenarios that are too
runtime-dependent for normal unit tests: Tk scheduling, `MotorVocalIA` thread
lifecycle, `speaking_start` / `speaking_end` callbacks, pygame audio playback,
agenda prefetch, direct/operator interruption, and clean process shutdown.

## Problem

The cohost crash investigation exposed a validation gap: existing tests cover
contract behavior with mocks and manually constructed objects, but they do not
prove that the real application process survives the risky combination of:

- Tk / CustomTkinter mainloop.
- `MotorVocalIA` worker thread and UI callbacks.
- pygame mixer initialization, playback, unload, and audio-device behavior.
- Agenda prefetch while Kira is speaking.
- Direct/operator interaction arriving during agenda speech.
- Clean app shutdown after overlapping runtime activity.

This is not a unit-test problem. Treating it like one would create slow,
flaky tests and would still miss native/audio-driver failures.

## Scope

The harness must validate process survival and observable lifecycle invariants.

Required scenario coverage:

- Start a minimal app/runtime session under a controlled timeout.
- Exercise cohost agenda speaking with at least one queued topic.
- Inject direct/operator interaction while agenda speech is active.
- Confirm direct work does not overlap with agenda-prefetch playback.
- Confirm `speaking_start` and `speaking_end` events balance.
- Confirm speech ownership does not remain stale after speech completes.
- Confirm the app process exits cleanly with a useful exit code and report.

## Harness Levels

### Level 1 - Deterministic smoke

Runs with deterministic fakes for LLM, TTS output, and audio playback where
possible, while still exercising the app/motor orchestration path as close to
runtime as practical.

Expected use:

- Safe for frequent local validation.
- No external network.
- No real audio device requirement.
- Useful after changes to cohost, agenda, motor, TTS lifecycle, or shutdown.

### Level 2 - Manual/semi-real runtime smoke

Runs as an explicit opt-in local validation mode with real pygame/audio behavior
where needed.

Expected use:

- Operator-triggered, not part of normal unit pytest.
- Uses hard timeouts and a subprocess wrapper.
- Produces logs/report artifacts for post-run inspection.
- May require local audio stack availability.

## Acceptance Criteria

- The harness is separate from the normal unit-test suite.
- A failed smoke scenario returns a non-zero exit code and a short diagnostic
  report.
- A successful smoke scenario proves process survival, balanced speech events,
  no agenda/direct overlap, and clean shutdown.
- The harness avoids raw chat persistence and reports only controlled scenario
  inputs, counters, states, and event names.
- The implementation does not introduce broad product behavior changes.

## Approved Design

| Area | Decision |
| --- | --- |
| Test boundary | Keep smoke validation separate from normal pytest unit tests. |
| Execution model | Use a subprocess wrapper with timeout and exit-code based pass/fail. |
| Modes | Provide deterministic smoke first; add semi-real local runtime smoke as opt-in. |
| Runtime evidence | Record structured event counters and lifecycle states, not raw chat. |
| Failure output | Produce a concise report with scenario, exit code, timeout status, event trace summary, and log path. |
| Product impact | Avoid broad runtime behavior changes; add only explicit smoke entrypoints/hooks if needed. |

The first scenario should model the bug that motivated the track:

1. Start a controlled OpenCohost runtime.
2. Queue/activate a cohost agenda topic.
3. Trigger agenda speech.
4. While agenda speech is active, inject a direct/operator interaction.
5. Assert that agenda prefetch does not play over the direct interaction.
6. Assert speech lifecycle events are balanced.
7. Assert the process remains alive until the planned shutdown.
8. Shut down cleanly and emit a report.

Signals to capture:

- Process started.
- Motor started.
- Agenda enabled.
- Agenda action enqueued.
- `speaking_start` count.
- `speaking_end` count.
- Current speech source transitions.
- Direct interaction queued/processed.
- Prefetch cleared or skipped when direct work is active.
- Shutdown requested/completed.

Failure criteria:

- Process crash or native fatal exit.
- Timeout.
- Unbalanced speaking lifecycle.
- Direct interaction overlaps with agenda-prefetch playback.
- Stale speech source after speech completes.
- Missing report output.

## Non-Goals

- Do not build a broad automation platform.
- Do not automate every manual release-readiness check.
- Do not require OBS, YouTube, real Twitch chat, or production OAuth.
- Do not add real audio/device-dependent checks to default pytest execution.
- Do not fix crash reporting in this track; that remains
  `crash_reporting_hardening_20260606`.
