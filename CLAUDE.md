# OpenCohost — CLAUDE.md

Agent instructions for this repository. Read this before planning any work.

## What is this project

**OpenCohost** is a local-first AI streaming co-host platform.
The core product is **Kira**, an AI co-host with a defined personality (dry sarcasm, sharp humor)
that runs entirely on the user's hardware using Ollama + Qwen3-TTS/F5-TTS.

Key subsystems (all app code lives in the `opencohost/` package):
- `opencohost/core/` — LLM engine, TTS engine, speech pipeline, health monitor
- `opencohost/ui/` — CustomTkinter shell; thread-safe via UIState observer pattern
- `conductor/` — Spec-Driven Development tracks and product guidelines
- `opencohost/config/` — YAML-based settings, storage, and model registry

Target: streamers who want a supervised AI co-host without cloud subscriptions.
Product direction: **OpenCohost** (`opencohost_launch_readiness_20260605` track).

## Current operating mode

**Less expansion, more controlled validation.**

- Do NOT start new feature tracks without explicit user approval.
- Goal is runtime validation and release readiness for OpenCohost launch.
- Active local checkpoints (NOT yet merged):
  - `dynamic_model_management_20260608` — Phase 1 + 2 done locally, 154 passed
  - `heavy_model_inference_recovery_20260609` — **the watchdog is validated; the rollback was
    not.** Split the claim, because the two halves have different evidence.
    - Watchdog: validated 2026-06-17 (`logs/opencohost_20260617_175453.log` at 18:05:24) and
      again 2026-08-13 — fires at exactly 45.00s on a hung `qwopus`, both times.
    - Rollback: **FAILED twice on 2026-08-13** (`logs/opencohost_20260813_114819.log` lines
      195-222, at 12:57 and 13:03). The watchdog fired, the rollback to `gemma4:e4b` was
      attempted, and both attempts died 150s later with `target_model_unavailable`. Root cause:
      the chat client's HTTP timeout was pinned at 180s while the watchdog budget was 45s, so
      the abandoned request kept Ollama's single runner busy and the rollback could not get one.
      Owner turns queued during that window were never served.
    - Fixed in `a8830bb` (chat client timeout now tracks the watchdog budget, so the socket
      close actually frees the runner). **The fix is NOT runtime-validated yet** — it needs a
      real stalling model, same class of evidence as 2026-06-17. Until then this is a code-level
      fix with unit coverage, not a closed gate.
    - The 2026-06-17 run rolled back to `gemma4:e2b` and succeeded; 2026-08-13 rolled back to
      the larger `gemma4:e4b` and failed. Do not assume one successful rollback generalises to
      another target model.
- Deferred (do not touch): knowledge cards, packaging, hardening suite, first-run wizard

Always read `AGENT_HANDOFF.md` first. It holds the latest operating mode and gate status.

## Model Assignments — Fable 5

Project-level override. Use `claude-fable-5` for high-reasoning phases.
These aliases map to Agent tool `model` param values.

| Phase | Model | Reason |
|---|---|---|
| sdd-explore | sonnet | Structural reading — speed matters |
| sdd-propose | fable | Architectural decisions — most capable model |
| sdd-spec | sonnet | Structured writing |
| sdd-design | fable | Architecture decisions — most capable model |
| sdd-tasks | sonnet | Mechanical breakdown |
| sdd-apply | sonnet | Implementation |
| sdd-verify | sonnet | Validation against spec |
| sdd-archive | haiku | Copy and close |
| jd-judge-a | fable | Adversarial review — highest confidence |
| jd-judge-b | fable | Adversarial review — highest confidence |
| jd-fix-agent | sonnet | Surgical fixes |
| default | sonnet | General delegation |

## Roadmap (Fable 5 era)

Priority order for the next sessions:

1. **Runtime validation — REOPENED 2026-08-13.** The 2026-06-17 evidence covers the watchdog
   only; the rollback failed twice on 2026-08-13 and was fixed in `a8830bb` (see the checkpoint
   note above). The fix needs its own runtime evidence: a real stalling model, the watchdog
   firing, and the rollback **completing** — plus confirmation that the Ollama runner is
   actually freed on socket close. This is a release gate again.
   Two other open runtime gates:
   - **Clause sanitizer** (ADR-039): run a real agenda session and report `[CLAUSE_SANITIZER]`
     verdict counts and `[TURN_LATENCY]` medians split by verdict.
   - **Chat ingestion** (new, `0768d25`): a live session must show whether the stream dies by
     filter or by disconnect. Grep `[CHAT_CONN]` and `[CHAT_INGEST] rollup`. High `arrived`
     with near-zero `survived` means the filters; `[CHAT_CONN] disconnected` means the socket.
2. **UI rendering track** — `ui_rendering_optimization_20260609` is the active branch (`audit/ui-rendering-analysis`). ADR-006 and ADR-007 are already written.
3. **OpenCohost launch readiness** — reconcile docs, validate all runtime claims, confirm smoke harness passes.
4. **Packaging** — only after runtime gates pass. Track exists at `conductor/tracks/packaging_deploy_20260510/`.

Do not invert this order without a user decision.

## Safety rules

- Do not commit unless the user explicitly asks.
- Do not revert or overwrite user's local changes.
- Never expose raw chat in LLM prompts, logs, or persistence.
- Keep LiveVoice and PTT separate — do not merge unless explicitly asked.
- Do not bypass pre-commit hooks (`--no-verify`) without user approval after review.
- Do not remove health gates, Vibe gates, or TTS fallback gates without verifying behavior first.

## Key files

| File | Purpose |
|---|---|
| `AGENT_HANDOFF.md` | Latest operating mode and active checkpoints — read first |
| `conductor/tracks.md` | All tracks and their status (`[x]` done, `[~]` in progress, `[ ]` pending) |
| `conductor/product.md` | Product vision and non-goals |
| `conductor/tech-stack.md` | Full stack: Python 3.13, CustomTkinter, Ollama, Qwen3-TTS, F5-TTS |
| `opencohost/config/settings.py` | Central settings and feature flags |
| `opencohost/core/llm_engine.py` | LLM orchestration and tier switching |
| `opencohost/ui/app_shell.py` | Main UI shell — thread-safety boundary |
| `opencohost/ui/model_panel.py` | Model management panel |
| `docs/adr/` | Architectural Decision Records |

## Test commands

```powershell
# Focused model + recovery suite (use after any core/ or ui/ change)
python -m pytest tests/test_llm_tiers.py tests/test_model_panel.py tests/test_heavy_model_inference_recovery.py -q

# Health monitor suite
python -m pytest tests/test_health_monitor.py tests/test_health_integration.py tests/test_app_shell_obs_resilience.py -q
```

Activate your project Python environment before running these commands.
