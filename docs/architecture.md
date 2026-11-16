# OpenCohost Architecture Overview

> Refreshed 2026-11-12 from the code on branch `refactor/split-identity-controls`.
> It replaces the 2026-08-14 map, which still described the CustomTkinter shell as
> a live composition root. That shell was removed in commit `1eecab7`; no file
> under `ui/` is tracked any more. Anything not confirmed in code is marked
> **(unverified)**. ADRs, audit reports and dated working notes are historical
> records and are not rewritten.

## 1. Purpose

OpenCohost is a local-first AI streaming co-host. Its persona, Kira, comes from
the owner-configured profile. The app runs on the streamer's machine:

- **Brain:** a local Ollama model by default. An OpenAI-compatible cloud provider
  is used only when the owner opts in.
- **Voice:** Edge-TTS (network, via the `edge_tts` package) or local Piper.
  `docs/PRIVACY.md` states what leaves the machine by default.
- **Presence:** a VRM avatar in the desktop UI, OBS scene updates over the OBS
  WebSocket, and an optional phone avatar served by the remote gateway.
- **Inputs:** typed owner turns, push-to-talk, viewer chat (YouTube, Twitch
  support code), and an agenda of approved topics.

The product is the Tauri desktop app over a FastAPI host. `python -m opencohost`
only prints a retirement notice and exits (`opencohost/__main__.py`).

## 2. Process topology

```
OpenCohost_UI  (Tauri shell, Rust)                 src-tauri/src/backend/supervisor.rs
  1. probe 127.0.0.1:8765 (primary), then :8770 (fallback), GET /api/health
  2. reuse a healthy API, or spawn one:
       python -m uvicorn opencohost.api.main:app
       (src-tauri/backend.config.json: port 8765, fallback_port 8770, spawn true)
  WebView: React front end (src/) ---- HTTP + operator bearer token ----> API
  child: F10 push-to-talk bridge (opencohost.api.ptt_f10_bridge); the shell
         gives it a Job Object when one is needed

API process: uvicorn, --workers 1 (must stay 1; see api/main.py)
  127.0.0.1:8765 | :8770   FastAPI host (api/main.py create_app)
  127.0.0.1:8780           remote phone gateway, opt-in (OPENCOHOST_REMOTE_GATEWAY=1)
  helpers:
    liveaudio-service      STT over WebSocket (stt/supervisor.py), headless child,
                           started by the API lifespan
    Laya decision worker   core/decision/laya_worker.py, only when configured
    vision daemon          started only once vision is enabled
  external services:
    Ollama (local) | cloud LLM (OpenAI-compatible, opt-in) | Edge-TTS
    OBS WebSocket | web search API (Tavily key) | YouTube / Twitch (stream_admin)
```

Notes:

- Ports 8765 (primary) and 8770 (fallback) are confirmed in
  `backend.config.json`, `backend/config.rs`, `api/ptt_f10_bridge.py` and
  `mcp_local/common.py`.
- The supervisor never spawns over a foreign or unhealthy listener.
  `decide_action` in `backend/health.rs` returns `ForeignBackend` or `BothBusy`
  for those cases.
- The gateway's bind host is hard-coded to `127.0.0.1` (`gateway/server.py`).
  Exposing it beyond the machine is an owner decision (ADR-067); the API never
  does it.
- The LiveAudio WebSocket range starts at `DEFAULT_BASE_PORT = 8765`
  (`stt/discovery.py`), the same value as the API's primary port. The actual
  port is reported at runtime through the `ws_port` event. How the two avoid
  each other **(unverified)**.

## 3. Backend layers (`opencohost/`)

