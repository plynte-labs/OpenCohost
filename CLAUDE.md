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
  - `heavy_model_inference_recovery_20260609` — **CLOSED 2026-08-13.** Watchdog and rollback are
    both runtime-validated now, but they were validated separately and the history matters.
    - Watchdog: fires at exactly 45.00s on a hung `qwopus`. Confirmed 2026-06-17
      (`logs/opencohost_20260617_175453.log` 18:05:24) and again twice on 2026-08-13.
    - Rollback: **FAILED twice on 2026-08-13** (`logs/opencohost_20260813_114819.log` lines
      195-222, at 12:57 and 13:03) — both attempts died 150s later with
      `target_model_unavailable`. Root cause: the chat client's HTTP timeout was pinned at 180s
      while the watchdog budget was 45s, so the abandoned request kept Ollama's single runner
      busy and the rollback could not get one. Owner turns queued in that window were lost.
    - Fixed in `a8830bb`, then **runtime-validated 2026-08-13**
      (`logs/opencohost_20260813_154829.log` 16:06:59-16:07:07): same hung `qwopus`, watchdog at
      45.00s, rollback to `llama3` **completed in 8.57s**, `Modelo cambiado a: llama3`, queue
      continued. 150s-and-fail became 8.57s-and-succeed.
    - Do not assume one successful rollback generalises to another target model: 2026-06-17 went
      to `gemma4:e2b` and worked, 2026-08-13 went to the larger `gemma4:e4b` and failed. What was
      actually broken was the runner never being freed, not the target.
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

1. ~~**Inference recovery**~~ — **CLOSED 2026-08-13**, watchdog and rollback both validated.
   See the checkpoint note above for the log evidence. Two runtime gates remain open:
   - **Chat ingestion** (`0768d25`) — **IN PROGRESS**, owner deferred it: the 2026-08-13 session
     never connected a stream, so nothing was measured. The instrumentation is shipped and
     waiting. Next live session: grep `[CHAT_CONN]` and `[CHAT_INGEST] rollup`. High `arrived`
     with near-zero `survived` means the filters are eating the chat; `[CHAT_CONN] disconnected`
     means the socket drops. Symptom to explain: Kira never activates and messages appear
     roughly one every few minutes.
   - **Clause sanitizer** (ADR-039) — run a real agenda session and report `[CLAUSE_SANITIZER]`
     verdict counts and `[TURN_LATENCY]` medians split by verdict.
   One datum is also outstanding for the streaming track: a **true keep-alive-expiry cold load**.
   `LLM_KEEP_ALIVE` is 7m and `_prepare_model` never runs on the turn path, so an idle gap over
   7 minutes makes the next turn pay a cold load inside the chat call. Warm `load_ms` is
   175-379ms and a semi-cold one measured 3587ms, but the real cold case has never been
   observed. It sets the streaming first-chunk budget. Grep `load_ms=` after a long idle.

   **Live-fire evidence, 2026-08-13** (`logs/opencohost_20260813_154829.log` 16:10:13): the
   `no_ai_self_identification` guardrail (R9) blocked a real `source=direct` reply. Under the
   streaming track's "head hot" policy that line would have been broadcast ~2.6s before the
   verdict landed. This is why the per-sentence guard in that design is mandatory, not optional.
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
