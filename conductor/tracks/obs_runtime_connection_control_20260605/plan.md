# Implementation Plan — OBS Runtime Connection Control

## Phase 1: Reproduce and Lock the OBS Lifecycle Bug

- [x] Task: Add failing tests for current OBS runtime lifecycle gaps.
    - [x] Verify enabling OBS from the panel requests a live runtime connection.
    - [x] Verify a successful connection action wires the live OBS client/bridge instead of only testing a temporary client.
    - [x] Verify disabling OBS cancels retry behavior and disconnects the current client.
    - [x] Verify repeated enable/connect actions do not start duplicate retry loops.
- [x] Task: Conductor - User Manual Verification 'Reproduce and Lock the OBS Lifecycle Bug' (Protocol in workflow.md)

## Phase 2: Implement Cancellable Runtime OBS Connection Control

- [x] Task: Add app-shell OBS lifecycle ownership.
    - [x] Add explicit start/reconnect and stop/cancel methods for OBS runtime connection.
    - [x] Track the retry thread and cancellation signal.
    - [x] Ensure only one retry loop can run at a time.
    - [x] Preserve startup auto-retry when OBS is enabled in config.
- [x] Task: Wire Avatar / OBS panel actions to runtime lifecycle.
    - [x] Persist current OBS settings before runtime connection attempts.
    - [x] Notify app shell when the OBS switch is enabled or disabled.
    - [x] Make the connection test establish the live runtime bridge on success, or adjust UI copy to make the action explicit.
    - [x] Update panel status when the live client connects, disconnects, or is disabled.
- [x] Task: Conductor - User Manual Verification 'Implement Cancellable Runtime OBS Connection Control' (Protocol in workflow.md)

## Phase 3: Verify OBS UX and Regression Boundaries

- [x] Task: Run targeted automated verification.
    - [x] Run OBS/app-shell lifecycle tests.
    - [x] Run Avatar / OBS panel tests.
    - [x] Run existing app-shell OBS resilience tests.
- [x] Task: Run manual verification with OBS.
    - [x] Launch app with OBS initially disabled.
    - [x] Enable OBS switch and confirm live connection without app restart.
    - [x] Use the connection action and confirm avatar bridge is live.
    - [x] Disable OBS switch and confirm retry loop stops.
    - [x] Re-enable OBS and confirm a clean reconnect.
- [x] Task: Conductor - User Manual Verification 'Verify OBS UX and Regression Boundaries' (Protocol in workflow.md)