| Package | Role |
|---|---|
| `api/` | FastAPI host. `main.py` builds the app: CORS, host guard, audit, bearer auth, then mounts every router in `routers/ALL_ROUTERS`. `engine_host.py` is the composition root and the only `api/` file that imports core engine classes. `dispatch.py` is the bounded command gate with idempotency keys. `direct_turn.py` is the shared owner-turn submission used by `/api/chat/turn` and by the gateway. `auth.py` mints operator and agent tokens. `host_guard.py` refuses non-loopback `Host` headers. |
| `api/routers/` | One module per surface: chat, ptt, speech, stream, agenda, memoria, perfiles, music, llm_provider, obs, avatar, avatar_vrm, vision, setup, status, events, decisions, guardrails, and others. `GET /openapi.json` on a running backend is the authoritative route list. |
| `core/` | Engine and runtime. See the sub-table below. |
| `smart_aggregator/` | Viewer-chat ingestion: source contracts, filtering, vibe and activity signals, highlight scoring, Kira agenda controller, topic suggester. |
| `stream_admin/` | YouTube provider, Twitch support code, OAuth store, moderation, analytics. |
| `avatar/` | OBS WebSocket client and headless push runtime (`obs_runtime.py`), avatar config and state, VRM folder handling. |
| `stt/` | Supervisor for the LiveAudio child process, WebSocket discovery, Whisper hallucination filter. |
| `gateway/` | Opt-in remote phone gateway. It is its own FastAPI app with no shared middleware, and it does device pairing and a turn relay into `submit_direct_turn`. It also serves the built phone-avatar bundle from `gateway/static/`. |
| `config/` | Settings and feature flags (`settings.py`), data-root resolution (`storage.py`), guardrail rule registry, LLM provider and tier config, decisions config, setup state and checkpoint, YAML and JSON defaults. |
| `security/` | OS keyring store (`keyring_store.py`) and search API key handling (`search_keys.py`). |
| `i18n/` | Language registry, locale coherence checks, active-language state. Strings live in `locales/`. |
| `mcp_local/` | Local MCP server with config and diagnostics tools over the running API. |
| `editorial_cli.py` | Command-line management of editorial cards, one-shot and safe while the app runs. |

`core/` sub-table:

| Module | Role |
|---|---|
| `llm_engine.py` | `MotorVocalIA`, the motor: queue, model state, Ollama and cloud dispatch, speech lifecycle. It keeps its legacy class name. Behaviour is split into mixins under `engine/`. |
| `cohost_engine.py` | `CohostEngine`, the facade that `EngineHost` owns around `MotorVocalIA`. |
| `engine/` | Generation: `generation_orchestrator.py` (retries, cloud failure classes, rate-limit backoff, streaming), `llm_inference_service.py` (Ollama or cloud dispatch), and `llm_engine_*.py` mixins for agenda, decisions, memorias, pregen, vision, web, speech and cloud transport. |
| `context/` | `prompt_assembler.py` (request build), `context_budget.py`, `generation_evaluator.py` (post-generation guardrail check), repetition and echo guards. |
| `turn_origin.py`, `turn_priority.py`, `turn_scheduler.py` | Origin of a turn (`desktop` or `remote`), priority tiers (PTT 0, direct 1, stream and agenda 2), and the thread-safe dispatch queue. |
| `decision/` | Laya decision layer. One `DecisionBus` per process. With no worker configured, every turn takes the rule path. |
| `speech/` | `router.py` (the one thread that calls `_hablar`), sentence splitter, streaming speech, `tts_sanitizer.py`, `backends/tts_piper.py`, VRM audio. |
| `providers/` | `cloud/cloud_llm_client.py` (OpenAI-compatible, no engine imports), `local/ollama_startup.py`, `local/vision_daemon.py`, `llm_tiers.py`. |
| `memory/` | Kira's memorias in SQLite (`memorias.db`): promotion, episodic merge, digest. |
| `editorial/` | Editorial cards in SQLite (`cards.db`), card matching and retrieval, agenda bridge. |
| `agenda/` | Agenda topic persistence. |
| `web/` | Web lookup: search, fetch, ranking, freshness, trigger rules. |
| `vision/` | Vision on demand: screen capture, gate, local or cloud describer, and a controller with states `off -> starting -> warming -> ready`. |
| `observability/` | `health_monitor.py` (VRAM, Ollama, RTF, Ollama watchdog) and agent notices. |
| `usage/`, `music/`, `profiles/`, `self_knowledge/`, `model_downloads/`, `scheduling/` | Content-free usage ledger, music library, profiles and personalization, self-knowledge card, in-app model downloads (ADR-063), turn stamping. |
| `temp_file_cleanup.py`, `ollama_locator.py`, `update_check.py`, `install_identity.py` | Housekeeping, Ollama discovery, update check, install identity. |

## 4. Main turn pipeline

```
input door -> dispatch -> priority queue -> decision -> context -> LLM
           -> guardrails -> speech (TTS) -> avatar / OBS / UI events
```

