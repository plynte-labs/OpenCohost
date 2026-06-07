# UI Thread Event Ownership Hardening

## Status

Pending design. This track records a structural risk discovered during
`crash_reporting_hardening_20260606` exploration.

## Discovery

`MotorVocalIA` runs as a background daemon thread and calls AppShell through
`ui_callback`. AppShell routes that callback to `_on_motor_event()`, which invokes
handlers directly.

Some handler internals use `_safe_after(...)`, but several paths still perform UI
or Tk-adjacent work directly from the callback path:

- `_actualizar_pipeline(...)` updates status/voice/avatar state.
- `_log_accion(...)` writes through `AdvancedModePanel.log_action(...)`.
- `_start_speaking_alt_timer()` / `_tick_speaking_alt()` use Tk timers.
- recording and other worker paths call `self.after(...)` from background
  threads directly.

## Why This Should Be Done

Crash reporting can tell us where the app failed, but it cannot make Tkinter
thread ownership safe. If background threads mutate Tk widgets or schedule timers
incorrectly, the app can raise runtime errors, hang, or crash through native/Tcl
paths that Python hooks may not capture reliably.

This deserves a separate track because it is a cause-prevention problem, not a
logging problem.

## Scope

- Audit all AppShell/UI callbacks invoked from background threads.
- Define a single ownership rule for UI mutation: Tk widget changes and Tk timers
  must run on the main Tk event loop.
- Preserve existing user-visible behavior.
- Add focused tests or harness checks for event routing where practical.

## Non-Goals

- Do not redesign the whole UI.
- Do not change Kira/cohost agenda policy.
- Do not change crash-reporting implementation.
- Do not add semi-real audio smoke here.

## Risks

- Moving event handlers to `after(...)` can change ordering and expose race
  conditions that were previously hidden.
- Over-wrapping everything in async scheduling can make tests harder to reason
  about.
- Some non-widget state updates may be safe off-thread; forcing all state writes
  through Tk could add unnecessary latency.

## Acceptance Criteria

- Motor/UI event dispatch has an explicit main-thread ownership rule.
- Background thread callbacks do not directly mutate Tk widgets.
- Existing cohost/direct orchestration behavior remains unchanged.
- Existing UI smoke/focal tests continue to pass.
