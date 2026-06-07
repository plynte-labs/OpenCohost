# Crash Reporting Hardening - Python, Tk, Thread, and Native Fatal Logs

## Status

In progress. Focused design is approved; implementation must proceed in small, independently verifiable slices.

## Why This Exists

The cohost crash produced no crash.log. Investigation found the handler only covers Python/Tk/thread exceptions, misses native hard exits, and currently references datetime without importing it.

## Design Notes for Next Session

- Fix Python crash handler reliability first, including the missing datetime import.
- Add faulthandler/fatal log capture for native crashes or hard process exits where possible.
- Redirect stderr/runtime fatal output to a durable log without exposing private content.
- Keep this separate from the audio arbitration bug so crash observability can be reviewed independently.
- Deep audit findings are captured in `exploration.md`; the confirmed high-risk areas are crash-handler self-failure, Tk/UI callbacks from background threads, native-ish audio surfaces, child-process evidence, daemon-thread cleanup limits, swallowed callback exceptions, subprocess hang surfaces, and mainloop/shutdown gaps.

## Approved Design Boundary

Crash reporting is an observability track, not a full crash-prevention refactor.

This track may:

- Make the Python/Tk/thread crash writer reliable and testable.
- Add defensive stderr fallback if writing `crash.log` fails.
- Add fatal/native best-effort logging with `faulthandler`.
- Record safe diagnostic metadata and child-process log paths.

This track must not:

- Refactor Tk/UI thread ownership. That belongs to `ui_thread_event_ownership_hardening_20260606`.
- Change cohost agenda, direct audio, TTS generation, or product behavior.
- Add external telemetry before local-first crash bundles are proven useful.
- Expose raw chat, prompt text, or private stream content.

## Verification Strategy

- Unit-test the crash writer directly so handler failures cannot hide behind AppShell imports.
- Unit-test installed Python and thread exception hooks.
- Keep Tk hook verification isolated unless a safe non-window test is practical.
- Verify `py_compile` for the crash reporter and AppShell integration.
- Run `git diff --check`.

## Remaining Scope

- Add `faulthandler`/fatal log support as the next slice.
- Consider safe crash bundle metadata only after fatal log behavior is verified.
