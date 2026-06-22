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
| **gemma4:e4b** | ~8B | ~19 s (mini-bench median) | None (0 near-dups); cleanest voseo | Borderline — ~2× slow for live |
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

## Mini-benchmark (small models) — results (2026-06-21, RTX 3060 12GB)

Short cohost probe per model (seed=42, cards ON, identical subset: agenda topics GTA6 delay / Valorant underdog / brutal soulslike + 2 chat requests, ~10 gens each), reasoning-cap auto-removed for `thinking` models.

| Model | Size | Latency median | p90 | Empty% | Near-dup pairs | Live-usable verdict |
|---|---|---|---|---|---|---|
| **gemma4:e2b** | 5.1B (~2B eff.) | **8.48 s** | 27.58 s | 0% | **0** | ✅ **SWEET SPOT** — coherent + live-usable (2 guardrail trips) |
| gemma4:e4b | 8.0B | 18.81 s | 32.26 s | 0% | 0 | ⚠️ Quality-first — cleanest voseo, but ~2× slow for live banter |
| llama3:latest | 8.0B | 4.05 s | 7.52 s | 0% | 6 | ❌ Fast but REPETITIVE — verbatim openings, 9 guardrail trips |
| qwen3:1.7b | 2.0B | 3.60 s | 6.29 s | 0% | 6 | ❌ Fastest but word-salad (stray English in asterisks), 10 guardrail trips |
| qwen3:4b | 4.0B | 41.54 s | 58.86 s | 0% | 0 | ❌ Clean but latency-disqualified (VRAM cliff → CPU/pagefile offload) |

**Sweet spot: `gemma4:e2b`** — the smallest model that stays coherent (0 near-dups, 0 empties) at live-usable latency (8.5 s median) on this 3060. The only model that clears BOTH gates.

Key reads:
- **The frontier is non-monotonic by param count** — it is gated by VRAM fit (12 GB) AND by enough capacity to avoid mode-collapse. e2b sits exactly at the knee.
- **Two failure modes bracket it**: below (qwen3:1.7b 3.6 s, llama3 4.05 s) = fast but repetition-collapse (6 near-dups, 9–10 guardrail trips each); above (e4b 18.8 s, qwen3:4b 41.5 s) = clean but slow. The smallest *coherent* model wins, not the smallest.
- **VRAM cliff is visible**: qwen3:4b's 41.5 s median is the signature of partial CPU/pagefile offload past 12 GB — capacity you can't fit on-GPU buys nothing for live.
- **Empty-output was SOLVED, not avoided**: 0% empty across ALL 5 including the 4 reasoning (`thinking`) models, because the reasoning-cap fix was applied; `llama3` (`thinking=False`) was correctly left alone. Validates the [ADR-014](./ADR-014-model-qualification-and-minibenchmark.md) detection approach.
- **Caveat — e2b is best-case-on-this-subset**: e2b showed 0 near-dups here but **~11 in the FULL stress run** (ADR-011). Repetition is prompt-diversity-sensitive and the mini-bench used a fixed 3-topic subset that was kind to it. So **e2b still needs the ADR-011 repetition handling under fuller agendas** — it is the sweet spot, not a repetition-free model.
- **p90 tail**: e2b median is 8.5 s but p90 spikes to 27.6 s on occasional long reasoning passes — fine for supervised banter, worth watching if the live target tightens.

---

## Consequences

- **Positive**: a concrete, hardware-anchored answer to "which model for cohost?" — the model choice is now a documented engineering tradeoff, presentable to users/stakeholders rather than a vibe. Defuses the naive "just use a bigger model" reading of ADR-011 D4.
- **Product implication**: OpenCohost should **guide** model choice on the user's actual hardware (latency probe — see ADR-014), not hardcode a model, because the viable window depends entirely on the rig.
- **Living document**: the small-model table is appended when the mini-benchmark completes; numbers are point-in-time (single-run, same seed) and directional.

---

## Related ADRs
- [ADR-011](./ADR-011-cohost-repetition-regenerate-on-duplicate.md) — repetition handling; D4 (model is the lever) is what this benchmark stress-tests against latency.
- [ADR-014](./ADR-014-model-qualification-and-minibenchmark.md) — turning this benchmark into a per-user, per-model qualification + mini-benchmark feature.
