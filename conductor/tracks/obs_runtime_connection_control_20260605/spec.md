# Specification — OBS Runtime Connection Control

## Overview

The Avatar / OBS panel currently lets the operator enable OBS and test connection settings, but the runtime behavior is incomplete:

- `ui/avatar_panel.py::_on_obs_toggle` saves `config.obs.enabled`, updates fields, and logs the change, but does not notify the app shell to create or connect the live OBS client.
- `ui/avatar_panel.py::_test_obs_connection` creates a temporary `OBSClient`, calls `test_connection()`, and discards it. A successful test does not connect the real avatar bridge.
- `ui/app_shell.py::_init_obs_client` only initializes the live OBS client during app startup if OBS was already enabled in config.
- `ui/app_shell.py::_connect_obs_loop` retries while `_obs_client` exists, but there is no explicit user-driven cancellation primitive for turning OBS off from the panel.

This causes a confusing UX: enabling OBS from the UI may require restarting the app before the live avatar bridge connects, and disabling OBS may not clearly cancel pending retry behavior.

## Goals

- Enabling OBS from the Avatar / OBS panel should start the live runtime connection flow immediately, without restarting the app.
- The "Probar conexión" action should either establish the real runtime OBS bridge on success or be replaced by behavior/copy that makes its effect unambiguous.
- Disabling OBS from the UI should cancel any pending OBS retry loop and disconnect the current OBS client.
- The OBS retry loop should be single-owner, cancellable, and safe against duplicate background threads.

## Functional Requirements

1. **Runtime enable connects**
   - When the OBS switch is enabled, the panel must persist the current OBS config and notify the app shell.
   - The app shell must create or refresh the runtime `OBSClient` from the saved config.
   - The app shell must start connection attempts immediately if OBS is enabled.
   - No app restart should be required.

2. **Live connection test**
   - The "Probar conexión" flow must not leave the operator with a false positive.
   - If the connection succeeds, the app should establish the live OBS client/bridge used by avatar updates.
   - If the connection fails, the app should show/log the failure and keep the UI recoverable.

3. **Runtime disable cancels**
   - When the OBS switch is disabled, any active retry loop must stop.
   - The current OBS client must disconnect and unsubscribe from the avatar bridge.
   - The Avatar / OBS panel must display a disconnected/disabled state.
   - No further retry logs should continue after the operator disables OBS.

4. **Single retry loop**
   - Repeated toggles or repeated connection attempts must not create multiple concurrent OBS retry loops.
   - The retry loop must have a clear cancellation mechanism such as a `threading.Event`.

5. **Startup compatibility**
   - Existing startup behavior should remain: if OBS is enabled in config at app launch, OpenCohost should attempt to connect/retry in the background.

## Non-Functional Requirements

- Do not block the CustomTkinter UI thread during OBS connection attempts.
- Preserve thread-safe UI updates via the existing scheduling pattern.
- Keep logs operator-friendly and avoid noisy repeated failures after cancellation.
- Do not persist or expose OBS passwords in logs.

## Out of Scope

- Do not change Stream Admin, YouTube chat, SmartAggregator, or Kira response execution.
- Do not rename OBS source defaults such as `KiraAvatar` or `KiraJoyita`.
- Do not redesign the Avatar / OBS panel layout beyond the minimum needed for correct lifecycle behavior.
- Do not change OBS plugin installation requirements.

## Acceptance Criteria

- With the app already open and OBS disabled, enabling the OBS switch can establish the live OBS connection without restarting the app.
- Pressing "Probar conexión" with valid OBS settings does not merely test a disposable client; the live avatar bridge becomes connected or the UI copy is adjusted to avoid implying a live connection.
- Disabling the OBS switch stops retry attempts and disconnects/unsubscribes the current client.
- Toggling OBS on/off repeatedly does not produce duplicate retry threads.
- Startup auto-retry still works when OBS is enabled before launching the app.
- Automated tests cover enable, disable/cancel, duplicate-loop prevention, and test/connect semantics with mocked OBS dependencies.
