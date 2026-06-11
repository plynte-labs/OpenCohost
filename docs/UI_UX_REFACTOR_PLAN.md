# UI/UX Refactor Plan

## Scope

This plan covers a safe UI/UX refactor for the current CustomTkinter app. The goal is to improve hierarchy and visual structure without changing backend behavior, model logic, TTS, audio capture, OAuth, YouTube/chat, memory, or existing contracts.

## Current Diagnosis

- The main UI is concentrated in `ui/app.py`, primarily inside `VocalAIApp._build_ui()` and `_build_stream_admin_tab()`.
- `VocalAIApp` currently mixes layout, widget state, audio controls, PTT, WebSocket, model selection, profile selection, Smart Aggregator, Stream Admin, OAuth, moderation, logging, and app lifecycle in one class.
- The top area contains unrelated controls at the same visual level: audio input, recording, voice sample loading, LiveAudio, memory, TTS mode, log visibility, compact mode, model status, model selector, profile selector, PTT, YouTube chat, and spam limits.
- Logs and admin tools dominate the central area through `CTkTabview`, even though normal operation should focus on talking to Kira and reading Kira's response.
- Stream Admin is functional but visually dense. It belongs in Advanced Mode or an admin workspace, not in the main interaction loop.
- Visual hierarchy is weak because many buttons use similar size/color and compete with the primary action.

## Target Architecture

The new layout should follow three clear zones.

### Main Interaction Area

Focused on normal VocalAI/Kira usage:

- Model state.
- Voice input / PTT state.
- Kira response.
- Large primary button: `Hablar` / `Detener`.
- Compact TTS / memory / chat state pills.
- Manual text input to Kira if still needed for operator context.

### Side Panel

Secondary configuration, visually quieter than the main area:

- Model selector.
- Profile selector.
- Audio input selector.
- YouTube connector.
- OAuth provider status/config.
- Moderation summary and safe entry points.
- Configuration groups should be tabbed as `Modelo/Perfil`, `Audio/TTS`, `PTT`, `YouTube`, and `Admin` to avoid forcing vertical scrolling during normal use.

### Advanced Mode

Opt-in operational/debug/admin section:

- Logs.
- Stream Admin.
- Kira actions.
- Manual admin actions.
- Debug/status details.

Stream Admin should live inside the main work area as its own top-level workspace beside `Kira`, because it is too broad to be a side-panel or bottom-drawer module. To avoid crowded horizontal tabs, the main `Kira` / `Stream Admin` selector should use a vertical navigation rail on the left. Logs must not be deleted. They should return to a bottom terminal/log section controlled by `Mostrar logs`. Stream Admin's detailed log belongs in that bottom logs area (`Stream Log`), not inside the operational Stream Admin form.

## Proposed Components / Frames

The first implementation should use CustomTkinter frames/wrappers, not a framework migration.

- `AppShell`: top-level layout frame owned by `VocalAIApp`.
- `SessionStatusBar`: compact state row for model, mic, PTT, TTS, chat, OAuth, memory, and moderation pending state.
- `MainInteractionPanel`: primary live interaction workspace.
- `VoiceControlPanel`: audio/PTT state and large `Hablar` / `Detener` control.
- `KiraResponsePanel`: visible Kira response/log excerpt area for normal users.
- `SideConfigPanel`: secondary configuration column.
- `SideConfigTabs`: tabbed configuration groups (`Modelo/Perfil`, `Audio/TTS`, `PTT`, `YouTube`, `Admin`) to avoid vertical scrolling.
- `ModelSelector`: wraps existing model combo/download/info controls.
- `ProfileSelector`: wraps existing profile combo/edit controls.
- `AudioInputSelector`: wraps existing audio device, record, sample load, and LiveAudio controls.
- `YouTubeConnector`: wraps existing RF3 chat video field, connect button, and `Max/u` control.
- `OAuthProviderPanel`: wraps RF4 OAuth client/status/connect controls.
- `ModerationPanel`: wraps moderation summary and links to advanced moderation details.
- `AdvancedModePanel`: contains opt-in tabs or collapsible area.
- `LogViewer`: wraps existing general log textbox.
- `StreamAdminPanel`: wraps existing `_build_stream_admin_tab()` content.
- `StreamAdminNavigation`: vertical Stream Admin navigation for connection, metadata, moderation, chat, and status.
- `ManualActionsPanel`: wraps existing Kira actions textbox and manual Kira/chat actions.
- `DebugPanel`: future place for internal state, queue status, reconnect info, and non-critical diagnostics.

## Required UI States

