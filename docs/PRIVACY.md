# Privacy & Data Policy

**OpenCohost Lite** — local-first AI co-host platform  
Last updated: 2026-07-02

---

## Overview

OpenCohost is designed to keep your viewers' data and your streaming context on your own machine. This document describes exactly what stays local, what leaves your machine, and where data is stored. No legal boilerplate — just an accurate description of how data flows.

---

## What stays on your machine

All of the following are processed and stored **locally only** and are never transmitted to any remote server by OpenCohost:

| Data | Notes |
|---|---|
| Viewer chat messages | Read from YouTube/Twitch APIs; never forwarded anywhere |
| Viewer usernames | Same as above — used locally for context aggregation |
| Prompts sent to the LLM | Sent to your local Ollama instance over loopback (`127.0.0.1`) |
| Conversation history / background memory digest | Held in RAM only; never written to disk; cleared on restart or profile switch |
| Kira's saved memorias (auto-captured + curated highlights) | Written to a local, per-profile SQLite database (`data/memorias/memorias.db`); persists across sessions until you purge them |
| LLM-generated responses | Processed locally; only the voice-synthesis step involves a remote call (see below) |
| Smart Aggregator session data | Stored in a local SQLite database (`data/smart_aggregator/sessions.db`) |
| Cohost profiles and settings | Stored in local config files under your user data directory |
| Action and runtime logs | Written to local log files only; no remote reporting |

---

## What leaves your machine

**One service. One direction.**

| Service | What is sent | Why |
|---|---|---|
| **Microsoft Edge-TTS** (cloud) | Kira's synthesized spoken text — sentence-sized fragments of her generated response | Voice synthesis: the default TTS engine is a free Microsoft cloud service |

**Important:** Only Kira's outgoing *generated speech text* reaches Edge-TTS. Viewer chat, usernames, raw prompts, conversation history, and LLM context are **never** part of this request. The data flow is:

```
viewer chat → local Ollama (LLM) → Kira's response text → [sentence fragment] → Edge-TTS
```

The compaction step that converts viewer chat into a prompt summary, and all LLM inference, happen entirely on your machine before Edge-TTS is involved.

### Fully local voice synthesis (optional)

If you install the `local-tts` extra and configure Piper TTS, Edge-TTS is bypassed entirely for every utterance. When the local-only flag is set, no voice data leaves your machine at any point.

```powershell
pip install -e ".[cloud-tts,integrations,local-tts]"
```

---

## Platform connections

These connections are **read-only** from OpenCohost's perspective — no viewer content is uploaded:

| Service | What OpenCohost sends | What it receives |
|---|---|---|
| **YouTube Live Chat** (via `pytchat`) | Your stream's video ID | Incoming chat messages |
| **Twitch IRC** (`irc.chat.twitch.tv:6667`) | An anonymous IRC nick (`justinfan{random}`), channel JOIN, and PONG keepalives | Incoming chat messages |

OpenCohost does not post messages, reactions, or any viewer data back to either platform.

---

## Local-only connections

| Service | What is sent | Location |
|---|---|---|
| **Ollama** | System prompt + conversation history + compacted chat intent summary | `http://127.0.0.1:11434` (loopback only) |
| **OBS WebSocket** | Avatar state name (`"idle"`, `"speaking"`) and local image paths | `localhost:4455` |

---

## Local data storage

All persistent data is stored under your user data directory:

