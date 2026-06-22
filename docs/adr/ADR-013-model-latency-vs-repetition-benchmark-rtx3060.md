# ADR-013: Model Latency vs Repetition Benchmark on Consumer Hardware (RTX 3060 12GB) — Presentation Case

**Date**: 2026-06-21
**Status**: Reference / presentation case (living document — small-model mini-benchmark appended when complete)
**Branch**: `feat/akira-voseo-fix-and-cohost-adr`
**Author**: Claude Code orchestrator + adversarial workflows (stress test, model-scaling test, mini-benchmark)
**Scope**: Reference data only — no code change. Indexed as the **presentation case** for OpenCohost's "which model for cohost mode?" story. Companion to [ADR-011](./ADR-011-cohost-repetition-regenerate-on-duplicate.md) (repetition, D4) and [ADR-014](./ADR-014-model-qualification-and-minibenchmark.md) (qualification design).

---

## Why this exists

ADR-011 D4 concluded that the cohost repetition/mode-collapse is largely a **small-model artifact** — scaling the model up fixes it. But "use a bigger model" collides head-on with **latency**, and latency is **hardware-dependent**. This document is the **presentation case**: the measured latency-vs-repetition tradeoff across local models on a representative consumer GPU, so the model-choice story is grounded in real numbers, not hand-waving.

It answers, concretely: *on real consumer hardware, you cannot simply pick the biggest model — there is a frontier, and the sweet spot is small.*

---

## Benchmark hardware (owner rig)

| Component | Spec |
|---|---|
| GPU | **NVIDIA RTX 3060, 12 GB VRAM** |
| System RAM | 32 GB |
| Swap | ~70 GB Windows pagefile (disk-backed virtual memory) |
| Runtime | Ollama (local), Python 3.13, gemma4 / qwen3 / llama3 families |

This is a mainstream mid-range gaming GPU — exactly the target OpenCohost user. The numbers below are *that machine's* reality, not a datacenter's.

---

## Results — the latency/quality frontier

Per-generation latency and repetition for cohost mode (WITH editorial cards, real `KiraAgendaController`, voseo Akira profile). Repetition is the near-duplicate-pair / mode-collapse signal from the stress tests.

| Model | ~Params | Latency / gen | Repetition (mode-collapse) | Live-usable? |
|---|---|---|---|---|
| **gemma4:e2b** | ~2B eff. | **~5–7 s** | HIGH — verbatim loops (GTA ×4), cross-topic echo; ~11 near-dup pairs in the full stress | Fast, but repeats |
| **gemma4:e4b** | ~8B | ~12 s | Better (taste test: 0/3 guardrail trips); not yet full-stressed | TBD (mini-bench) |
| **gemma4:12b** | ~11.9B | **~40–110 s** (avg ~64 s) | **None** — 7→0 near-dup pairs, both collapse signatures gone | No — ~10× too slow |
| **gemma:26b** | ~25B | **~65–216 s** | n/a (blocked) | No — too slow + **empty-output bug** |

### The frontier, stated plainly
- **Going up the model size axis kills repetition but destroys latency.** gemma4:12b is clean but ~10× slower than e2b — a co-host quip that lands in ~64 s is not a co-host. On this 3060, the large models are off the table for live use.
- **Going down keeps latency live-usable but reintroduces mode-collapse.** e2b is fast (~5–7 s) but loops.
- **The sweet spot is therefore SMALL** — the smallest model that no longer mode-collapses at a latency the 3060 sustains. The unexplored middle (e4b ~8B, qwen3 1.7B/4B, llama3 8B) is exactly where the mini-benchmark (below) looks.
- **Two large-model traps surfaced**: (1) the `num_predict` reasoning-cap bug makes gemma4:12b / gemma:26b return **empty output** unless detected (see ADR-011 / ADR-014); (2) even when fixed, their latency is unusable on consumer hardware.

### Decision fallout
- **Dropped from further testing**: gemma:26b and gemma4:12b — confirmed unusable for live cohost on this class of hardware (owner decision). Kept here only as the upper-bound reference that defines the frontier.
- **Focus models** (mini-benchmark): gemma4:e2b, gemma4:e4b, llama3, qwen3:1.7b, qwen3:4b.

---

## Mini-benchmark (small models) — *pending*

A background mini-benchmark (`minibench-small-models` workflow) measures latency (median / p90), empty-rate, and repetition for the 5 focus models on this rig, with a consistent short cohost probe (seed=42, cards ON, ~3 topics + 2 requests, identical subset across models). **Results table + sweet-spot recommendation to be appended here on completion.**

---

## Consequences

- **Positive**: a concrete, hardware-anchored answer to "which model for cohost?" — the model choice is now a documented engineering tradeoff, presentable to users/stakeholders rather than a vibe. Defuses the naive "just use a bigger model" reading of ADR-011 D4.
- **Product implication**: OpenCohost should **guide** model choice on the user's actual hardware (latency probe — see ADR-014), not hardcode a model, because the viable window depends entirely on the rig.
- **Living document**: the small-model table is appended when the mini-benchmark completes; numbers are point-in-time (single-run, same seed) and directional.

---

## Related ADRs
- [ADR-011](./ADR-011-cohost-repetition-regenerate-on-duplicate.md) — repetition handling; D4 (model is the lever) is what this benchmark stress-tests against latency.
- [ADR-014](./ADR-014-model-qualification-and-minibenchmark.md) — turning this benchmark into a per-user, per-model qualification + mini-benchmark feature.
