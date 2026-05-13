# Implementation Plan: Product UI Refactor — Kira + Avatar/OBS

## Phase 1: Inventory and Safety Baseline

- [ ] Task: Verify current UI inventory against code
    - [ ] Review `ui/app_shell.py`, `ui/voice_control.py`, `ui/model_panel.py`, `ui/profile_panel.py`, `ui/smart_aggregator_ui.py`, `ui/stream_admin_ui.py`, `ui/advanced_panel.py`
    - [ ] Update `inventory.md` with any missing controls/callbacks
    - [ ] Mark every control as preserve, move, hide, future, or remove candidate
    - [ ] Identify duplicate/confusing controls such as RF3 chat vs authenticated Stream Admin chat
- [ ] Task: Add layout safety tests before moving UI
    - [ ] Test AppShell creates the current panels without real external services
    - [ ] Test model/profile callbacks are still wired
    - [ ] Test voice/PTT callbacks are still wired
    - [ ] Test StreamAdminUI can still build inside a supplied parent frame
    - [ ] Test AdvancedModePanel remains hidden/shown through the existing switch
- [ ] Task: Conductor - User Manual Verification 'Phase 1: Inventory and Safety Baseline' (Protocol in workflow.md)

## Phase 2: Product Layout Shell Without Behavior Changes

- [ ] Task: Introduce product layout containers
    - [ ] Create a persistent left `Kira` product area
    - [ ] Create right-side classified panel area
    - [ ] Preserve existing widgets by moving containers, not rewriting logic
    - [ ] Keep StreamAdminUI internals untouched
- [ ] Task: Move current Kira response/state into left panel
    - [ ] Place Kira identity/header, last response, state labels, and quick manual input
    - [ ] Keep current `text_kira_response` compatibility for logs/advanced panel
    - [ ] Preserve status updates from `UIState`
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

- [ ] Task: Add avatar configuration model
    - [ ] Create `avatar/avatar_config.py` for mode and paths
    - [ ] Support modes: `none`, `placeholder`, `image_2d`, `obs_overlay` as configuration values
    - [ ] Mark unimplemented 3D/Live2D modes as future, not active controls
- [ ] Task: Add avatar runtime state bridge
    - [ ] Create `avatar/avatar_state.py` with states: idle, listening, thinking, speaking, error
    - [ ] Provide API: `set_state()`, `set_speech_text()`, `show()`, `hide()`
    - [ ] Keep it independent from Tkinter widgets
- [ ] Task: Add Avatar/OBS UI placeholder
    - [ ] Create `ui/avatar_panel.py`
    - [ ] Show avatar preview/placeholder in left Kira panel
    - [ ] Show configuration in right Avatar/OBS panel
    - [ ] Add tests for config/state without requiring OBS
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
