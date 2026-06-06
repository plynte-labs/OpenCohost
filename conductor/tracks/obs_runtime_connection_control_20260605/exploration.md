## Exploration: OBS Runtime Connection Control

### Current State
OBS runtime ownership is split between the Avatar / OBS panel and `VocalAIApp`. The panel owns user inputs and persistence: `_on_obs_toggle()` only flips `config.obs.enabled`, saves `config/avatar.yaml`, enables/disables fields, and logs. `_test_obs_connection()` saves the UI config, creates a temporary `OBSClient`, calls `test_connection()`, then discards that client, so a successful test does not wire the live avatar bridge.

The app shell owns the actual runtime client, but only at startup. `_init_obs_client()` loads avatar config and returns early unless OBS is already enabled; when enabled it creates `self._obs_client` and starts `_connect_obs_loop()` in a daemon thread. `_connect_obs_loop()` retries while `self._obs_client` is not `None`, connects, subscribes to `self._avatar_bridge`, pushes the current state once, and updates the panel. There is no explicit cancellation event, retry-thread tracking, or panel callback that starts/stops the runtime lifecycle after startup. Disabling OBS in the panel therefore does not disconnect the live client or intentionally stop pending retries.

`OBSClient` already has the primitives needed for lifecycle ownership: `connect()`, `disconnect()`, `subscribe_bridge()`, `unsubscribe_bridge()`, `is_connected`, `test_connection()`, and `set_obs_text()`. `disconnect()` unsubscribes from the bridge and clears the underlying OBS request client. Tests already cover connection basics, bridge subscription, image source updates, disconnect safety, startup reconnect resilience, and AvatarPanel behavior, but not runtime enable/disable from the panel.

### Affected Areas
- `ui/avatar_panel.py` — Needs a small app-shell callback seam for OBS enable/disable and connection action; should continue owning UI inputs/status but not own runtime OBS lifecycle.
- `ui/app_shell.py` — Should become the single owner of runtime OBS lifecycle: create/refresh client, start one retry loop, cancel retries, disconnect, update panel status, and preserve startup auto-retry.
- `avatar/obs_client.py` — Likely no broad redesign needed; may need a small runtime-connect helper only if `test_connection()` semantics are replaced by using real `connect()`.
- `tests/test_app_shell_obs_resilience.py` — Best place for focused lifecycle tests using `object.__new__(VocalAIApp)` and mocked OBS/client/threading dependencies.
- `tests/test_avatar_panel.py` — Best place for panel callback wiring tests; existing source-level OBS safety test is stale in wording but currently only asserts no raw `websocket.connect` and presence of `Próximamente`.
- `tests/test_obs_client.py` — Existing client behavior should remain stable; only extend if `OBSClient` itself changes.

### Approaches
1. **App-shell lifecycle API with panel callbacks** — Add callbacks to `AvatarPanel` such as `on_obs_enabled(config)`, `on_obs_disabled()`, and/or `on_obs_connect_requested(config)`. App shell implements explicit start/stop methods, tracks a retry thread and `threading.Event`, and reuses the same path for startup, toggle-on, and connection action.
   - Pros: Clear ownership, minimal coupling to Stream Admin/YouTube/Kira execution, testable with existing app-shell style, supports true cancellation and duplicate-loop prevention.
   - Cons: Requires careful UI scheduling and thread-state handling; must avoid logging passwords or racing against stale clients.
   - Effort: Medium

2. **Make `AvatarPanel` directly own runtime OBS** — Let the panel create/connect/disconnect the live `OBSClient` and subscribe to the bridge directly.
   - Pros: Localizes switch/button behavior near the widgets.
   - Cons: Blurs UI/runtime ownership, duplicates startup logic, requires passing the avatar bridge and shell logging/timer behavior into the panel, increases coupling and future cleanup risk.
   - Effort: Medium-High

3. **Change only UI copy and keep startup-only runtime** — Rename the button/copy to indicate it is only a temporary connectivity test and leave runtime connection to startup config.
   - Pros: Very small change, low regression risk.
   - Cons: Does not satisfy the reported bug or acceptance criteria; users still need restart to connect live and disabling still cannot cancel retries.
   - Effort: Low

### Recommendation
Use Approach 1. Keep `VocalAIApp` as the single runtime OBS lifecycle owner and make `AvatarPanel` a configuration/control surface. The implementation should add explicit app-shell methods along these lines: start/refresh OBS from saved config, stop/cancel OBS, and connect-now/test-as-runtime. Startup should call the same start method when config is enabled. The retry loop should receive a cancellation `threading.Event`, check it before/after sleeps, and only run if it is still the active generation/client. On successful connection, it should subscribe once, push the current avatar state, and update the panel via `after()`.

For “Probar conexión”, the strongest UX is to make it establish the real runtime bridge on success rather than test a disposable client. If product copy remains “Probar conexión”, then success must mean the live bridge is connected; otherwise rename it to an explicit “Conectar OBS”/“Conectar ahora” action. Given the user report, prefer live runtime connection.

### Risks
- Background retry loops can overlap if thread identity/generation is not guarded; repeated switch toggles need single-loop protection.
- Cancellation can be delayed up to `retry_delay` unless the loop waits on `Event.wait(retry_delay)` instead of `time.sleep()`.
- A stale loop may update the panel after the user disabled OBS unless success handling checks that its client/event is still current.
- `config/avatar.yaml` currently has user runtime changes; implementation/tests must mock persistence or temp paths and must not normalize or overwrite that file accidentally.
- Existing `KiraJoyita` OBS text integration depends on `self._obs_client`; disconnect semantics must leave it as no-op when OBS is disabled without touching Stream Admin/YouTube/Kira execution.

### Ready for Proposal
Yes. The proposal should scope a small lifecycle fix: app shell owns a cancellable single OBS runtime connection loop; AvatarPanel forwards enable/disable/connect requests after saving config; tests lock runtime enable, live connect action, disable cancellation/disconnect, duplicate-loop prevention, and startup compatibility.