1. **Input doors.**
   - Typed owner turn: UI `POST /api/chat/turn` -> `direct_turn.submit_direct_turn`
     (source `direct`, origin `desktop`).
   - Phone: gateway `POST /turn` -> the same `submit_direct_turn` (origin `remote`).
   - Push-to-talk: F10 bridge or `POST /api/ptt/*`. The transcript arrives as text
     from the LiveAudio STT WebSocket. `api/owner_speech_dispatch.py` drops Whisper
     silence hallucinations before dispatch.
   - Viewer chat: `/api/stream/chat-live*` -> `smart_aggregator/chat_source.py` ->
     filters -> counters, vibe, highlights and agenda signals. Raw chat does not
     go into prompts, logs or persistence.
   - Agenda: `api/agenda_driver.py` ticks the Kira agenda controller, which feeds
     approved topics to the motor.
2. **Dispatch.** `api/dispatch.py` admits the command through a bounded queue and
   an idempotency TTL cache. HTTP mapping (200/404/409/429) is done by the
   endpoint. The motor queue orders by `turn_priority`: PTT first, then direct
   (always above stream), then stream and agenda (their relative order is a
   streamer setting).
3. **Decision.** `_turn_decisions` in `engine/llm_engine_decisions.py` runs one
   `DecisionBus.decide()` per owner turn, before vision and generation. Laya
   answers when its worker is configured. Otherwise the rule path decides.
4. **Context.** An optional vision look (`engine/llm_engine_vision.py`) and an
   optional web lookup (`engine/llm_engine_web.py`) run first. Then memorias and
   editorial cards are retrieved, and `context/prompt_assembler.py` builds the
   request inside the context budget.
5. **LLM.** `engine/llm_inference_service.py` sends the request to Ollama (local)
   or to the cloud OpenAI-compatible client, per `config/llm_provider.py`. Tiers
   come from `config/llm_tiers.json`. Local direct and PTT turns stream
   sentence by sentence when `LLM_STREAMING_ENABLED` is on. It defaults on
   (`config/settings.py::_resolve_llm_streaming`), and the adverse-path
   validation is still open (see section 11).
6. **Guardrails.** `config/guardrail_policy.py` is the rule registry. Rules marked
   `locked` cannot be disabled. Rules marked `toggleable` (persona and style) can.
   `context/generation_evaluator.py` inspects finished output. The streamed path
   checks each sentence before it is spoken. `speech/tts_sanitizer.py` cleans the
   text before TTS.
7. **Speech.** `speech/router.py` is the only caller of `_hablar`. It is armed only
   by `EngineHost` (`_speech_router_enabled`, `_speech_interrupt_enabled`). TTS is
   Edge-TTS, which has a circuit breaker (`EDGE_BREAKER_*` in `config/settings.py`),
   or Piper (`speech/backends/tts_piper.py`).
8. **Presentation.** Speaking and processing events go through
   `avatar/obs_runtime.py` to OBS (`avatar/obs_client.py`). VRM playback state goes
   to `routers/avatar_vrm.py` and the UI avatar feature. `api/engine_host.py`
   holds three sinks: `EventLogSink` (events), `ChatReplySink` (replies) and
   `ChatFeedSink` (stream feed). The UI reads them through `GET /api/events`,
   `/api/chat/last-reply` and `/api/chat/speech-progress`.

## 5. UI feature map (`OpenCohost_UI/src/features/`)

| Folder | Role |
|---|---|
| `shell/` | App layout and `BackendGate`: startup, routing to setup, backend failure screens. |
| `setup/` | Setup wizard: flow, steps (cloud key, model, voice, personalization, inference test), state machine, checkpoint resume. |
| `controles/` | Controls panel cards: model, provider, guardrails, decisions, reply length, pronunciation, OBS, PTT, avatar, search key, self-knowledge. |
| `experiencia/` | Conversation view: transcript, speech playback bar, stream chat panel, logs, usage activity, vision look controls and thumbnails. |
| `agenda/` | Agenda panel: topics and session settings. |
| `avatar-vrm/` | VRM avatar rendering (`@pixiv/three-vrm`): components, domain model, routes. |
| `commands/` | Composer command panel and command registry. |
| `desktop-companion/` | Companion window for desktop mode: vision look, gestures, operator turn channel. |
| `memoria/` | Kira's memories, memory layers, editorial cards, personalization card, active-card controls. |
| `models/` | In-app model downloads (`downloads/`). |
| `musica/` | Music panel. |
| `perfiles/` | Profile switcher, editor and playlist. |
| `remote/` | Link-phone panel, pairing code, paired-device list. |
| `stream/` | Stream panel: viewer-chat connect, disconnect and limits. |
| `shared/` | LLM readiness card and hook shared across features. |

