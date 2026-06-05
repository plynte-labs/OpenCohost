# Implementation Plan ? OpenCohost Launch Readiness

## Phase 1: Product and Brand Boundary

- [ ] Task: Define OpenCohost positioning statement.
    - [ ] Decide whether Kira is product identity, default persona, or demo persona.
    - [ ] Decide whether VoiceAI remains internal codename or legacy reference.
    - [ ] Document local-first promise and cloud-service boundaries.
- [ ] Task: Document naming risk and validation notes.
    - [ ] Record discarded names and why they were rejected.
    - [ ] Record OpenCohost SEO rationale as a hypothesis, not legal proof.
    - [ ] Add legal/trademark validation as external/non-code task.
- [ ] Task: Conductor - User Manual Verification 'Product and Brand Boundary' (Protocol in workflow.md)

## Phase 2: Repo and Safety Audit

- [ ] Task: Audit files for public-repo readiness.
    - [ ] Search for secrets, tokens, `.env`, OAuth files, local runtime DBs, logs, and generated media.
    - [ ] Confirm `.gitignore` covers runtime/private artifacts.
    - [ ] Confirm pre-commit safety hook blocks obvious private artifacts.
- [ ] Task: Audit local-machine coupling.
    - [ ] Identify hardcoded Windows paths.
    - [ ] Identify environment-specific model/cache assumptions.
    - [ ] Identify external services that need setup docs.
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

## Phase 4: Public Documentation Draft

- [ ] Task: Draft OpenCohost README skeleton.
    - [ ] Product promise.
    - [ ] Local-first architecture summary.
    - [ ] Current prototype limitations.
    - [ ] Setup expectations.
    - [ ] Safety/privacy notes.
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
