# Design: OBS Runtime Connection Control

## Technical Approach

Keep `VocalAIApp` as the single owner of the live OBS runtime client. `AvatarPanel` remains a configuration/control surface: it saves current form values, updates widget state, and calls app-shell callbacks for enable, disable, and connect-now. Startup and UI-driven enable should share the same app-shell lifecycle path.

## Architecture Decisions

| Decision | Choice | Alternatives considered | Rationale |
|---|---|---|---|
| Runtime owner | `ui/app_shell.py::VocalAIApp` owns `_obs_client`, retry thread, and cancellation event. | Let `AvatarPanel` own OBS runtime. | App shell already owns `_avatar_bridge`, startup OBS setup, and `KiraJoyita` OBS text calls. Keeping ownership there avoids duplicate bridge/subscription logic. |
| Panel contract | Add optional callbacks to `AvatarPanel` for `on_obs_enable`, `on_obs_disable`, and `on_obs_connect`. | Continue temporary `test_connection()`. | The reported bug is runtime connection, not network reachability. Button success must wire the live bridge. |
| Cancellation | Use `threading.Event` and `Event.wait(retry_delay)` in the retry loop. | Keep `time.sleep()` and set `_obs_client = None`. | `Event.wait()` exits promptly when the operator disables OBS and avoids stale retry logs. |
| Duplicate-loop guard | Track `_obs_retry_thread` plus `_obs_retry_cancel`; do not start a new live loop if one is alive for the current client. | Blindly spawn a daemon thread per click/toggle. | Repeated toggles/clicks must not create competing loops that update the panel after disable. |
| Client changes | Prefer no `avatar/obs_client.py` redesign. Use existing `connect()`, `disconnect()`, `subscribe_bridge()`, `on_state_change()`. | Add a new client abstraction. | Existing client has sufficient primitives; this should be a lifecycle wiring fix. |

## Data Flow

```text
AvatarPanel switch ON
  -> save_avatar_config()
  -> VocalAIApp._obs_start_from_config(connect_now=True)
  -> create/refresh OBSClient
  -> start one cancellable retry loop
  -> OBSClient.connect()
  -> subscribe_bridge(_avatar_bridge)
  -> push current avatar state
  -> AvatarPanel.set_obs_client(client)

AvatarPanel switch OFF
  -> save_avatar_config(enabled=False)
  -> VocalAIApp._obs_stop_runtime()
  -> cancel event
  -> OBSClient.disconnect()
  -> AvatarPanel.set_obs_client(None)
```

## File Changes

| File | Action | Description |
|---|---|---|
| `ui/avatar_panel.py` | Modify | Add callback parameters; `_on_obs_toggle()` dispatches enable/disable; `_test_obs_connection()` becomes live connect request after saving config. |
| `ui/app_shell.py` | Modify | Add `_obs_start_from_config()`, `_obs_stop_runtime()`, `_obs_connect_now()`, cancellable `_connect_obs_loop(cancel_event, client)`, and wire callbacks into `AvatarPanel`. |
| `avatar/obs_client.py` | Keep mostly unchanged | Only adjust if tests expose a small idempotency issue; no broad client redesign. |
| `tests/test_app_shell_obs_resilience.py` | Modify | Add lifecycle tests for enable/start, disable/cancel, duplicate-loop prevention, and stale-loop guard. |
| `tests/test_avatar_panel.py` | Modify | Add callback wiring tests for switch on/off and connect button. |

## Interfaces / Contracts

`AvatarPanel` should accept optional callables:

```python
on_obs_enable: Callable[[], None] | None
on_obs_disable: Callable[[], None] | None
on_obs_connect: Callable[[], None] | None
```

The panel saves config before calling these callbacks. App shell reloads config from disk to avoid passing mutable UI objects or secrets through logs.

## Testing Strategy

| Layer | What to Test | Approach |
|---|---|---|
| Unit | Panel callback dispatch | Mock switches/entries and assert callbacks fire after config save path. |
| Unit | App-shell lifecycle | Use `object.__new__(VocalAIApp)`, fake OBS client, fake thread/event where practical. |
| Regression | Existing OBS resilience | Run `tests/test_app_shell_obs_resilience.py`. |
| Manual | Real OBS UX | Toggle ON connects without restart; connect button wires bridge; toggle OFF stops retry/disconnects. |

## Migration / Rollout

No data migration required. Existing `config/avatar.yaml` remains compatible.

## Open Questions

None blocking. Keep button copy as `Probar conexión` only if success means the live runtime bridge is connected; otherwise rename to `Conectar OBS`.
