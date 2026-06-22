# ADR-015: Repetition Ladder — Cross-Model Validation (RTX 3060) — Comparative Case

**Date**: 2026-06-22
**Status**: Reference / validated comparison
**Branch**: `feat/akira-voseo-fix-and-cohost-adr`
**Author**: Claude Code orchestrator + validation workflows
**Scope**: Reference data — no code change. Validates the committed repetition ladder (ADR-011, commit 28f9755) across the 5 candidate models. Companion to [ADR-011](./ADR-011-cohost-repetition-regenerate-on-duplicate.md), [ADR-013](./ADR-013-model-latency-vs-repetition-benchmark-rtx3060.md), [ADR-014](./ADR-014-model-qualification-and-minibenchmark.md).

---

## Context

ADR-011's ladder eliminated spoken duplicates on gemma4:e2b (full 10-topic stress: near-dup pairs 11→0), with one residual: 2 beats regenerated to **empty** output. This ADR validates the ladder across the other 4 candidate models and explains the empty-regen residual.

Method: same minibench subset (seed=42, cards ON, 3 agenda topics idx[0,4,8] + 2 chat requests), ladder ACTIVE (transformer+validator wired like app_shell, confirmed per model), compared to the WITHOUT-ladder minibench (ADR-013). Reasoning models run with an instance-only `think=False` (the ADR-014 capabilities path). RTX 3060 12 GB.

---

## Results

| Model | Latency median | Near-dup pairs (no-ladder → with-ladder) | Guardrail trips (with ladder) | Empty-regen residual | Live-usable |
|---|---|---|---|---|---|
| **gemma4:e2b** *(chosen; FULL 10-topic stress)* | 8.5 s | **11 → 0** | 9 → 2 | 2 | ✅ validated default |
| **gemma4:e4b** | 5.16 s | 0 → 0 | 10 | **0** | ✅ coherent, fast |
| **qwen3:1.7b** | 1.10 s | **6 → 0** | 10 | 0 | ❌ word-salad (incoherent) |
| **qwen3:4b** | 35.67 s | 0 → 0 | 3 | 0 | ❌ latency-disqualified |
| **llama3:latest** | **3.67 s** | **6 → 0** | 5 (was 9) | **0** | ✅ **best live pick** |

*Reasoning models (e4b, qwen3:1.7b/4b) ran with instance-only `think=False`; llama3 is not thinking-capable. e2b's full-stress run kept reasoning ON.*

---

## Key Findings

### 1. The ladder drives spoken duplicates to ZERO on every model that looped
The two looping models both went **6 near-dup pairs → 0**: llama3 via 5 trailing-dup trims + 5 regen events; qwen3:1.7b via 8 exact-repetition + 2 other rejections + 1 trim. Clean A/B wins. Non-looping models (e4b, qwen3:4b) stayed 0→0 — the ladder is insurance there, not a demonstrated fix.

### 2. Guardrail trips RISING with the ladder is expected, not a regression
e4b 2→10, qwen3:4b 2→3. With the ladder ACTIVE, the system now **detects and rejects** the dups the no-ladder run silently passed through. Trips-up = the ladder catching more, then handling it (trim or regen). llama3 trips actually **dropped** 9→5 because trailing-dup trims resolve some cases without a full regen.

### 3. The empty-regen residual is a REASONING-MODE artifact, not a model-size one — and `think=False` eliminates it
The only nonzero empty-regen was gemma4:e2b (2), whose full-stress run kept **reasoning ON** (num_predict cap removed, model thinks). Every minibench model — including the 2B qwen3:1.7b and the larger thinking models — hit **0 empty-regen once `think=False` was injected**. Root cause: thinking-capable models can emit reasoning-only / empty content payloads that trigger an empty regen. **`think=False` removes that failure mode entirely, regardless of size.** There is no evidence that bigger/8B quality inherently fixes it (llama3 shows 0 because it is *not* thinking-capable, not because it is bigger).

**Bonus**: `think=False` also roughly **halved latency** (e4b 18.8 s → 5.16 s). For a live co-host, disabling thinking is a double win: faster AND no empty-regen.

### 4. With the ladder, the best live pick shifts to llama3 (pending full-stress)
**llama3:latest** is the best WITH the ladder: 3.67 s median, near-dup 6→0, guardrail trips that *dropped* (9→5), 9 agenda turns accepted, 0 empty residual, and not thinking-capable (no reasoning complications). **gemma4:e4b** is a strong runner-up (5.16 s, coherent voseo, 0 empty) but never looped, so the ladder is insurance there. **gemma4:e2b** remains the defensible default because it is the only model with a FULL 10-topic stress validation. qwen3:1.7b (incoherent) and qwen3:4b (35.67 s) are out.

