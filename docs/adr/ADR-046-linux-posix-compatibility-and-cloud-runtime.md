# ADR-046: Linux/POSIX Compatibility, Tauri Cross-Platform Shell, and Cloud LLM Runtime

**Date**: 2026-08-19  
**Status**: Accepted  
**Branch**: `feat/linux-support` (OpenCohost and OpenCohost_UI)  
**Author**: Antigravity  
**Scope**: Backend (`opencohost/`), Frontend Tauri Shell (`OpenCohost_UI/`), and Linux Packaging.

---

## Context

OpenCohost was developed primarily on Windows with native Windows dependencies (`msvcrt`, `CREATE_NO_WINDOW`, `CREATE_NEW_PROCESS_GROUP`, `pynput` Windows mouse button identifiers `x1`/`x2`, and hardcoded PowerShell scripts for Tauri development).

Running OpenCohost on Linux (e.g. Pop!_OS / Ubuntu) and executing purely in Cloud mode (NVIDIA NIM / OpenAI) without local Ollama revealed four architectural seams:

1. **POSIX File Locking and OS Process Flags**: `engine_host.py` relied solely on `msvcrt.locking`, and `health_monitor.py` referenced Windows-only subprocess creation flags.
2. **Tauri Backend Process Spawning**: `backend.rs` hardcoded execution of `"python"` without resolving the virtual environment path (`.venv/bin/python3` on Unix) or checking system `python3` in PATH.
3. **Cloud-Only `is_ready` Initialization and Transitions**: When Ollama was not running, `_check_ollama_service` left `self.is_ready = False`. Switching to a cloud provider via `PUT /api/llm/provider` did not update `self.is_ready` to `True` in memory, causing subsequent direct chat messages (`process_context`) to be dropped with `ollama_unavailable` errors.
4. **Dev Server Process Cleanup**: Terminating `tauri dev` on Linux left orphan `uvicorn` processes bound to ports `8765`/`8770`, which shadowed newer code across dev restarts.

---

## Decisions

### 1. POSIX File Locking and OS Guards in Python Backend
- In `opencohost/api/engine_host.py`: Added `fcntl.flock` for POSIX platforms with seamless fallback to `msvcrt.locking` on Windows.
- In `opencohost/core/observability/health_monitor.py`: Safe `getattr` for `subprocess.CREATE_NO_WINDOW`, `subprocess.CREATE_NEW_PROCESS_GROUP`, and `signal.CTRL_BREAK_EVENT` with `SIGTERM` fallback on non-Windows hosts.
- In `opencohost/ui/ptt_manager.py`: Added cross-platform button mapping (`button8`/`button9` on Linux, `x1`/`x2` on Windows).

### 2. Intelligent Python Binary Resolution in Tauri Shell (`backend.rs`)
- Implemented `resolve_python_binary` in `OpenCohost_UI/src-tauri/src/backend.rs`:
  1. Checks if `python_path` exists relative to `working_dir` (e.g. `.venv/bin/python3` on Linux or `.venv/Scripts/python.exe` on Windows).
  2. Detects local virtualenv `.venv` in the repository root automatically.
  3. Falls back to checking `python3` in `PATH` on non-Windows hosts when `python` is not available.

### 3. Cloud LLM Ready State Synchronization
- In `opencohost/core/llm_engine.py`: Initialized `self.is_ready = not self._is_local` so cloud configurations start ready without requiring local Ollama.
- In `opencohost/core/engine/llm_engine_cloud.py`: In `set_provider_config()`, when the incoming provider is cloud (`incoming_provider != "local"`), `self.is_ready` is immediately set to `True`.

### 4. Cross-Platform Scripts and Stale Port Purge
- Replaced Windows-only PowerShell commands in `package.json` with Node.js runners: `scripts/predev.js` and `scripts/tauri-debug.js`.
- `scripts/predev.js` now terminates any orphan processes listening on ports `1420`, `8765`, and `8770` prior to launching dev servers.
- Enabled `deb` and `appimage` bundle targets in `src-tauri/tauri.conf.json`.

---

## Verification

1. **Python Unit & Integration Test Suites**:
   - `pytest tests/test_llm_tiers.py tests/test_health_monitor.py tests/test_model_panel.py`: 192 passed.
   - `pytest tests/test_api_*.py`: 557 passed (100% pass rate).
2. **Rust Test Suite**:
   - `cargo test --manifest-path OpenCohost_UI/src-tauri/Cargo.toml`: 37 passed, 0 failed.
3. **Live Runtime Validation**:
   - Tauri UI launched cleanly on Linux Pop!_OS.
   - NVIDIA NIM model `nvidia/nemotron-3-ultra-550b-a55b` executed live chat responses with streaming dialogue.
   - Agenda mode generated 6/6 prefetch hits with 4-5ms turn gap.
