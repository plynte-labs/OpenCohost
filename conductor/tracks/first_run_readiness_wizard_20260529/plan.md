# Implementation Plan — First-run Readiness Wizard

## Phase 0 — Track Safety

- [ ] Task: Confirm worktree and scope
    - [ ] Run `git status --short`.
    - [ ] Keep `data/` untracked unless explicitly decided otherwise.
    - [ ] Keep this track separate from Track B streaming pipeline.
- [ ] Task: Conductor - User Manual Verification 'Track Safety' (Protocol in workflow.md)

## Phase 1 — Readiness Checker Contracts

- [ ] Task: Add red tests for Ollama dependency states
    - [ ] Python package missing.
    - [ ] Binary missing.
    - [ ] Binary present but API down.
    - [ ] API ready.
- [ ] Task: Add red tests for storage/model path reporting
    - [ ] Respect existing `OLLAMA_MODELS` when configured as auto.
    - [ ] Report configured custom path.
    - [ ] Warn when running Ollama may not be using newly configured path.
- [ ] Task: Add red tests for hardware detection
    - [ ] RAM detected.
    - [ ] `nvidia-smi` success parses GPU/VRAM.
    - [ ] `nvidia-smi` missing falls back to unknown.
- [ ] Task: Implement minimal `core/readiness.py`
    - [ ] Keep checks pure/mockable.
    - [ ] Avoid real storage mutation in tests.
- [ ] Task: Conductor - User Manual Verification 'Readiness Checker Contracts' (Protocol in workflow.md)

## Phase 2 — Managed Ollama Startup Diagnostics

- [ ] Task: Add red tests for slow/managed startup
    - [ ] Slow API readiness remains “starting” instead of premature failure.
    - [ ] Process-start failure surfaces diagnostic details.
    - [ ] Already-running port is treated as active/conflict with clear message.
- [ ] Task: Implement startup monitor facade
    - [ ] Keep process handle when VoiceAI starts Ollama.
    - [ ] Capture sanitized stderr/log hints.
    - [ ] Use longer/configurable timeout with non-spam progress.
- [ ] Task: Conductor - User Manual Verification 'Managed Ollama Startup Diagnostics' (Protocol in workflow.md)

## Phase 3 — First-run Wizard UI Skeleton

- [ ] Task: Add red tests for wizard visibility state
    - [ ] Show when setup is incomplete or blocking readiness exists.
    - [ ] Skip when setup completed and readiness is green.
    - [ ] Reopen from configuration/help.
- [ ] Task: Implement minimal `ui/readiness_wizard.py`
    - [ ] Show welcome, checks, diagnostics, and next action.
    - [ ] Do not block advanced/manual access unnecessarily.
- [ ] Task: Add local first-run persistence
    - [ ] Store outside tracked config.
    - [ ] Allow reset/reopen.
- [ ] Task: Conductor - User Manual Verification 'First-run Wizard UI Skeleton' (Protocol in workflow.md)

## Phase 4 — Model Recommendation and Probe

- [ ] Task: Add red tests for hardware recommendations
    - [ ] Low/no GPU recommends small models.
    - [ ] GTX 1060 class warns about limits.
    - [ ] RTX 3060 class recommends practical 7B/8B-style models.
    - [ ] Unknown hardware falls back conservatively.
- [ ] Task: Add red tests for model probe outcomes
    - [ ] Success with non-empty response.
    - [ ] Empty response diagnostic.
    - [ ] Timeout diagnostic.
    - [ ] Missing model diagnostic.
- [ ] Task: Implement recommendation/probe facade
    - [ ] Keep real model calls behind injectable ports.
    - [ ] Avoid warming heavy resources unnecessarily during light checks.
- [ ] Task: Conductor - User Manual Verification 'Model Recommendation and Probe' (Protocol in workflow.md)

## Phase 5 — Integration and Regression

- [ ] Task: Integrate checker with ModelPanel/AppShell
    - [ ] Avoid duplicate Ollama detection logic where practical.
    - [ ] Preserve existing pending model intent behavior.
- [ ] Task: Run focused regression
    - [ ] Run readiness, model panel, app shell, and LLM model trace tests.
    - [ ] Verify no raw chat/prompt appears in diagnostics.
- [ ] Task: Prepare reviewable work units
    - [ ] Use `python tools/safe_stage_check.py`.
    - [ ] Stage explicit paths only.
- [ ] Task: Conductor - User Manual Verification 'Integration and Regression' (Protocol in workflow.md)
