# Project Tracks

This file tracks all major tracks for the project. Each track has its own detailed plan in its respective folder.

---

- [x] **Track: UI God Class Refactor — Split app.py into Modular Components**
  *Link: [./tracks/ui_refactor_20260508/](./tracks/ui_refactor_20260508/)*

---

- [~] **Track: Empaquetado y Distribución (Smart Wrapper / Install UI)**
  *Link: [./tracks/packaging_deploy_20260510/](./tracks/packaging_deploy_20260510/)*
  *Status 2026-06-11: Phases 2-4 DONE and merged (PRs #26, #28, #29, #30 —
  opencohost/ package, launcher + 82 tests, CI, tag-driven release pipeline;
  v0.1.0 draft built end-to-end). Phase 5 (clean-machine validation) PAUSED —
  resume steps in AGENT_HANDOFF.md "Packaging update — 2026-06-11".*

---

- [ ] **Track: Product UI Refactor — Kira-Centered Layout + Avatar/OBS Module**
  *Link: [./tracks/product_ui_kira_avatar_refactor_20260513/](./tracks/product_ui_kira_avatar_refactor_20260513/)*
  *Status 2026-06-13: RECLASSIFIED [~]→[ ] — listed as "Deferred for now, do not implement
  without a fresh user decision" in AGENT_HANDOFF.md, which contradicts in-progress. The
  conductor folder is absent; WIP lives on branch feature/kira-product-ui-redesign.*

---

- [ ] **Track: Hardening & Failure Testing for Kira Agenda, Interruptions, Chat Load, and Installer Readiness**
  *Link: [./tracks/hardening_failure_testing_20260515/](./tracks/hardening_failure_testing_20260515/)*

---

- [x] **Track: Startup and Model Lifecycle Polish for Ollama, OBS, and Memory Release**
  *Link: [./tracks/startup_model_lifecycle_polish_20260522/](./tracks/startup_model_lifecycle_polish_20260522/)*
  *Status 2026-06-13: CLOSED — work merged alongside manual_llm_tier_switching_20260522
  (commit 903b9af "polish startup and model lifecycle", merged via 5adbc8d); that track is
  already [x]. No open tasks identifiable and the folder was never created. Reopen if a
  concrete open item surfaces.*

---

- [x] **Track: Manual LLM Tier Switching — Quality, Balanced, and Fast Models**
  *Link: [./tracks/manual_llm_tier_switching_20260522/](./tracks/manual_llm_tier_switching_20260522/)*

---

- [~] **Track: Editorial Cue Cards MVP — Streamer-Curated One-Turn Context**
  *Link: [./tracks/knowledge_card_mvp_20260525/](./tracks/knowledge_card_mvp_20260525/)*

---

- [~] **Track: Streaming Speech Pipeline — LLM Streaming to Sentence TTS Playback**
  *Link: [./tracks/streaming_speech_pipeline_20260529/](./tracks/streaming_speech_pipeline_20260529/)*

---

- [ ] **Track: First-run Readiness Wizard — External Ollama and Hardware Setup**
  *Link: [./tracks/first_run_readiness_wizard_20260529/](./tracks/first_run_readiness_wizard_20260529/)*

---

- [x] **Track: Ollama Offline UI/UX Guardrails — Prevent Unexpected Switches and Silent Freezing**
  *Link: [./tracks/ollama_offline_ux_guardrails_20260601/](./tracks/ollama_offline_ux_guardrails_20260601/)*


---

- [~] **Track: OpenCohost Launch Readiness ? Brand, Repo Safety, and Release Validation**
  *Link: [./tracks/opencohost_launch_readiness_20260605/](./tracks/opencohost_launch_readiness_20260605/)*

---

- [x] **Track: OBS Runtime Connection Control — Connect on Toggle, Live Test, and Cancel Retry Loop**
  *Link: [./tracks/obs_runtime_connection_control_20260605/](./tracks/obs_runtime_connection_control_20260605/)*

---

- [x] **Track: Cohost Audio Arbitration Crash - Prevent Agenda Prefetch Over Direct Interactions**
  *Link: [./tracks/cohost_audio_arbitration_crash_20260606/](./tracks/cohost_audio_arbitration_crash_20260606/)*

---

- [x] **Track: Crash Reporting Hardening - Python, Tk, Thread, and Native Fatal Logs**
  *Link: [./tracks/crash_reporting_hardening_20260606/](./tracks/crash_reporting_hardening_20260606/)*

---

