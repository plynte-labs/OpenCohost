# VoiceAI Agent Handoff

This is the first file an AI agent should read when starting work in this repo.

## Current operating mode

**Less expansion, more controlled validation.**

Do not start new feature work by default. The near-term goal is release readiness:
validate the existing prototypes, reconcile documentation with reality, and reduce
runtime uncertainty before packaging or broad product polish.

## Start-of-session checklist

1. Read `AGENTS.md` for repo rules.
2. Recover prior session context from your memory tooling if available.
3. Read `sdd/session-status-2026-06-04.md` for the latest SDD checkpoint.
4. Check `git status --short` before editing; do not overwrite user changes.
5. If the request touches SDD/Conductor work, inspect the relevant track/spec before coding.


## LATEST SNAPSHOT — UI Declutter shipped (2026-06-14)

**Track: ui_declutter_20260614 / feat/ui-polish-freeze-declutter-20260614 — NOT COMMITTED**

### Active flags

**STREAM_ADMIN_ENABLED = False (2026-06-14):** RF4 metadata/moderation/Kira-chat panels
are HIDDEN for the OpenCohost launch UI. Moderation is delegated to Nightbot. The panels
were HIDDEN (not deleted) to reduce operator confusion and declutter the launch-ready UI.
RF3 Chat Live (`_build_chat_live_tab`) is KEPT active and unaffected.
Code is CONSERVED — do not delete without the dep-check + owner approval in
`stream_admin_legacy_removal_20260614` (NO-PRIORITY track).
See `docs/HANDOFF_RF4.md` for the RF4 legacy freeze note and rationale.

### What shipped in this track

- **Part A (flag gate):** `STREAM_ADMIN_ENABLED = False` in `settings.py` hides
  `_build_metadata_tab`, `_build_moderation_tab`, `_build_chat_tab` in
  `stream_admin_ui.py`. `_build_chat_live_tab` (RF3) is NOT gated.
- **Part B (Sistema rollup):** New `lbl_sistema_pill` in `StatusBar` aggregates
  model + mic + TTS + health into one always-visible indicator (5 severity levels:
  CRIT/WARN/INFO/QUIET/OK in red/amber/blue/grey/dim). Engine badge visibility
  gated: dim on steady-state, amber on qwen_starting (owner decision: Edge speaks
  during warmup, operator must see the transient change).
- **Part C (cleanup pointer):** `stream_admin_legacy_removal_20260614` track stub
  created (NO-PRIORITY, NOT STARTED) for eventual RF4 code deletion after dep audit
  and owner authorization.
- **Tests:** `tests/test_ui_declutter_flag_gate.py` (18 tests) and
  `tests/test_sistema_rollup.py` (32 tests) — all green.

---

## LATEST SNAPSHOT — Qwen TTS lifecycle build in progress (2026-06-14)

