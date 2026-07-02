# OpenCohost Trust Model

> Architecture-level data-flow and threat model for OpenCohost Lite.
> Last updated: 2026-07-02

---

## Overview

OpenCohost is a supervised AI co-host that runs on the user's own machine.
The LLM, all viewer-chat processing, conversation memory, and configuration
remain local. The one external service the default configuration reaches is
**Microsoft Edge-TTS**, which receives only Kira's synthesized spoken text.

This document describes every trust boundary, every network call, and the
design principles that keep viewer content contained.

---

## Trust Boundaries

```
┌─────────────────────────────────────────────────────────┐
│  User's machine (trusted)                               │
│                                                         │
│  ┌──────────────┐   localhost   ┌──────────────────┐   │
│  │  OpenCohost  │◄─────────────►│  Ollama          │   │
│  │  (Python)    │  :11434       │  (LLM inference) │   │
│  └──────┬───────┘               └──────────────────┘   │
│         │ localhost                                      │
│         ▼ :4455                                         │
│  ┌──────────────┐                                       │
│  │  OBS Studio  │                                       │
│  │  (WebSocket) │                                       │
│  └──────────────┘                                       │
│                                                         │
│  All viewer chat, LLM prompts, and conversation memory  │
│  remain inside this boundary.                           │
└──────────────────────────────┬──────────────────────────┘
                               │
          ┌────────────────────┼──────────────────────┐
          │ EXTERNAL (untrusted / read-only inbound)   │
          │                    │                        │
          ▼                    ▼                        ▼
   YouTube live chat    Twitch IRC chat       Microsoft Edge-TTS
   (read-only poll)     (read-only IRC)       ← Kira's speech text
                                                (OUTBOUND — cloud TTS)
```

---

## Network Calls — Complete Enumeration

| Destination | Direction | Data sent by OpenCohost | Notes |
|---|---|---|---|
| `http://127.0.0.1:11434` — Ollama inference | LOCAL loopback | Kira's system prompt + conversation history + compacted viewer-intent summary | Never raw chat text or usernames |
| `http://127.0.0.1:11434/api/tags` — Ollama health | LOCAL loopback | Empty GET — no user data | Startup probe and periodic health check |
| Ollama `generate` (warm/unload) | LOCAL loopback | Model name only; dummy prompt string | Model lifecycle management |
| Ollama `pull` (download) | LOCAL (Ollama→registry) | Model tag string — initiated by Ollama, not by OpenCohost | User triggers via model panel |
| **Microsoft Edge-TTS** (cloud) | **OUTBOUND** | **Kira's synthesized response text — sentence fragments only** | Default TTS engine; see Local TTS Option below |
| YouTube live chat via `pytchat` | REMOTE inbound | YouTube video ID (from user config) | OpenCohost receives messages; no user content uploaded |
| Twitch IRC `irc.chat.twitch.tv:6667` | REMOTE inbound | Anonymous `justinfan{random}` NICK, JOIN and PONG keepalives only | Read-only; no user content transmitted |
| OBS WebSocket `localhost:4455` | LOCAL loopback | Avatar state name (`"idle"`, `"speaking"`) and local file paths | No chat content, no LLM output |

**Summary:** the only data that leaves the user's machine under the default
configuration is Kira's outgoing spoken text, sent to Microsoft Edge-TTS for
voice synthesis.

---

## Local TTS Option

Users who require fully local voice synthesis can install the `local-tts`
extra to enable Piper TTS:

```
pip install -e ".[cloud-tts,integrations,local-tts]"
```

When a locally synthesized utterance begins, the `tts_local_only` flag is
latched for that utterance and Edge-TTS is never called. With Piper active for
all utterances, **no audio data leaves the machine**.

---

## Viewer Chat: Untrusted Input

Viewer chat is **untrusted data**, not operator instructions.

The pipeline enforces this at every stage:

1. **Aggregation** — the Smart Aggregator (`aggregator.py`, `message_filter.py`)
   compacts raw messages into an intent-summary string. Individual messages and
   usernames are never forwarded downstream as-is.

2. **Context sanitization** — before viewer intent enters the Ollama prompt,
   `_sanitize_history_context()` strips injection-style patterns and caps context
   to 800 characters.

3. **History isolation** — agenda turns are stored as a placeholder string;
   raw agenda prompts are never written to conversation history.

4. **Output guard** — LLM output passes through `output_guard()` before it is
   spoken or displayed.

