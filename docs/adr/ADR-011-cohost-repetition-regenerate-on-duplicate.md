# ADR-011: Cohost Repetition Handling — Detect → Trim → Regenerate, with In-Character Recovery

**Date**: 2026-06-21
**Status**: Proposed — investigation complete, not yet implemented
**Branch**: Investigation only (throwaway harnesses under `temp/`; no implementation branch yet)
**Author**: Claude Code orchestrator + adversarial workflow (stress test + card-causation judge)
**Scope (future implementation)**: `opencohost/smart_aggregator/kira_agenda_controller.py` (guardrail gate: `accept_output` → `register_failure` → re-tick), `opencohost/core/llm_engine.py` (output transformer alongside `enforce_live_safety_cap`). New track `cohost_repetition_regenerate_20260621`. Sibling tracks (own scope): editorial matcher recall, input-sanitizer gaming-word false positives.

---

## Context

A realistic cohost-mode **stress test** was run to assess stream-readiness: 20 armed editorial cards, 10 agenda topics, ~18 randomized chat events (seed=42) including 10 viewer requests, on **gemma4:e2b**, driving the real `KiraAgendaController.next_action()` loop (46 real generations, 327s, audio mocked). The overall verdict was **not stream-ready**, on two independent FAILs: **latency** and **repetition / mode-collapse**.

This ADR covers the **repetition** finding and the decided handling strategy. Latency is deliberately out of scope as a defect (see Decision **D5** — it is a configurable user preference, not a bug). The editorial-matcher and input-sanitizer findings are spun out to their own sibling tracks.

### Findings — repetition under sustained load

- **~24% of generations were involved in a duplicate cluster.** 11 near-duplicate output pairs (difflib ratio ≥ 0.85) across 46 generations; 4 verbatim/near-verbatim clusters.
- **One topic stalled completely.** The GTA6 topic emitted **the same answer four times** (open + 2 turns + close, verbatim) — a hard stall masked by a `completed` status.
- **Cross-topic contamination.** The Overwatch topic `open` was a **byte-for-byte copy** of the previous (unrelated) streamer-permaban `open`, with zero Overwatch content.
- **The guardrail detects but does not recover.** All 9/46 (19.6%) guardrail trips (`ERR_GUARDRAIL_SIMILAR/repeats_recent_line` ×8, `ERR_GUARDRAIL_LOOPING/exact_repetition` ×1) landed exactly on the duplicated events — yet the runtime **still emitted the stale text** before re-ticking. The guardrail is a *detector with no in-line recovery path*.
- **Pipeline did not crash.** 0 errors, 0 empty generations, all 10 topics closed, all 10 requests answered, persona/voseo held (46/46 voseo, 0 mexicanisms). The failure is **content quality**, not infrastructure.

### The card-causation A/B (adversarially judged)

A hypothesis was raised: *the editorial card is causing the repetition.* It was tested with a controlled A/B — identical 10 topics, identical seed, identical timeline, **cards ON vs cards OFF** — then the causal verdict was adversarially judged.

| Metric (real generations) | WITH cards | WITHOUT cards |
|---|---|---|
| Near-dup pairs (≥0.85) | 11 | **2** (−82%) |
| Verbatim clusters | 4 | **1** |
| Guardrail trip rate | 9/46 (19.6%) | 5/44 (11.4%) |
| Avg output length | 843.7 chars | 823.7 chars (flat) |

The naive read ("card-guilty", repetition dropped 82% without cards) was **overturned by the adversarial judge** to **card-CONTRIBUTOR (medium confidence, n=1/arm)** on decisive attribution evidence:

- Of the 11 with-card near-dup pairs, **zero are card-on-card**; 7/11 are between two no-card outputs.
- The worst duplicate (idx14) *had* the Overwatch card attached but copied an unrelated previous topic verbatim and **never mentioned the card**.
- The persona tic *"mirá es que cuando"* appears at the **same rate with or without cards** (17/46 vs 16/44).
- Output length was flat (843 vs 823), so the drop is **not** a shorter-output artifact.

**Root cause**: model-level **context recycling / mode-collapse intrinsic to gemma4:e2b** (a 2B-class model) under the long Akira persona prompt — the model attends to and regenerates its own recent output (a KV-cache attractor). **Editorial cards amplify** the loop by enlarging and uniformizing the prompt, but they do **not generate** the repeated text. The biggest lever is therefore **model choice**, not card removal.

