# Privacy & Data Policy

**OpenCohost** — local-first AI co-host platform  
Last updated: 2026-08-14

---

## Overview

OpenCohost is designed to keep your viewers' data and your streaming context on your own machine. This document describes exactly what stays local, what leaves your machine, and where data is stored. No legal boilerplate — just an accurate description of how data flows.

---

## What stays on your machine

All of the following are processed and stored **locally only** and are never transmitted to any remote server by OpenCohost:

| Data | Notes |
|---|---|
| Viewer chat messages | Read from Twitch's anonymous IRC gateway (or, if you opt in, YouTube's unofficial live-chat endpoint — see below); never forwarded anywhere |
| Viewer usernames | Same as above — used locally for context aggregation |
| Prompts sent to the LLM | Sent to your local Ollama instance over loopback (`127.0.0.1`) — **unless you enable an optional cloud LLM provider**, which is off by default. See ["Optional cloud LLM providers"](#optional-cloud-llm-providers-opt-in) below |
| Conversation history / background memory digest | Held in RAM only; never written to disk; cleared on restart or profile switch |
| Kira's saved memorias (auto-captured + curated highlights) | Written to a local, per-profile SQLite database (`data/memorias/memorias.db`); persists across sessions until you purge them |
| LLM-generated responses | Generated locally by Ollama; only the voice-synthesis step involves a remote call (see below). Same cloud-provider caveat as prompts, above |
| Smart Aggregator session data | Stored in a local SQLite database (`data/smart_aggregator/sessions.db`) |
| Cohost profiles and settings | Stored in local config files under your user data directory |
| Action and runtime logs | Written to local log files only; no remote reporting |

---

## What leaves your machine

**One service by default. A second one only if you opt in.**

| Service | What is sent | Why |
|---|---|---|
| **Microsoft Edge-TTS** (cloud) | Kira's synthesized spoken text — sentence-sized fragments of her generated response | Voice synthesis: the default TTS engine is a free Microsoft cloud service |
| **An OpenAI-compatible LLM provider** (cloud) — **opt-in, off by default** | The complete LLM prompt: system prompt, active persona, saved memorias, personalization block, **and the filtered viewer-chat context** | You chose a cloud model instead of local Ollama. See the dedicated section below |

**Important:** Only Kira's outgoing *generated speech text* reaches Edge-TTS. Viewer chat, usernames, raw prompts, conversation history, and LLM context are **never** part of an Edge-TTS request. With the default local LLM, the data flow is:

```
viewer chat → local Ollama (LLM) → Kira's response text → [sentence fragment] → Edge-TTS
```

The screening, filtering, and compaction steps that decide what viewer chat the LLM may see — and, by default, all LLM inference — happen entirely on your machine before Edge-TTS is involved.

### Optional cloud LLM providers (opt-in)

OpenCohost can run inference against any OpenAI-compatible endpoint (OpenAI, NVIDIA NIM, or a custom `base_url`) instead of local Ollama. **This is off by default and you must configure it deliberately**: `active_provider` defaults to `"local"`, and an absent, unreadable, or corrupt provider config all resolve back to local-only.

Understand what changes when you turn it on:

```
viewer chat → [filtering] → PROMPT → your chosen cloud provider → response → Edge-TTS
                                     ^^^^^^^^^^^^^^^^^^^^^^^^^^
                        the filtered chat context leaves your machine here
```

- **What is sent:** the entire message array — system prompt, the active cohost persona, the saved-memorias block, the personalization block, and the viewer-chat context that survived filtering. This is strictly more than Edge-TTS ever receives.
- **Whose data:** the chat context is derived from your viewers' messages. Filtering and compaction reduce and reshape it, but it is not anonymised, and usernames may appear in the context Kira is given.
- **Retention and training are the provider's policy, not ours.** OpenCohost cannot control how your chosen provider stores or uses what it receives. Read their terms before enabling this on a live stream.
- **Automatic fallback:** if the provider errors or times out, the turn falls back to local Ollama. The failed request had already been sent.
- **API keys** live in a separate store (`config/llm_keys.json`), never in `config/llm_provider.json`, so the provider config stays safe to inspect or share. Neither file is committed to git.

To stay fully local for inference, simply leave this off — that is the shipped default.

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
| **Twitch IRC** (`irc.chat.twitch.tv:6667`) — default | An anonymous IRC nick (`justinfan{random}`), channel JOIN, and PONG keepalives | Incoming chat messages |
| **YouTube live chat** (via `pytchat`) — opt-in, unofficial | Your stream's video ID | Incoming chat messages |

OpenCohost does not post messages, reactions, or any viewer data back to either platform.

---

## Local-only connections

| Service | What is sent | Location |
|---|---|---|
| **Ollama** | System prompt + conversation history + screened chat context (a compacted summary by default; may include individual filtered messages, and today also carries viewer usernames) | `http://127.0.0.1:11434` (loopback only) |
| **OBS WebSocket** | Avatar state name (`"idle"`, `"speaking"`) and local image paths | `localhost:4455` |

---

## Local data storage

All persistent data is stored under your user data directory:

- **Windows:** `%APPDATA%\OpenCohost\` (e.g. `C:\Users\YourName\AppData\Roaming\OpenCohost\`)<!-- path-ok -->
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
| Streamer personalization (nickname, occupation, interests, custom instructions) | `config/personalization.json` |

**Conversation memory** (the in-session history and background digest Kira uses for context) is RAM-only. It is never written to disk and is gone when the app closes, when you switch profiles, or when you use Clear History.

**Kira's saved memorias** are different: short, host-distilled extracts of your own direct/voice turns (never viewer chat) that Kira captures automatically and you can edit, pin, mark private, or delete from the "Memoria de Kira" window. They are written to a local, per-profile SQLite database (`data/memorias/memorias.db`) and persist across app restarts and profile switches — the one exception to the RAM-only rule above. This is a local-only write; no new network destination is introduced.

- **Pausing memorias capture is disk-only.** It stops new memorias from being written going forward. It does not retroactively delete anything already captured, and it does not block the RAM-only conversation/digest above from continuing to operate normally — a turn already tagged as capturable before you paused may still be written to disk.
- **A hard crash or force-kill can lose the current live window** (at most the last ~10 exchanges) that had not yet flushed to disk — the same way Clear History, a model switch, or a model download clears the live window without flushing it first. A clean app close attempts to flush first (best-effort, time-bounded — a very slow disk can still drop the tail on close).
- **To purge memorias**, open "Memoria de Kira" and use the per-profile delete action. It is explicit-only and scoped to the active profile; there is no automatic expiry.

**Streamer personalization** is a small, operator-authored form (nickname, occupation, interests, custom instructions) that you fill in yourself from the "Personalización..." panel — it is never inferred or auto-captured from chat. Unlike per-profile memorias, this store is global (shared across all Kira profiles/personas) and is read directly into Kira's prompt for your own direct/voice turns only; it is never applied to viewer chat processing. It is written in plaintext to a local file (`config/personalization.json`) under your user data directory and is never transmitted anywhere. You can disable it at any time with the "Habilitar personalización" checkbox, or permanently erase it with the panel's "Limpiar" (Clear) action — both take effect immediately, with no automatic expiry otherwise.

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
| Twitch IRC | Twitch Interactive | Anonymous IRC nick, channel name, PONG | Receive viewer chat (read-only). Default platform. |
| YouTube live chat (unofficial endpoint, via `pytchat`) | Google | Video ID | Receive viewer chat (read-only). **Opt-in only** — see the warning below. |
| Ollama | Local process | LLM prompt (local only, loopback) | Language model inference. **Default.** |
| Any OpenAI-compatible LLM endpoint (OpenAI, NVIDIA NIM, custom `base_url`) | Whoever you point it at | The full LLM prompt, **including the filtered viewer-chat context**, persona, and saved memorias | Language model inference. **Opt-in, off by default** — see ["Optional cloud LLM providers"](#optional-cloud-llm-providers-opt-in). |

For Microsoft's data practices regarding Edge-TTS requests, refer to [Microsoft's Privacy Statement](https://privacy.microsoft.com/en-us/privacystatement).

If you enable a cloud LLM provider, its operator's policy — not this document — governs what happens to the prompts it receives. OpenCohost neither controls nor can audit that.

For YouTube's data practices, refer to [Google's Privacy Policy](https://policies.google.com/privacy).

### YouTube live chat is opt-in and unofficial

Twitch is OpenCohost's supported chat platform. It connects to Twitch's public
anonymous IRC gateway, which Twitch documents and permits, and it needs no
credentials and no extra install.

YouTube support is different, and you should read this before enabling it:

- It uses [`pytchat`](https://pypi.org/project/pytchat/), which reads the same
  unofficial live-chat endpoint the YouTube web player uses. **It is not
  Google's official YouTube Data API, and YouTube's Terms of Service do not
  permit accessing the service by automated means outside the published API.**
- It is therefore **not installed by default**. You have to ask for it
  explicitly — `pip install -e ".[youtube-chat]"`, or `uv sync --extra
  youtube-chat` if you use uv. Without it, connecting to a YouTube URL simply
  fails; the app shows a generic connection error and the reason above is
  written to the log file.
- The risk, if any, falls on your own YouTube channel. OpenCohost cannot
  accept that risk on your behalf, which is why it is a separate, deliberate
  install step rather than a default.
- Nothing about this changes where the data goes: chat read this way is still
  processed locally and still never leaves your machine.

An officially-compliant YouTube path — the YouTube Data API v3 with your own
API key — is the intended replacement. It is not implemented yet.

For Twitch's data practices, refer to [Twitch's Privacy Notice](https://www.twitch.tv/p/en/legal/privacy-notice/).

---

## Questions

Open an issue on [GitHub](https://github.com/plynte-labs/opencohost) if you have questions about data handling or want to report a concern.
