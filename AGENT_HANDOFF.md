# VoiceAI Agent Handoff

This is the first file an AI agent should read when starting work in this repo.

## Current operating mode

**Less expansion, more controlled validation.**

Do not start new feature work by default. The near-term goal is release readiness:
validate the existing prototypes, reconcile documentation with reality, and reduce
runtime uncertainty before packaging or broad product polish.

## Start-of-session checklist

1. Read `AGENTS.md` for repo rules.
2. If Engram is available, run `mem_context(project="voiceai")` before planning.
3. Read `sdd/session-status-2026-06-04.md` for the latest SDD checkpoint.
4. Check `git status --short` before editing; do not overwrite user changes.
5. If the request touches SDD/Conductor work, inspect the relevant track/spec before coding.


## Product direction update ? 2026-06-05

- The next product/release direction is **OpenCohost**.
- User reports `OpenCohost.com` is purchased for 3 years.
- User reports the target repo is secured at `plynte-labs/opencohost`.
- Treat this as launch-readiness preparation first, not a blind rebrand.
- Current Conductor track: `opencohost_launch_readiness_20260605`.

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
- Active bug-recovery checkpoint: `heavy_model_inference_recovery_20260609`
  is implemented locally but still needs manual runtime validation.
  - completed locally: watchdog around first real chat after switch, stuck-processing recovery,
    pending-switch escape path, and rollback to last known good model
  - validated locally: focused recovery/model-management suite reached `159 passed`
  - pending intentionally: real runtime verification against an actually heavy/stalling model
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
- packaging / installer work
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
