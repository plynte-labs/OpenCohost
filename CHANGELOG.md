# Changelog

All notable changes to OpenCohost are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html) with pre-release tags.

## [0.5.0] - 2026-11-17

### Why 0.5.0

This release finishes moving OpenCohost to a single architecture: the Tauri desktop app over a local FastAPI engine, with the last legacy paths (the separate Qwen3-TTS server among them) retired.
Memory, profiles and settings now use a new storage format and database schema, so 0.5.0 does not import 0.3.0 memories. Profiles, settings, assets and API keys stored in the previous install folder are copied selectively.
The installer also changed shape: it now bundles the engine, its Python runtime and the offline voices and models, so Kira can run without extra downloads.
0.4.0 (screen vision and the 3D VRM avatar) was developed on internal branches but never shipped as an installer; its changes are included in 0.5.0, and because upgrading from 0.3.0 is not a drop-in replacement, this remains a breaking release.

### Breaking

- Selective upgrade: memories from 0.3.0 and 0.5.0-alpha are not imported, because the memory storage format and database schema changed.
- Profiles, settings, assets and API keys found in the previous install folder are copied to the new data folder without overwriting existing files; verified originals are kept under `legacy-preserved`. Built-in default profiles and setup progress are not carried over, so the first-run wizard still runs, and the welcome screen says so when an old install is detected.
- The Qwen3-TTS voice server is no longer started or supported; local voices use Piper, online voices use Edge-TTS.

### Added

- First-run wizard with one decision per screen, a resumable checkpoint (stop and continue where you left off) and `/setup <section>` to reopen a section.
- Wizard language choice also sets Kira's language, and a notice explains that it applies after a restart.
- Wizard voice choice between a local Piper voice and an Edge-TTS voice, with voice speed and a clear message when a voice file is missing.
- Guided setup for local GPU, CPU-only and bring-your-own-key engines, with an inference speed test and plain-language results.
- Offline install: the installer (about 1.46 GB) bundles the engine, its Python runtime, the MSVC runtime, the Laya decision model, the MiniLM memory model and two Piper voices (`es_MX-claude-high`, `en_US-kristin-medium`).
- On-demand vision: Kira looks at your screen only when asked (or with `/mira`), with screen, video-clip and text modes, read-aloud and frame thumbnails.
- Vision runs on a separate local vision model, with a model picker, readiness state and optional cloud vision sources.
- Pasted images in the chat composer (up to three) are described by the vision model before the turn.
- Self-knowledge: Kira answers real questions about the app and the moment (time, active model, VRAM, memory, commands), with an audience filter and opt-out toggles.
- Active cards: talking points, sponsors included, that surface only when the topic matches, with their forbidden phrases always enforced.
- Kira VRM avatar with lip-sync, plus a transparent desktop companion window that stays in sync with the main conversation.
- In-app model downloader with pause, resume and recovery after a crash, and a curated catalog for CPU and low-VRAM cards.
- Automatic context sizing from free VRAM, a per-model override, and an adaptive reasoning budget with a manual ceiling.
- Memory v5: local semantic recall with a multilingual MiniLM model on a CPU worker, per-layer memory toggles and session topics.
- Reversible memory cleanup: duplicate and superseded entries can be restored, and an owner-run tool cleans poisoned drafts.
- Laya decision layer that decides per turn whether to reply, look or search, with per-decision modes and thresholds.
- Optional owner web lookup with safe search and fetch, a search API fallback and spoken-safe sources.
- Reply length setting with an automatic mode and four fixed levels.
- Per-profile warmth level and an optional owner gender for how Kira addresses you.
- Owner toggles for persona and style output rules, and an editable pronunciation dictionary for the voices.
- Read-aloud mode with pause, and controls to discard leftover speech after an interruption.
- Push-to-talk dictation modes that write into the chat composer.
- Cloud provider templates for Gemini, DeepSeek, Anthropic and OpenRouter, with a key test that lists the provider's models.
- Opt-in remote voice gateway: pair a phone with a one-time code and talk to Kira from a call-style page with the avatar.
- New-version notice that links to the GitHub release, with a switch to turn the update check off.
- Contextual crisis detector: laughter or joke markers never lower an explicit self-harm statement.
- Local activity panel backed by a content-free usage ledger.
- Turn events log: one structured event per turn in `logs/turn_events.jsonl`, without message text.
- Kira says out loud when the stall watchdog recovers a hung model.
- Optional local diagnostics and configuration MCP servers for developers.