The UI must explicitly represent these states with text, not color alone:

- Model loading.
- Model ready.
- Model error.
- Microphone disconnected.
- Microphone listening.
- PTT active.
- TTS generating.
- TTS speaking.
- Chat disconnected.
- Chat connected.
- OAuth disconnected.
- OAuth read-only.
- OAuth write-enabled.
- Memory available.
- Memory clearing.
- Moderation action pending.

Initial implementation may reuse existing state sources and labels, but should place them in `SessionStatusBar` and avoid changing backend event contracts.

## Incremental Refactor Plan

### Phase 1: Audit and Documentation

- Inspect `main.py`, `ui/app.py`, `ui/profiles_window.py`, and existing test files.
- Identify entry point, UI classes, callbacks, and high-risk state references.
- Create this plan before touching functional UI code.

### Phase 2: Safe Layout Shell

- Add internal helper methods or lightweight frame classes in `ui/app.py` only if needed.
- Create layout containers for `AppShell`, `SessionStatusBar`, `MainInteractionPanel`, `SideConfigPanel`, and `AdvancedModePanel`.
- Reparent or rebuild existing widgets into these containers while keeping widget attribute names unchanged, such as `self.combo_modelos`, `self.btn_download`, `self.switch_ptt`, `self.entry_youtube_video`, `self.btn_youtube_chat`, `self.consola`, and Stream Admin widgets.
- Keep all existing callbacks and method names intact.
- Do not change `MotorVocalIA`, RF3, RF4, OAuth provider code, audio capture, WebSocket logic, or TTS logic.

### Phase 3: Visual Hierarchy Pass

- Make `Hablar` / `Detener` the largest primary control.
- Reduce competing blue buttons by using muted secondary styles for config/admin actions.
- Convert status labels into compact pill-like labels where feasible.
- Move logs/admin tabs into Advanced Mode.
- Put Stream Admin inside the main interaction panel as a vertical-navigation workspace beside Kira, not inside the bottom terminal/log area.
- Keep Stream Admin logs in the bottom log area while Stream Admin itself focuses on actions and state.
- Keep critical errors visible through message boxes and logs.

### Phase 4: State Mapping

- Update `_actualizar_pipeline()` and existing RF3/RF4 callbacks to refresh status pills in addition to legacy labels.
- Preserve existing button state updates.
- Add small helper methods for state text if this reduces risk and avoids duplicated UI state logic.

### Phase 5: Cleanup After Validation

- Remove only dead visual scaffolding after the new layout is verified.
- Avoid deleting functional methods.
- Consider splitting large UI sections into separate files only after the layout is stable.

## First Safe Code Block Proposal

The first code block should be limited to `ui/app.py` and should only restructure visual containers:

- Increase window minimum size modestly if needed for side panel ergonomics.
- Replace the current stacked top/model/profile/PTT/tab/bottom layout with a shell grid.
- Create top `SessionStatusBar` row.
- Create left `MainInteractionPanel` with Kira response area, primary voice button placeholder/wrapper, and manual context entry.
- Create right `SideConfigPanel` and move existing model/profile/audio/PTT/YouTube controls there.
- Create tabbed `SideConfigPanel` sections for `Modelo/Perfil`, `Audio/TTS`, `PTT`, `YouTube`, and `Admin`.
- Create `Stream Admin` as a main-area workspace selected from a left vertical rail and move existing log/action/YT/Stream Log tabs to a bottom logs panel.
- Split Stream Admin into vertical navigation sections so metadata, moderation, chat, and status do not compete visually.
- Preserve existing widget attribute names and commands.
- Preserve `_toggle_modo_compacto()` behavior or adapt it minimally to hide side/advanced sections rather than old rows.

No backend, audio, TTS, OAuth, RF3, RF4, memory, or model logic should change in this first block.

## Second Pass - Interaction Hierarchy Cleanup

### Audit Findings

- The left main navigation improves clarity, but the previous Stream Admin vertical navigation created a second sidebar inside an already sectioned workspace.
- The Kira view has the correct primary elements, but the voice state, TTS state, memory state, and chat state need to be visible in the main interaction area, not only in the global status bar.
- The `Hablar` / `Detener` control should remain the most visually dominant action in the app.
- Stream Admin should read as an administrative module. Metadata, moderation, chat, and status controls should remain available, but with less navigation noise.
- The right configuration panel structure is correct: `Modelo/Perfil`, `Audio/TTS`, `PTT`, `YouTube`, and `Admin` should remain separate.
- OAuth and moderation summaries in the right panel should remain informative and compact, not action-heavy.
- Logs should remain opt-in through `Mostrar logs`, occupy no space when hidden, and use a constrained height when shown.

