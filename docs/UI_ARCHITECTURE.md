# VoiceAI UI Architecture

## Overview

The VoiceAI UI has been refactored from a 2722-line God class (`ui/app.py`) into a modular architecture with 12 modules, totaling ~3500 lines across well-defined boundaries.

## Module Diagram

```
ui/app.py (6 lines)
    │
    └── re-exports create_vocalai_app from:
            │
            ▼
    ui/app_shell.py (~1375 lines) ──── Composition Root
            │
            ├── ui/state.py (245 lines) ────────────── UIState container
            ├── ui/protocols.py (68 lines) ─────────── CallbackDispatcher + Protocols
            ├── ui/ptt_manager.py (246 lines) ──────── PTT hotkey management
            ├── ui/voice_control.py (277 lines) ────── WebSocket, audio, state machine
            ├── ui/model_panel.py (234 lines) ──────── Model selection panel
            ├── ui/profile_panel.py (83 lines) ─────── Profile selection panel
            ├── ui/status_bar.py (95 lines) ────────── Status pills + observer
            ├── ui/smart_aggregator_ui.py (222 lines) ─ YouTube chat, vibe, activity
            ├── ui/stream_admin_ui.py (613 lines) ──── OAuth, metadata, moderation
            └── ui/advanced_panel.py (155 lines) ───── Log viewer, debug controls
```

## Module Responsibilities

### `ui/app.py` (6 lines)
**Responsibility**: Thin re-export layer for backward compatibility.
- Exports `create_vocalai_app` from `app_shell`
- No logic, no imports beyond the re-export

### `ui/app_shell.py` (~1375 lines)
**Responsibility**: Composition root and widget wiring layer.
- Creates the main application window
- Instantiates all panel modules
- Wires callbacks between modules via `CallbackDispatcher`
- Manages layout and geometry
- Handles application lifecycle (startup, shutdown, cleanup)

### `ui/state.py` (245 lines)
**Responsibility**: Thread-safe UI state container with observer pattern.
- `UIState` class with typed properties (connection status, model state, PTT state, etc.)
- Internal `threading.Lock` for all property access
- Observer pattern: `subscribe(key, callback)` / `unsubscribe(key, callback)`
- `notify_observers(key)` triggers callbacks on state changes
- All property getters/setters are lock-protected

### `ui/protocols.py` (68 lines)
**Responsibility**: Protocol classes and callback dispatcher.
- `CallbackDispatcher`: Centralized error-handling for all UI callbacks
  - Replaces `try/except: pass` with proper error logging
  - `dispatch(name, callback, *args, **kwargs)` wraps callback execution
  - Logs errors with full tracebacks instead of silently swallowing them
- Protocol classes define interfaces for inter-module communication

### `ui/ptt_manager.py` (246 lines)
**Responsibility**: Push-to-Talk hotkey and keyboard listener management.
- Registers/unregisters global hotkeys
- Manages keyboard listener lifecycle
- Coordinates with `voice_control` for PTT state transitions
- Thread-safe PTT state tracking

### `ui/voice_control.py` (277 lines)
**Responsibility**: WebSocket communication, audio recording, RMS calculation, state machine.
- Audio input/output management
- WebSocket connection to voice backend
- RMS (Root Mean Square) calculation for voice activity detection
- State machine for recording/playback/idle states
- Thread-safe audio buffer management
- All UI updates scheduled via `schedule_ui_update`

### `ui/model_panel.py` (234 lines)
**Responsibility**: Model selection, Ollama status, model download UI.
- Displays available models
- Shows Ollama connection status
- Model download progress tracking
- Model activation/deactivation

### `ui/profile_panel.py` (83 lines)
**Responsibility**: Profile selection and editor integration.
- Profile dropdown/list
- Profile creation/editing integration
- Profile persistence

### `ui/status_bar.py` (95 lines)
**Responsibility**: Status pills and UIState observer.
- Displays connection status, model status, PTT status as visual pills
- Subscribes to `UIState` changes for automatic updates
- Color-coded status indicators

