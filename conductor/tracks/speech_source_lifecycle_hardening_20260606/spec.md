# Speech Source Lifecycle Hardening - Prevent Stale Audio Ownership State

## Status

Pending design. This track records a bug discovered while implementing
`runtime_smoke_harness_20260606`.

## Problem

The deterministic runtime smoke harness exposed that
`MotorVocalIA.current_speech_source` could remain set to `kira-agenda` after
`speaking_end`. That stale ownership state can confuse cohost/direct arbitration,
runtime diagnostics, and future smoke assertions because the app may believe
agenda still owns speech after TTS has finished.

## Why This Deserves a Separate Track

The runtime smoke harness applied a minimal local fix for the detected TTS
completion path. A broader lifecycle audit should not be hidden inside the smoke
harness track because it is a production behavior concern:

- It affects all speech sources, not only the deterministic harness scenario.
- It needs review of normal completion, empty text, missing reference audio,
  playback exceptions, timeout paths, and any future interrupt/stop path.
- It should verify `current_processing_source` and `current_speech_source`
  lifecycle semantics as product invariants, not only as smoke-report fields.

## Scope

- Audit all `MotorVocalIA._hablar()` exit paths.
- Verify `speaking_start` / `speaking_end` balance.
- Verify `current_speech_source` clears on every speech end path.
- Verify source clearing does not hide useful diagnostics before callbacks run.
- Add focused tests for common edge cases.

## Non-Goals

- Do not change cohost agenda policy.
- Do not add real audio/device smoke coverage here.
- Do not modify crash reporting; that belongs to
  `crash_reporting_hardening_20260606`.
- Do not expand to OBS, YouTube, OAuth, or Stream Admin runtime behavior.

## Acceptance Criteria

- All speech end paths clear stale speech ownership.
- Tests cover at least normal completion, empty/invalid text, and missing heavy
  TTS reference audio.
- Existing cohost/direct priority tests continue to pass.
- Runtime smoke harness reports `no_stale_speech_source: true`.

## Approved Design

`current_speech_source` is an ownership signal, not a historical diagnostic.
The source is set before `speaking_start`, and it must be cleared before
`speaking_end` is emitted so callbacks that react to `speaking_end` observe that
audio ownership has already been released.

The focused audit maps `_hablar()` to these production-relevant paths:

- Missing heavy TTS reference: start event is emitted, source clears, end event
  is emitted, and the method returns.
- Empty, `None`, or otherwise invalid TTS text: text is normalized to an empty
  string, no audio generation runs, source clears, and end event is emitted.
- Normal TTS completion: source clears before the final end event.
- Playback exception: playback failure is logged, temporary audio cleanup still
  runs, source clears before the final end event.

This keeps cohost/direct arbitration conservative: direct input may still wait
while audio is actually speaking, but once `speaking_end` fires there must be no
stale agenda ownership left to block or confuse the next decision.