### Implemented Cleanup Direction

- Keep `Kira` and `Stream Admin` as top-level workspaces selected from the main left rail.
- Refine `Kira` as the real home screen with a response card, an in-view status strip, and a larger primary `Hablar` / `Detener` button.
- Keep manual text input as a secondary action with muted styling.
- Replace Stream Admin's internal vertical navigation with internal segments/tabs for `Conexión`, `Metadata`, `Moderación`, `Chat`, and `Estado` to avoid double sidebars.
- Reduce competing blue buttons: blue is for normal primary actions, green for apply/success, red for reject/danger, and gray for secondary/admin actions.
- Preserve all existing widget attribute names and callback method names.

### State Requirements Preserved

- Model: ready, loading, and error are represented through the main model status label.
- Mic: connected, disconnected, and listening are represented in the status bar and Kira voice state strip.
- TTS: idle, generating, and speaking are represented in the status bar and Kira state strip.
- Chat: connected and disconnected are represented in the status bar and Kira state strip.
- OAuth: disconnected, read-only, and write-enabled are represented in the status bar and Admin summary.
- Memory: available and clearing are represented in the status bar and Kira state strip.
- Moderation: no pending actions and pending actions are represented in the status bar and Admin summary.

### Non-Goals

- No backend, OAuth, RF3, RF4, audio, TTS, LLM, memory, or callback contract changes.
- No new dependencies or framework migration.
- No deletion of logs, Stream Admin controls, or high-risk confirmation flows.

## Manual Test Checklist

- App imports and compiles with `python -m compileall .`.
- `main.py` can instantiate `VocalAIApp` without import errors in the target environment.
- Model dropdown still lists `MODELS_CATALOG` entries.
- Download/model switch buttons still call the existing callbacks.
- Profile dropdown still applies profiles and opens `ConfiguradorPerfiles`.
- Audio device dropdown still populates from `sounddevice`.
- Record and load WAV controls still enable after model ready.
- LiveAudio connect/disconnect button still updates state and logs.
- PTT toggle, hotkey label, and map button still work.
- YouTube chat field, connect button, and `Max/u` still call RF3 callbacks.
- OAuth controls still save/connect/disconnect without exposing secrets.
- Stream Admin workspace still reads metadata, suggests/applies, connects chat, sends chat, and shows moderation controls.
- Logs still display general logs, Kira actions, YouTube chat, and Stream Admin logs in Advanced Mode.
- Compact/advanced mode does not hide critical errors or break callbacks.
- Closing the app still disconnects RF3/RF4, saves geometry, stops PTT, and shuts down the model thread.

## Validation Commands

Activate your project Python environment, then:

- `python -m compileall .`
- `python smart_aggregator\test_local.py`
- `python stream_admin\test_local.py`

If tests require credentials or live services, skip live-service execution and document the reason.

## Risks

- `ui/app.py` relies on many `self.*` widget names. Renaming or delaying widget creation can break callbacks.
- `_init_stream_admin()` calls `_populate_stream_oauth_client_fields()` and expects OAuth widgets to exist after `_build_ui()`.
- `_init_smart_aggregator()` and RF3 callbacks expect YouTube widgets/log textboxes to exist.
- `_actualizar_pipeline()` expects `self.lbl_status` and `self.barra_rms` to exist.
- `_on_motor_event()` toggles many widget states directly.
- `_toggle_modo_compacto()` currently assumes old frame names `_frame_model`, `_frame_profile`, and `_frame_bottom`.
- Moving logs into Advanced Mode must not make `_print_log()` fail when logs are hidden.
- Threaded callbacks use `after()`. UI widgets must only be updated on the Tk thread.
- Stream Admin moderation buttons are high-risk actions and must stay visually separated with confirmation.

## Rollback Plan

- Work on a dedicated branch: `feature/ui-ux-refactor-safe`.
- Keep the previous RF4 commit intact on the branch base.
- First implementation should be one small patch limited to UI layout.
- If validation fails, revert only the UI refactor patch, not RF3/RF4 backend work.
- Avoid touching secrets, `.env`, OAuth token files, runtime data, or backend modules.
- Use `git diff` before any commit to verify only intended UI/doc files changed.

## Out Of Scope For This Pass

- React/Tauri migration.
- New dependencies.
- Backend/model/TTS/audio rewrites.
- OAuth provider changes beyond visual relocation.
- RF3/RF4 contract changes.
- Removing logs or admin features.
- Persisted settings migrations.