- [x] **Track: Runtime Smoke Harness - Real App Cohost and Audio Safety Validation**
  *Link: [./tracks/runtime_smoke_harness_20260606/](./tracks/runtime_smoke_harness_20260606/)*
  *Status 2026-06-13: CLOSED — Gate 4 PASSED and re-stamped on master @ ec0a95c
  (deterministic mode, exit 0, all five invariants true). Evidence lives in
  runtime_validation_gates_20260610/validation_log.md. The track folder was never
  created; the closure record is in the validation_gates track.*

---

- [x] **Track: TTS Markdown Emphasis Sanitizer - Preserve Emphasized Words Without Breaking Expressions**
  *Link: [./tracks/tts_markdown_emphasis_sanitizer_20260606/](./tracks/tts_markdown_emphasis_sanitizer_20260606/)*

---

- [x] **Track: Speech Source Lifecycle Hardening - Prevent Stale Audio Ownership State**
  *Link: [./tracks/speech_source_lifecycle_hardening_20260606/](./tracks/speech_source_lifecycle_hardening_20260606/)*

---

- [x] **Track: Speech Start Callback Cleanup - Prevent Stale Speech State on UI Event Failure**
  *Link: [./tracks/speech_start_callback_cleanup_20260606/](./tracks/speech_start_callback_cleanup_20260606/)*

---

- [x] **Track: UI Thread Event Ownership Hardening - Keep Tk Mutations on the Main Loop**
  *Link: [./tracks/ui_thread_event_ownership_hardening_20260606/](./tracks/ui_thread_event_ownership_hardening_20260606/)*

---

- [~] **Track: Thin Model Management over Ollama - Curated UX, Installed Discovery, and Stable Persistence**
  *Link: [./tracks/dynamic_model_management_20260608/](./tracks/dynamic_model_management_20260608/)*

---

- [x] **Track: Heavy Model Inference Recovery - Watchdog, Cancel Path, and Rollback**
  *Link: [./tracks/heavy_model_inference_recovery_20260609/](./tracks/heavy_model_inference_recovery_20260609/)*

---

- [~] **Track: UI Rendering Optimization - VSync, Antialiasing, and Layout Stability**
  *Link: [./tracks/ui_rendering_optimization_20260609/](./tracks/ui_rendering_optimization_20260609/)*

---

- [x] **Track: Portable TTS Runtime Path - Configurable Qwen Server Interpreter**
  *Link: [./tracks/portable_tts_runtime_path_20260610/](./tracks/portable_tts_runtime_path_20260610/)*

---

- [x] **Track: Local Light TTS - Piper/Kokoro Evaluation and Integration (post-Lite)**
  *Link: [./tracks/local_light_tts_piper_20260610/](./tracks/local_light_tts_piper_20260610/)*

---

- [~] **Track: Runtime Validation Gates - Real-World Proof for Implemented Systems**
  *Link: [./tracks/runtime_validation_gates_20260610/](./tracks/runtime_validation_gates_20260610/)*

---

- [ ] **Track: OpenCohost Repo Export - Land the Migration and Move to plynte-labs**
  *Link: [./tracks/opencohost_repo_export_20260610/](./tracks/opencohost_repo_export_20260610/)*

---

- [~] **Track: Kira Conversational Memory - Direct-Path History Summarization**
  *Link: [./tracks/kira_history_summarization_20260611/](./tracks/kira_history_summarization_20260611/)*
  *Status 2026-06-13: L1 (intra-session memory digest) implemented on branch
  feat/pipeline-memory — strict TDD (50 tests), passed a 3-round dual adversarial
  review (Judgment Day APPROVED). Owner-approved design D1/D2/D3 + E3 defense-in-depth.
  Awaiting owner runtime validation gate before closure. Future layers (L2 session
  snapshots, L3 long-term retrieval) captured as an aim-high RFC at
  ./tracks/kira_memory_architecture_rfc_20260612/. Non-blocking follow-ups at
  ./tracks/pipeline_memory_followups_20260612/.*
  *Status 2026-06-11: Deferred by design (option B of the Kira memory audit).
  Direct-path LLM history is a 20-message deque with no summarization, cleared
  on model/profile switch. Editorial cards CLI (shipped 2026-06-11) covers the
  curated-context axis; this track covers conversational memory. Audit details in
  engram topic architecture/kira-memory-subsystem.*

---

- [ ] **Track: Repo Hygiene Audit - Consolidated Low/Med Cleanup Findings**
  *Link: [./tracks/repo_hygiene_audit_20260612/](./tracks/repo_hygiene_audit_20260612/)*
  *Status 2026-06-12: PROPOSAL ONLY — not started. Consolidated low/med
  cleanup findings from the 2026-06-12 dual-reviewer audit: gitignore gap,
  dead code, filter pattern duplication, stale path refs. See proposal.md.*