- **Windows:** `%APPDATA%\OpenCohost\` (e.g. `C:\Users\YourName\AppData\Roaming\OpenCohost\`)
- **Linux/macOS:** `~/.local/share/OpenCohost/` (or equivalent)

| What | Path (relative to user data dir) |
|---|---|
| Conversation profiles | `perfiles.json` |
| Cohost agenda profiles | `cohost_profiles.json` |
| Action/event log | `logs/acciones.jsonl` |
| Runtime log | `logs/opencohost_YYYYMMDD_HHMMSS.log` |
| Crash log | `logs/crash.log` |
| PTT settings | `config/ptt_settings.json` |
| Last model selection | `config/last_model.json` |
| LLM tier config | `config/llm_tiers.json` |
| TTS local-only flag | `config/tts_local_only.json` |
| Window geometry | `config/window_geometry.json` |
| Avatar config | `config/avatar.yaml` |
| Music library index | `config/music_library.json` |
| Editorial cards database | `data/editorial_cards/cards.db` (SQLite) |
| Smart Aggregator session database | `data/smart_aggregator/sessions.db` (SQLite) |
| Smart Aggregator chat log | `data/smart_aggregator/chat_log.jsonl` |
| TTS audio chunks | `temp/tts_chunk_*.mp3 / *.wav` (deleted after playback) |
| Fatal crash log | `logs/fatal.log` |
| TTS speed config | `config/tts_speed.json` |
| Kira's saved memorias | `data/memorias/memorias.db` (SQLite, per-profile) |
| Memorias disclosure-banner dismiss state | `config/memorias_notice.json` |

**Conversation memory** (the in-session history and background digest Kira uses for context) is RAM-only. It is never written to disk and is gone when the app closes, when you switch profiles, or when you use Clear History.

**Kira's saved memorias** are different: short, host-distilled extracts of your own direct/voice turns (never viewer chat) that Kira captures automatically and you can edit, pin, mark private, or delete from the "Memoria de Kira" window. They are written to a local, per-profile SQLite database (`data/memorias/memorias.db`) and persist across app restarts and profile switches — the one exception to the RAM-only rule above. This is a local-only write; no new network destination is introduced.

- **Pausing memorias capture is disk-only.** It stops new memorias from being written going forward. It does not retroactively delete anything already captured, and it does not block the RAM-only conversation/digest above from continuing to operate normally — a turn already tagged as capturable before you paused may still be written to disk.
- **A hard crash or force-kill can lose the current live window** (at most the last ~10 exchanges) that had not yet flushed to disk — the same way Clear History, a model switch, or a model download clears the live window without flushing it first. A clean app close attempts to flush first (best-effort, time-bounded — a very slow disk can still drop the tail on close).
- **To purge memorias**, open "Memoria de Kira" and use the per-profile delete action. It is explicit-only and scoped to the active profile; there is no automatic expiry.

---

## Telemetry and analytics

**None.** OpenCohost does not include any remote telemetry, crash reporting that phones home, analytics SDKs, or usage tracking of any kind.

The in-app session analytics (chat rate, vibe temperature, uptime) are computed in-process in memory and displayed in the UI only. No data is exported to any external service.

Crash information is written exclusively to local files (`logs/crash.log`, `logs/fatal.log`) using Python's built-in `faulthandler`. These files stay on your machine unless you choose to share them when reporting an issue.

---

## Third-party services summary

| Service | Operator | Data sent by OpenCohost | Purpose |
|---|---|---|---|
| Edge-TTS | Microsoft | Kira's generated response text (sentence fragments) | Cloud voice synthesis (default TTS) |
| YouTube Live Chat API | Google | Video ID | Receive viewer chat (read-only) |
| Twitch IRC | Twitch Interactive | Anonymous IRC nick, channel name, PONG | Receive viewer chat (read-only) |
| Ollama | Local process | LLM prompt (local only, loopback) | Language model inference |

For Microsoft's data practices regarding Edge-TTS requests, refer to [Microsoft's Privacy Statement](https://privacy.microsoft.com/en-us/privacystatement).

For YouTube's data practices, refer to [Google's Privacy Policy](https://policies.google.com/privacy).

For Twitch's data practices, refer to [Twitch's Privacy Notice](https://www.twitch.tv/p/en/legal/privacy-notice/).

---

## Questions

Open an issue on [GitHub](https://github.com/plynte-labs/opencohost) if you have questions about data handling or want to report a concern.
