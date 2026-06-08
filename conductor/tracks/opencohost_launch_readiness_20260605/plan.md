# Implementation Plan ? OpenCohost Launch Readiness

## Phase 1: Product and Brand Boundary

- [ ] Task: Define OpenCohost positioning statement.
    - [ ] Decide whether Kira is product identity, default persona, or demo persona.
    - [ ] Decide whether VoiceAI remains internal codename or legacy reference.
    - [ ] Document local-first promise and cloud-service boundaries.
- [ ] Task: Create OpenCohost identity map and staged rename boundary.
    - [ ] Classify names as platform, persona, legacy/internal, or technical identifier.
    - [ ] Decide which names can change in public docs before code changes.
    - [ ] Decide which internal names must stay temporarily to protect runtime stability.
- [ ] Task: Document naming risk and validation notes.
    - [ ] Record discarded names and why they were rejected.
    - [ ] Record OpenCohost SEO rationale as a hypothesis, not legal proof.
    - [ ] Add legal/trademark validation as external/non-code task.

- [x] Task: Rename user-facing chassis copy to OpenCohost.
    - [x] Update visible window title/product footer copy.
    - [x] Update user-facing startup/status/OAuth messages that say VoiceAI/VocalAI.
    - [x] Preserve Kira everywhere as persona/cohost identity.
    - [x] Do not rename internal classes, package names, imports, paths, or app IDs in this task.
- [ ] Task: Conductor - User Manual Verification 'Product and Brand Boundary' (Protocol in workflow.md)

## Phase 2: Repo and Safety Audit

- [x] Task: Audit files for public-repo readiness.
    - [x] Search for secrets, tokens, `.env`, OAuth files, local runtime DBs, logs, and generated media.
    - [x] Confirm `.gitignore` covers runtime/private artifacts.
    - [x] Confirm pre-commit safety hook blocks obvious private artifacts.
    - [x] Documented findings in `repo_safety_audit.md`.
- [x] Task: Audit local-machine coupling.
    - [x] Identify hardcoded Windows paths.
    - [x] Identify environment-specific model/cache assumptions.
    - [x] Identify external services that need setup docs.
    - [x] Documented findings in `repo_safety_audit.md`.
- [ ] Task: Define migration branch policy.
    - [ ] Decide base branch for `plynte-labs/opencohost`.
    - [ ] Decide which local branches should not be pushed.
    - [ ] Decide whether current audit branch is release-prep only or migration source.
- [ ] Task: Conductor - User Manual Verification 'Repo and Safety Audit' (Protocol in workflow.md)

## Phase 3: Runtime Validation Gates

- [ ] Task: Execute user-owned manual runtime validation.
    - [ ] Heavy vs light TTS.
    - [ ] Visible fallback behavior.
    - [ ] Manually started Qwen server.
    - [ ] Ollama online/offline behavior.
    - [ ] Health pill state changes.
- [ ] Task: Convert validation results into blockers.
    - [ ] Mark release-blocking failures.
    - [ ] Mark acceptable prototype limitations.
    - [ ] Decide whether `qwen-tts-lifecycle-hardening` is required before release.
- [ ] Task: Conductor - User Manual Verification 'Runtime Validation Gates' (Protocol in workflow.md)

## Phase 4: Public Documentation and Contributor Orientation

- [x] Task: Define public documentation methodology.
    - [x] Require English for all public documentation artifacts.
    - [x] Define docs set: README, AGENTS, docs index, architecture, methodology, testing, contributing, security/trust model, license, and third-party notices.
    - [x] Documented strategy in `public_docs_methodology.md`.
- [x] Task: Design evidence-backed documentation workflow.
    - [x] Define small documentation tracks by module/workflow.
    - [x] Define anti-hallucination protocol based on source/test/config evidence.
    - [x] Define where current behavior, limitations, decisions, and future plans live.
    - [x] Documented design in `public_docs_design.md`.
- [x] Task: Define module-by-module documentation track map.
    - [x] Define output docs, order, primary evidence, and acceptance criteria.
    - [x] Define module doc template.
    - [x] Define review strategy for unsupported claims and future plans.
    - [x] Documented map in `public_docs_track_map.md`.
- [ ] Task: Draft OpenCohost README skeleton.
    - [ ] Product promise.
    - [ ] Local-first architecture summary.
    - [ ] Current prototype limitations.
    - [ ] Setup expectations.
    - [ ] Safety/privacy notes.
- [x] Task: Draft agent/contributor documentation index.
    - [x] Link README, architecture, methodology, testing, security, and contributing docs.
    - [x] Explain where to go before touching runtime, UI, TTS/audio, OBS, Stream Admin, SmartAggregator, and release work.
- [x] Task: Draft initial work methodology documentation.
    - [x] Document controlled-validation workflow.
    - [x] Document when to use Conductor tracks.
    - [x] Document evidence labels for public docs.
    - [x] Document runtime/privacy boundaries.
- [x] Task: Draft architecture and file context documentation.
    - [x] Document high-level runtime architecture.
    - [x] Document major files/modules and ownership boundaries.
    - [x] Document key decisions and deferred tracks.
    - [x] Keep module details shallow until module-specific docs are produced.
- [ ] Task: Expand work methodology documentation after module docs.
    - [ ] Reconcile methodology with final architecture/testing docs.
    - [ ] Add PR/commit/review examples once CONTRIBUTING.md exists.
- [x] Task: Draft UI shell module documentation.
    - [x] Document composition-root role and ownership boundaries.
    - [x] Document Tk mainloop / `_safe_after` / UI task queue rules.
    - [x] Document tests and validation limits for UI shell changes.
    - [x] Keep broad UI extraction and product polish deferred.
- [x] Task: Draft runtime speech module documentation.
    - [x] Document MotorVocalIA ownership, command queue, priority queue, and speech source lifecycle.
    - [x] Document agenda prefetch/direct interaction arbitration.
    - [x] Document TTS modes and automated-vs-runtime validation boundaries.
    - [x] Document focused tests and deferred runtime smoke/Qwen lifecycle work.
- [x] Task: Draft testing documentation.
    - [x] Catalog current tests by subsystem.
    - [x] Document test commands and local assumptions.
    - [x] Separate focal/unit tests from manual and opt-in runtime smoke validation.
    - [x] Verify every test claim against real test files and commands.
- [ ] Task: Draft release-readiness checklist.
    - [ ] Required before public push.
    - [ ] Required before domain launch.
    - [ ] Required before packaging.
- [ ] Task: Conductor - User Manual Verification 'Public Documentation Draft' (Protocol in workflow.md)

## Phase 5: Go / No-Go Decision

- [ ] Task: Produce OpenCohost launch readiness report.
    - [ ] Summarize completed validations.
    - [ ] Summarize blockers.
    - [ ] Recommend next track: Product UI review, Qwen lifecycle hardening, packaging, or public docs.
- [ ] Task: Decide whether to proceed with repo migration.
- [ ] Task: Conductor - User Manual Verification 'Go / No-Go Decision' (Protocol in workflow.md)
