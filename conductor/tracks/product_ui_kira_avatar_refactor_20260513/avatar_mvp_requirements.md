# Avatar / OBS MVP Requirements

## Intent

Add the first Avatar/OBS module without introducing Live2D, VRM, OBS WebSocket, or a custom animation engine yet.

The goal is simple: the user can assign Kira images to avatar states from the UI, preview them, persist the configuration, and let the app switch visual state as Kira listens, thinks, speaks, or sleeps.

## Product Principle

Less is more. This MVP must make Kira feel present without turning the app into a complex VTuber studio.

## Functional Requirements

### 1. Avatar states

Support these states in the first version:

| State | Meaning | Example asset from user |
|---|---|---|
| `idle` | Kira is available/normal | `Kira.png` |
| `listening` | Kira is listening through LiveAudio/PTT/manual input | `Kira_escuchando.png` |
| `thinking` | Kira is waiting for LLM response | `Kira_pensando.png` |
| `speaking` | Kira is speaking/TTS is active | `Kira_hablando.png` |
| `speaking_alt` | Optional alternate speaking frame | `Kira_hablando2.png` |
| `sleeping` | Kira is inactive/disconnected | `Kira_durmiendo.png` |
| `angry` | Optional mood/error/future expression | `Kira_enfadada.png` |
| `error` | Something failed; may reuse `angry` if no error image is configured | optional |

### 2. User-managed image loading

- Do not hardcode local paths like `C:\Users\...\Downloads`.
- The Avatar/OBS panel must provide a button per state: `Elegir imagen` / `Cambiar imagen`.
- Selecting an image should use a normal file picker.
- Supported formats for MVP: `.png`, `.jpg`, `.jpeg`, `.webp` if the current image library supports it safely.
- After selection, the app should either:
  - copy the selected image into a managed project/user-data avatar folder, or
  - store the selected absolute path only if the project already has a clear user-config convention for external assets.
- Preferred MVP behavior: copy to a managed avatar asset folder so the config does not depend on `Downloads`.
- Replacing a state image must update the preview immediately.

### 3. Configuration persistence

- Add a simple avatar config file, for example `config/avatar.yaml`.
- Store:
  - avatar enabled/disabled
  - selected mode: `image_states`
  - state-to-image mapping
  - preview size/display options if needed
- Missing images should not crash startup.
- If a configured image is missing, show a clear placeholder and log a warning.

### 4. UI requirements

- Add a right-side `Avatar / OBS` product tab/panel.
- The panel should show:
  - current avatar mode
  - current state
  - preview image
  - one row per state with:
    - state label
    - configured filename/path summary
    - `Elegir imagen` button
    - optional `Probar` button to preview that state
- The left Kira panel should show the current avatar image once configured.
- Future controls must say `Próximamente` if visible. Do not expose fake working controls.

### 5. Runtime state bridge

- Create a small runtime boundary independent from Tkinter internals.
- Suggested API:
  - `set_state(state: AvatarState) -> None`
  - `get_state() -> AvatarState`
  - `set_speech_text(text: str) -> None`
  - `subscribe(listener) -> unsubscribe`
- UI widgets observe state changes; core voice/chat logic should not directly manipulate Tk widgets.

### 6. Minimal automatic state wiring

Wire only safe, obvious states at first:

- App idle/ready -> `idle`
- LiveAudio/PTT active -> `listening`
- Request sent to LLM / waiting response -> `thinking`
- TTS playback / Kira response speaking -> `speaking`
- Disconnected/inactive where already detectable -> `sleeping`
- Error path where already centralized -> `error` or `angry`

If a state transition is not already cleanly available, do not invent a fragile hook. Leave it manual/testable and document it for a later slice.

### 7. OBS scope for MVP

- Do not implement OBS WebSocket yet.
- Do not implement Live2D/VRM yet.
- Do not implement mouth tracking yet.
- Optional only if cheap: a simple preview/overlay window that OBS can capture manually.
- If overlay is not implemented in this slice, the Avatar/OBS panel must say: `Overlay OBS: Próximamente`.

## Non-Functional Requirements

- Must not break existing app startup if no avatar images are configured.
- Must keep UI responsive when switching images.
- Must avoid global hardcoded paths.
- Must preserve current Kira/voice/stream behavior.
- Must be testable without OBS and without real image files from the user's Downloads folder.

## Acceptance Criteria

- [ ] User can open `Avatar / OBS` and assign an image to each supported state.
- [ ] Selected images persist across app restart.
- [ ] Missing configured image shows placeholder/warning instead of crashing.
- [ ] Left Kira panel displays the configured current avatar image.
- [ ] Manual `Probar` state action changes the preview.
- [ ] Basic automatic transitions update avatar state for listening/thinking/speaking when safe hooks exist.
- [ ] OBS/Live2D/VRM future options are hidden or labeled `Próximamente`.
- [ ] Tests cover config load/save, missing image fallback, and state bridge subscriptions.

## Out of Scope

- Full OBS integration.
- OBS WebSocket control.
- Live2D.
- VRM/3D.
- Audio-driven mouth tracking.
- Expression detection from LLM sentiment.
- Shipping the user's personal image assets in git.

## Suggested Files

- `config/avatar.yaml` — user-editable avatar config.
- `avatar/avatar_config.py` — load/save/validate avatar configuration.
- `avatar/avatar_state.py` — runtime state bridge and enum.
- `ui/avatar_panel.py` — Avatar/OBS settings and preview panel.
- `ui/app_shell.py` — integrate left-panel avatar preview and right-side Avatar/OBS tab.
- `tests/test_avatar_config.py` — config persistence and missing image behavior.
- `tests/test_avatar_state.py` — state bridge tests.
- `tests/test_avatar_panel.py` or extend `tests/test_product_ui_refactor_safety.py` — UI build/safety tests.