Other `src/` directories: `api/` (typed client per API surface; `types.gen.ts` is
generated from `openapi.snapshot.json`), `phone-avatar/` (phone bundle, built
into `opencohost/gateway/static/`), `state/`, `store/`, `ui/` (shared primitives),
`i18n/`, `theme/`, `lib/`.

## 6. Tauri shell (`OpenCohost_UI/src-tauri/src/`)

- `backend/`: `supervisor.rs` (spawn or adopt the API, stop it), `config.rs`
  (`backend.config.json`), `health.rs` (probe `GET /api/health` on loopback),
  `token.rs` (reads the operator token with retry), `identity.rs` (checks that the
  answering backend is this install's build), `diagnostics.rs` (closed,
  IPC-safe diagnostics), `shell_log.rs` (rotation to `.1`), `shutdown.rs`
  (`POST /api/system/shutdown`), `ptt.rs` (F10 bridge child), `prefs.rs` (shell
  preferences read at exit, such as unloading models on close).
- `commands/`: IPC commands for backend, first run, and `browser.rs` (opens only
  http and https links in the OS browser).
- `first_run/`: data-root selection, first-run status and service.
- `provisioning/`: managed Python runtime and model artifacts. Downloads are
  SHA-256 verified. Zip extraction has size limits. Operations have cancellation
  and an operation lock. `native_runtime.rs` places a verified CRT next to the
  managed Python executables.
- `runtime/`: runtime manifest and launch handoff (`handoff.rs`, `manifest.rs`).
- `platform/windows/`: Job Object (child lifetime) and registry handoff.
- `desktop_companion.rs`: companion-window state for desktop mode.
- `lib.rs`: a single-instance plugin (a second launch focuses the existing window),
  the native file dialog (the only way the webview gets absolute paths), and a
  window that stays hidden until first paint, with a 4-second fallback.

## 7. Configuration and persistence

- **Settings:** `config/settings.py` holds the environment flags (`OPENCOHOST_*`)
  and the paths. `config/storage.py` resolves the data root: the
  `OPENCOHOST_DATA_ROOT` override, otherwise `%APPDATA%\OpenCohost` in a frozen
  Windows build, otherwise the repository root in development.
- **Files under the data root:**
  - `config/`: `llm_keys.json` (path of the LLM key store; see Secrets),
    `api_tokens.json` (operator and agent tokens), `setup_state.json`,
    `setup_checkpoint.json`, `self_knowledge.json`, `memorias_notice.json`.
  - `data/`: `editorial_cards/cards.db`, `memorias/memorias.db`,
    `usage/usage_ledger.db`, `stream_admin/oauth_tokens.json`.
  - `logs/`: `opencohost_<timestamp>.log` from `config/logger.py`. Override with
    `OPENCOHOST_LOG_DIR`.
  - Shell preferences: `%APPDATA%\OpenCohost\config\shell_prefs.json`.
- **Secrets:** the OS keyring (`security/keyring_store.py`). `OAuthStore` selects
  the keyring for the `llm_keys.json` path. The search API key uses the keyring
  service `opencohost:search`. Stream OAuth tokens are a local file
  (`token_storage: local_file_mvp` in `config/stream_admin.yaml`).
- **Privacy rules:** raw viewer chat never reaches prompts, diagnostics, logs or
  persistence (`smart_aggregator` design decision). The usage ledger is
  content-free. `docs/PRIVACY.md` is the user-facing statement.

## 8. Setup wizard and checkpoint

- Status lives in `config/setup_state.py`, as `setup_state.json` with
  `pending | skipped | completed`. A missing or corrupt file counts as `pending`.
- The checkpoint lives in `config/setup_checkpoint.py`, as `setup_checkpoint.json`
  with `version`, `step`, `answers` and `updated_at`. Only an allowlist of
  non-secret choices is stored. Writes are atomic.
