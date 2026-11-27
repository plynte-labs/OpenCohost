# Privacy & Data Policy

**OpenCohost** — local-first AI co-host platform  
Last updated: 2026-11-11

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
| Live conversation history / background memory digest | Held in RAM only; cleared on restart or profile switch. A separate on-disk record of your own direct/voice turns exists — see the next row |
| Memory v5 episodic record (your direct/voice turns and Kira's replies) | Written verbatim to a local SQLite database (`data/memory_v5_shadow/memory_v5_shadow.db`, under your user data directory), plus a derived semantic-search cache (`memory_v5_semantic_cache.db`, same folder). Never viewer chat, never turns while memorias capture is paused/private, never the text of a pasted image. Kept for **90 days** (setting `OPENCOHOST_MEMORY_V5_EVIDENCE_TTL_DAYS`; `0` = keep forever), then deleted automatically. Purging a profile's memorias also purges its record here |
| Kira's saved memorias (auto-captured + curated highlights) | Written to a local, per-profile SQLite database (`data/memorias/memorias.db`); persists across sessions until you purge them |
| LLM-generated responses | Generated locally by Ollama; only the voice-synthesis step involves a remote call (see below). Same cloud-provider caveat as prompts, above |
| Smart Aggregator session data | Stored in a local SQLite database (`data/smart_aggregator/sessions.db`) |
| Cohost profiles and settings | Stored in local config files under your user data directory |
| Action and runtime logs | Written to local log files only; no remote reporting |

---

## What leaves your machine

**Two services by default (voice synthesis and the update check). A third one only if you opt in.**

| Service | What is sent | Why |
|---|---|---|
| **Microsoft Edge-TTS** (cloud) | Kira's synthesized spoken text — sentence-sized fragments of her generated response | Voice synthesis: the default TTS engine is a free Microsoft cloud service |
| **GitHub API** (`api.github.com`) — update check, **on by default, can be turned off** | A single `GET` for the public release list of OpenCohost, once per app launch. GitHub sees your IP address and a generic `User-Agent` of the form `OpenCohost/<version>`. Nothing else: no name, email, profile, chat, machine information or identifier | Tell you when a newer version is published. It only notifies and links to the GitHub release page: nothing is downloaded or installed, and no file of yours is touched. See ["Update check"](#update-check) below |
| **An OpenAI-compatible LLM provider** (cloud) — **opt-in, off by default** | The complete LLM prompt: system prompt, active persona, saved memorias, personalization block, **and the filtered viewer-chat context** | You chose a cloud model instead of local Ollama. See the dedicated section below |

**Important:** Only Kira's outgoing *generated speech text* reaches Edge-TTS. Viewer chat, usernames, raw prompts, conversation history, and LLM context are **never** part of an Edge-TTS request. With the default local LLM, the data flow is:

```
viewer chat → local Ollama (LLM) → Kira's response text → [sentence fragment] → Edge-TTS
```

The screening, filtering, and compaction steps that decide what viewer chat the LLM may see — and, by default, all LLM inference — happen entirely on your machine before Edge-TTS is involved.

### Update check

Once per launch, the backend asks `https://api.github.com/repos/plynte-labs/opencohost/releases` whether a release newer than the running version exists, and the app shows a notice with a link if so. The answer is kept in memory for that launch only; the response is never logged or stored, and a failure is silent.

- **What is sent:** the request itself (so GitHub sees your IP address) and the header `User-Agent: OpenCohost/<version>`. No cookies, tokens, personal data or machine information.
- **How to turn it off:** Settings → *Check for updates on launch* (stored in `config/update_check.json`). When off, no request is made at all.

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

#### Cloud memoria draft promotion (opt-in, off by default)

By default, **memoria promotion does not use a cloud provider**: if you are on a cloud provider and have no local Ollama model loaded, automatic draft promotion stays paused on your machine (`model_not_loaded`). Drafts are never sent to a cloud API unless you explicitly opt in by setting `"cloud_promotion_enabled": true` in `config/llm_provider.json` (or `OPENCOHOST_CLOUD_PROMOTION_ENABLED=1` in the environment).

- **What is sent when enabled (only when no local judge model is loaded):** up to 4 non-private, active operator-only draft excerpts per sweep (plus up to a few active non-private related fact strings for deduplication and contradiction checks) to your active cloud provider's configured model.
- **What is never sent:** viewer chat messages, rows marked private, muted/inactive rows, other profiles' rows, and API keys.
- **Provider policy applies:** any excerpts sent under this opt-in are governed by your cloud provider's retention and training policy. Leave `"cloud_promotion_enabled"` at `false` (default) to keep all unjudged memoria drafts on your machine until a local model is loaded.

#### Sending images directly to the cloud model (opt-in, off by default)

By default, when Kira looks at your screen or you paste an image into the chat, a **local** vision model describes it and only that text description goes to the chat model. If you turn on **Send images directly to the cloud model** (Vision settings) **and** the active provider is a cloud provider whose chosen model can see images, the picture itself is sent instead:

- **What is sent:** the screen capture (a downscaled JPEG, up to a few frames, or the one frame of a text look) or the images you pasted (up to three, downscaled to at most 1568 px on the long side when larger), as part of the same request that carries the prompt, to the provider you configured.
- **What is not sent:** nothing else changes. Images are never saved to disk, never logged and never added to conversation history or memory; history keeps only a short text note that an image was shared.
- **Provider policy applies:** retention and training on what you send are the provider's policy, not ours. Screens can show private things (other windows, notifications, chat); read your provider's terms first.
- **Only when all of these hold:** a cloud provider is active, its model is marked as seeing images, the switch is on and vision is enabled. Otherwise the local describe path is used and no image leaves your machine.
- **Fallback:** if the cloud call fails and the turn falls back to local Ollama, the images are dropped and Kira says she could not see them. The failed request had already been sent.
- **How to turn it off:** switch *Send images directly to the cloud model* off in Vision settings (stored as `cloud_direct_allowed` in the vision settings file); it takes effect on the next look or pasted image. Switching the provider back to local also stops it.

#### Choosing where images are described (Vision source)

Vision settings have one **Vision source** with three values, stored as `vision_source`: `local` (default), `chat_model` (the option described above; it replaces the old on/off switch, and a settings file that only has `cloud_direct_allowed: true` is read as `chat_model`) and `cloud_model`.

- **`cloud_model` sends images to a vision provider you pick**, which can be a different provider from your chat model (or your chat model may be local). Each screen look or pasted image is sent there as a base64 image with the question, and only the returned text description goes to the chat model. The same points as above apply: images are never saved, never logged, and the provider's policy governs retention and training.
- **Its API key** is stored in the operating system keyring under its own entry (`vision_<provider>`), or you may tick *use my chat provider key* to reuse the key you already saved for the same provider. The key is write-only: the API never returns it and it is never logged.
- **Nothing leaves your machine under `local`.** With `chat_model` or `cloud_model` the local vision model is not started at all.
- **How to turn it off:** choose *On this PC* as the Vision source; it takes effect on the next look or pasted image, with no restart.

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

All persistent data is stored under your user data directory. Where that is depends on
how OpenCohost was installed — `get_user_data_dir()` in `opencohost/config/storage.py`
branches on whether the process is a frozen build:

- **Editable install** (`pip install -e`, the documented setup): the **repository root**.
  Nothing is written outside the folder you cloned into.
- **Packaged (frozen) build, Windows:** `%APPDATA%\OpenCohost\` (e.g. `C:\Users\YourName\AppData\Roaming\OpenCohost\`)<!-- path-ok -->
- **Packaged (frozen) build, Linux/macOS:** `~/.config/OpenCohost/`

Either way it is a local directory on your own disk; nothing below is uploaded anywhere.

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

Three rows above are anchored somewhere else, which only becomes visible in a packaged
build — in an editable install they all resolve to the same repository root:

- **TTS audio chunks** go to `STORAGE_PATHS.temp_root` (the app directory's `temp/`),
  redirectable via `opencohost/config/storage.yaml`.
- **Smart Aggregator files** use the relative `data/smart_aggregator/...` paths from the
  aggregator config, resolved against the working directory.
- **Crash logs:** the current app (Tauri desktop + API host) writes no separate
  `crash.log` or `fatal.log`; the legacy CustomTkinter shell that did was removed.
  Errors go to the runtime log under `<data root>\logs\`, which `OPENCOHOST_LOG_DIR`
  can relocate.

All of them are still local files; none introduces a network destination.

**Live conversation memory** (the in-session history and background digest Kira uses for context) is RAM-only. It is gone when the app closes, when you switch profiles, or when you use Clear History. It is **not** the only copy, though: the Memory v5 episodic layer (below) keeps its own on-disk record of your direct/voice turns.

**Memory v5 episodic record** lets Kira recall past conversations. After each of your direct, push-to-talk or owner-bundle turns it stores two rows verbatim in `data/memory_v5_shadow/memory_v5_shadow.db`: what you said and what Kira replied. It also groups them into sessions and episodes and keeps a derived search index (embeddings and keyword tokens) in `memory_v5_semantic_cache.db`. Viewer chat is never stored; turns are skipped while memorias capture is paused or private; a pasted image's description (which can contain text read from the screenshot) is stripped before storing. When a past conversation is recalled into Kira's prompt, only **your side** of it is used; Kira's own earlier replies are never re-injected as memory.

- **Retention:** rows older than 90 days (by turn time), the closed sessions/episodes built from them and their index entries are deleted automatically by a bounded background job (at startup and every few hours). Set `OPENCOHOST_MEMORY_V5_EVIDENCE_TTL_DAYS` to change the window; `0` keeps everything.
- **Purge:** the per-profile purge in "Memoria de Kira" now also erases that profile's episodic record: the journal, sessions, episodes and the search index. This happens even when the memorias feature itself is switched off.
- **Deleting one memoria** also removes the owner turns it was distilled from (matched by the same key that identifies the memoria) and Kira's reply to each, plus the index entries of the episodes they were in. v5 keeps no direct link between a memoria and its source turns, so rows with no matching turn (imports, summaries, reflection facts) leave v5 unchanged; those turns expire with the 90-day retention or a profile purge.

**Kira's saved memorias** are different: short, host-distilled extracts of your own direct/voice turns (never viewer chat) that Kira captures automatically and you can edit, pin, mark private, or delete from the "Memoria de Kira" window. They are written to a local, per-profile SQLite database (`data/memorias/memorias.db`) and persist across app restarts and profile switches — the one exception to the RAM-only rule above. This is a local-only write; no new network destination is introduced.

- **Pausing memorias capture is disk-only.** It stops new memorias from being written going forward. It does not retroactively delete anything already captured, and it does not block the RAM-only conversation/digest above from continuing to operate normally — a turn already tagged as capturable before you paused may still be written to disk.
- **A hard crash or force-kill can lose the current live window** (at most the last `HISTORY_MAX_TURNS` exchanges — 3 today, `opencohost/config/settings.py`) that had not yet flushed to disk — the same way Clear History, a model switch, or a model download clears the live window without flushing it first. A clean app close attempts to flush first (best-effort, time-bounded — a very slow disk can still drop the tail on close).
- **To purge memorias**, open "Memoria de Kira" and use the per-profile delete action. It is explicit-only and scoped to the active profile. Memorias have no automatic expiry; the Memory v5 episodic record does (90 days, see above) and is erased by the same purge.

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
