# Implementation Plan: Product UI Refactor — Kira + Avatar/OBS

## Phase 1: Inventory and Safety Baseline

- [x] Task: Verify current UI inventory against code
    - [x] Review `ui/app_shell.py`, `ui/voice_control.py`, `ui/model_panel.py`, `ui/profile_panel.py`, `ui/smart_aggregator_ui.py`, `ui/stream_admin_ui.py`, `ui/advanced_panel.py`
    - [x] Update `inventory.md` with any missing controls/callbacks
    - [x] Mark every control as preserve, move, hide, future, or remove candidate
    - [x] Identify duplicate/confusing controls such as RF3 chat vs authenticated Stream Admin chat
- [x] Task: Add layout safety tests before moving UI
    - [x] Test AppShell creates the current panels without real external services
    - [x] Test model/profile callbacks are still wired
    - [x] Test voice/PTT callbacks are still wired
    - [x] Test StreamAdminUI can still build inside a supplied parent frame
    - [x] Test AdvancedModePanel remains hidden/shown through the existing switch
- [ ] Task: Conductor - User Manual Verification 'Phase 1: Inventory and Safety Baseline' (Protocol in workflow.md)

## Phase 2: Product Layout Shell Without Behavior Changes

- [x] Task: Introduce product layout containers
    - [x] Create a persistent left `Kira` product area
    - [x] Create right-side classified panel area
    - [x] Preserve existing widgets by moving containers, not rewriting logic
    - [x] Keep StreamAdminUI internals untouched
- [x] Task: Move current Kira response/state into left panel
    - [x] Place Kira identity/header, last response, state labels, and quick manual input
    - [x] Keep current `text_kira_response` compatibility for logs/advanced panel
    - [x] Preserve status updates from `UIState`
- [ ] Task: Conductor - User Manual Verification 'Phase 2: Product Layout Shell' (Protocol in workflow.md)

## Phase 3: Reclassify Existing Configuration Panels

- [ ] Task: Build `Agent / Brain` panel
    - [ ] Move/wrap `ModelPanel`
    - [ ] Move/wrap `ProfilePanel`
    - [ ] Move memory status and `Limpiar Memoria`
    - [ ] Keep model/profile/memory callbacks unchanged
- [ ] Task: Build `Voice / Input` panel
    - [ ] Move microphone selector
    - [ ] Move `Grabar` and `Cargar WAV`
    - [ ] Move TTS mode switch
    - [ ] Move LiveAudio connect control
    - [ ] Move PTT switch/hotkey mapping
- [ ] Task: Build `Stream` panel
    - [ ] Move YouTube RF3 controls into Stream area
    - [ ] Place full `StreamAdminUI` under Stream area
    - [ ] Clarify labels for RF3 chat vs authenticated Stream Admin chat
    - [ ] Keep high-risk moderation confirmations unchanged
- [ ] Task: Build `System` and `Logs` panels
    - [ ] Add storage/cache entry point if already available
    - [ ] Move compact/log switches into appropriate product area
    - [ ] Keep `AdvancedModePanel` hidden by default and accessible
- [ ] Task: Conductor - User Manual Verification 'Phase 3: Reclassify Panels' (Protocol in workflow.md)

## Phase 4: Avatar/OBS Module Boundary

- [x] Task: Add avatar configuration model
    - [x] Create `config/avatar.yaml` with safe defaults and no hardcoded user paths
    - [x] Create `avatar/avatar_config.py` for loading/saving avatar mode and state image paths
    - [x] Support MVP mode: `image_states`
    - [x] Support states: `idle`, `listening`, `thinking`, `speaking`, `speaking_alt`, `sleeping`, `angry`, `error`
    - [x] Prefer copying selected images into a managed avatar asset folder instead of depending on `Downloads`
    - [x] Mark OBS WebSocket, Live2D, and VRM as future/`Próximamente`, not active controls
- [x] Task: Add avatar runtime state bridge
    - [x] Create `avatar/avatar_state.py` with an enum/value object for all MVP states
    - [x] Provide API: `set_state()`, `get_state()`, `set_speech_text()`, and `subscribe()`
    - [x] Keep it independent from Tkinter widgets
- [x] Task: Add Avatar/OBS UI for user-selected images
    - [x] Create `ui/avatar_panel.py`
    - [x] Add one row per state with current path/filename, `Elegir imagen`, and optional `Probar`
    - [x] Use a file picker to assign/replace state images from the UI
    - [x] Update preview immediately after image selection
    - [x] Show avatar preview/placeholder in left Kira panel using the current avatar state
    - [x] Show configuration in right Avatar/OBS panel
    - [x] If no image exists for a state, fallback to `idle` or a clear placeholder instead of crashing
    - [x] Add `Overlay OBS: Próximamente` copy if overlay is not implemented in this slice
- [x] Task: Wire only safe automatic avatar transitions
    - [x] App ready/normal state sets avatar to `idle`
    - [x] LiveAudio/PTT active sets avatar to `listening` where that state is already reliable
    - [x] Waiting for LLM response sets avatar to `thinking` only through an existing safe hook
    - [x] TTS/playback or Kira speaking sets avatar to `speaking` only through an existing safe hook
    - [x] Inactive/disconnected state sets avatar to `sleeping` where already detectable
    - [x] Centralized error paths may set avatar to `error` or `angry`
    - [x] Do not add fragile hooks just to force every transition in this slice
- [x] Task: Add avatar MVP tests
    - [x] Test avatar config load/save with temporary paths
    - [x] Test missing image fallback does not crash startup
    - [x] Test avatar state subscriptions notify UI listeners
    - [x] Test AvatarPanel builds without real OBS or user Downloads assets
    - [x] Test selecting/replacing an image updates config and preview through mocked file picker
    - [x] Add tests for config/state without requiring OBS
- [ ] Task: Conductor - User Manual Verification 'Phase 4: Avatar/OBS Module Boundary' (Protocol in workflow.md)

## Phase 5: Cleanup, Copy, and Regression Verification

- [ ] Task: Remove or relabel phantom controls
    - [ ] Verify `Registrar logs en avanzado` behavior or relabel/remove it
    - [ ] Mark future avatar/OBS options clearly as unavailable if not implemented
    - [ ] Remove duplicate labels that confuse product categories
- [ ] Task: Product copy pass
    - [ ] Rename technical tabs to user-facing categories
    - [ ] Add short descriptions where controls are risky or advanced
    - [ ] Ensure Kira/VoiceAI language is consistent
- [ ] Task: Regression tests
    - [ ] Run `python -m pytest tests/test_model_panel.py tests/test_profile_panel.py tests/test_voice_control.py tests/test_ptt_manager.py tests/test_smart_aggregator.py tests/test_stream_admin_ui.py tests/test_stream_admin.py`
    - [ ] Run targeted UI compile checks
    - [ ] Document any environment-only failures such as missing optional dependencies
- [ ] Task: Update docs and Engram
    - [ ] Update architecture docs with new UI product layout
    - [ ] Add ADR for Kira-centered product layout and Avatar/OBS boundary
    - [ ] Save architecture decision and session summary to Engram
- [ ] Task: Conductor - User Manual Verification 'Phase 5: Cleanup and Regression' (Protocol in workflow.md)