- API (`api/routers/setup.py`): `GET/PUT /api/setup/state`,
  `PUT/DELETE /api/setup/checkpoint`, `POST /api/setup/inference-test`.
- UI: `features/setup/SetupFlow.tsx`, `setupMachine.ts`, `setupCheckpoint.ts`.
  `BackendGate` routes to setup while the status is `pending`.
- Tauri side: `first_run/` handles the data root and first-run status.

## 9. Testing layout

- **Python:** `tests/` holds about 490 `test_*.py` files, with `conftest.py`,
  `fixtures/`, `live_cloud/` and `realenv/`; `pytest.ini` is at the repo root.
  Focused run: `python -m pytest tests/test_llm_tiers.py -q`. Trust tiers: ADR-042.
  `docs/TESTING.md` has the catalog, but its counts are a 2026-06-07 snapshot.
- **Runtime gates:** `docs/runtime-validation-plan.md` and
  `docs/RUNTIME_SMOKE_HARNESS.md`. Any runtime check of an `EngineHost`-gated
  feature has to run on the Tauri surface. Otherwise it measures nothing.
- **UI:** Vitest. `pnpm test` runs `pretest`, which regenerates the API types from
  the offline snapshot. About 210 `*.test.ts(x)` files live under `src/`.
  `pnpm check:api` compares a running backend's `/openapi.json` against the
  snapshot.
- **Rust:** unit tests in `src-tauri/src/**/tests.rs` and integration tests in
  `src-tauri/tests/`. The exact run command in CI is **(unverified)**.

## 10. Ownership rules and current decisions

- **Composition roots.** `api/main.py::create_app` builds the HTTP app.
  `api/engine_host.py` builds the engine and is the only place that arms the speech
  flags. Routers import shared objects from `api/shared.py`, never `main.py`.
- **Single worker.** The API must run with one worker. `EngineHost` takes a
  lockfile, so a second engine cannot grab the audio device.
- **Turn priority.** PTT, then owner-typed direct turns, then stream and agenda
  (`core/turn_priority.py`).
- **Remote gateway.** A separate app on loopback only, with no shared middleware
  and no CORS. Exposure outside the machine is an owner decision (ADR-067).
- **Retired.** The CustomTkinter UI (commit `1eecab7`) and Qwen3-TTS, which is
  retired to a legacy folder outside this repository and not ported. Do not
  reintroduce either.
- **Local-first.** Ollama is the default model path. Cloud providers are opt-in
  and are described in `docs/PRIVACY.md` and `docs/TRUST_MODEL.md`.

## 11. Open runtime gates

These are not validated. Do not describe them as done.

- **LLM output streaming** is on by default, but the adverse path is not validated.
  That covers a forced stall mid-stream, rollback, freeing the Ollama runner on
  socket close, and the queue continuing.
- **Chat ingestion** and the **clause sanitizer** still need live-session evidence.
- **Real audio devices, OBS, and YouTube or OAuth** need runtime validation. Unit
  tests do not prove them (ADR-042).
- **Packaging and installer** are not validated. Public docs must not claim
  readiness.

## 12. Related documents

- Historical, describing the retired CustomTkinter shell:
  [`UI_ARCHITECTURE.md`](UI_ARCHITECTURE.md), [`modules/ui-shell.md`](modules/ui-shell.md).
- Current modules: [`modules/runtime-speech.md`](modules/runtime-speech.md),
  [`desktop-companion.md`](desktop-companion.md), [`api-reference.md`](api-reference.md).
- Trust and privacy: [`PRIVACY.md`](PRIVACY.md), [`TRUST_MODEL.md`](TRUST_MODEL.md).
- Testing: [`TESTING.md`](TESTING.md), [`RUNTIME_SMOKE_HARNESS.md`](RUNTIME_SMOKE_HARNESS.md).
- Decisions (examples): ADR-022 (Ollama over llama.cpp), ADR-028 (memory),
  ADR-039 (clause sanitizer), ADR-042 (test trust tiers), ADR-063 (in-app model
  downloads), ADR-067 (remote voice gateway). The full set is in [`adr/`](adr/).
- Index of every document: [`INDEX.md`](INDEX.md).