### `ui/smart_aggregator_ui.py` (222 lines)
**Responsibility**: YouTube chat connection, vibe detection, activity triggers.
- YouTube chat WebSocket connection
- Vibe/sentiment analysis display
- Activity trigger management
- Chat message aggregation

### `ui/stream_admin_ui.py` (613 lines)
**Responsibility**: OAuth flow, stream metadata, moderation, analytics.
- OAuth authentication flow
- Stream title/category management
- Moderation controls
- Analytics display
- Stream health monitoring

### `ui/advanced_panel.py` (155 lines)
**Responsibility**: Log viewer, debug controls, action logging.
- Real-time log display
- Debug toggle controls
- Action history
- Diagnostic tools

## State Flow Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                        UIState                               │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────┐  │
│  │connection_  │  │model_state   │  │ptt_state           │  │
│  │status       │  │              │  │                    │  │
│  └──────┬──────┘  └──────┬───────┘  └────────┬───────────┘  │
│         │                │                    │              │
│  ┌──────┴──────┐  ┌──────┴───────┐  ┌────────┴───────────┐  │
│  │observers:   │  │observers:    │  │observers:          │  │
│  │status_bar   │  │model_panel   │  │ptt_manager         │  │
│  │voice_ctrl   │  │status_bar    │  │voice_control       │  │
│  └─────────────┘  └──────────────┘  └────────────────────┘  │
│                                                              │
│  Thread-safe via threading.Lock on all property access       │
│  notify_observers(key) → dispatches to all subscribers       │
└──────────────────────────────────────────────────────────────┘
         ▲                    ▲                    ▲
         │                    │                    │
    set_property()       set_property()       set_property()
         │                    │                    │
┌────────┴────────┐  ┌───────┴────────┐  ┌────────┴────────┐
│ voice_control   │  │ model_panel    │  │ ptt_manager     │
│ (background     │  │ (UI thread)    │  │ (listener       │
│  threads)       │  │                │  │  thread)        │
└─────────────────┘  └────────────────┘  └─────────────────┘
```

## Callback Wiring Diagram

```
app_shell.py (Composition Root)
    │
    ├── Creates UIState ──────────────────────────────────┐
    │                                                      │
    ├── Creates CallbackDispatcher                         │
    │                                                      │
    ├── Instantiates panels:                               │
    │   ├── VoiceControl(state, dispatcher, ...)           │
    │   ├── PTTManager(state, dispatcher, ...)             │
    │   ├── ModelPanel(state, dispatcher, ...)             │
    │   ├── ProfilePanel(state, dispatcher, ...)           │
    │   ├── StatusBar(state) ──── subscribes to UIState    │
    │   ├── SmartAggregatorUI(state, dispatcher, ...)      │
    │   ├── StreamAdminUI(state, dispatcher, ...)          │
    │   └── AdvancedPanel(state, dispatcher, ...)          │
    │                                                      │
    ├── Wires callbacks:                                   │
    │   ├── dispatcher.on("ptt_pressed", voice_control.start_recording)
    │   ├── dispatcher.on("ptt_released", voice_control.stop_recording)
    │   ├── dispatcher.on("model_selected", model_panel.activate_model)
    │   ├── dispatcher.on("connection_changed", status_bar.update)
    │   └── ...                                            │
    │                                                      │
    └── Cleanup:                                           │
        ├── Unsubscribe all UIState observers              │
        ├── Stop all listeners                             │
        └── Close all connections                          │
