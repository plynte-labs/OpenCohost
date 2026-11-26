# OpenCohost Documentation Index

A map of every active document in `docs/`, grouped by who is reading and why. Each
line says what question the document answers. Historical snapshots, dated working
notes, superseded CustomTkinter-era specs, and old run dumps have been archived
outside this repository (see [Archived historical documentation](#archived-historical-documentation)).

> **Note on internal paths.** `conductor/` (the internal Spec-Driven Development
> tracks) and `AGENT_HANDOFF.md` are excluded by `.gitignore`, so references to
> them inside immutable `adr/` records are kept as historical provenance rather
> than rewritten.
>
> [`architecture.md`](architecture.md) was refreshed against the code on
> 2026-11-12, [`QUICKSTART.md`](QUICKSTART.md) on 2026-11-22, and
> [`PRIVACY.md`](PRIVACY.md) and [`api-reference.md`](api-reference.md) on
> 2026-08-14.

## Getting Kira running

| Document | What it answers |
|---|---|
| [`QUICKSTART.md`](QUICKSTART.md) | From zero to Kira speaking: prerequisites (Ollama, Python, and the Node/Rust toolchain the desktop app needs), cloning with submodules, installing, launching the Tauri app, and the privacy note to read before you start. |
| [`LINUX_GUIDE.md`](LINUX_GUIDE.md) | How to configure, run, and package OpenCohost on Linux (Pop!_OS / Ubuntu) using cloud LLM providers and lightweight TTS. In Spanish. |
| [`GUIA_USUARIO.md`](GUIA_USUARIO.md) | How to actually drive Kira as a streamer — PTT buffering, what happens when chat and your voice collide, small-stream mode — with a symptom/cause/fix table per feature. In Spanish. |
| [`TROUBLESHOOTING.md`](TROUBLESHOOTING.md) | Root-cause journal of known issues: why Kira used to talk to herself, threading crashes, and the layered fixes that stopped them. In Spanish. |
| [`reportar_falso_positivo.md`](reportar_falso_positivo.md) | Step-by-step guide to submitting unsigned OpenCohost binaries to Microsoft Security Intelligence to clear Windows Defender SmartScreen false positives. In Spanish. |
| [`guides/remote-phone-access.md`](guides/remote-phone-access.md) | How to connect and control Kira remotely from a phone over the local network / gateway. |

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
| [`SAFETY_CONTEXTUAL_CRISIS_DESIGN.md`](SAFETY_CONTEXTUAL_CRISIS_DESIGN.md) | Architecture and non-negotiable invariants for contextual crisis safety and persona precedence (S1–S7 vulnerable-user guardrails). |
| [`EDITORIAL_CARDS_CLI.md`](EDITORIAL_CARDS_CLI.md) | How a human or an automated script creates and manages editorial cards from the command line, and what the one-shot, crash-safe execution model guarantees when the app is running at the same time. |
| [`agenda-bulk-prompt-template.md`](agenda-bulk-prompt-template.md) | A copy-paste prompt for generating agenda topic lists in the exact pipe-separated format the Tauri parser accepts — and why legacy-format blocks fail silently. |
| [`AGENT_GATEWAY.md`](AGENT_GATEWAY.md) | The only HTTP surface external AI agents may call (`/api/agent/*`): the propose-don't-command philosophy, token auth, and why there is deliberately no agent chat endpoint. |
| [`desktop-companion.md`](desktop-companion.md) | How Kira's frameless desktop companion window works when the main OpenCohost window is minimized. |
| [`usage-activity.md`](usage-activity.md) | How the local Activity panel tracks calls, tokens, and compute time without storing conversation content, plus export/delete controls. |
| [`specs/VRM_INTEGRATION_PLAN.md`](specs/VRM_INTEGRATION_PLAN.md) | Specification and quick path for the modular VRM 3D avatar and lip-sync overlay (`/overlay/vrm`, `OPENCOHOST_VRM_MODEL_DIR`). |

## Understanding the build

| Document | What it answers |
|---|---|
| [`architecture.md`](architecture.md) | Which entry point is the product (Tauri + FastAPI), current architecture after CustomTkinter removal (`1eecab7`), system map, and module ownership boundaries. |
| [`api-reference.md`](api-reference.md) | The backend HTTP surface and the conventions behind it: the error contract, the no-raw-exposure rule, locking, and verb whitelists. `GET /openapi.json` on a running backend is the authoritative list. |
| [`modules/runtime-speech.md`](modules/runtime-speech.md) | How direct speech, agenda speech, TTS generation, and priority arbitration share a single speech owner inside the runtime motor. |
| [`modules/turn-state.md`](modules/turn-state.md) | The explicit state of one foreground turn: state x event table, terminal states, where each `[TURN_STATE]` event is emitted. |
| [`modules/ui-shell.md`](modules/ui-shell.md) | How the legacy CustomTkinter composition root wired panels, motor, and shutdown — historical: marked deprecated 2026-08-13, kept as a module reference for the retired shell (removed in `1eecab7`; current architecture is [`architecture.md`](architecture.md)). |
| [`memory_v5/wu0_contract_inventory.md`](memory_v5/wu0_contract_inventory.md) | Source-backed contract inventory for Memory v5 shadow formation foundation (paired with [`memory_v5/wu0-evidence.md`](memory_v5/wu0-evidence.md) and [`memory_v5/memory_v5_shadow_ddl_v1.sql`](memory_v5/memory_v5_shadow_ddl_v1.sql)). |

## Working on it, testing & releases

| Document | What it answers |
|---|---|
| [`TESTING.md`](TESTING.md) | Which test suites exist, how to collect and run them, and which environment pitfalls break collection. |
| [`RUNTIME_SMOKE_HARNESS.md`](RUNTIME_SMOKE_HARNESS.md) | How to prove the risky cohost/audio path (balanced speech lifecycle, no agenda/direct overlap) without putting real audio devices into the unit suite. |
| [`AGENDA_BENCHMARK_HARNESS.md`](AGENDA_BENCHMARK_HARNESS.md) | How to run the offline Agenda Mode benchmark harness and log analyzer across prompt variants and models. |
| [`runtime-validation-plan.md`](runtime-validation-plan.md) | The standing release gate: which runtime drills must pass against the live API before the backend-reliability verdict moves from NO-GO. |
| [`RELEASE.md`](RELEASE.md) | How to cut a release: version bump, tag discipline, and what the release workflow builds and verifies. |
| [`release-notes-template.md`](release-notes-template.md) | Maintainer template for drafting release notes, breaking-change notices, and license disclosures. |
| [`release/announcement-0.5.0.md`](release/announcement-0.5.0.md) | Release announcement draft/reference for OpenCohost 0.5.0. |
| [`packaging/WINDOWS_INSTALLER_PLAN.md`](packaging/WINDOWS_INSTALLER_PLAN.md) | Architecture and build plan for the Windows desktop installer. |
| [`packaging/CLEAN_VM_CHECKLIST.md`](packaging/CLEAN_VM_CHECKLIST.md) | Clean-VM verification checklist for validating packaged Windows installers before release. |

## Why it is built this way

| Document | What it answers |
|---|---|
| [`METHODOLOGY.md`](METHODOLOGY.md) | The working rules behind every change: the evidence model, claim labels, and when work needs a full track versus a direct fix. |
| [`DECISIONS.md`](DECISIONS.md) | The earliest architecture decisions in journal form — local-first over cloud, PTT as the answer to Whisper hallucinations. In Spanish; its ADR numbering is an earlier series, separate from `adr/`. |
| [`SDD_SKILLS_USAGE.md`](SDD_SKILLS_USAGE.md) | How Spec-Driven Development runs in this repo with Conductor skills and persistent project memory — internal contributor workflow, not needed to use the product. |

### Architecture Decision Records — [`adr/`](adr/)

Each of the 74 files in `adr/` (ADR-001 through ADR-070, including a few
dual-numbered pairs, plus `windows-installer-clean-vm-recovery-20261024.md`) is
an immutable, dated record of one decision or investigation: the context at the
time, the options weighed, the evidence — often with `file:line` citations and
benchmark numbers from real hardware — and what was chosen. They are
deliberately not rewritten when the system moves on, so older records may
describe the retired CustomTkinter surface or reference internal working files.
Read them as history with reasons, in a mix of English and Spanish.

Good first reads for a newcomer:

- [`ADR-016`](adr/ADR-016-public-repo-mit-and-fresh-history-export.md) — why the public repository is an MIT-licensed, curated fresh-history export.
- [`ADR-022`](adr/ADR-022-llm-backend-ollama-vs-llamacpp.md) — why inference stays on Ollama rather than bare llama.cpp, with the measured tradeoffs.
- [`ADR-028`](adr/ADR-028-kira-memory-and-topic-architecture.md) — how Kira's memory actually works: three stores plus editorial cards as a primitive RAG. In Spanish.
- [`ADR-042`](adr/ADR-042-test-trust-tiers.md) — what a green test suite does and does not prove here; the trust tiers every contributor should internalize.

## Archived historical documentation

Superseded CustomTkinter-era specs, dated audit snapshots, one-off validation
checklists, old Memory v5 generation run dumps, session logs, and root scratch
notes are preserved outside this repository at:

```text
E:\opencohostHistorical\docs\
```

This external archive contains `archive/`, `audit/`, `audit_snapshots/`,
`memory_v5/generations/`, `sdd_tracks/`, `sesiondanger/`, `superpowers/`,
`root-legacy/`, and dated closeout/validation/handoff documents moved out of
`docs/` to keep the active tree focused on current reference material.