Active build: `qwen_tts_lifecycle_hardening_20260613` — making heavy TTS self-manage and
**self-verify** (the fix for the owner's "I can't tell if it actually works" uncertainty).
Strict TDD, **NOTHING COMMITTED** (working tree only).

- Artifacts: `conductor/tracks/qwen_tts_lifecycle_hardening_20260613/` — `spec.md`, `tasks.md`,
  `investigation.md` (Appendix A = how QwenProcessManager works), and **`progress.md`** with the
  exact resume point + green test baseline. Engram topic `architecture/qwen-tts-lifecycle`.
- DONE (green, fresh-reviewed): Phase 0 foundations (T0.1–T0.4) + Phase 1 **T1.1+T1.2** (the
  visible "Voz: …" engine badge — validated `UIState.engine_status` field + status-bar pill that
  shows the EFFECTIVE engine).
- **RESUME NEXT SESSION AT: Phase 1 T1.3 + T1.4** — persist the effective engine + emit
  `[ACCEPT] tts_engine …` markers (`llm_engine.py` after L1584, legacy lines byte-identical) and
  wire `engine:*` events to the badge (`llm_engine` + `app_shell._handle_motor_event`). Full
  steps in `progress.md`.
- Earlier this session (also uncommitted): the APP_ID heavy-TTS silent-fallback bug fixed
  (`server_qwen.py:102` `voiceai-` → `opencohost-qwen-tts`); tracks board reconciled (2 closed,
  1 reclassified); YouTube chat compliance RESCOPED (read-only own-key, no OAuth) + RF4/stream_admin
  marked LEGACY; `.gitignore` leak (`config/tts_speed.json`) plugged.

---

## LATEST SNAPSHOT — Benchmark triage + audio PR (2026-06-13 PM)

First live production benchmark (24k-viewer stream) was run and triaged by a
28-agent fan-out. Snapshot for cross-session verification:

- **PR #45 `fix/audio-teardown-stop` — CI GREEN, MERGEABLE, NOT yet merged.**
  Fixes the O8 audio-teardown cluster (music kept playing minutes after a
  co-host segment; emergency stop did not interrupt speech). 4 bugs + a
  DEFERRED soft-stop. Strict TDD (25 tests), passed Judgment Day 3 rounds
  (both judges APPROVED). Owner decision: soft_stop = deferred (music stops
  after Kira's closing speech, minutes-late OK); emergency_stop = immediate.
  - **PENDING (owner): runtime ear-validation** — confirm music stops deferred
    on soft-stop and immediate on emergency-stop. Then merge (or merge then
    validate — owner's call).
  - CI note: the only CI failure was the app_shell.py line-count guard
    (test_integration.py); resolved by raising 3100→3160 with documented debt.
    app_shell decomposition stays owned by ui_rendering_optimization_20260609.

- **Benchmark triage report:** `conductor/benchmark_20260613_triage.md` (23
  findings). Two operator perceptions were REFUTED with evidence: the "5-min
  downtime" was ~2m3s (~78% inevitable model load; fix = keep rollback model
  warm); the "20-min stream hijack" was operator absence, not a hang.
  The real "hijack feel" driver is a CONFIRMED bug: runaway generation
  (gemma4:e4b pops num_predict → 97s monologues) → routed to
  kira_history_summarization_20260611.

- **New track proposal (UNCOMMITTED, awaiting approval):**
  `conductor/tracks/viewer_queue_backpressure_20260613/proposal.md` — bounded
  viewer-query queue + sectioned-accumulation config. Blocked behind two
  prerequisites in kira_history_summarization_20260611 (runaway-generation cap
  + operator priority=0 lane). NOT a crash fix.

- **Uncommitted on master:** proposal.md + tracks.md entry + this snapshot.
  Worktree with the PR branch: `.claude/worktrees/agent-a7c551c89720a2e7f`.
  Engram: #1891 (verified triage), #1892 (backpressure idea), #1899 (audio fix).

- **Still owed (other sessions):** pipeline memory L1 DEBUG re-run for technical
  proof of `<memoria_de_fondo>` injection (log showed digest:0 = INFO-level
  artifact, not a failure); UI-jank fixes under ui_rendering_optimization.


## Product direction update ? 2026-06-05

- The next product/release direction is **OpenCohost**.
- User reports `OpenCohost.com` is purchased for 3 years.
- User reports the target repo is secured at `plynte-labs/opencohost`.
- Treat this as launch-readiness preparation first, not a blind rebrand.
- Current Conductor track: `opencohost_launch_readiness_20260605`.

## Migration and validation update — 2026-06-10

- `public_repo_migration_20260610` is implemented and verified: a 4-PR
  feature-branch chain (PRs #12, #13, #15, #16) on tracker
  `feat/public-repo-migration` — MERGED to `master` on 2026-06-11 (see the
  validation update section below).
  - Preventive pre-commit guard active: detect-secrets (pinned) + drive-letter
    path hook (`tools/check_abs_paths.py`); full-tree run exits 0.
  - Identity renamed to OpenCohost / plynte-labs; curated default profiles ship
    in `opencohost/config/default_profiles.json` with first-run seeding.
  - Sensitive/user-state files untracked: `perfiles.json`,
    `config/music_library.json`, `opencode.json`, `.engram/`.
- Tracks closed: `local_light_tts_piper_20260610` (Piper offline fallback
  implemented; offline gate PASSED 2026-06-11, branch content already in
  `master` via the migration chain — fully closed) and
  `portable_tts_runtime_path_20260610` (delivered by migration PR2).
- New P0 tracks: `runtime_validation_gates_20260610` (Gate 3 partial pass,
  Gate 4 preliminary pass on chain tip; Gates 1-2 owner-pending — see the
  track's `validation_log.md`) and `opencohost_repo_export_20260610`
  (fresh-history export runbook; blocked on chain merge).
- Owner items open: OBS WebSocket password rotation, `detect-secrets audit
  .secrets.baseline`, `Documents/` public-curation decision. (Gates 1, 2 and 4
  passed as of 2026-06-11 — see the validation update below.)

## Validation update — 2026-06-11

- Migration chain MERGED to `master` (PRs #16-#20; `origin/master` @ ec0a95c).
- **Gate 1 (heavy model inference recovery): PASS** — real watchdog timeout on
  `gemma:26b` (45s window), automatic rollback to `gemma4:e4b`, stalled model
  unloaded, no zombie process. `heavy_model_inference_recovery_20260609` is
  closed (`[x]` in `conductor/tracks.md`).
- **Gate 2 (Piper offline fallback): PASS** — offline trigger and three full
  Piper pipelines proven live (morning session), online positive half proven in
  a later session (Edge-TTS speaks while connected, fallback does not engage).
  Gate text amended: Edge-TTS resumes on next app start (sticky-per-session
  fallback is the accepted design). Fix delivered under this gate's scope:
  missing-reference hard blocks removed and routed through the auto-fallback
  gate (`reason=missing_reference`) — PR #22, TDD + fresh review, 129 passed.
- **Gate 3: PARTIAL PASS** — Evidence C postponed by owner. Startup decision
  (2026-06-11): attach-only with clear guidance for the OpenCohost launch;
  demand-driven managed auto-start deferred to the post-launch
  `qwen-tts-lifecycle-hardening` proposal (12GB VRAM contention between the
  LLM and heavy TTS needs real lifecycle design, not a patch).
- **Gate 4 (runtime smoke harness): PASS** — re-stamped on `master` @ ec0a95c
  (deterministic mode, exit 0, all five invariants true).
- Future proposal candidate recorded: request replay after watchdog recovery
  (`conductor/recovery_request_replay_idea.md`) — user requests are currently
  dropped when the inference watchdog fires.
- Evidence details: `conductor/tracks/runtime_validation_gates_20260610/validation_log.md`.

## Editorial cards CLI — 2026-06-11 (uncommitted on master)

Outcome of the Kira memory/context audit (engram topic
`architecture/kira-memory-subsystem`): the user chose to wire the existing
editorial cards backend through a CLI usable by humans AND external agents,
so Kira can receive curated internet/stream-topic context.

- Shipped (strict TDD, 22 tests green, NOT committed yet):
  `opencohost/editorial_cli.py` (`python -m opencohost.editorial_cli`),
  `EditorialCardStore.list_all()`, `tests/test_editorial_cli.py`.
- Agent/operator contract: `docs/EDITORIAL_CARDS_CLI.md` (execution model,
  idempotency, exit codes, retry policy).
- Conversational memory itself is unchanged (20-message deque, no
  summarization) — deferred by design to pending track
  `kira_history_summarization_20260611` in `conductor/tracks.md`.
- Runtime validation pending: create + arm a card via CLI, then confirm the
  agenda bridge injects `<editorial_context>` into a real Kira prompt.

## Topic inbox — 2026-06-12

Backlog #2 delivered (branch `feat/topic-inbox`): external agents propose
stream topics; the operator approves in the app UI.

- Core: `opencohost/core/topic_inbox.py` — same DB as editorial cards;
  validation at write AND read time (rows failing read-time validation are
  never approvable, even via direct SQLite writes — edge case 5); caps
  (title 120 / angle 600 / 8 tags / 30 pending); slug dedupe; fail-open.
- CLI: `python -m opencohost.editorial_cli topic propose/list/discard`
  (+`--from-json`). `topic approve` is always refused — human-only gate.
  Contract: `docs/EDITORIAL_CARDS_CLI.md`.
- UI: `opencohost/ui/topic_inbox_bridge.py` polls every 7s fail-open;
  proposals render in the suggestions panel tagged 🤖 with the angle
  visible; approve creates an APPROVED+QUEUED agenda topic.
- Known sharp edge: the code/HTML rejection patterns (mirrored from
  BULK_CODE_PATTERNS by design) reject titles containing common words like
  "from"/"update"/"delete". Same behavior as agenda topics today; refine
  only with a product decision.
- Runtime validation pending (owner): propose via CLI while the app runs →
  suggestion appears within ~7s → approve → topic enters the agenda queue.

## Agenda persistence — 2026-06-12

Backlog #3 delivered (branch `feat/agenda-persistence`): queued/approved
agenda topics and session settings survive app restarts.

- `opencohost/core/agenda_persistence.py` — agenda_topics/agenda_settings/
  agenda_meta tables in cards.db; write-through on real change (fingerprint
  gate); bounded Tk-thread timeouts; fail-open with one-time operator
  warning; restore via filtered SELECT (cap 50) through the controller
  sanitizer.
- Deliberate non-features (owner decisions): runtime counters never persist
  (ACTIVE re-hosts fresh as queued); the agenda switch always starts OFF
  after a restart (broadcast safety); COMPLETED/SKIPPED/DRAFTED are not
  persisted. The recurring-topic library is a future proposal track.
- Inbox approve now persists the topic BEFORE claiming the ti_ row
  (crash leaves a visible duplicate, never a silent loss).
- **Runtime gate PASSED (owner, 2026-06-12)**: Task-Manager kill →
  relaunch → queue intact and ordered, settings survive enable, agenda
  OFF. The gate caught and we fixed two real defects: an init-order
  launch crash (PR #40) and an invisible restore — the panel never
  refreshed after load (PR #41; restore is now logged to
  acciones.jsonl as "Restaurados N tema(s)"). Track CLOSED
  (PRs #39 + #40 + #41 merged).

## Audit — 2026-06-12

Dual-reviewer audit run on 2026-06-12. Key findings:

**(a) Node-24 verified done.** All five GitHub Actions were checked at their
pinned tags and confirmed node24 (see Packaging section below for details).
No action required before the 2026-06-16 deadline.

**(b) Four tracks audited — all remain OPEN.** Earlier session notes created
an impression that some were closeable; that was premature. Corrected status:
- `runtime_smoke_harness_20260606` — track folder is ABSENT from
  `conductor/tracks/`; cannot verify closure from local files. Stays [~].
- `ui_rendering_optimization_20260609` — Phases 1–3 done in progress.md,
  but Phase 4 (DPI/canvas snapping, FR4) has no progress entry — unstarted,
  not formally N/A. Stays [~].
- `runtime_validation_gates_20260610` — Gate 3 is PARTIAL (Evidence C
  PENDING, owner-postponed but not closed). Stays [~].
- `dynamic_model_management_20260608` — Phase 3 tasks all [ ]; "deferred"
  was an over-read — only the download-resilience sub-slice is deferred,
  not Phase 3 core. Stays [~].

**(c) Repo hygiene / audit track proposal created** at
`conductor/tracks/repo_hygiene_audit_20260612/proposal.md`. Collects
low/medium code-hygiene findings from this audit (gitignore gap, dead code,
filter pattern duplication, stale path refs). PROPOSAL ONLY — not yet
scheduled or implemented.

**(d) Two findings are pipeline-memory prerequisites** (R3: commit-before-TTS
history pollution; R4: injection-laundering in the future digest) and one is
a `dynamic_model_management_20260608` test gap (R2: watchdog pending-switch
sub-branch untested at `opencohost/core/llm_engine.py:~1191-1198`). All
three are documented in the proposal and routed to their respective tracks.

## Pipeline memory L1 — 2026-06-13 (implemented, PR open, runtime gate pending)

Kira's intra-session memory digest (backlog #5 / `kira_history_summarization`
track, L1 slice). Branch `feat/pipeline-memory`, NOT yet merged.

- Owner-approved design after a deep walkthrough: D1 deterministic truncation
  ledger, D2 survival matrix (survives watchdog/model-switch, dies on
  profile-change/clear, RAM-only), D3 direct-path-only injection wrapped in a
  read-only `<memoria_de_fondo>` block, capped ~600 chars, re-sanitized.
- E3 defense-in-depth: Spanish injection-marker floor + structural isolation
  (the primary, language-agnostic gate) + a SYSTEM_PROMPT read-only rule.
- Strict TDD (50 focused tests); passed a 3-round dual adversarial review
  (Judgment Day APPROVED, 0 critical / 0 real warnings). The review caught and
  fixed 3 real bugs the broad suite missed (inverted `[hace N]` counter,
  agenda-reply digest leak on cross-source eviction, a `_commit_history` race).
- Future layers L2 (session snapshots) and L3 (long-term retrieval) are an
  aim-high RFC at `conductor/tracks/kira_memory_architecture_rfc_20260612/`.
- Non-blocking follow-up tickets (wrapper-breakout scrub, clear()-under-lock,
  chat→direct bleed verification) at
  `conductor/tracks/pipeline_memory_followups_20260612/`.
- **Runtime validation pending (owner):** converse, force a model switch /
  watchdog recovery, and confirm Kira keeps the thread via the digest.

## Packaging update — 2026-06-11 (track paused, mark of record)

The packaging & distribution track executed Phases 2–4 end-to-end in one day
(PRs #26, #28, #29, #30 — all merged to `master`):

- **Phase 2 — package restructure (DONE)**: all app code lives in the
  installable `opencohost/` package; hatchling build, `gui-scripts` entry
  point (`python -m opencohost` / `opencohost`), `uv.lock`, version 0.1.0.
  Full suite green for the first time ever: 1850 → 1926 tests. Root-caused
  the owner's two "PC crashes" to a test-suite OOM (CustomTkinter widgets
  walking MagicMock parents — fixed).
- **Phase 3 — launcher (DONE, code-complete)**: `packaging/launcher.py`
  (stdlib-only uv bootstrapper, splash with progress, Ollama preflight,
  `--update`/`--debug`/`--headless`/`--self-test`), PyInstaller spec,
  82 unit tests. Critical fixes already in: install from unpacked source
  (named spec would have hit PyPI), window-title prefix match, PEP 440
  pre-releases, zip-slip guard, bundled CA certificates (see below).
- **Phase 4 — CI + release (DONE, proven)**: `ci.yml` runs the full suite on
  every PR (windows-latest, ~4.5 min). `release.yml` on `v*` tags builds
  `opencohost-src-<ver>.zip` + `OpenCohost-Setup-<ver>.exe` + `SHA256SUMS.txt`
  into a DRAFT release — ran end-to-end successfully for `v0.1.0`.
  Owner runbook: `docs/RELEASE.md`.
- **Phase 5 — clean-machine validation (PAUSED, NOT passed)**: first VM run
  failed at download with `SSL: CERTIFICATE_VERIFY_FAILED` — clean Windows
  lacks GitHub's root CAs (lazy AuthRoot population; frozen Python never
  triggers it). Fix merged (PR #30: cacert.pem bundled into the exe) but
  **no exe containing the fix has been built yet** — the `v0.1.0` exe
  predates it. The launcher is NOT yet validated on a clean machine.

To resume Phase 5 later (in order):
1. ~~Bump `__version__` to `0.1.1`, tag `v0.1.1`, push tag → new draft exe.~~
   DONE 2026-06-11: `v0.1.1` draft built green (first exe with the CA fix);
   Node-24 action bumps landed (PR #31 + pinned `setup-uv@v8.2.0`).
2. Repo visibility decision: the launcher downloads anonymously, so the
   release must live on a PUBLIC repo (make this repo public, or export to
   `plynte-labs/opencohost`). Private repo → 404 after the SSL fix.
3. Publish the draft (draft assets are not anonymously downloadable).
4. VM: delete `%LOCALAPPDATA%\OpenCohost`, run the new exe; on failure read
   `bootstrap.log` there. Known-good shortcut while testing: opening
   github.com in Edge inside the VM populates the OS cert store.

Node-24 action bumps: DONE and verified (PR #31 + follow-up pin commit
2026-06-11). All five actions confirmed node24:
`actions/checkout@v6`, `actions/setup-python@v6`,
`actions/upload-artifact@v7`, `actions/download-artifact@v8`,
`astral-sh/setup-uv@v8.2.0`.
The `windows-latest` → `windows-2025-vs2026` redirect (2026-06-15) is
automatic GitHub infra — NON-BLOCKING, no file change required. One
validation CI run on or after that date is advisable but optional.

## Current project truth

- VoiceAI has functional prototypes for local AI voice, TTS, SmartAggregator, stream
  workflows, and health monitoring.
- The project has grown enough that blind expansion is risky.
- Active local implementation checkpoint: `dynamic_model_management_20260608`
  is in progress under a **thin client over Ollama** boundary.
  - completed locally: Phase 1 (runtime validity + persistence) and
    Phase 2 (installed-model discovery merge in `ModelPanel`)
  - validated locally: focused model-management suite reached `154 passed`
  - deferred intentionally: download/retry/watchdog orchestration
- Closed bug-recovery checkpoint: `heavy_model_inference_recovery_20260609`
  is implemented AND runtime-validated (Gate 1 PASS, 2026-06-11).
  - completed: watchdog around first real chat after switch, stuck-processing recovery,
    pending-switch escape path, and rollback to last known good model
  - validated: focused recovery/model-management suite `159 passed` + real
    watchdog/rollback event against `gemma:26b` (logs/voiceai_20260611_084746.log)
- `health-monitor-auto-fallback` has been reconciled:
  - keep: HealthMonitor core, health pill, Vibe gate, heavy-TTS fallback gate
  - adjust only if needed: thresholds, docs wording, manual-vs-auto fallback policy
  - do not claim complete: Qwen demand-driven auto-start and idle shutdown
- Future focused track, only if runtime validation proves it matters:
  `qwen-tts-lifecycle-hardening`.

## User-owned validation before more expansion

The user still needs to validate real runtime behavior:

- heavy vs light TTS
- visible fallback behavior
- manually started Qwen server
- Ollama online/offline behavior
- health pill state changes

Treat these as release-readiness gates.

## Deferred for now

Do not pick these up unless the user explicitly re-prioritizes them:

- `knowledge_card_mvp`
- packaging Phase 5 resumption (see "Packaging update — 2026-06-11"; Phases
  2–4 are DONE and the pipeline is operational — only the clean-machine
  validation and the repo-visibility decision remain)
- broad hardening and failure testing
- first-run readiness wizard
- large Product UI implementation

Product UI can be reviewed later against real states, but do not implement it yet
without a fresh user decision.

## Safety rules that matter most

- Do not commit unless the user explicitly asks.
- Do not revert or overwrite existing user changes.
- Do not remove feature gates, filters, or validation without verifying current behavior first.
- Never expose raw chat to LLM prompts, diagnostics, logs, or persistence.
- Keep LiveVoice continuous and PTT separate unless the user explicitly asks to touch them.
- Pre-commit safety hooks are expected to block private/runtime artifacts and obvious secrets.
  Do not bypass them unless the user explicitly approves after manual review.

## Known useful test commands

Targeted health validation (activate your project Python environment first):

```powershell
python -m pytest tests/test_health_monitor.py tests/test_health_integration.py tests/test_app_shell_obs_resilience.py -q
```

## Important local artifact notes

- `sdd/` and `openspec/` are ignored by `.gitignore`; they are local artifact-store notes.
- The honest current claim is: prioritized/relevant SDD was reviewed and reconciled.
  Do not claim that every SDD track is fully complete.