### Changed

- The desktop splash screen stays until the engine is ready, and the engine reports its startup state.
- Web lookup and Laya decisions are on by default; vision uses a rule gate by default.
- Prompt layout reworked so local models reuse their cache, which cut the median time to first speech in local tests.
- The first spoken chunk can start at soft punctuation, and pauses between sentences sound more natural.
- Replies follow the owner's profile tone more closely, without fixed openers, repeated profile facts or forced closing questions.
- Prompt guidance, chat reactions and spoken math symbols are localized for Spanish and English.
- Edge-TTS failures fall back to Piper, with a breaker for repeated transient failures.
- The Controls panel is split into separate panes for profile rules, models, vision and phone.
- Closing the app can unload the models first, and an optional setting keeps them loaded.
- OBS control is not included in the installer build. `obsws-python` is an opt-in `obs` extra for source installs only.

### Fixed

- Gemini works as a cloud provider: requests no longer send parameters Gemini rejects, and the key test sends a real chat request.
- Installing 0.5.0 over a previous install now finds and removes the old version, including when the publisher key changed.
- The installer shows the license page and cleans its data and registry entries on uninstall.
- The English Piper voice now ships with the installer.
- Engine readiness recovers when Ollama starts late.
- The push-to-talk key bridge is restarted after a backend restart or reload.
- Push-to-talk no longer drops late speech, replayed LiveAudio frames or the start of a phrase on fast presses.
- Whisper silence hallucinations are dropped from push-to-talk speech.
- Local models stop thinking when told to, and a reply is retried without thinking when the model does not support it.
- Prompt echoes and internal markers no longer leak into Kira's spoken replies.
- Kira no longer claims a web search that never ran, or screen content when no look happened.
- Weak or off-topic memories are kept out of the prompt, and finished recalls are no longer discarded when slow.
- Hashtags are no longer read aloud as the hash sign.
- Expired sponsor cards are no longer spoken or enforced.

### Privacy and security

- Provider error text and reply text are kept out of failure state and logs, and provider errors are sanitized before they are shown.
- The turn events log and the usage ledger record no message text.
- Memory drafts store only the owner's own words, and recall exchanges and pasted images are excluded from memory.
- Screen captures stay in memory and are shown only to the operator.
- Cloud API keys and web search keys are stored in the operating system keyring.
- The local API requires the operator token by default, the token file is restricted to the current user, and non-loopback Host headers are refused.
- The desktop webview uses an explicit Content Security Policy instead of none.
- The remote gateway is off by default, listens on loopback only and stores hashed device tokens.

### Known limitations

- The installer is not code-signed, so Windows SmartScreen may warn before it runs; verify the file against `SHA256SUMS` first.
- Windows is the only platform validated end to end; Linux runs from source and other platforms are not validated.
- Streaming replies are on by default, but recovery from a stall in the middle of a streamed reply has not yet been validated in a live session.
- Gemini (Gemini 3.5 Flash, on a clean VM install) and NVIDIA NIM were verified live. DeepSeek, Anthropic, OpenRouter and cloud vision were tested only with simulated calls.
- The new Content Security Policy has not yet been verified in a running installed app.
- The push-to-talk bridge after a backend restart, a true cold model load, the clause sanitizer and chat ingestion (not yet tested on a live stream) still need live validation.
- Real audio devices, OBS, YouTube and OAuth are not covered by automated tests.

## [0.3.0-alpha.4] - 2026-09-09

Baseline for this changelog. See the [GitHub release](https://github.com/plynte-labs/OpenCohost/releases/tag/v0.3.0-alpha.4).

[0.5.0]: https://github.com/plynte-labs/OpenCohost/compare/v0.3.0-alpha.4...HEAD
[0.3.0-alpha.4]: https://github.com/plynte-labs/OpenCohost/releases/tag/v0.3.0-alpha.4
