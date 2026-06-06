# Implementation Plan — OBS Runtime Connection Control

## Phase 1: Reproduce and Lock the OBS Lifecycle Bug

- [ ] Task: Add failing tests for current OBS runtime lifecycle gaps.
    - [ ] Verify enabling OBS from the panel requests a live runtime connection.
    - [ ] Verify a successful connection action wires the live OBS client/bridge instead of only testing a temporary client.
    - [ ] Verify disabling OBS cancels retry behavior and disconnects the current client.
    - [ ] Verify repeated enable/connect actions do not start duplicate retry loops.
- [ ] Task: Conductor - User Manual Verification 'Reproduce and Lock the OBS Lifecycle Bug' (Protocol in workflow.md)

## Phase 2: Implement Cancellable Runtime OBS Connection Control

- [ ] Task: Add app-shell OBS lifecycle ownership.
    - [ ] Add explicit start/reconnect and stop/cancel methods for OBS runtime connection.
    - [ ] Track the retry thread and cancellation signal.
    - [ ] Ensure only one retry loop can run at a time.
    - [ ] Preserve startup auto-retry when OBS is enabled in config.
- [ ] Task: Wire Avatar / OBS panel actions to runtime lifecycle.
    - [ ] Persist current OBS settings before runtime connection attempts.
    - [ ] Notify app shell when the OBS switch is enabled or disabled.
    - [ ] Make the connection test establish the live runtime bridge on success, or adjust UI copy to make the action explicit.
    - [ ] Update panel status when the live client connects, disconnects, or is disabled.
- [ ] Task: Conductor - User Manual Verification 'Implement Cancellable Runtime OBS Connection Control' (Protocol in workflow.md)

## Phase 3: Verify OBS UX and Regression Boundaries

- [ ] Task: Run targeted automated verification.
    - [ ] Run OBS/app-shell lifecycle tests.
    - [ ] Run Avatar / OBS panel tests.
    - [ ] Run existing app-shell OBS resilience tests.
- [ ] Task: Run manual verification with OBS.
    - [ ] Launch app with OBS initially disabled.
    - [ ] Enable OBS switch and confirm live connection without app restart.
    - [ ] Use the connection action and confirm avatar bridge is live.
    - [ ] Disable OBS switch and confirm retry loop stops.
    - [ ] Re-enable OBS and confirm a clean reconnect.
- [ ] Task: Conductor - User Manual Verification 'Verify OBS UX and Regression Boundaries' (Protocol in workflow.md)
