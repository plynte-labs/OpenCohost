# Implementation Plan: UI God Class Refactor

## Phase 1: Foundation — Test Infrastructure + State Design [checkpoint: a1b2c3d]

- [x] Task: Setup pytest infrastructure for VoiceAI `a1b2c3d`
    - [x] Create `tests/` directory with `__init__.py`
    - [x] Create `pytest.ini` or `pyproject.toml` with pytest configuration
    - [x] Create `conftest.py` with shared fixtures (mock Ollama, mock TTS server, mock WebSocket)
    - [x] Migrate `smart_aggregator/test_local.py` to `tests/test_smart_aggregator.py`
    - [x] Migrate `stream_admin/test_local.py` to `tests/test_stream_admin.py`
    - [x] Verify all migrated tests pass with `pytest tests/ -v`
- [x] Task: Design and implement UI state container `a1b2c3d`
    - [x] Create `ui/state.py` with `UIState` class (thread-safe, uses locks)
    - [x] Define state properties: model_status, mic_status, tts_status, chat_status, pipeline_state, ws_connected, smart_agg_connected
    - [x] Add observer pattern for state changes (panels subscribe to updates)
    - [x] Write tests for `UIState` thread safety and observer notifications
- [x] Task: Define callback protocols `a1b2c3d`
    - [x] Create `ui/protocols.py` with Protocol classes for all callback signatures
    - [x] Document expected callback parameters and return types
    - [x] Write tests that verify protocol compliance with mocks

- [x] Task: Conductor - User Manual Verification 'Phase 1: Foundation' (Protocol in workflow.md) `a1b2c3d`

## Phase 2: Extract PTT Manager [checkpoint: e4f5g6h]

- [x] Task: Write tests for PTT functionality `e4f5g6h`
    - [x] Create `tests/test_ptt_manager.py`
    - [x] Test hotkey configuration load/save
    - [x] Test hotkey mapping (F1-F12, ScrollLock, Insert, Pause, Mouse4, Mouse5)
    - [x] Test PTT press/release behavior with mocks
    - [x] Test reverse lookup (pynput key → display name)
- [x] Task: Extract PTT code from app.py `e4f5g6h`
    - [x] Create `ui/ptt_manager.py` with `PTTManager` class
    - [x] Move `_PTT_KB_MAP`, `_PTT_MOUSE_MAP`, `_PTT_REVERSE_MAP`
    - [x] Move `_cargar_ptt_config()`, `_guardar_ptt_config()`
    - [x] Move PTT listener logic with proper lock-based thread safety
    - [x] Update `app.py` to import and use `PTTManager`
    - [x] Verify tests pass
- [x] Task: Conductor - User Manual Verification 'Phase 2: Extract PTT Manager' (Protocol in workflow.md) `e4f5g6h`

## Phase 3: Extract Status Bar + State Display [checkpoint: i7j8k9l]

- [x] Task: Write tests for status bar `i7j8k9l`
    - [x] Create `tests/test_status_bar.py`
    - [x] Test status pill creation for each state (model, TTS, chat, mic)
    - [x] Test state-driven visual feedback (color, text changes)
    - [x] Test state observer integration with `UIState`
- [x] Task: Extract status bar code from app.py `i7j8k9l`
    - [x] Create `ui/status_bar.py` with `StatusBar` class
    - [x] Move status pill creation and update logic
    - [x] Implement state observer subscription
    - [x] Update `app.py` to use `StatusBar`
    - [x] Verify tests pass
- [x] Task: Conductor - User Manual Verification 'Phase 3: Extract Status Bar' (Protocol in workflow.md) `i7j8k9l`

## Phase 4: Extract Voice Control Panel [checkpoint: m0n1o2p]

- [x] Task: Write tests for voice control `m0n1o2p`
    - [x] Create `tests/test_voice_control.py`
    - [x] Test WebSocket connection lifecycle (connect, disconnect, reconnect)
    - [x] Test audio recording start/stop
    - [x] Test RMS animation (replace random with actual audio levels)
    - [x] Test voice button state machine (Idle → Listening → Processing → Speaking → Error)
- [x] Task: Extract voice control code from app.py `m0n1o2p`
    - [x] Create `ui/voice_control.py` with `VoiceControlPanel` class
    - [x] Move WebSocket management code
    - [x] Move audio recording code (sounddevice, soundfile)
    - [x] Move RMS animation logic
    - [x] Move voice button state machine
    - [x] Update `app.py` to use `VoiceControlPanel`
    - [x] Verify tests pass
- [x] Task: Conductor - User Manual Verification 'Phase 4: Extract Voice Control' (Protocol in workflow.md) `m0n1o2p`

## Phase 5: Extract Model + Profile Panels [checkpoint: pending]

- [x] Task: Write tests for model and profile panels
    - [x] Create `tests/test_model_panel.py`
    - [x] Test model catalog display and selection
    - [x] Test model download/activation state machine
    - [x] Create `tests/test_profile_panel.py`
    - [x] Test profile selection and editor dialog integration