---

## Decisions

### D1 — Recovery LADDER: detect → trim → evaluate → regenerate (cheap-first)

When the guardrail flags a repeat, do **not** jump straight to a full regeneration (an extra model pass = extra latency, which is expensive in the owner's monologue mode). Apply the cheapest viable repair first:

1. **Detect** the duplicated span (the guardrail already computes similarity to recent lines).
2. **Trim** a repeated **trailing** segment (a recycled hook/closing phrase) at a sentence/phrase boundary.
3. **Evaluate** what remains: is there enough fresh, self-standing content to speak?
4. **Regenerate** (bounded) **only** if trimming did not salvage a usable response.

**Why**: the most common, cheapest-to-fix pattern is *tic saturation* — a repeated trailing hook (e.g. `"¿entendés?"` in 16/46, `"es como si"` in 32/46). Trimming that tail and speaking the rest costs **zero** extra latency. Regeneration is reserved as the last resort.

### D2 — Recovery speech is IN-CHARACTER, never machine-meta

If a spoken filler is used to cover a regeneration, it must stay inside Kira's persona. A line like *"estoy teniendo problemas de duplicación"* is **rejected**: it directly violates two of Akira's own profile rules — *"NUNCA digas que sos una IA o un modelo de lenguaje"* and *"NUNCA DECIR ALGO PREDECIBLE o DECIR QUE EL CHAT SE REPITE"* — and breaks the on-stream illusion. The correct shape is an in-character self-catch (e.g. *"pará, eso ya lo tiré, dejame ir por otro lado"*), drawn from a **varied pool** (a single canned line would itself become the new repetition — at ~20% trip rate it would be heard ~9× per session).

### D3 — The guardrail must REGENERATE on detection, not emit-then-flag (the platform bug)

Today the guardrail detects the duplicate **after** the output is already on its way out, and the stale text is emitted anyway. The detection → recovery path (D1) must run **before** the output is surfaced to TTS. This is the single most important fix and is **model-independent**: it holds regardless of which model is used.

### D4 — Root cause is model-level; model choice is the biggest lever

The repetition is intrinsic to gemma4:e2b's small size. A non-2B production model is expected to collapse far less. Therefore: **do not ship gemma4:e2b for cohost mode**, and **re-run this stress test on the production-target model** before judging the platform (or the cards) harshly. Cards are an amplifier, not the cause — they need no removal, only awareness that they enlarge the prompt on small models.

### D5 — Latency / response length is a user PREFERENCE, not a defect (scope boundary)

The stress test's 7.15s median latency correlates with output length (r=0.75; avg 844 chars). This is **not a bug**: response length is fully configurable (`response_length` ∈ {`corta`, `normal`, `expandida`} with an auto-degrade chain, plus a `monologue` rhythm), and the owner deliberately runs **monologue** mode. The latency is the cost of that chosen verbose mode. It is **noted as a known, configurable tradeoff** and explicitly **not** treated as something to "fix" by capping output globally.

---

## Edge Cases Considered

- **Trim leaves nothing (full-output duplicate).** The stress test's worst cases were *whole* outputs duplicated (GTA ×4; Overwatch open = permaban open). There is no "rest" to speak — trimming yields an empty string. The ladder (D1 step 4) must fall back to bounded regeneration / skip-the-beat here. Trim alone is only half the solution; it handles partial/trailing repetition cheaply but cannot rescue a total duplicate.
- **Callback vs. loop.** Not all repetition is bad — intentional callbacks (*"como te decía hace un rato…"*) are part of Kira's persona. The detector must distinguish a stale loop from a deliberate callback; over-eager trimming would kill a charming feature. (Open question — see below.)
- **Filler that itself repeats.** A single recovery line, fired ~9×/session, becomes the new tic. Mitigation: a varied pool (D2).
- **Filler-loop.** If the model is mode-collapsed, the *regeneration* may also be a duplicate → filler → regenerate → duplicate → filler… Mitigation: **bounded** retries (N), then a graceful final exit (skip the beat / clean transition) rather than an infinite stall.
- **Card amplification on small models.** Cards lengthen/uniformize the prompt and push a 2B model deeper into the loop (A/B: 11→2 near-dup pairs without cards). This is expected to shrink or vanish on a larger model; no card change is mandated.

---

## Adversarial Review — What It Caught

The card-causation verdict was produced by a 3-phase workflow (control run → measure → adversarial judge). The judge **overturned the measurement agent's proposed "card-guilty"** verdict:

1. **Over-attribution caught.** The measure stage saw an 82% repetition drop without cards and proposed *card-guilty*. The judge independently reproduced the metrics (confirming they were accurate, not inflated) but showed the **attribution was wrong**: 0/11 duplicate pairs were card-on-card, the worst dup ignored its own card, and the persona-tic rate was identical with/without cards. Conclusion corrected to **card-contributor** (amplifier, not generator).
2. **Confidence bounded honestly.** n=1 per arm → the judge capped confidence at **medium** and flagged that a larger sample / larger model is needed to separate model-specific drift from platform behavior.

This is exactly the failure mode adversarial review exists to catch: a real, correctly-measured effect (cards reduce repetition) attached to the **wrong causal story** (cards *cause* repetition).

---

## Open Questions (owner decisions pending)

- **Acknowledge vs. cover.** When Kira catches herself repeating, should she **acknowledge it in character** (a charming *"uy, ya dije eso, dame otra"* — human but visible) or **cover it seamlessly** (a transition the audience never registers as a glitch)? This decides the wording and the tolerable frequency of the recovery line.
- **Bounded-retry count.** How many regeneration attempts before the graceful exit, and what is the exit (skip the beat / clean topic transition / fallback line)?
- **Prompt-uniformity mitigation.** Worth reducing card/prompt uniformity to help small models, or simply mandate a larger model and leave the prompt as-is?

---

## Consequences

- **Positive**: a **model-independent** fix (D3 + D1) stops stale duplicates from ever reaching the audience, cheaply (trim-first), while staying in persona (D2). The biggest quality lever (model choice, D4) is made explicit. Latency stays the owner's configurable choice (D5), not a forced cap.
- **Reversible / low-blast-radius**: the recovery ladder lives in the existing guardrail gate and the output-transformer seam; no change to the agenda contract, which already held end-to-end (10/10 topics, no derailment).
- **Deferred**: editorial matcher recall (no stemming → plural queries miss cards; single-use + one-active-card lock) and the input-sanitizer false positive on gaming words (`drop`) are **out of scope** here — own tracks, now decided in **[ADR-012](./ADR-012-editorial-matcher-sanitizer-and-card-lifecycle.md)**.
- **Validation gate**: re-run the cohost stress test (a) on the production-target model and (b) after wiring the regenerate-on-duplicate path, targeting a duplicate-cluster rate near zero and **no** stale duplicate ever surfaced. Do not declare cohost mode stream-ready on gemma4:e2b.

---

## Related ADRs

- **[ADR-012](./ADR-012-editorial-matcher-sanitizer-and-card-lifecycle.md)** — the sibling matcher-recall / single-use-lifecycle / input-sanitizer / parked-sessions decisions that this ADR deferred.

## Update log

- **2026-06-21**: Cross-linked ADR-012 (sibling findings decided). The D4 "validation gate" — re-running the stress test on a larger model — is now IN PROGRESS (see the stress-rerun-bigger-model workflow); results recorded below.
- **2026-06-21 (result)**: **D4 VALIDATED** — the repetition is largely a small-model artifact. Re-ran the stress test (same seed=42, cards ON, documented 5-topic subset) on **gemma4:12b** (11.9B, ~6× the e2b effective params, same family): near-duplicate pairs **7 → 0**, verbatim clusters **2 → 0**, guardrail trips **17.9% → 4.8%**, avg length flat (840 → 804). Both signature collapses vanished — the GTA ×4 verbatim loop (6 GTA pairs → 0) and the Overwatch-open ≡ permaban-open cross-topic echo (similarity 0.999 → 0.023). Verdict: *bigger-model-fixes-it* (medium confidence; n=1, same seed, subset). Residual on 12b: a recurring opener tic and some rephrased within-topic recycling (no verbatim loops), plus 2/5 chat requests returning generic deflection instead of engaging the attached card.
- **2026-06-21 (BLOCKER found)**: D4's "use a bigger model" is **gated by a production bug**. `_uses_reasoning_token_budget` (llm_engine.py:1295) removes the `num_predict` cap only for model names matching `qwen3|e2b|e4b|think`. Larger gemma reasoning models (**gemma4:12b**, **gemma:26b**) emit a thinking block but are NOT whitelisted → the cap is spent on thinking → they return **empty content** every generation. A user selecting them as the cohost model gets silent empty output. Registered as its own track.
