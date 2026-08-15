# OpenCohost Documentation Index

A map of every document in `docs/`, grouped by who is reading and why. Each
line says what question the document answers. Documents that are dated working
notes, superseded specs, or descriptions of the frozen legacy CustomTkinter
shell are flagged as historical on their line — that flag is the one thing a
filename cannot tell you.

> **Some paths cited below do not ship.** `conductor/` (the internal
> Spec-Driven Development tracks) and `AGENT_HANDOFF.md` are both excluded by
> `.gitignore`, so every reference to them — concentrated in `adr/` and the
> dated working notes — is a dead link once you clone. They were real paths
> when those documents were written; historical records are kept as a dated
> record and deliberately not rewritten.
>
> [`QUICKSTART.md`](QUICKSTART.md), [`PRIVACY.md`](PRIVACY.md), and
> [`api-reference.md`](api-reference.md) were re-verified against the code on
> 2026-08-14. The rest of this tree has not been through that pass.

## Getting Kira running

| Document | What it answers |
|---|---|
| [`QUICKSTART.md`](QUICKSTART.md) | From zero to Kira speaking: prerequisites (Ollama, Python, and the Node/Rust toolchain the desktop app needs), cloning with submodules, installing, launching the Tauri app, and the privacy note to read before you start. |
| [`GUIA_USUARIO.md`](GUIA_USUARIO.md) | How to actually drive Kira as a streamer — PTT buffering, what happens when chat and your voice collide, small-stream mode — with a symptom/cause/fix table per feature. In Spanish. |
| [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) | Root-cause journal of every known bug: why Kira used to talk to herself, Tk threading crashes, and the layered fixes that stopped them. In Spanish; fixes are described against the legacy shell paths where they were found. |

## What leaves your machine

The pair a cautious reader should check first.

| Document | What it answers |
|---|---|
| [`PRIVACY.md`](PRIVACY.md) | Exactly which data stays local and which leaves: only Kira's spoken text reaches Edge-TTS by default, and full prompts leave only if you opt into a cloud LLM provider. Includes where everything is stored on disk. |
| [`TRUST_MODEL.md`](TRUST_MODEL.md) | Every trust boundary and network call in one diagram: what stays inside your machine, what crosses out, and the design principles that keep viewer chat contained. |

## Using the features

| Document | What it answers |
|---|---|
| [`KIRA_COHOST_AGENDA_MODE.md`](KIRA_COHOST_AGENDA_MODE.md) | Why Kira is an agenda-driven co-host rather than a fully autonomous host, and how approved topics, short turns, and safe-exit states work. |
| [`LIVE_SAFETY_CONTROLS.md`](LIVE_SAFETY_CONTROLS.md) | What protects a real live stream: high-traffic chat sampling, vibe-call cooldowns, and the `live_safe` / `monologue` / `test` output caps. |
| [`EDITORIAL_CARDS_CLI.md`](EDITORIAL_CARDS_CLI.md) | How a human or an automated script creates and manages editorial cards from the command line, and what the one-shot, crash-safe execution model guarantees when the app is running at the same time. |
| [`agenda-bulk-prompt-template.md`](agenda-bulk-prompt-template.md) | A copy-paste prompt for generating agenda topic lists in the exact pipe-separated format the Tauri parser accepts — and why legacy-format blocks fail silently. |
| [`AGENT_GATEWAY.md`](AGENT_GATEWAY.md) | The only HTTP surface external AI agents may call (`/api/agent/*`): the propose-don't-command philosophy, token auth, and why there is deliberately no agent chat endpoint. |

## Understanding the build

| Document | What it answers |
|---|---|
| [`architecture.md`](architecture.md) | Which entry point is the product (Tauri + FastAPI) and which is frozen legacy, plus the system map and module ownership boundaries. |
| [`api-reference.md`](api-reference.md) | The backend HTTP surface and the conventions behind it: the error contract, the no-raw-exposure rule, locking, and verb whitelists. Covers 46 of the 72 method+path pairs the app actually serves, and says which are still missing — `GET /openapi.json` on a running backend is the authoritative list. |
| [`modules/runtime-speech.md`](modules/runtime-speech.md) | How direct speech, agenda speech, TTS generation, and priority arbitration share a single speech owner inside the runtime motor. |
| [`modules/ui-shell.md`](modules/ui-shell.md) | How the legacy CustomTkinter composition root wires panels, motor, and shutdown — historical: marked deprecated 2026-08-13, kept as an accurate record of the frozen shell. |
| [`UI_ARCHITECTURE.md`](UI_ARCHITECTURE.md) | The module diagram of the legacy CustomTkinter UI after its God-class breakup — historical: marked deprecated 2026-08-13; the product UI is Tauri + React. |

## Working on it

| Document | What it answers |
|---|---|
| [`TESTING.md`](TESTING.md) | Which test suites exist, how to collect and run them, and which environment pitfalls break collection — counts are a verified snapshot from 2026-06-07 and the suite has grown substantially since. |
| [`RUNTIME_SMOKE_HARNESS.md`](RUNTIME_SMOKE_HARNESS.md) | How to prove the risky cohost/audio path (balanced speech lifecycle, no agenda/direct overlap) without putting real audio devices into the unit suite. |
| [`runtime-validation-plan.md`](runtime-validation-plan.md) | The standing release gate: which runtime drills must pass against the live API before the backend-reliability verdict moves from NO-GO. |
| [`runtime-validation-20260730.md`](runtime-validation-20260730.md) | Dated session checklist (2026-07-30): what shipped that tests cannot prove, including the debug-flag-before-launch trap that silently hides sanitizer logs. Historical working notes. |
| [`runtime-validation-20260731.md`](runtime-validation-20260731.md) | Dated session checklist (2026-07-31): the follow-up validation loop, its injection-first checks, and why the clause-sanitizer gate stayed open. Historical working notes. |
| [`test_suite_audit_full.md`](test_suite_audit_full.md) | Which core tests were false positives or weak — tests that pass without exercising the real handler — and how to fix each. Audit snapshot dated 2026-06-01. |
| [`RELEASE.md`](RELEASE.md) | How to cut a release: version bump, tag discipline, and what the release workflow builds and verifies. |

