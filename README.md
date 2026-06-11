# OpenCohost — Local-First AI Streaming Co-Host

OpenCohost is a local-first AI streaming co-host platform. The core product is **Kira**, an AI co-host with a defined personality (dry sarcasm, sharp humor) that runs entirely on your hardware using Ollama and local TTS models. No cloud subscriptions. No latency spikes mid-stream.

> Spanish version: [README.es.md](README.es.md)

## What Kira Does

- Listens to your stream via a local WebSocket transcription feed
- Responds via voice using local LLM inference (Ollama) and local TTS
- Maintains a sliding conversation window across a stream session
- Switches between a lightweight TTS engine (Edge-TTS, 0% GPU) and a high-quality local voice engine (Qwen3-TTS zero-shot cloning)
- Monitors its own health: if TTS becomes unavailable, Kira degrades gracefully rather than crashing

## Features

| Feature | Status |
|---|---|
| Manual chat input with voice response | Stable |
| Push-to-talk with global hotkey | Stable |
| Lightweight TTS (Edge-TTS) | Stable |
| Local TTS (Qwen3-TTS zero-shot cloning) | Stable |
| Piper offline TTS fallback | Stable |
| WebSocket live transcription feed | Stable |
| Streaming TTS pipeline (speaks before LLM finishes) | Stable |
| Conversational memory (10-turn sliding window) | Stable |
| Personality profiles (editable from UI) | Stable |
| LLM model catalog with one-click switching | Stable |
| Ollama lifecycle management from the UI | Stable |
| Smart Chat Aggregator (YouTube Live) | Stable |
| Stream Admin panel (YouTube OAuth, Twitch placeholder) | MVP |
| Health monitor with TTS fallback gate | Stable |
| Compact mode for second-monitor streaming | Stable |

## Requirements

### Hardware

| Tier | GPU VRAM | Use case |
|---|---|---|
| Minimum | 6 GB | Edge-TTS only; Ollama on a smaller model |
| Recommended | 12 GB | Concurrent LLM + Qwen3-TTS |
| Ideal | 24 GB | Dedicated GPU server; Ollama + full TTS without contention |

### Software

- Python 3.13 (activated conda or venv environment)
- [Ollama](https://ollama.com/) installed and running
- `customtkinter`, `ollama`, `sounddevice`, `soundfile`, `numpy`, `websockets`, `requests`, `pygame`, `edge-tts`, `pytchat` (main environment)
- `flask`, `torch`, `torchaudio`, `soundfile`, `qwen-tts`, `edge-tts` (TTS server environment, optional — only if using Qwen3-TTS)

## Setup

> These commands assume your Python environment is already activated. Replace `python` with the path to your environment's interpreter if you are not in an activated shell.

```powershell
# Install main dependencies
python -m pip install -r requirements.txt

# (Optional) Install TTS server dependencies in a separate environment
# python -m pip install flask torch torchaudio soundfile qwen-tts edge-tts
```

### Run

```powershell
# Terminal 1 — main app (LLM + UI + audio pipeline)
python main.py

# Terminal 2 — Qwen3-TTS server (only needed for high-quality local voice cloning)
# Run this in the environment that has torch + qwen-tts installed
python server_qwen.py
```

Kira's voice will use Edge-TTS (lightweight, online) if the Qwen3-TTS server is not available.

### Configure TTS Python path

If you are using Qwen3-TTS, set the environment variable `XTTS_PYTHON` to the Python interpreter in your TTS environment:

```powershell
$env:XTTS_PYTHON = "path/to/your/tts-env/python.exe"
python main.py
```

Or set `tools.xtts_python` in `config/storage.yaml`.

### Configure storage paths

To move Ollama models, cache, or temp files to a different drive, edit `config/storage.yaml`:

```yaml
storage:
  # cache_root: "/path/to/your/cache"
  # temp_root: "/path/to/your/temp"
  # ollama_models: "/path/to/ollama/storage"
```

## Architecture

```
OpenCohost/
├── main.py               # Entry point
├── server_qwen.py        # Multi-engine TTS server (Flask)
├── config/
│   ├── logger.py         # Structured logging (console + rotating files)
│   ├── settings.py       # Constants, model catalog, system prompt
│   ├── storage.py        # Portable path resolution for cache/temp
│   └── storage.yaml      # Storage path overrides
├── core/
│   ├── llm_engine.py     # LLM orchestration, memory, TTS pipeline
│   ├── health_monitor.py # Service health, TTS fallback gate
│   └── profiles.py       # Personality profile load/save
├── ui/
│   ├── app_shell.py      # Main UI shell (thread-safe UIState observer)
│   └── model_panel.py    # Model management panel
├── smart_aggregator/     # YouTube Live Chat aggregator (RF3)
├── stream_admin/         # Stream Admin panel: OAuth, metadata, moderation (RF4)
└── config/
    ├── default_profiles.json  # Seeded personality profiles
    └── stream_admin.yaml      # Stream Admin configuration
```

## Personality Profiles

On first run, OpenCohost seeds a set of default personality profiles into your local `perfiles.json` (ignored by git):

| Profile | Persona |
|---|---|
| Akira | Default co-host — balanced and sharp |
| Akira (Learn) | Learning mode — educational and encouraging |
| Comunidad | Community mode — warm and inclusive |
| Calmado | Calm mode — slower pace, grounded tone |
| Técnico | Technical mode — precise, dry, cynical |
| Show | High-energy performance mode |

Kira's name and base personality are preserved across all profiles.

## Ollama Integration

The app validates Ollama at startup and disables actions that depend on the LLM if the service is unavailable. The model panel provides a single contextual button:

- **Install Ollama** — opens the official download page when the binary is not found
- **Start Ollama** — runs `ollama serve` when installed but not responding
- **Download model** — pulls the selected model when Ollama is ready but the model is missing locally
- **Activate model** — switches to the selected model when already installed
- **Install Python dependency** — alerts when the `ollama` Python package is missing from the active environment

The service is checked at `http://127.0.0.1:11434/api/tags`. If Ollama becomes unavailable during a session, the engine marks itself as not ready and blocks processing, model switching, and downloads until the service responds again.

## Stream Admin (RF4)

The Stream Admin panel supports YouTube OAuth/API for reading and writing chat, metadata, moderation, and analytics. A Twitch provider placeholder is included. OAuth credentials are stored in `data/stream_admin/oauth_client.json` (gitignored). Environment variables `YOUTUBE_OAUTH_CLIENT_ID` and `YOUTUBE_OAUTH_CLIENT_SECRET` are also supported.

## Testing

```powershell
# Full test collection
python -m pytest --collect-only -q

# Focused model + recovery suite
python -m pytest tests/test_llm_tiers.py tests/test_model_panel.py tests/test_heavy_model_inference_recovery.py -q

# Health monitor suite
python -m pytest tests/test_health_monitor.py tests/test_health_integration.py tests/test_app_shell_obs_resilience.py -q
```

See [docs/TESTING.md](docs/TESTING.md) for the full test surface.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE) for the full text.