---

- [ ] **Track: Viewer Queue Backpressure - Bounded Queue and Sectioned Accumulation**
  *Link: [./tracks/viewer_queue_backpressure_20260613/](./tracks/viewer_queue_backpressure_20260613/)*
  *Status 2026-06-13: PROPOSAL ONLY — not started, awaiting user approval.
  Origin: 2026-06-13 live 24k-viewer benchmark (O7). Bounded viewer-query queue
  + opt-in sectioned-accumulation config toggle (live-control vs immediacy).
  Framed as a turn-taking/flow-control feature, NOT a crash fix (the 20min
  "deadlock" was refuted as operator absence). Blocked on two prerequisites in
  kira_history_summarization_20260611 (runaway-generation cap + operator
  priority=0 lane). Does NOT touch PTT-accumulate. See proposal.md.*

---

- [ ] **Track: YouTube Chat Compliance Audit — Terms of Service & API Analysis**
  *Link: [./tracks/youtube_chat_compliance_audit_20260614/](./tracks/youtube_chat_compliance_audit_20260614/)*
  *Status 2026-06-14: PROPOSAL ONLY — not started. Surfaced from user request to audit live chat consumption against YouTube policies. The current pytchat integration scrapes YouTube internal endpoints, violating general ToS Section 4 (automated access). Transition to the official YouTube Data API v3 is proposed. See proposal.md.*
  *Status 2026-06-13: RESCOPED (owner decision). Heavy remediation DROPPED — no OAuth,
  no dual-backend, no shared credential vault. New scope: (a) Twitch stays first-class
  (compliant anonymous IRC, already shipped); (b) moderation is OUT of product scope,
  delegated to Nightbot via docs — RF4/stream_admin marked LEGACY (frozen, see
  docs/HANDOFF_RF4.md); (c) YouTube becomes opt-in, READ-ONLY via the streamer's own
  Data API v3 key (no OAuth: videos.list → activeLiveChatId → liveChatMessages.list),
  key encrypted locally, with a documented quota ceiling. Rationale: keep OpenCohost
  focused on the co-host, preserve local-first (no cloud aggregators), stay compliant
  without API/moderation complexity. proposal.md rewritten; spec.md and plan.md carry
  "superseded" banners. Open item: verify liveChatMessages.list quota cost before
  publishing the ceiling. Door left open (distant) if a clearly better option appears.
  No implementation authorized — direction only.*

---

- [x] **Track: OpenCohost UI Declutter — Status Bar Rollup and RF4 Panel Hide**
  *Link: [./tracks/opencohost_ui_declutter_20260614/](./tracks/opencohost_ui_declutter_20260614/)*
  *Status 2026-06-15: MERGED to master via PR #46 (commit 5505629, CI green).
  Track 1 (main-thread freeze elimination) DONE at ada82d5; Track 2 (this track)
  under Strict TDD; plus the operator_console_focus_fix slice (window_utils
  show_toplevel/raise_window) and a CI bugfix (81be9cb). Owner runtime validation
  of the visible UI behaviour still pending.
  Part A shipped: STREAM_ADMIN_ENABLED=False. RF4 legacy panels (metadata,
  moderation, Kira-chat) HIDDEN, not deleted. Moderation delegated to Nightbot.
  RF3 Chat Live kept. Code conserved per HANDOFF_RF4.md.
  Part B: Sistema rollup pill added (5 severity levels); engine badge visibility
  gated (dim on steady-state, amber on qwen_starting per owner decision).
  Part C: Cleanup deferred to stream_admin_legacy_removal_20260614 (NO-PRIORITY).*

---

- [ ] **Track: Stream Admin Legacy Removal — Eventual RF4 Code Deletion (NO-PRIORITY)**
  *Link: [./tracks/stream_admin_legacy_removal_20260614/](./tracks/stream_admin_legacy_removal_20260614/)*
  *Status 2026-06-14: NOT STARTED, NO-PRIORITY. RF4 metadata/moderation/Kira-chat
  panels are currently HIDDEN via STREAM_ADMIN_ENABLED=False (see ui_declutter_20260614).
  This track covers eventual deletion of the RF4 code after: (1) full dep audit,
  (2) youtube_chat_compliance_audit_20260614 resolved, (3) owner authorizes deletion.
  RF3 Chat Live is NOT in scope. See proposal.md and HANDOFF_RF4.md.*