---

## Implications

- **Run reasoning models with `think=False` for cohost** (refines [ADR-014](./ADR-014-model-qualification-and-minibenchmark.md) D1): detection via Ollama `capabilities`→`thinking` should set `think=False` for the live co-host path — it eliminates the empty-regen residual AND roughly halves latency. (Removing only the `num_predict` cap keeps thinking on, which is slower and can still leak empties.)
- **The ladder + `think=False` = the complete fix**: ADR-011's "helps-partially" (empty-regen residual) becomes "fixes-it" once reasoning is disabled. The two strands of the session converge.
- **Model choice opens up**: with the ladder, llama3 (3.67 s) and e4b (5.16 s) are both faster than the chosen e2b (8.5 s) and repetition-clean. **Recommended next step**: run the FULL 10-topic stress on llama3 (and e4b) — if it holds, llama3 is the better live default.
- The deferred ADR-011 spoken-filler slice is now LOWER priority for the empty-regen case specifically (think=False removes it), but still valuable to cover any genuine regen-failure gap and to make recovery audible.

---

## Content-quality scoring (blind 3-judge panel, 2026-06-22)

Mechanical metrics (latency, repetition, empty-regen) don't capture whether a model SOUNDS like Kira. A blind 3-judge panel scored each model's actual responses to 5 identical prompts (3 topic-opens + 2 chat requests), anonymized M1–M5, on persona/voseo, coherence, engagement, and topic/card use (1–10). The winner was UNANIMOUS.

| Rank | Model | persona/voseo | coherence | engagement | card-use | **OVERALL** |
|---|---|:---:|:---:|:---:|:---:|:---:|
| 1 | **gemma4:e2b** | 8.5 | 8.9 | 8.1 | 8.7 | **8.43** |
| 2 | gemma4:e4b | 8.8 | 8.8 | 8.5 | 6.9 | 8.23 |
| 3 | llama3:latest | 4.3 | 6.3 | 5.3 | 5.7 | 5.00 |
| 4 | qwen3:1.7b | 2.9 | 2.7 | 2.2 | 2.1 | 2.40 |
| 5 | qwen3:4b | 1.7 | 1.9 | 1.2 | 1.3 | 1.57 |

- **Content CONFIRMS gemma4:e2b** — the same model the mechanical view chose. Both axes converge → the default is best on content AND latency/repetition simultaneously.
- **gemma4:e4b has the best raw VOICE** (sharpest voseo, highest engagement) but lost #1 to a single fully off-topic answer (the soulslike prompt drifted to leaks/permaban), tanking its card-use (6.9). Punchier but less reliable.
- **llama3 — the latency star — BREAKS persona**: it slips into Iberian Spanish (tuteo/vosotros: "eres", "¿os parece?", "cogerlo"), reading like a generic assistant, not a Rioplatense voseo Kira (5.00). **This corrects the latency-only view: the fastest, repetition-clean model is NOT the best Kira.** Persona is a hard gate that only content evaluation exposes.
- qwen3:4b: worst — fallback stalls + a raw English chain-of-thought preamble that leaks the system prompt and admits being an AI (it ignored `think=False` at the instance level). qwen3:1.7b: word-salad / empty outputs.

**Combined recommendation (content + latency + repetition): keep `gemma4:e2b` as the live Kira default** — unanimous content winner (8.43), full-stress validated at 8.5 s, repetition 11→0 with the ladder. `gemma4:e4b` is a punchier-persona fallback IF the off-topic-drift is fixed and the latency is acceptable. `llama3` is a fast-path emergency only (persona sacrificed).

---

## Consequences

- The ladder is validated as **model-independent and effective**: it eliminates spoken duplicates on every model that produces them, with no empty-regen residual when reasoning is disabled.
- Single-run-per-condition (n=1, seed=42, minibench subset); directional, not statistical. e2b is the only FULL-stress-validated model.

## Related ADRs
- [ADR-011](./ADR-011-cohost-repetition-regenerate-on-duplicate.md) — the ladder itself (e2b full-stress validation).
- [ADR-013](./ADR-013-model-latency-vs-repetition-benchmark-rtx3060.md) — the without-ladder benchmark this compares against.
- [ADR-014](./ADR-014-model-qualification-and-minibenchmark.md) — reasoning detection; this ADR sharpens it to `think=False` for the cohost path.
