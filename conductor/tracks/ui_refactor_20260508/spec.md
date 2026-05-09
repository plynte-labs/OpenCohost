# Track: UI God Class Refactor — Split app.py into Modular Components

## Problem Statement
`ui/app.py` is a 2722-line God class that handles all UI concerns: WebSocket management, PTT, Smart Aggregator callbacks, Stream Admin callbacks, audio recording, model management, profile management, and UI state management. This creates:
- High risk of merge conflicts in multi-agent development
- Unclear ownership of code sections
- Difficulty isolating changes for testing
- Tight coupling between UI and business logic

## Scope
- Split `ui/app.py` into focused, testable modules
- Preserve all existing functionality
- Add thread safety for shared state
- Add tests for extracted modules
- No changes to backend logic (llm_engine, server_qwen, smart_aggregator, stream_admin)

## Out of Scope
- Migration to Tauri/React (future track)
- Backend refactoring
- New features
- Visual redesign (separate track if needed)

## Target Architecture

```
ui/
├── app.py                    # Thin shell (AppShell), <200 lines
├── voice_control.py          # VoiceControlPanel: WebSocket, audio recording, RMS
├── ptt_manager.py            # PTTManager: hotkey handling, config
├── model_panel.py            # ModelPanel: model selection, download, activation
├── profile_panel.py          # ProfilePanel: profile selection, editor dialog
├── smart_aggregator_ui.py    # SmartAggregatorUI: YT chat tab, callbacks
├── stream_admin_ui.py        # StreamAdminUI: stream admin tab, callbacks
├── status_bar.py             # StatusBar: model/TTS/chat status pills
├── advanced_panel.py         # AdvancedModePanel: logs, debug, manual actions
└── profiles_window.py        # (existing) Profile editor dialog
```

## Acceptance Criteria

1. **Functionality preserved**: All existing features work identically after refactor
2. **No regressions**: All existing tests pass
3. **New tests**: Each extracted module has unit tests with >80% coverage
4. **Thread safety**: Shared state between UI and background threads uses proper locks
5. **No silent errors**: Replace `except: pass` patterns with proper error logging
6. **Clean imports**: No circular dependencies between new modules
7. **app.py < 200 lines**: Only AppShell with composition of panels
8. **Backward compatible**: `main.py` works without changes

## Risks

| Risk | Impact | Mitigation |
|---|---|---|
| Lambda closure capture bugs | High | Audit all `after(0, lambda: ...)` patterns, replace with functools.partial |
| Thread safety regressions | High | Add locks for all shared state, test concurrent access |
| Callback signature mismatches | Medium | Define explicit callback protocols, test with mocks |
| UI state desync | Medium | Centralize state in AppShell, panels read-only |
| Merge conflicts during refactor | Medium | One module per task, commit after each |

## Dependencies
- Existing `vocalai-ui-ux-architect` skill for layout and component guidance
- Existing `smart_aggregator/` and `stream_admin/` modules (unchanged)
- Existing `core/llm_engine.py` (unchanged)