```

## Thread Safety Rules

### Rule 1: UIState is the Single Source of Truth
- All shared state flows through `UIState`
- Direct property access between modules is forbidden
- Use `state.get("key")` and `state.set("key", value)`

### Rule 2: Background Threads Must Schedule UI Updates
- **Never** call tkinter methods from background threads
- Use `schedule_ui_update` callback (typically `self.after(0, fn)`)
- `voice_control.py` runs audio/WebSocket on background threads
- `ptt_manager.py` keyboard listener runs on separate thread
- All UI updates from these threads must be scheduled

### Rule 3: Lock All Shared State Access
- `UIState` properties use `threading.Lock` internally
- Getters and setters are atomic
- Compound operations (read-modify-write) must use explicit lock context

### Rule 4: Cleanup All Observers
- Every `subscribe()` must have a matching `unsubscribe()`
- Cleanup happens in module `cleanup()` or `destroy()` methods
- `app_shell.py` orchestrates cleanup on application shutdown

### Rule 5: CallbackDispatcher for All Callbacks
- Never call callbacks directly with `try/except: pass`
- Use `dispatcher.dispatch(name, callback, *args, **kwargs)`
- Errors are logged with full tracebacks
- Callback failures don't cascade to other callbacks

## Testing Strategy

### Coverage Targets
| Module | Target | Actual |
|---|---|---|
| `voice_control.py` | 95% | 97% |
| `smart_aggregator_ui.py` | 95% | 100% |
| `status_bar.py` | 95% | 100% |
| `profile_panel.py` | 95% | 100% |
| `state.py` | 95% | 95% |
| `stream_admin_ui.py` | 90% | 93% |
| `advanced_panel.py` | 90% | 92% |
| `protocols.py` | 95% | 96% |

### Test Categories

1. **Unit Tests**: Each module tested in isolation with mocked dependencies
2. **Integration Tests**: Callback wiring and state flow between modules
3. **Thread Safety Tests**: Concurrent access to `UIState` properties
4. **Observer Pattern Tests**: Subscribe/unsubscribe/notify lifecycle

### Test Structure
```
tests/
├── test_state.py              # UIState thread safety, observers
├── test_protocols.py          # CallbackDispatcher error handling
├── test_ptt_manager.py        # Hotkey registration, lifecycle
├── test_voice_control.py      # Audio, WebSocket, state machine
├── test_model_panel.py        # Model selection, download
├── test_profile_panel.py      # Profile CRUD
├── test_status_bar.py         # Status pill rendering, observer
├── test_smart_aggregator_ui.py # YouTube chat, vibe
├── test_stream_admin_ui.py    # OAuth, metadata, moderation
├── test_advanced_panel.py     # Log viewer, debug
├── test_app_shell.py          # Composition, wiring
└── test_integration.py        # Cross-module integration
```

### Running Tests
```bash
pytest tests/ -v --cov=ui --cov-report=term-missing
```

## Key Decisions

### Why UIState Over Scattered Variables?
- Previous implementation had unsynchronized variables scattered across the God class
- Race conditions occurred when background threads updated UI state
- `UIState` centralizes state with lock-protected access and observer notifications

### Why CallbackDispatcher?
- Previous implementation used `try/except: pass` everywhere
- Errors were silently swallowed, making debugging impossible
- `CallbackDispatcher` logs all errors with full tracebacks
- Centralized error handling enables consistent error reporting

### Why Extract Incrementally?
- Big-bang refactoring of 2722 lines is error-prone
- Each module was extracted with its own tests before moving to the next
- TDD approach: write tests for the module's behavior, then extract
- Verification pass after each extraction catches agent-introduced bugs

## Migration Notes

### From God Class to Modular
1. `ui/app.py` → thin re-export (6 lines)
2. Original logic → `ui/app_shell.py` (composition root)
3. State variables → `ui/state.py` (UIState container)
4. Callback patterns → `ui/protocols.py` (CallbackDispatcher)
5. PTT logic → `ui/ptt_manager.py`
6. Voice/WebSocket → `ui/voice_control.py`
7. Model UI → `ui/model_panel.py`
8. Profile UI → `ui/profile_panel.py`
9. Status display → `ui/status_bar.py`
10. Aggregator UI → `ui/smart_aggregator_ui.py`
11. Stream admin → `ui/stream_admin_ui.py`
12. Advanced/debug → `ui/advanced_panel.py`

### Backward Compatibility
- `ui/app.py` re-exports `create_vocalai_app` for existing imports
- No changes needed to `main.py` or other entry points
- All public APIs preserved through composition root
