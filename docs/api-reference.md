# OpenCohost — Backend API Reference

Local-first HTTP API that the Tauri app (and any client) uses to drive Kira.
The API **orchestrates**; it holds state and manages data. Audio playback and
UI rendering live in the client.

- **Base URL:** `http://127.0.0.1:8765`, falling back to `8770` if 8765 is already
  taken (`OpenCohost_UI/src-tauri/backend.config.default.json`, `run-api.bat`)
- **App factory:** `opencohost/api/main.py` (`create_app`) — it mounts routers and
  holds no routes of its own
- **Routes:** `opencohost/api/routers/`, one module per product surface
- **Models:** `opencohost/api/models.py`
- **Host state:** `opencohost/api/engine_host.py` (`EngineHost`)

> **The running app is the authority on the endpoint list.** Fetch
> `GET /openapi.json`, or open the interactive schema at `/docs`. As measured at
> the time of writing, that is **72 method+path pairs across 60 paths**.
>
> **This file documents 46 of those 72.** Still to be written up here: the
> `/api/agent/*` gateway (8 — see [`AGENT_GATEWAY.md`](AGENT_GATEWAY.md)),
> `/api/ptt/*` (6), `/api/personalization` (3), `/api/llm/provider` and
> `/api/llm/provider/probe` (3), `/api/i18n` (2), `/api/memoria/row/{row_id}`,
> `/api/memoria/import`, `/api/stream/chat-live/messages`, and `/api/events`.
> Until they land, read them from `/openapi.json`.

> **Status:** all endpoints pass the automated test suites (`flux_env`), but the
> parity endpoints added in fase 1/2/3 are **not yet runtime-validated** against
> a live Tauri client. Treat runtime validation as the open release gate.
>
> `✦` marks endpoints/verbs added during the CTK→API→Tauri parity work.

---

## Conventions

- **Error contract:** `503 {detail}` when a subsystem is unavailable (agenda /
  music / host not ready); `422 {detail}` on validation failures or `ValueError`
  from a controller; `404 {detail}` for unknown named resources; `409 / 429` for
  profile-switch conflicts / queue-full.
- **R8 (no raw exposure):** endpoints never return raw chat, persona prompts, or
  memoria content unless explicitly scoped. `GET /api/perfiles` returns names
  only; `GET /api/perfiles/{name}` returns only `prompt`/`use_system`; memoria
  `list`/`stats` are metadata-only.
- **Concurrency:** read-modify-write paths take a lock — `_config_lock`
  (avatar/OBS yaml), `_profiles_lock`, `_cohost_profiles_lock`,
  `host.agenda_lock`, `host.music_lock`.
- **Whitelists:** `/api/commands`, `/api/agenda/topic/action`, and
  `/api/agenda/session/action` gate their verbs against a server-side frozenset —
  unknown verbs are rejected before reaching the engine.

---

## Health · Status · Models · TTS

| Method | Path | Description |
|---|---|---|
| GET | `/api/health` | Liveness probe. |
| GET | `/api/status` | Full runtime status: current model, `is_speaking`/`is_processing`, health state, `avatar_state`, `ollama_warming`, `state_version`. |
| GET | `/api/models` | Model catalog + discovered Ollama models + resolved LLM tiers + active tier. |
| GET | `/api/tts/config` | TTS config: piper voice, `local_only`, speed, engine, heavy-TTS availability. |

## Perfiles — LLM personas

Backing: `opencohost/core/profiles/profiles.py` (`cargar_perfiles` / `guardar_perfiles`).

| Method | Path | Description |
|---|---|---|
| GET | `/api/perfiles` | List profile names only (R8). |
| GET ✦ | `/api/perfiles/{name}` | Single profile — `prompt` + `use_system` only (R8, never chat). |
| POST ✦ | `/api/perfiles` | Create a profile. |
| PUT ✦ | `/api/perfiles/{name}` | Edit + rename (rename preserves the on-disk id). |
| DELETE ✦ | `/api/perfiles/{name}` | Delete (last-profile guard; deleting the active profile is allowed, CTK parity). |
| POST | `/api/perfiles/switch` | Switch active profile (Idempotency-Key, dispatcher). |

## Memoria — Kira's memory

Backing: `opencohost/core/memory/memoria_store.py` (`MemoriaStore`), `opencohost/config/settings.py`
(notice flag), raw SQLite over `MEMORIAS_DB` for reads/purge.

| Method | Path | Description |
|---|---|---|
| GET | `/api/memoria/stats` | Counters: session turns, digest entries, saved memorias, pinned. |
| GET | `/api/memoria/list` | Metadata per memoria (id, timestamps, revision, pinned, private, `inactive` ✦). Metadata-only (R8). |
| POST | `/api/memoria/purge` | Hard-delete all memorias for a profile (raw SQLite). |
| POST ✦ | `/api/memoria/flags` | Toggle pin / private / inactive via `set_flags` — preserves the F5 freeze invariant (pin/private promote `status='curated'`; un-pin never demotes). |
| POST ✦ | `/api/memoria/delete` | Delete one memoria (`delete_row`, idempotent). |
| POST ✦ | `/api/memoria/update` | Edit title/content (`update_row`; inbound-only, length-capped). |
| POST ✦ | `/api/memoria/capture` | Pause/resume auto-capture (dedicated route → `motor.set_memorias_private`; engine dispatch untouched). |
| GET / POST ✦ | `/api/memoria/notice` | Read/set the disclosure-banner dismissed flag (`{dismissed: bool}`, fail-open). |

## Música — orchestration (client plays audio)

Backing: `opencohost/core/music/music_library.py` (`MusicLibrary`) + `MusicState` on
`EngineHost`. The API never drives backend audio; the Tauri client plays.

