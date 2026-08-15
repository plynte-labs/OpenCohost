# OpenCohost — Quick Start

Get Kira, your AI streaming co-host, running on the Tauri desktop app.

---

## Prerequisites

### Engine

| Requirement | Notes |
|---|---|
| **Windows 10 / 11** | Primary supported platform. Linux/macOS community-supported; clean-machine validation in progress. |
| **Python 3.10 or later** | `pyproject.toml` sets `requires-python = ">=3.10"`. [python.org/downloads](https://www.python.org/downloads/) |
| **Ollama** | Local LLM runtime. Install from [ollama.com](https://ollama.com/) and keep it running. |
| **Internet access** | Required for Edge-TTS (the default voice) and for pulling models. |

### Desktop app

The product UI is a Tauri 2 shell, so it needs a Node/Rust toolchain on top of the above:

| Requirement | Notes |
|---|---|
| [Node.js](https://nodejs.org/) | Any current LTS |
| [pnpm](https://pnpm.io/) `11.5.2` | Pinned via `packageManager` in `OpenCohost_UI/package.json`; `corepack enable` picks it up automatically |
| [Rust + Cargo](https://rustup.rs/) | Unpinned — any recent stable toolchain works |
| [WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/) | Windows 11 ships it; install manually on Windows 10 |
| MSVC Build Tools (C++ workload) | Required for the `rustc` MSVC target on Windows — install via [Visual Studio Build Tools](https://visualstudio.microsoft.com/visual-cpp-build-tools/) |

> **Privacy note.** With the shipped defaults, viewer chat, prompts, LLM context, and
> conversation memory stay on your machine — inference runs against local Ollama over
> loopback. Two things do cross the network:
>
> - **Microsoft Edge-TTS** receives Kira's outgoing spoken text, in sentence-sized
>   fragments, for voice synthesis. Install the `local-tts` extra and switch to Piper
>   in **Controls → Kira's voice** to remove that call entirely.
> - **Twitch IRC**, once you connect a chat source: OpenCohost sends an anonymous nick
>   (`justinfan…`), a channel JOIN, and PONG keepalives. No viewer data is uploaded,
>   but the connection itself is outbound — "fully local" means no chat, prompts, or
>   memory leave, not that the process never opens a socket.
>
> One opt-in changes the picture materially: pointing inference at an **OpenAI-compatible
> cloud LLM provider** sends the complete prompt — persona, saved memorias, and the
> filtered viewer-chat context — to that provider. It is off by default
> (`active_provider` is `"local"`). Full detail in [PRIVACY.md](PRIVACY.md).

---

## 1. Start Ollama

Open a terminal and start the Ollama daemon if it is not already running:

```powershell
ollama serve
```

Leave that terminal open (or let Ollama run as a system service).

---

## 2. Pull a language model

OpenCohost works with any Ollama-compatible model. A fast starting point:

```powershell
ollama pull llama3
```

Other well-tested options: `qwen3:4b`, `qwen3:1.7b`, `phi4-mini`, `gemma4:e4b`.

OpenCohost never downloads models itself — it lists what Ollama already has. Adding
and removing models is always `ollama pull` / `ollama rm`.

---

## 3. Clone the repo and install the engine

The Tauri front end is its own repository, wired in as a submodule at `OpenCohost_UI/`.
A plain `git clone` leaves that directory empty and step 4 has nothing to `cd` into.

```powershell
git clone --recursive https://github.com/plynte-labs/opencohost.git
cd opencohost

# already cloned without --recursive?
git submodule update --init --recursive

# Create and activate a virtual environment (recommended)
python -m venv .venv
.venv\Scripts\activate

# Install with Edge-TTS voice, platform integrations, and the HTTP API
pip install -e ".[cloud-tts,integrations,api]"
```

The `api` extra is **not** optional in practice: it installs FastAPI and uvicorn, which
are what the desktop app drives Kira through.

**Extras:**

| Extra | What it adds |
|---|---|
| `cloud-tts` | Microsoft Edge-TTS (free cloud voice — the default) |
| `api` | FastAPI + uvicorn — the HTTP API the desktop app spawns and talks to. Required to run the product and to collect the test suite. |
| `integrations` | OBS WebSocket, NVIDIA VRAM monitor |
| `local-tts` | Piper TTS — fully offline voice synthesis |
| `youtube-chat` | Unofficial YouTube live chat — opt-in, [read this first](PRIVACY.md#youtube-live-chat-is-opt-in-and-unofficial) |
| `dev` | pytest, pre-commit, detect-secrets (contributors only) |

To add offline Piper TTS as well:

```powershell
pip install -e ".[cloud-tts,integrations,api,local-tts]"
```

---

## 4. Run OpenCohost

```powershell
cd OpenCohost_UI
pnpm install
pnpm tauri:debug
```

That is the whole launch. Nothing else needs to be running: on startup the Tauri shell
probes ports 8765 and 8770, reuses a healthy backend if one answers, and otherwise
spawns `python -m uvicorn opencohost.api.main:app` itself and kills it again on exit.

Two things to know on a first run:

- **It compiles the Rust shell**, so the first `pnpm tauri:debug` takes noticeably
  longer than every run after it.
- **Run it from the shell where your Python environment is activated.** The tracked
  `src-tauri/backend.config.default.json` sets `"python_path": "python"`, resolved from
  `PATH`. To pin a specific interpreter instead, create your own
  `src-tauri/backend.config.json` (gitignored) — see
  [`OpenCohost_UI/README.md`](../OpenCohost_UI/README.md).

The window blocks on a health check until the backend answers `GET /api/health` with a
live engine, then the app opens.

---

## 5. Verify voice works

1. **Pick a model** — go to **Controls → Model** and choose the model you pulled
   (e.g. `llama3`) under **Active Model**.
2. **Check the voice** — in **Controls → Kira's voice**, confirm the TTS engine is
   **Light (Edge-TTS)**, or tick **Local TTS only (Piper)** if you installed the
   `local-tts` extra.
3. **Say something** — go to **Experience**, type into the composer and press **Send**,
   or hold the push-to-talk button in the transport bar. Kira should answer with text
   and audio.

If you hear silence or see a TTS error:

- Check that your system default audio output is set correctly.
- Confirm the TTS engine selection in **Controls → Kira's voice**.
- Edge-TTS needs an active internet connection — if you are offline, install `local-tts`
  and switch to Piper.

---

## Platform integrations (optional)

| Feature | What to do |
|---|---|
| **Twitch chat** (default) | Enter your channel name in **Stream**. OpenCohost connects anonymously (read-only). No extra, no credentials. |
| **YouTube live chat** (opt-in) | Install the `youtube-chat` extra first, then paste your live stream URL or video ID. It uses an unofficial endpoint that YouTube's Terms of Service do not permit — [read PRIVACY.md](PRIVACY.md#youtube-live-chat-is-opt-in-and-unofficial) before enabling it. |
| **OBS avatar** | Install the OBS WebSocket plugin, enable it on port 4455, and enter the password in **Controls → OBS**. |

---

## Logs and data

Where files land depends on how OpenCohost was installed.

**Editable install (`pip install -e`, what this guide describes):** everything is written
to the repository root — `opencohost/config/storage.py::get_user_data_dir` returns the
repo root whenever the process is not a frozen build.

| What | Path |
|---|---|
| Runtime log | `logs\opencohost_<YYYYMMDD_HHMMSS>.log` |
| Action event log | `logs\acciones.jsonl` |
| Config (model selection, TTS preferences, window layout, …) | `config\` |
| Personality profiles | `perfiles.json` |
| Cache and TTS temp files | `modelos_f5\`, `temp\` (override in `opencohost/config/storage.yaml`) |

**Packaged (frozen) build:** the same tree moves under a per-user directory —
`%APPDATA%\OpenCohost\` on Windows, `~/.config/OpenCohost/` elsewhere. Logs are not
special-cased: `LOG_DIR` is that directory plus `logs`
(`opencohost/config/settings.py`).

To enable verbose logging, set `OPENCOHOST_DEBUG=1` before launching. `pnpm tauri:debug`
already sets it — but only for a backend it spawns itself, so a backend you started
separately will not pick it up.

---

## Troubleshooting

**Ollama not detected on startup**
Confirm `ollama serve` is running and reachable at `http://127.0.0.1:11434`. The engine
checks `/api/tags`; LLM features stay disabled until it responds.

**No audio / TTS silent**
Edge-TTS requires an active internet connection. If you are offline, install `local-tts`
and switch to Piper in **Controls → Kira's voice**.

**Model not listed in the app**
Pull it first with `ollama pull <name>`. The catalog only reflects what Ollama reports —
OpenCohost does not download models.

**`pnpm tauri:debug` warns that something is already listening on 8765/8770**
Tauri will reuse that backend instead of spawning its own, which means `OPENCOHOST_DEBUG=1`
never reaches it. Close the other backend first if you need debug logs.

For more detail, see [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## Next steps

- [docs/KIRA_COHOST_AGENDA_MODE.md](KIRA_COHOST_AGENDA_MODE.md) — configure Kira's agenda and personality
- [docs/LIVE_SAFETY_CONTROLS.md](LIVE_SAFETY_CONTROLS.md) — viewer guardrails and content filters
- [docs/api-reference.md](api-reference.md) — the HTTP API the app drives
- [docs/TESTING.md](TESTING.md) — running the test suite

---

## Legacy: the CustomTkinter shell

`python -m opencohost` still opens the old CustomTkinter desktop shell. It was **frozen
as legacy on 2026-08-13**: kept around, not maintained, and not the product. Feature
flags armed by `EngineHost` — the composition root the Tauri app uses — never engage
there, so it is not a valid surface for evaluating OpenCohost. Use step 4.

---

*OpenCohost is MIT licensed. Contributions welcome — open an issue or pull request on GitHub.*
