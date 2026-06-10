# OpenCohost Launch Readiness Specification

## Overview

Prepare the existing VoiceAI prototype line for the OpenCohost product direction.

OpenCohost is the intended public product name and launch identity. The user reports that `OpenCohost.com` has been purchased for 3 years and that the GitHub repository has been secured at `plynte-labs/opencohost`.

This track is not a blind rebrand and not a packaging sprint. It is a launch-readiness track: verify what can safely become OpenCohost, document what remains VoiceAI/internal, and create a low-risk path toward public release.

## Current Strategic Decision

- Product direction: OpenCohost.
- Previous working name: VoiceAI / Kira-centric prototype.
- Repository target: `plynte-labs/opencohost`.
- Domain target: `OpenCohost.com`.
- Operating principle: less expansion, more controlled validation.

## Brand Discovery Notes

- User discarded names with heavier search/noise risk: StreamCohost, CohostAI, StreamAI, LiveCohost.
- User chose OpenCohost because exact-name search appears low-noise and could support strong SEO.
- Preliminary public search did not surface an obvious dominant exact `OpenCohost` product, but adjacent names exist (`OpenHost`, `Open CoWork`, `Cohost`). This is not legal/trademark clearance.

## Functional Requirements

### Requirement: Launch Identity Boundary

The project MUST define what OpenCohost means before renaming code.

Acceptance criteria:

- [ ] Document product positioning for OpenCohost in one concise statement.
- [ ] Define whether Kira remains a default persona, example persona, or product-facing identity.
- [ ] Define what stays local-first and what may use cloud services.
- [ ] Define whether VoiceAI remains an internal codename, legacy name, or removed branding.


### Requirement: User-Facing Chassis Rename

The project MUST rename only the user-visible chassis from VoiceAI/VocalAI to OpenCohost while preserving Kira and avoiding internal technical renames.

Acceptance criteria:

- [ ] Window title and visible product labels use OpenCohost.
- [ ] User-facing startup/status/OAuth messages no longer say VoiceAI/VocalAI.
- [ ] Kira remains the cohost/persona name in UI, prompts, avatar, agenda, and stream flows.
- [ ] Internal classes, imports, package name, Engram project key, local paths, and runtime app IDs are not renamed in this phase.
- [ ] Any future internal/repo/class rename is deferred to a separate dedicated track with full regression testing.

### Requirement: Release Readiness Audit

The project MUST audit release blockers before packaging or migration.

Acceptance criteria:

- [ ] Check for sensitive files, tokens, local paths, runtime DBs, and generated artifacts.
- [ ] Verify `.gitignore` and pre-commit hook cover known private/runtime artifacts.
- [ ] Identify hardcoded local paths that block external use.
- [ ] Identify dependencies that require separate install/setup instructions.
- [ ] Produce a release blocker list with severity and owner.

### Requirement: Repo Migration Safety

The project MUST prepare for `plynte-labs/opencohost` without losing current history or accidentally publishing private artifacts.

Acceptance criteria:

- [ ] Confirm desired base branch and migration source branch.
- [ ] Confirm what branches should be pushed or ignored.
- [ ] Confirm public/private status expectations for the target repo.
- [ ] Confirm license/readme/security files required before first public push.
- [ ] Confirm no secrets or user runtime data are included.

### Requirement: Product Documentation Entry Point

The project MUST have a concise public-facing entry point before broad release.

Acceptance criteria:

- [ ] Draft README positioning for OpenCohost.
- [ ] Include local-first value proposition.
- [ ] Include current prototype limitations honestly.
- [ ] Include setup boundaries without overpromising one-click install.
- [ ] Include brand/domain/repo notes only where appropriate.

### Requirement: Public Documentation Language

All public documentation artifacts MUST be written in English.

Acceptance criteria:

- [ ] README, architecture, contributor, security, license, testing, and agent-facing documentation are written in English.
- [ ] Internal chat replies may remain Spanish when the user writes Spanish, but generated documentation artifacts stay English.
- [ ] Documentation avoids local slang, private session language, and AI-agent-specific chat tone.

### Requirement: Agent and Contributor Navigation Index

The project MUST provide an index that helps human contributors and coding agents
find the right documentation before touching code.

Acceptance criteria:

- [ ] Provide an agent/contributor index that links to README, architecture, methodology, testing, security, and contribution docs.
- [ ] Explain which document to read first for common tasks: runtime debugging, UI changes, TTS/audio work, stream/admin changes, tests, and release work.
- [ ] Include repo map guidance for key modules and files.
- [ ] Keep the index short enough to scan quickly, with deeper links for details.

