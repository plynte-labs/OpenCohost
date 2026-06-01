# Specification — First-run Readiness Wizard

## Overview

Add a first-run readiness/setup flow that prepares a user’s machine for VoiceAI with Ollama as an external dependency. The wizard must detect installation/runtime status, model storage configuration, hardware capability, and whether a recommended model can respond to a minimal probe.

## Goals

- Prevent users from entering the main app with a confusing “Ollama/model not ready” state.
- Support Ollama installed on non-default paths and models stored on another disk through `OLLAMA_MODELS`/`storage.yaml` guidance.
- Give practical hardware-based model recommendations.
- Provide actionable diagnostics when startup is slow, shadowed, or fails.
- Keep checks lightweight and testable without real Ollama/GPU in unit tests.

## Functional Requirements

### FR1 — External Ollama Policy

- VoiceAI MUST treat Ollama as externally installed software.
- The wizard MAY open official install/download guidance.
- VoiceAI MUST NOT bundle or silently install Ollama in this track.

### FR2 — Readiness Checker

- A shared checker MUST report:
  - Python `ollama` package availability.
  - Ollama executable discovery.
  - API readiness at `127.0.0.1:11434`.
  - Effective models path and whether it came from `OLLAMA_MODELS`, `storage.yaml`, or default behavior.
  - Basic RAM and NVIDIA GPU/VRAM when detectable.
  - Disk/path writability for configured storage paths.
- The checker MUST be pure/mockable and usable by tests without touching real user storage.

### FR3 — Managed Startup Diagnostics

- Starting Ollama from VoiceAI MUST capture enough diagnostics to explain failure or slow startup.
- The startup wait MUST tolerate slow Windows startup better than the current 10-second fixed window.
- If port `11434` is already active, the UI MUST treat that as an existing service or conflict with a clear message.
- `stderr`/logs MUST NOT be discarded without an operator-visible diagnostic path.

### FR4 — Custom Model Folder Guidance

- The wizard MUST show the effective Ollama models path.
- If the user chooses a custom path, the wizard MUST explain that `OLLAMA_MODELS` must be active before Ollama starts.
- If Ollama is already running, the wizard MUST warn that changing the folder requires restarting Ollama.

### FR5 — Hardware Recommendation

- The wizard MUST classify hardware into practical buckets: low/no GPU, GTX 1060 class, RTX 3060 class, and higher/unknown.
- Recommendations MUST be based on detected RAM/VRAM where possible, not only GPU marketing names.
- Unknown detection MUST not block setup; it should fall back to conservative recommendations.

### FR6 — Model Probe

- “Ready” MUST require a minimal model probe for the selected/recommended model, unless the user explicitly skips with a warning.
- Empty response, timeout, missing model, and slow first response MUST produce distinct diagnostics.

### FR7 — First-run State

- Setup completion MUST be persisted locally outside tracked repo config.
- The wizard MUST be reopenable from the app after first run.

## Acceptance Criteria

- Unit tests cover all checker states without real Ollama/GPU.
- Starting Ollama has test coverage for slow startup, already-running service, and process-start failure diagnostics.
- Hardware detection tests cover RAM-only, `nvidia-smi` success, `nvidia-smi` missing, and unknown GPU.
- First-run wizard can be shown/skipped/reopened deterministically.
- No raw chat/prompt content is stored in diagnostics.

## Out of Scope

- Bundling Ollama.
- Automatic model migration between disks.
- Full packaging/installer implementation.
- Real GPU benchmarking.
- Qwen3-TTS readiness beyond basic messaging.

## Stress-first TDD Rule

Every implementation slice must start with a red test for one exact user failure mode, then the smallest implementation to pass, then a ghost-bug review for false-ready states, path conflicts, timeouts, and missing diagnostics.
