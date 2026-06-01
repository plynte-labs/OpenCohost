# Exploration — First-run Readiness Wizard

## Decision

VoiceAI should treat Ollama as an external dependency, not bundle it. The app may detect, guide, start, and diagnose Ollama, but the user installs/owns Ollama separately.

## Current Behavior

- `ModelPanel._detectar_estado_ollama()` checks Python package, HTTP `127.0.0.1:11434/api/tags`, and common Windows binary locations.
- `ModelPanel._iniciar_ollama()` starts `ollama serve` hidden with `OLLAMA_MODELS` set from `config.settings.OLLAMA_MODELS_DIR`.
- The starter hides stdout/stderr, does not keep the process handle, waits only 10 seconds, and cannot explain slow startup or process death.
- `config/storage.py` supports `storage.yaml` and respects external `OLLAMA_MODELS` when configured as auto.
- If Ollama is already running, changing `OLLAMA_MODELS` in VoiceAI cannot change that running process; the user must restart Ollama.

## External Facts / Constraints

- Ollama uses local API `http://localhost:11434/api`.
- Windows default model path is typically `%USERPROFILE%\.ollama\models`.
- `OLLAMA_MODELS` can move models to another disk, but must be set before Ollama starts.
- Useful Windows logs include `%LOCALAPPDATA%\Ollama\server.log` and `%LOCALAPPDATA%\Ollama\app.log`.
- If the tray app is already using port `11434`, starting another `ollama serve` should be treated as “already active” or port conflict, not an opaque failure.

## Hardware Readiness Heuristics

Use VRAM/RAM as guidance, not promises:

- No GPU / less than 8 GB RAM: limited experience; recommend small 1B–3B models and warn about latency.
- GTX 1060 class: usually 3–6 GB VRAM; recommend 3B or very quantized 7B with short context.
- RTX 3060 class: often 8–12 GB VRAM; practical for 7B–8B quantized models.
- 16 GB+ VRAM: larger 14B-class quantized models become more realistic.
- Unknown GPU: continue with conservative recommendations.

## Proposed Architecture

- `core/readiness.py`: pure/mockable readiness checker for Ollama package, binary, API, model path, RAM/GPU/VRAM, disk space, and lightweight model probe.
- `ui/readiness_wizard.py`: CustomTkinter first-run wizard/panel with progressive checks.
- `config/first_run.py`: persist local `setup_completed` and last readiness decisions outside tracked config.
- `ModelPanel`: eventually consumes shared readiness checker instead of duplicating detection.
- `AppShell`: decides whether to show wizard on startup and provides a way to reopen it.

## Key UX Principles

- “Ready” must mean the API responds and the selected/recommended model can answer a minimal probe, not merely that a process exists.
- Long startup must show progress/diagnostics, not silent shadow failure.
- The wizard should explain custom model folders and warn when Ollama must be restarted for `OLLAMA_MODELS` changes.
- Diagnostics must avoid raw prompts/chats and should show counts, paths, ports, versions, and sanitized errors.

## Non-goals

- Do not bundle Ollama.
- Do not automatically migrate model files between disks.
- Do not kill user-owned Ollama processes.
- Do not solve Qwen3-TTS hardware readiness in this first slice unless needed for messaging.