### Requirement: Architecture and File Context Documentation

The project MUST document what the major files/modules do and why they exist so
new contributors can move safely through the codebase.

Acceptance criteria:

- [ ] Document the high-level runtime architecture: UI shell, MotorVocalIA, TTS, LLM, OBS, Stream Admin, SmartAggregator, crash reporting, and runtime safety.
- [ ] Document ownership boundaries: what can be changed locally, what requires design, and what must not be mixed.
- [ ] Document the important decisions already made: OpenCohost identity, Kira preservation, local-first direction, direct-audio arbitration, UI-thread ownership, crash evidence, and runtime validation gates.
- [ ] Document known deferred tracks and why they are deferred.

### Requirement: Work Methodology Documentation

The project MUST document how OpenCohost work should be planned, implemented,
reviewed, and validated.

Acceptance criteria:

- [ ] Document the controlled-validation methodology: small changes, real behavior proof, no blind expansion.
- [ ] Document when to use Conductor tracks and when a tiny direct fix is acceptable.
- [ ] Document PR/commit expectations: focused changes, no unrelated refactors, no runtime-private artifacts.
- [ ] Document review expectations for runtime, UI, TTS/audio, privacy, and stream integrations.

### Requirement: Testing and Validation Documentation

The project MUST document the existing test surface in depth before broad
external contribution.

Acceptance criteria:

- [ ] Catalog current automated tests by subsystem and purpose.
- [ ] Document known test commands and required local environment assumptions.
- [ ] Separate unit/focal tests from manual runtime validation and opt-in smoke/runtime tests.
- [ ] Explain what cannot be proven by unit tests alone: pygame/audio device behavior, Tk mainloop/thread interaction, Qwen subprocess lifecycle, OBS websocket behavior, and real stream service interactions.
- [ ] Document how to add tests without making runtime/audio/device tests mandatory for every contributor.

### Requirement: Evidence-Backed Documentation

The project MUST prevent public documentation from presenting guesses or future
plans as current behavior.

Acceptance criteria:

- [ ] Documentation is produced in small module/workflow slices, not one broad pass.
- [ ] Each slice lists source evidence from files, tests, configs, or committed track docs.
- [ ] Claims are separated into current behavior, current configuration, known limitation, design decision, and future plan.
- [ ] A module-by-module documentation track map defines output docs, evidence requirements, and acceptance criteria before public docs are drafted.
- [ ] README is treated as a summary/router after core architecture/testing/module docs are validated.
- [ ] Future plans live in Conductor tracks or clearly labeled roadmap/deferred sections.

### Requirement: Validation Before Expansion

The project MUST preserve the current controlled-validation strategy.

Acceptance criteria:

- [ ] Validate heavy vs light TTS behavior manually.
- [ ] Validate visible fallback behavior manually.
- [ ] Validate manually started Qwen server flow.
- [ ] Validate Ollama online/offline behavior.
- [ ] Validate health pill state changes.
- [ ] Use validation results to decide Product UI or Qwen lifecycle next.

## Non-Functional Requirements

- Avoid broad code renames until release blockers are known.
- Avoid packaging work before runtime validation.
- Keep commits small and reviewable.
- Keep private/runtime artifacts out of Git.
- Preserve existing working prototype behavior.

## Out of Scope

- Full UI redesign.
- Full package installer.
- Internal repo/package/class/module rename from VoiceAI/VocalAI to OpenCohost.
- Local data path migration.
- Legal trademark clearance.
- Domain/DNS deployment.
- New feature expansion unrelated to release readiness.
- Demand-driven Qwen lifecycle implementation, unless explicitly moved into `qwen-tts-lifecycle-hardening`.

## Success Criteria

This track is successful when OpenCohost has a validated release-readiness map: product positioning, repo safety, public documentation plan, known blockers, and manual runtime validation checklist.

## Repo Safety Audit Result - 2026-06-07

Current status: NO-GO for public repository migration.

The audit found no concrete OAuth secret checked into the reviewed config, and
the current `.gitignore` / safety hook protect many future runtime artifacts.
However, several ignored/private or machine-local files are already tracked:

- `.engram/graph.db`
- `.engram/config.json`
- `Documents/*.md`
- `config/music_library.json`
- `config/avatar.yaml`
- `perfiles.json`

The detailed findings and recommended remediation order are documented in
`repo_safety_audit.md`.
