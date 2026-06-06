# Runtime Smoke Harness - Real App Cohost and Audio Safety Validation

## Status

Pending design. This track is intentionally registered for the next session and should not be implemented before a focused design pass.

## Why This Exists

The pygame/native crash cannot be reliably asserted in unit tests. We need a separate smoke/integration harness to exercise real app flows, audio/device behavior, and process survival without polluting unit tests.

## Design Notes for Next Session

- Design a smoke harness separate from normal pytest unit tests.
- Cover cohost agenda speaking, direct/operator interaction, no agenda overlap, and app remains alive.
- Prefer deterministic fakes where possible, but allow semi-automatic local runtime validation when real audio/GPU/OBS are required.
- Keep the scope validation-focused, not a broad automation platform.

## Out of Scope Until Design

- No implementation in this session.
- No broad refactor.
- No changes to product behavior without an approved design.