---

- [ ] **Track: Qwen TTS Lifecycle Hardening — Auto-Manage, Visible Progress, Switch-Driven Stop**
  *Link: [./tracks/qwen_tts_lifecycle_hardening_20260613/](./tracks/qwen_tts_lifecycle_hardening_20260613/)*
  *Status 2026-06-13: INVESTIGATION captured + runtime contract owner-approved (A–G).
  Origin: owner runtime-validation of heavy vs light TTS surfaced (1) a release-blocking
  bug — APP_ID drift (server_qwen.py "voiceai-qwen-tts" vs HealthMonitor
  "opencohost-qwen-tts") made heavy TTS silently fall back to Edge forever; fixed in
  server_qwen.py (MERGED to master via PR #46, 2026-06-15; engram #1931). The Phase 1
  visible engine badge (qwen_markers.py + state.engine_status + startup self-check) also
  merged via PR #46. (2) Decision to finish the deferred
  self-managing lifecycle: eager start on switch-to-Pesado, keep-warm 30s then stop,
  VRAM-gated via existing VRAMGuard, progress in the in-app console, Edge during startup.
  ~80% already built in QwenProcessManager. PACKAGING of the heavy env is PARKED (out of
  current scope). Next: spec + TDD implementation of the lifecycle wiring. See investigation.md.*

---

- [x] **Track: Branding Log Leak Remediation — Runtime Output Still Says "VoiceAI"** — SUPERSEDED
  *Link: [./tracks/branding_log_leak_remediation_20260616/](./tracks/branding_log_leak_remediation_20260616/)*
  *Status 2026-06-17: SUPERSEDED/DONE — all runtime items (L1–L5, C1–C4, and S1) shipped by the
  VoiceAI→OpenCohost runtime rebrand (commit ad7ca94, merged on audit/comprehensive-review).
  Verified: `git grep voiceai` in opencohost/ = 0; logger "OpenCohost"; opencohost_*.log;
  OPENCOHOST_DEBUG; S1 tag now ["kira","opencohost","live"]. Original proposal (historical):
  launch-readiness branding sweep
  found the running app still emits the retired "VoiceAI" name in runtime output.
  Highest-impact (L1): the shared logger is named "VoiceAI" and the format includes
  %(name)s, so EVERY console+file log line reads [VoiceAI]. Also L2 log file written
  as voiceai_*.log; L3/L4 literal log messages (llm_engine.py:870, temp_file_cleanup.py:69,72);
  L5 public env var VOICEAI_DEBUG. Cross-cutting security constraint: the logger name
  must be renamed in all three getLogger("VoiceAI") sites together (logger.py:43,
  avatar_panel.py:36, obs_client.py:25) or avatar_panel/obs_client lose the
  SensitiveDataFilter (token/liveChatId redaction). C1–C4 cosmetic (docstrings/ids,
  not printed). S1 routed+frozen: admin_manager.py:325 metadata tag "voiceai" behind
  STREAM_ADMIN_ENABLED=False — external leak if RF4 re-enabled, owned by
  stream_admin_legacy_removal_20260614. See proposal.md.*

---

- [x] **Track: Product Rebrand VoiceAI → OpenCohost**
  *Link: [./tracks/product_rebrand_voiceai_to_opencohost_20260617/](./tracks/product_rebrand_voiceai_to_opencohost_20260617/)*
  *Status 2026-06-17: DONE. Workstream A (docs reconciliation) + Workstream B (runtime rename) both
  complete and merged on audit/comprehensive-review (commits 28df4c3 docs, ad7ca94 runtime,
  6f30231 operator log strings). Runtime: logger "OpenCohost", opencohost_*.log, OPENCOHOST_DEBUG/
  _CRASH_LOG/_FATAL_LOG, identifiers, log strings, S1 tag — verified `git grep voiceai` in opencohost/
  = 0. Absorbs/supersedes branding_log_leak_remediation_20260616. Git-verified: appdata rename shipped
  in v0.1.0/v0.1.1 (commit e76c9b1 ancestor) → no orphaned users, migration shim unnecessary.
  PRESERVED: ADR-004, docs/audit/*, engram key `voiceai`. Only the separate `VocalAI` class
  identifiers (MotorVocalIA, VocalAIApp) remain deferred — see docs/architecture.md.*

- [ ] **Track: Public Site Rebrand — voiceaikira.vercel.app → OpenCohost (PRIORITY)**
  *Link: [./tracks/public_site_rebrand_opencohost_20260617/](./tracks/public_site_rebrand_opencohost_20260617/)*
  *Status 2026-06-17: PROPOSAL ONLY. Highest external leverage — the legacy brand the US audience
  actually sees (URL + /docs + /es). Includes domain migration to opencohost.com (ADR-0010) with
  301/SEO/analytics continuity. Separate Vercel project/repo. See proposal.md.*

- [ ] **Track: English Compatibility / i18n — US-ready OpenCohost (PRIORITY)**
  *Link: [./tracks/english_compatibility_i18n_20260617/](./tracks/english_compatibility_i18n_20260617/)*
  *Status 2026-06-17: PROPOSAL ONLY. Motivated by ADR-0001 + US/English web traffic. Externalize
  CustomTkinter UI strings (en default, es retained), locale-aware Kira persona/prompts,
  English-first docs. Constraints: no PyInstaller bloat (dict/gettext), CTk thread-safety via
  UIState/_safe_after. Pattern ref: LiveAudio bilingual (engram liveaudio). See proposal.md.*

---

### Accumulated from 2026-06-17 runtime session (owner observations + untested features)

- [ ] **Track: First-Run & Ollama-Off Model Onboarding UX**
  *Link: [./tracks/first_run_model_onboarding_20260617/](./tracks/first_run_model_onboarding_20260617/)*
  *Status 2026-06-17: PROPOSAL. Ollama-off blocks model choice (only "open Ollama" works); on start it
  auto-loads an unwanted model; fresh install FORCES downloading llama3 (can't choose/skip). Mitigation:
  decouple selection from Ollama-running state + persist intent; honor user intent on start; fresh-install
  chooser instead of forced llama3; never force a download when models exist. See proposal.md.*

- [ ] **Track: App Startup Clarity (loading vs frozen)**
  *Link: [./tracks/app_startup_clarity_20260617/](./tracks/app_startup_clarity_20260617/)*
  *Status 2026-06-17: PROPOSAL. At startup only Kira shows; new users can't tell if it's loading or frozen
  during the slow cold start. Mitigation: surface the phases the backend already logs (Iniciando→Conectando
  Ollama→Preparando modelo→Listo) + warm-up progress, via UIState/_safe_after. See proposal.md.*

- [ ] **Track: Status Bars Stale / Cryptic Errors (investigate)**
  *Link: [./tracks/status_bar_stale_state_20260617/](./tracks/status_bar_stale_state_20260617/)*
  *Status 2026-06-17: PROPOSAL (investigation-first). Some status bars never update — keep showing
  `system:error` and cryptic strings that alarm without being actionable. Investigate UIState observer
  wiring (which keys never refresh / never clear on recovery) + inventory error strings, then fix wording
  + refresh. See proposal.md.*

- [ ] **Track: Kira Memory Hardening — MemoryDigest E5 + E3**
  *Link: [./tracks/kira_memory_hardening_20260617/](./tracks/kira_memory_hardening_20260617/)*
  *Status 2026-06-17: PROPOSAL. Memory verified WIRED & working (deque sliding window + MemoryDigest L1 in
  direct-path prompts). Remaining: E5 (history commits before TTS speaks → unspoken replies pollute digest;
  gate on was_spoken) and E3 (digest sanitizer narrower than commit-time; add Spanish markers/NFKC/whole-digest
  scan). Supersedes repo_hygiene R3/R4 memory slice. Strict TDD. See proposal.md.*

- [ ] **Track: RF3 Chat Ingestion — runtime validation (UNTESTED)**
  *Status 2026-06-17: not validated at runtime (owner had no live stream in the 2026-06-17 session; zero
  connection attempts in the log — expected, not a bug). To test without owning a stream: "Chat Live (RF3)"
  tab → paste `twitch.tv/<any live channel>` (anonymous, no OAuth) or any public YouTube live URL → Conectar
  Chat Live; success logs `[StreamAdmin] Chat Live conectado [...]`. Note: failure paths are silent in the
  log (invalid URL = UI toast; connect_to False = nothing). OAuth tokens present at data/stream_admin/.*

- [ ] **Track: Editorial Cards — runtime end-to-end validation (UNTESTED)**
  *Status 2026-06-17: cards verified WIRED (EditorialCardStore + EditorialAgendaBridge in app_shell:149,192;
  ~15 refs in cohost_agenda_panel.py UI; CLI editorial_cli.py; raw-chat rejected at model boundary). Almost
  certainly functional but NOT exercised this session. Validate end-to-end: author → arm → fires in agenda →
  Kira uses it. Pairs with the cohost-engine deep-dive (deferred).*