5. **TTS boundary** — only the validated `dialogo` string (Kira's response)
   reaches the TTS layer. Viewer usernames and raw chat text never reach
   Edge-TTS or any other external service.

### Anti-Prompt-Injection Design Principle

The system treats the boundary between viewer input and LLM instructions as a
hard architectural boundary, not a soft convention. Viewer text is data; Kira's
system prompt and operator configuration are instructions. These are never
merged. The sanitization and output-guard layers exist to enforce this
separation even when viewer messages contain adversarial content.

No specific exploit pattern is documented here by design — the principle is
boundary enforcement, not exploit enumeration.

---

## Supervised-AI Co-Host Design

OpenCohost is explicitly a **supervised** AI co-host. The human streamer
remains in control at every point:

- Kira speaks only when triggered; the streamer can interrupt or mute at any time.
- PTT (Push-to-Talk) and LiveVoice modes are separate paths and are never merged automatically.
- The streamer configures Kira's persona, topic guardrails, and agenda; Kira cannot modify its own instructions at runtime.
- No autonomous actions are taken on streaming platforms — OBS state changes are limited to avatar visibility, triggered by Kira's own speech events.
- All LLM model selection, tier switching, and health monitoring are operator-visible and operator-controlled through the UI.

---

## Local Data Storage

All persistent state lives on the user's machine. Nothing is synced to a
remote backend.

| Data | Location (Windows) | Notes |
|---|---|---|
| Conversation profiles | `%APPDATA%\OpenCohost\perfiles.json` | |
| Cohost agenda profiles | `%APPDATA%\OpenCohost\cohost_profiles.json` | |
| Action/event log | `%APPDATA%\OpenCohost\logs\acciones.jsonl` | |
| App runtime log | `%APPDATA%\OpenCohost\logs\opencohost_<timestamp>.log` | |
| PTT / model / tier / TTS settings | `%APPDATA%\OpenCohost\config\*.json` | |
| Avatar config | `%APPDATA%\OpenCohost\config\avatar.yaml` | |
| Smart Aggregator session DB | `%APPDATA%\OpenCohost\data\smart_aggregator\sessions.db` | SQLite, local only |
| Smart Aggregator chat log | `%APPDATA%\OpenCohost\data\smart_aggregator\chat_log.jsonl` | Local only |
| Editorial cards DB | `%APPDATA%\OpenCohost\data\editorial_cards\cards.db` | SQLite, local only |
| TTS temp audio chunks | `%APPDATA%\OpenCohost\temp\tts_chunk_*` | Deleted after playback |
| Kira's saved memorias | `%APPDATA%\OpenCohost\data\memorias\memorias.db` | SQLite, local only, per-profile |

**Conversation memory** (`historial` deque and `MemoryDigest`) is RAM-only.
It is never written to disk and is cleared on app restart, profile switch, or
explicit clear.

**Kira's saved memorias** (`memorias.db`) are the one exception: short,
host-distilled extracts of the streamer's own direct/voice turns (never
viewer chat), written to a local, per-profile SQLite database under the user
data directory — a local-only write, no new network destination. Pausing
capture is disk-only: it stops new writes going forward (a turn already
tagged as capturable before you paused may still be written to disk) but
never retroactively deletes existing rows or blocks the RAM-only
conversation/digest above. A hard crash can still lose the current live
window (at most ~10 exchanges) that had not yet flushed. Purge is
explicit-only, scoped to the active profile, from the "Memoria de Kira"
window.

---

## Telemetry and Analytics

There is no remote telemetry, crash reporting to an external service, or
analytics pipeline.

- The in-process `AnalyticsTracker` counts stream stats (chat rate, vibe
  temperature, uptime) in memory only. It has no outbound HTTP calls.
- The crash reporter writes only to local files (`logs/crash.log`,
  `logs/fatal.log`) via Python's `faulthandler`. No Sentry, Mixpanel, or
  equivalent service is configured.

---

## Threat Model Summary

| Threat | Mitigation |
|---|---|
| Viewer chat used as LLM instructions (prompt injection) | Hard input/instruction boundary; sanitization + output guard; viewer text never reaches the LLM as a system message |
| User data leaked to cloud services | Only Kira's outgoing speech text reaches Edge-TTS; all other data stays local |
| Malicious model output reaching external services | Output guard validates LLM response before TTS; TTS receives only the validated `dialogo` string |
| Passive data collection via telemetry | No remote telemetry exists in the codebase |
| Twitch/YouTube credentials exfiltrated | Twitch connection uses anonymous nick; YouTube uses only the video ID; neither token nor credentials are logged or forwarded |
| OBS WebSocket used to exfiltrate content | OBS integration sends avatar state names and local file paths only; no LLM or chat content |

---

## Scope Note (Lite Release)

This document covers **OpenCohost Lite**. The Lite release does not include
the heavy local TTS server (Qwen3-TTS / voice cloning) or the Stream Admin
module. Those subsystems are absent from this repository and from this threat
model.