## Why it is built this way

| Document | What it answers |
|---|---|
| [`METHODOLOGY.md`](METHODOLOGY.md) | The working rules behind every change: the evidence model, claim labels, and when work needs a full track versus a direct fix. |
| [`DECISIONS.md`](DECISIONS.md) | The earliest architecture decisions in journal form — local-first over cloud, PTT as the answer to Whisper hallucinations. In Spanish; its ADR numbering is an earlier series, separate from `adr/`. |
| [`SDD_SKILLS_USAGE.md`](SDD_SKILLS_USAGE.md) | How Spec-Driven Development runs in this repo with Conductor skills and persistent project memory — internal contributor workflow, not needed to use the product. |

### Architecture Decision Records — [`adr/`](adr/)

Each of the 44 files in `adr/` (ADR-001 through ADR-045; there is no ADR-010)
is a dated record of one decision or investigation: the context at the time,
the options weighed, the evidence — often with `file:line` citations and
benchmark numbers from real hardware — and what was chosen. They are
deliberately not rewritten when the system moves on, so some describe the
legacy CustomTkinter surface or reference internal working files that do not
ship; that was true when they were written. Read them as history with reasons,
in a mix of English and Spanish. Filenames state each record's topic.

Good first reads for a newcomer:

- [`ADR-016`](adr/ADR-016-public-repo-mit-and-fresh-history-export.md) — why the public repository is an MIT-licensed, curated fresh-history export.
- [`ADR-022`](adr/ADR-022-llm-backend-ollama-vs-llamacpp.md) — why inference stays on Ollama rather than bare llama.cpp, with the measured tradeoffs.
- [`ADR-028`](adr/ADR-028-kira-memory-and-topic-architecture.md) — how Kira's memory actually works: three stores plus editorial cards as a primitive RAG. In Spanish.
- [`ADR-042`](adr/ADR-042-test-trust-tiers.md) — what a green test suite does and does not prove here; the trust tiers every contributor should internalize.

### Audit reports — [`audit/`](audit/)

Three diagnostic reports from a 16-auditor review dated 2026-06-16, in
Spanish. They diagnose the pre-migration (CustomTkinter-era) codebase and
drove later refactors — read them as the reasoned state of the code at that
date, not as a description of today.

| Document | What it answers |
|---|---|
| [`audit/adr-arquitectura-mantenibilidad.md`](audit/adr-arquitectura-mantenibilidad.md) | What architecture the code actually implements (pragmatic layered, event-driven — not hexagonal) and where the God classes live. |
| [`audit/adr-deuda-tecnica-refactors.md`](audit/adr-deuda-tecnica-refactors.md) | Which technical debt is actually worth paying — with several auditor severities corrected downward against the real code. |
| [`audit/adr-framework-4r.md`](audit/adr-framework-4r.md) | Risk, readability, reliability, and resilience findings ranked by severity, concentrated at the boundaries with external services. |

## Historical record and parked plans

Superseded specs, dated phase reports, and ideas parked on purpose. Kept
because they explain how the product got here; none are current guidance.

| Document | What it answers |
|---|---|
| [`changes.md`](changes.md) | The original requirements spec (SRS v1.0) plus dated change entries from May 2026 — the system as designed in the CustomTkinter era. In Spanish. |
| [`RF3_Smart_Aggregator_Spec.md`](RF3_Smart_Aggregator_Spec.md) | The original spec for the YouTube live-chat aggregator (RF3). May 2026, in Spanish, historical. |
| [`RF4_Functional_Requirements.md`](RF4_Functional_Requirements.md) | The original functional requirements for Stream Admin mode: OAuth, moderation, metadata, read-only first. May 2026, in Spanish, historical. |
| [`RF4_Quality_Requirements.md`](RF4_Quality_Requirements.md) | The security and quality bar RF4 had to meet, especially OAuth token protection. May 2026, in Spanish, historical. |
| [`RF4_Test_Scenarios.md`](RF4_Test_Scenarios.md) | The manual test scenarios RF4 was accepted against. May 2026, in Spanish, historical. |
| [`UI_UX_REFACTOR_PLAN.md`](UI_UX_REFACTOR_PLAN.md) | The plan that reshaped the legacy CustomTkinter UI into three zones — executed and superseded; the product UI is now Tauri. Historical. |
| [`SESSION_ANALYTICS_EXPORTER_SPEC.md`](SESSION_ANALYTICS_EXPORTER_SPEC.md) | A parked spec for local session analytics — the app would only produce data, never display it — explicitly marked not to implement yet. In Spanish. |
| [`closeout-20260722-agenda-no-dead-air-phase2.md`](closeout-20260722-agenda-no-dead-air-phase2.md) | Phase report: how agenda dead air went from 16-36s pauses to sub-second via pregeneration, and what remained to validate. Dated 2026-07-22. |
| [`deferred-20260729-clause-sanitizer-scope.md`](deferred-20260729-clause-sanitizer-scope.md) | What was deliberately left out of the clause sanitizer, with the evidence behind each deferral and the condition that would reopen it. Decision register dated 2026-07-29. |
| [`sdd_tracks/big_file_decomposition_20260629.md`](sdd_tracks/big_file_decomposition_20260629.md) | Refactors the big-file audit judged too risky for drive-by fixes — proposals only, no code. Dated 2026-06-29, in Spanish. |