| Method | Path | Description |
|---|---|---|
| GET | `/api/music/library` | List tracks + moods (read-only, guarded by `music_lock` ✦). |
| POST ✦ | `/api/music/mood` | Set active mood as state (422 on unknown mood). No backend audio. |
| GET ✦ | `/api/music/state` | Read mood + fade state (lets reconnecting clients recover). |
| POST ✦ | `/api/music/fade` | Record fade intent as state (monotonic seq). No backend audio. |
| POST ✦ | `/api/music/import` | Import a track (`{path, mood}`; guards: absolute-only, extension, existence, 200 MB cap). |
| DELETE ✦ | `/api/music/track/{id}` | Remove a track (confined to `library_dir`, idempotent). |
| GET ✦ | `/api/music/track/{id}/audio` | Serve track bytes (`FileResponse`). API is the availability checkpoint — 404 on unknown/missing/invalid; `library_dir`-confined. |

## Agenda — Kira's co-host agenda

Backing: `opencohost/smart_aggregator/kira_agenda_controller.py` +
`opencohost/core/profiles/cohost_profiles.py`. All mutations under `host.agenda_lock`.

| Method | Path | Description |
|---|---|---|
| GET | `/api/agenda` | Full agenda state: active/queued/drafted topics, session settings, metrics. |
| POST | `/api/agenda/topic` | Add a topic (+ `constraints`/`priority`/`response_length` passthrough ✦). |
| POST | `/api/agenda/topic/action` | Verb on a topic: `approve` / `queue` / `remove` / `move` / `reject` ✦ (`reject` sends a drafted suggestion to `SKIPPED`). |
| PUT | `/api/agenda/session` | Update session settings (style, max turns, rhythm, safety mode). |
| POST ✦ | `/api/agenda/session/action` | Session verb: `enable` / `soft_stop` / `emergency_stop`. |
| GET ✦ | `/api/agenda/cohost-profiles` | List saved co-host profiles from disk (defaults fallback). |
| POST ✦ | `/api/agenda/cohost-profiles` | Save a co-host profile (`{name, style, priority, length}`; sanitize + caps 40/600). |
| POST ✦ | `/api/agenda/cohost-profiles/select` | Apply a profile's style to the controller. **Stateless** — persists nothing, no `selected` field. |

## Stream (chat-live)

Backing: chat aggregator. R8: state + limits only.

| Method | Path | Description |
|---|---|---|
| GET | `/api/stream/chat-live` | Source connection state + limits. |
| POST | `/api/stream/chat-live/connect` | Connect a platform source. |
| POST | `/api/stream/chat-live/disconnect` | Disconnect. |
| PUT | `/api/stream/chat-live/limits` | Adjust rate limits. |

## Chat · Commands

| Method | Path | Description |
|---|---|---|
| POST | `/api/commands` | Whitelisted engine verbs (`clear_history`, `set_tts_*`, `switch_model`, `switch_llm_tier`, `set_memorias_private`, …). |
| POST | `/api/chat/turn` | Manual chat turn (empty/whitespace rejected; capped at 4000 chars). |
| GET | `/api/chat/last-reply` | Kira's last reply. |

## OBS · Avatar

Backing: OBS client + `avatar.yaml` (shared file; writes under `_config_lock`).

| Method | Path | Description |
|---|---|---|
| GET / PUT | `/api/obs/config` | Read/update OBS config (password is write-only). |
| POST | `/api/obs/test` | Test OBS connection (bounded, 5s timeout). |
| GET / PUT | `/api/avatar/config` | Read/update avatar config (enabled, mode, `state_images`). |

---

## Backend files

| File | Role |
|---|---|
| `opencohost/api/main.py` | App factory, middleware, lifespan, router mounting. No route handlers. |
| `opencohost/api/routers/` | One module per product surface — every route handler lives here. |
| `opencohost/api/shared.py` | Cross-cutting helpers: locks, response builders, logger. |
| `opencohost/api/deps.py` | Call-time accessors routers use instead of importing `main`. |
| `opencohost/api/models.py` | Pydantic request/response models. |
| `opencohost/api/engine_host.py` | `EngineHost`: motor, monitor, agenda, `music_library`, `MusicState`, `music_lock`, `agenda_lock`. |
| `opencohost/api/dispatch.py` | Command dispatcher (idempotency, `state_version`). |
| `opencohost/core/profiles/profiles.py` | LLM persona persistence (`cargar_perfiles` / `guardar_perfiles`). |
| `opencohost/core/memory/memoria_store.py` | `MemoriaStore` — flags/update/delete with the F5 freeze rule. |
| `opencohost/core/profiles/cohost_profiles.py` | Agenda co-host profile persistence. |
| `opencohost/core/music/music_library.py` | `MusicLibrary` (`add_file`, `remove`, validation) + `AudioBedEngine`. |
| `opencohost/smart_aggregator/kira_agenda_controller.py` | Agenda controller (topics, session, `reject_topic`). |
| `opencohost/config/settings.py` | Feature flags and config (TTS, memoria notice, gates). |

## Test suites

`tests/test_api_*.py` — one focused suite per domain. Parity work is covered by
`test_api_memoria_mutations.py`, `test_api_memoria_notice.py`,
`test_api_perfiles_crud.py`, `test_api_music_state.py`,
`test_api_music_library_mutations.py`, `test_api_cohost_profiles.py`,
`test_api_agenda.py`, plus `test_kira_agenda_controller.py` for `reject_topic`.

Run:

```powershell
python -m pytest tests/test_api_*.py -q -p no:cacheprovider --basetemp=temp/pytest-api
```

`--basetemp` is explicit because pytest's default lands under the system temp
directory, which some Windows setups lock down; any writable path works.
