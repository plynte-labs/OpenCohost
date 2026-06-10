# Speech Start Callback Cleanup - Prevent Stale Speech State on UI Event Failure

## Status

Pending design and implementation. This track records a lifecycle risk discovered
after auditing `speech_source_lifecycle_hardening_20260606`.

## Discovery

The previous lifecycle hardening fixed the observed stale `current_speech_source`
problem for normal TTS completion, empty/invalid text, missing heavy TTS
reference, and playback errors.

A broader audit found one remaining real risk:

- `MotorVocalIA._hablar()` sets `_speaking = True` and
  `_current_speech_source = source`.
- It then calls `ui_callback("speaking_start")`.
- If that callback raises before `_hablar()` enters the TTS pipeline, there is no
  outer cleanup guard.
- Verified exploratory result: `is_speaking=True` and
  `current_speech_source='kira-agenda'` after a raising `speaking_start`
  callback.

## Why This Should Be Done

This is a lifecycle integrity issue, not a UI polish issue. `current_speech_source`
is used as live ownership state by cohost/direct arbitration and diagnostics.
Leaving it stale can make the app believe agenda speech is still active after the
speech path already failed.

The risk is plausible because `speaking_start` handlers in `ui/app_shell.py`
touch UI state, audio bed ducking, agenda state transitions, prefetch scheduling,
logging, avatar state, and Tk timers. Any startup/shutdown race or UI attribute
failure in that handler can strand motor state unless `_hablar()` owns cleanup.

## Verified False Positives

- `speaking_end` callback failure does not leave stale ownership in the current
  apply because `_current_speech_source` is already cleared before
  `speaking_end` is emitted.
- Producer-thread failure before `FIN` does not leave stale ownership; the
  consumer times out and clears source. This is noisy and potentially slow, but
  it is not the same stale-source bug.

## Scope

- Add a focused guard around the early `speaking_start` callback failure window.
- Preserve the existing invariant: source is visible during `speaking_start`.
- Preserve the existing invariant: source is cleared before `speaking_end`.
- Add tests that prove callback failure cannot leave `_speaking` or
  `_current_speech_source` stale.

## Non-Goals

- Do not redesign AppShell event handling.
- Do not change cohost agenda policy.
- Do not implement semi-real pygame/audio smoke here.
- Do not swallow all callback failures silently without logging or diagnostics.
- Do not use this track to broaden crash reporting; native/fatal logging belongs
  to `crash_reporting_hardening_20260606`.

## Risks

- If callback exceptions are swallowed too broadly, real UI bugs may become
  harder to see.
- If cleanup emits `speaking_end` after a failed `speaking_start`, downstream UI
  handlers may observe a partial event pair; design must decide whether that is
  safer than raising.
- If cleanup happens before enough diagnostics are captured, debugging startup or
  shutdown races may become harder.
- If the implementation wraps too much of `_hablar()`, it may change current
  error propagation behavior beyond the targeted early callback window.

## Limitations

- This track hardens Python-level callback failures only.
- It will not catch native process exits, hard crashes, or mixer/device fatal
  errors that terminate the process.
- It does not prove real Tk mainloop shutdown behavior unless paired later with
  opt-in runtime smoke coverage.

## Implementation Options to Evaluate

1. Minimal local guard around `ui_callback("speaking_start")`.
   - Pros: smallest behavioral surface; directly targets the verified risk.
   - Cons: `_hablar()` still has repeated cleanup logic in separate branches.

2. Small helper for speech lifecycle begin/end cleanup.
   - Pros: centralizes ownership semantics and reduces future drift.
   - Cons: slightly larger refactor; must be kept narrow to avoid destabilizing
     TTS flow.

3. Broader `try/finally` around all of `_hablar()`.
   - Pros: strongest cleanup guarantee.
   - Cons: highest risk of changing callback/error ordering; needs careful
     tests to avoid masking useful failures.

## Acceptance Criteria

- A failing `speaking_start` callback cannot leave `is_speaking=True`.
- A failing `speaking_start` callback cannot leave `current_speech_source` set.
- The `speaking_start` callback still observes the intended source while it runs.
- Existing lifecycle tests continue to pass.
- Existing cohost/direct orchestration tests continue to pass.
- Deterministic runtime smoke still reports `no_stale_speech_source: true`.

## Approved Design

Use a narrow guard around the initial `ui_callback("speaking_start")` window.

Decision:

- Keep setting `_speaking=True` and `_current_speech_source=source` before
  `speaking_start`, so the callback can observe the active source.
- If `speaking_start` raises, immediately clear `_speaking` and
  `_current_speech_source` under the existing lock.
- Log the callback failure with traceback.
- Re-raise the exception so the fixed crash-reporting layer can capture it
  through `threading.excepthook`.
- Do not emit `speaking_end` after a failed `speaking_start`; the start handler
  already failed, and calling more UI lifecycle code can compound the original
  failure.

Why this design:

- It targets the verified stale-source window directly.
- It does not redesign AppShell UI event ownership; that belongs to
  `ui_thread_event_ownership_hardening_20260606`.
- It preserves the existing normal-path ordering:
  `source visible during speaking_start` → `source cleared before speaking_end`.
- It keeps failures visible instead of silently hiding UI bugs.

Rejected alternatives:

- Swallow callback exceptions: safer locally, but hides real UI defects.
- Emit `speaking_end` after failed start: creates a partial lifecycle pair and
  can trigger the same broken UI path again.
- Wrap all of `_hablar()` in a broad `finally`: stronger cleanup, but too much
  behavioral surface for this small bug slice.
