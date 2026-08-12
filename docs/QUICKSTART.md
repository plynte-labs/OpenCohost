# OpenCohost — Quick Start

Get Kira, your AI streaming co-host, running in about ten minutes.

---

## Prerequisites

| Requirement | Notes |
|---|---|
| **Windows 10 / 11** | Primary supported platform. Linux/macOS community-supported; clean-machine validation in progress. |
| **Python 3.10 or later** | 3.12 recommended. [python.org/downloads](https://www.python.org/downloads/) |
| **Ollama** | Local LLM runtime. Install from [ollama.com](https://ollama.com/) and keep it running. |
| **Internet access** | Required for Edge-TTS (default voice) and initial model download. |

> **Privacy note:** Viewer chat, prompts, LLM context, and conversation memory stay on your machine.
> The only data that leaves is Kira's outgoing spoken text, sent to Microsoft Edge-TTS (cloud) for synthesis.
> If that matters to you, install the `local-tts` extra and switch to Piper in Settings → Voice — then nothing leaves your machine.

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
The model panel inside the app can also download models for you — see step 5.

---

## 3. Clone the repo and install

```powershell
git clone https://github.com/plynte-labs/opencohost.git
cd opencohost

# Create and activate a virtual environment (recommended)
python -m venv .venv
.venv\Scripts\activate

# Install with Edge-TTS voice and platform integrations
pip install -e ".[cloud-tts,integrations]"
```

**Optional extras:**

| Extra | What it adds |
|---|---|
| `cloud-tts` | Microsoft Edge-TTS (free cloud voice — the default) |
| `local-tts` | Piper TTS — fully offline voice synthesis |
| `integrations` | OBS WebSocket, VRAM monitor |
| `youtube-chat` | Unofficial YouTube live chat — opt-in, [read this first](PRIVACY.md#youtube-live-chat-is-opt-in-and-unofficial) |
| `dev` | pytest, pre-commit, detect-secrets (contributors only) |

To add offline Piper TTS as well:

```powershell
pip install -e ".[cloud-tts,integrations,local-tts]"
```

---

## 4. Run OpenCohost

```powershell
python -m opencohost
```

If the installer put the environment `Scripts\` folder on your PATH, you can also run:

```powershell
opencohost
```

The GUI window opens. The status bar at the bottom will show whether Ollama is reachable.

---

## 5. Verify voice works

1. **Select a model** — in the Model panel, choose the model you pulled (e.g. `llama3`). Click **Activate**.
2. **Open Kira settings** — set your stream topic and persona style, or leave defaults.
3. **Click Speak** (or use Push-to-Talk) — Kira should respond with text and audio.

If you hear silence or see a TTS error:

- Check that your system default audio output is set correctly.
- Open **Settings → Voice** and confirm Edge-TTS is selected.
- Try toggling the speed slider to force a re-init.

---

## Platform integrations (optional)

| Feature | What to do |
|---|---|
| **Twitch chat** (default) | Enter your channel name. OpenCohost connects anonymously (read-only). No extra, no credentials. |
| **YouTube live chat** (opt-in) | Install the `youtube-chat` extra first, then paste your live stream URL or video ID in the Chat Source panel. It uses an unofficial endpoint that YouTube's Terms of Service do not permit — [read PRIVACY.md](PRIVACY.md#youtube-live-chat-is-opt-in-and-unofficial) before enabling it. |
| **OBS avatar** | Install the OBS WebSocket plugin, enable it on port 4455, and enter the password in Settings → OBS. |

---

## Logs and data

All app data is written locally to `%APPDATA%\OpenCohost\` on Windows:

- `logs\opencohost_<date>.log` — runtime log
- `logs\acciones.jsonl` — action event log
- `config\` — model selection, window layout, TTS preferences

To enable verbose logging, set the environment variable `OPENCOHOST_DEBUG=1` before launching.

---

## Troubleshooting

**Ollama not detected on startup**
Confirm `ollama serve` is running and accessible at `http://127.0.0.1:11434`. The app checks `/api/tags` on startup; LLM features are disabled until it responds.

**No audio / TTS silent**
Edge-TTS requires an active internet connection. If you are offline, install `local-tts` and switch to Piper in Settings → Voice.

**Model not listed in the UI**
Pull the model with `ollama pull <name>` first, then click **Refresh** in the Model panel. The app queries Ollama's model list at startup and on refresh.

For more detail, see [docs/TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## Next steps

- [docs/KIRA_COHOST_AGENDA_MODE.md](KIRA_COHOST_AGENDA_MODE.md) — configure Kira's agenda and personality
- [docs/LIVE_SAFETY_CONTROLS.md](LIVE_SAFETY_CONTROLS.md) — viewer guardrails and content filters
- [docs/TESTING.md](TESTING.md) — running the test suite

---

*OpenCohost is MIT licensed. Contributions welcome — open an issue or pull request on GitHub.*