- [x] Task: Extract model panel code from app.py
    - [x] Create `ui/model_panel.py` with `ModelPanel` class
    - [x] Move model catalog, download, activation logic
    - [x] Move Ollama status detection and management
    - [x] Update `app.py` to use `ModelPanel`
    - [x] Verify tests pass
- [x] Task: Extract profile panel code from app.py
    - [x] Create `ui/profile_panel.py` with `ProfilePanel` class
    - [x] Move profile selection logic
    - [x] Move profile editor dialog integration
    - [x] Update `app.py` to use `ProfilePanel`
    - [x] Verify tests pass
- [x] Task: Conductor - User Manual Verification 'Phase 5: Extract Model + Profile Panels' (Protocol in workflow.md)

## Phase 6: Extract Smart Aggregator UI [checkpoint: pending]

- [x] Task: Write tests for smart aggregator UI
    - [x] Create `tests/test_smart_aggregator_ui.py`
    - [x] Test YT chat tab creation and display
    - [x] Test callback integration with aggregator
    - [x] Test message display and filtering UI
    - [x] Test connection status display
- [x] Task: Extract smart aggregator UI code from app.py
    - [x] Create `ui/smart_aggregator_ui.py` with `SmartAggregatorUI` class
    - [x] Move YT chat tab creation
    - [x] Move callback handlers (with proper error logging, no `except: pass`)
    - [x] Move message display logic
    - [x] Update `app.py` to use `SmartAggregatorUI`
    - [x] Verify tests pass
- [x] Task: Conductor - User Manual Verification 'Phase 6: Extract Smart Aggregator UI' (Protocol in workflow.md)

## Phase 7: Extract Stream Admin UI [checkpoint: pending]

- [x] Task: Write tests for stream admin UI
    - [x] Create `tests/test_stream_admin_ui.py`
    - [x] Test Stream Admin tab creation
    - [x] Test OAuth status display
    - [x] Test metadata management UI
    - [x] Test moderation controls UI
    - [x] Test analytics display
- [x] Task: Extract stream admin UI code from app.py
    - [x] Create `ui/stream_admin_ui.py` with `StreamAdminUI` class
    - [x] Move Stream Admin tab creation
    - [x] Move OAuth status and controls
    - [x] Move metadata, moderation, analytics UI
    - [x] Move callback handlers (with proper error logging)
    - [x] Update `app.py` to use `StreamAdminUI`
    - [x] Verify tests pass
- [x] Task: Conductor - User Manual Verification 'Phase 7: Extract Stream Admin UI' (Protocol in workflow.md)

## Phase 8: Extract Advanced Mode Panel [checkpoint: pending]

- [x] Task: Write tests for advanced panel
    - [x] Create `tests/test_advanced_panel.py`
    - [x] Test log viewer (toggle, display, monospaced font)
    - [x] Test debug controls visibility
    - [x] Test manual actions panel
- [x] Task: Extract advanced panel code from app.py
    - [x] Create `ui/advanced_panel.py` with `AdvancedModePanel` class
    - [x] Move log viewer (toggle, display, truncation)
    - [x] Move debug controls
    - [x] Move manual actions panel
    - [x] Update `app.py` to use `AdvancedModePanel`
    - [x] Verify tests pass
- [x] Task: Conductor - User Manual Verification 'Phase 8: Extract Advanced Panel' (Protocol in workflow.md)

## Phase 9: AppShell Composition + Cleanup [checkpoint: pending]

- [x] Task: Refactor app.py into thin AppShell
    - [x] Reduce `app.py` to <200 lines with only `VocalAIApp` (AppShell) class
    - [x] Compose all extracted panels in `__init__`
    - [x] Wire up state observer subscriptions
    - [x] Wire up panel inter-communication through state
    - [x] Fix all lambda closure capture bugs (use functools.partial)
    - [x] Remove all direct access to private attributes of other modules
    - [x] Verify `main.py` works without changes
- [x] Task: Integration tests
    - [x] Create `tests/test_integration.py`
    - [x] Test full app initialization
    - [x] Test panel composition and layout
    - [x] Test state propagation across panels
    - [x] Test shutdown sequence (on_closing)
- [x] Task: Final coverage verification
    - [x] Run `pytest tests/ -v --cov=ui --cov-report=term-missing`
    - [x] Verify >80% coverage for all `ui/` modules
    - [x] Fix any coverage gaps
- [x] Task: Conductor - User Manual Verification 'Phase 9: AppShell Composition' (Protocol in workflow.md)

## Phase 10: Documentation + Memory [checkpoint: pending]

- [x] Task: Update documentation
    - [x] Update `README.md` with new UI module structure
    - [x] Update `docs/SDD_SKILLS_USAGE.md` if needed
    - [x] Update `conductor/tech-stack.md` if needed
    - [x] Create `docs/UI_ARCHITECTURE.md` with module diagram and responsibilities
- [x] Task: Save Engram memories
    - [x] Save architecture decision con topic_key `architecture/ui-module-structure`
    - [x] Save patterns discovered con topic_key `pattern/ui-state-management`
    - [x] Save any bugfixes with appropriate topic_keys
    - [x] Save session summary with full accomplishments
