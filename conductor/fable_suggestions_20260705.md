# Fable 5 Suggestions — OpenCohost Backlog Candidates (2026-07-05)

Author: Fable 5 (final session contribution, owner-requested).
Status: SUGGESTIONS ONLY — none of these are tracks yet. Each needs an explicit
owner decision before any SDD phase starts. Ordered by leverage-per-effort,
grounded in the current codebase (post FastAPI sidecar + Tauri MVP).

Companion tracks designed this same session (separate folders, not repeated here):
- `kira_bilingual_e2e_20260705` — EN/ES across agenda, viewer chat, PTT, TTS, profiles.
- `kira_personalization_onboarding_20260705` — ChatGPT-style "about you" + interview.
- `agent_context_gateway_20260705` — safe CLI/API ingestion for external agents.

---

## 1. `/api/events` — one SSE event stream (API) — HIGHEST LEVERAGE

**What**: A single Server-Sent Events endpoint on the FastAPI sidecar that emits
engine events: `speaking_start` / `speaking_end` (with the sentence being spoken),
`agenda_topic_changed`, `model_switched`, `profile_switched`, `health_changed`,
`memoria_captured`.

**Why**: Today the Tauri app polls (`refetchInterval` 1500ms on last-reply and
agenda events). Every future consumer — OBS overlays, external agents, a mobile
remote — would need its own polling loop. The engine ALREADY produces these
events internally (motor event handlers, UIState observer pattern, ObsRuntime
push chain); the API just never re-emits them. One bounded in-process
subscriber queue → SSE generator closes the gap.

**Surfaces unlocked**: Tauri (kill polling, instant UI), OBS browser-source
overlays (see #2), agent gateway (react-to-events instead of poll).

**Effort**: M (~200-300 lines + tests). No engine changes — subscribe at the
same seam ObsRuntime already uses.

**Risk/care**: R8 still applies — the stream must emit Kira's OWN speech and
state only, never viewer chat text. Reuse the existing last-reply projection
rules verbatim.

## 2. OBS live captions overlay (API + OBS)

**What**: A browser-source HTML page (served by the sidecar or shipped static)
that subscribes to `/api/events` and renders Kira's spoken sentences as stream
captions with the existing TTS-fragment timing.

**Why**: Accessibility + viewer engagement for near-zero engine work. The TTS
pipeline already speaks sentence-by-sentence, so caption granularity is free.
This is the most VISIBLE five-minute win a streamer gets from the API layer.

**Depends on**: #1. **Effort**: S-M (static page + one projection).

## 3. `opencohost doctor` CLI (CLI) — LAUNCH SUPPORT

**What**: `python -m opencohost.doctor` — one command that checks and prints a
report: Ollama reachable + which models are pulled + VRAM headroom, Edge-TTS
reachability, Piper voices present per locale, OBS WebSocket connect, config
files parse (perfiles.json, locale.json, music_library.json), user-data dir
writable, port 8765 free.

**Why**: The public launch will generate "it doesn't work" reports. Every check
already exists somewhere (HealthMonitor, ollama_startup, obs_client, i18n
startup resolver) — doctor just runs them WITHOUT starting the app and prints
actionable lines. Cuts support cost and doubles as the first-run wizard's
backend (first_run_readiness_wizard_20260529 can consume it later).

**Effort**: M. Pure composition + report formatting; strict-TDD friendly
(each check injectable).

## 4. Post-stream session recap (API + Tauri/CTK)

**What**: On session close (or on demand), Kira generates a short recap from
data that ALREADY persists: agenda topics covered (agenda persistence),
memorias captured this session, session duration/model stats. Output: a local
markdown file + `GET /api/recap/latest`. Optional: Kira SPEAKS a 2-line
sign-off summary.

**Why**: Streamers currently get nothing back after a stream. A recap turns
Kira from "co-host during" into "producer after" — differentiator vs every
soundboard-style tool, and it's 100% local (one extra LLM call on data already
on disk — no new privacy surface, host-only sources only).

**Effort**: M. **Care**: recap prompt must consume only host-side stores
(memorias/agenda), never viewer chat (R8 again).

## 5. Profile export/import — shareable persona packs (CLI + apps)

**What**: Export a profile (persona prompt + locale + voice prefs + agenda
style defaults) as a single versioned JSON (`.kirapack`); import with
validation + sanitization (same caps as profile CRUD, id re-minted on import,
never import `use_system` silently).

**Why**: Community growth lever for the public launch — personas are the most
shareable artifact OpenCohost has, and the profile schema is small and stable
(id/prompt/use_system + locale after the bilingual track). MIT repo + shareable
personas = the contribution funnel the export track's README strategy wants.

**Effort**: S-M. **Care**: imported prompts are untrusted text — cap lengths,
strip control chars, show a preview-before-apply in UI (never auto-activate).

## 6. Viewer-chat language bridge (engine, AFTER bilingual track)

**What**: Detect the dominant language of the incoming viewer-chat batch and
let the agenda controller answer chat in that language when it differs from
Kira's locale (per-batch override, not a locale switch; coherence gate stays).

**Why**: A US-facing launch means EN viewers landing on ES streams (and the
reverse). Kira answering a clearly-English question in English is a "wow"
moment that costs one prompt-line + detection, once agenda templates are
bundle-driven.

**Depends on**: `kira_bilingual_e2e_20260705` fully landed (agenda templates in
bundles + per-locale validator patterns — the fail-closed validator rule applies
per-response-language, not just per-session-locale).
**Effort**: S on top of the bilingual track; NOT viable before it.

---

## Explicitly NOT suggested (and why)

- **Cloud LLM engine work** — already explored (`sdd/external-llm-api-engine/explore`);
  gated on the ADR-029 prefix-cache fix + a trust-boundary ADR. Nothing new to add.
- **Hot-swap locale switching** — next-boot-only is the honest design; a
  process-wide cached bundle + live threads makes hot-swap a bug farm.
- **Embeddings/vector memory** — the lexical ceiling is documented
  (engram_simulado track); revisit only after real retrieval-quality pain.
- **Any new feature before the runtime-validation gates in AGENT_HANDOFF.md pass** —
  operating mode is still "less expansion"; everything above is post-validation
  backlog except #3 (doctor), which directly SERVES launch readiness.
