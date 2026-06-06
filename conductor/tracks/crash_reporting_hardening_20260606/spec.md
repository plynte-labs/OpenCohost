# Crash Reporting Hardening - Python, Tk, Thread, and Native Fatal Logs

## Status

Pending design. This track is intentionally registered for the next session and should not be implemented before a focused design pass.

## Why This Exists

The cohost crash produced no crash.log. Investigation found the handler only covers Python/Tk/thread exceptions, misses native hard exits, and currently references datetime without importing it.

## Design Notes for Next Session

- Fix Python crash handler reliability first, including the missing datetime import.
- Add faulthandler/fatal log capture for native crashes or hard process exits where possible.
- Redirect stderr/runtime fatal output to a durable log without exposing private content.
- Keep this separate from the audio arbitration bug so crash observability can be reviewed independently.

## Out of Scope Until Design

- No implementation in this session.
- No broad refactor.
- No changes to product behavior without an approved design.
