# OpenCohost Public Documentation Design

This design defines how OpenCohost public documentation should be produced
without turning guesses, old plans, or agent memory into false project claims.

## Problem

OpenCohost needs documentation that helps new contributors and coding agents
understand:

- what the project does today,
- what each major module/file owns,
- why important decisions were made,
- how work should be planned and validated,
- what tests exist and what they prove,
- what is planned but not implemented yet.

The risk is that documentation can hallucinate architecture: it can describe
intended behavior as if it already exists, merge future plans with current code,
or hide module-specific uncertainty behind broad product language.

## Design Decision

Public documentation should be produced in small, evidence-backed documentation
tracks instead of one broad documentation pass.

Each documentation slice must have:

1. a bounded module or workflow scope,
2. source evidence from real files, tests, configs, and prior committed tracks,
3. a current-state section,
4. a known-limitations section,
5. a future-plans section,
6. a verification checklist.

## Documentation Tracks

| Track | Output | Purpose |
|---|---|---|
| Docs index | `docs/INDEX.md` or equivalent | Route humans and agents to the right document before changing code. |
| Architecture map | `docs/ARCHITECTURE.md` | Explain high-level system boundaries and module ownership. |
| Module context: UI shell | `docs/modules/ui-shell.md` | Explain Tk ownership, panels, event queue, and UI safety rules. |
| Module context: MotorVocalIA/runtime speech | `docs/modules/runtime-speech.md` | Explain speech ownership, direct vs agenda audio, TTS flow, and cleanup rules. |
| Module context: TTS/audio | `docs/modules/tts-audio.md` | Explain light/heavy TTS, Qwen subprocess, pygame/audio limits, and validation needs. |
| Module context: SmartAggregator | `docs/modules/smart-aggregator.md` | Explain agenda/chat boundaries and raw chat privacy rules. |
| Module context: OBS/Stream Admin | `docs/modules/stream-integrations.md` | Explain OBS, YouTube/OAuth, token boundaries, and setup expectations. |
| Testing guide | `docs/TESTING.md` | Catalog tests, commands, coverage boundaries, and manual validation gates. |
| Methodology guide | `docs/METHODOLOGY.md` | Explain controlled validation, Conductor usage, review expectations, and no-blind-expansion rule. |
| Contributor guide | `CONTRIBUTING.md` | Explain PR rules, acceptable changes, validation proof, and review flow. |
| Trust/security guide | `SECURITY.md` or `docs/TRUST_MODEL.md` | Explain local-first assumptions, privacy boundaries, secrets, tokens, and vulnerability reporting. |
| License/notices | `LICENSE`, `THIRD_PARTY_NOTICES.md` | Explain project license and third-party/model/asset obligations. |

The detailed module-by-module track map is defined in
`public_docs_track_map.md`.

## Anti-Hallucination Protocol

Every public documentation slice must follow this evidence model.

### 1. Evidence First

Before writing a module doc, inspect the relevant files directly. Do not rely on
memory alone.

Required evidence types:

- source files,
- tests,
- configuration files,
- committed track docs,
- recent commit messages only as supporting context, not primary proof.

### 2. Claim Labels

Every important claim must fit one of these labels:

| Label | Meaning |
|---|---|
| Current behavior | Verified in source/tests or already committed runtime behavior. |
| Current configuration | Verified in config files or setup docs. |
| Known limitation | Verified gap, risk, or missing validation. |
| Design decision | Documented decision with rationale. |
| Future plan | Not implemented yet; belongs in roadmap/deferred tracks, not current behavior. |

If a claim does not fit a label, it should not enter public docs.

### 3. Future Plans Stay Separate

Future work must not be written as current functionality.

Use explicit sections:

- `Current State`
- `Known Limitations`
- `Deferred Work`
- `Planned Tracks`

Examples:

- Correct: "Runtime smoke validation is deferred and opt-in."
- Incorrect: "OpenCohost includes a complete runtime smoke harness."

### 4. Module-by-Module Verification

Each module doc should be verified independently before it becomes part of the
public documentation set.

Verification checklist:

- [ ] The module files listed in the doc exist.
- [ ] The described responsibilities match source code.
- [ ] The described tests exist or are clearly marked as missing.
- [ ] The doc separates current behavior from planned work.
- [ ] The doc lists known local/runtime assumptions.
- [ ] The doc does not expose private local data, prompts, tokens, or raw chat.

### 5. Testing Claims Require Test Evidence

Testing docs must be generated from actual test discovery, not from assumptions.

Required evidence:

- test file list,
- test names or grouped scenarios,
- known commands that have been run or are documented,
- distinction between automated tests and manual validation.

Testing docs must state what unit/focal tests cannot prove:

- real audio device behavior,
- pygame mixer/native behavior,
- Tk mainloop/thread edge cases,
- Qwen subprocess lifecycle,
- OBS websocket behavior,
- real YouTube/OAuth behavior,
- real stream/chat service behavior.

### 6. Review Pass Before Publication

Before committing public docs, run a documentation audit:

- compare claims against files/tests,
- check English-only public artifact rule,
- check no private data or local memory leaks,
- check no future feature is presented as current behavior,
- check links and filenames.

## Recommended Phase Order

1. Documentation design and methodology.
2. Agent/contributor index.
3. Work methodology.
4. Testing guide.
5. Architecture map.
6. Module docs in small batches.
7. Contributing/security/license/third-party notices.
8. README public entry point.
9. Final doc audit and launch-readiness report.

README should come late, not first, because it summarizes the validated docs. A
README written too early is likely to overpromise.

## Where Plans Live

Plans and deferred work should live in one of these places:

- Conductor tracks for active or pending engineering/design work.
- `docs/ROADMAP.md` only after public wording is curated.
- `docs/ARCHITECTURE.md` deferred-work sections only when needed to explain a boundary.

Plans should not be mixed into module current-state docs unless clearly labeled
as deferred or planned.

## Acceptance Criteria for This Design

- [ ] Documentation work is split into small, reviewable slices.
- [ ] Each slice has evidence requirements before writing.
- [ ] Current behavior, limitations, decisions, and future plans are separated.
- [ ] Testing documentation is based on discovered tests, not assumptions.
- [ ] README is treated as a summary/router after core docs are validated.
