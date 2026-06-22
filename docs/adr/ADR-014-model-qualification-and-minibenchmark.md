# ADR-014: Model Qualification — Capabilities-Augmented Reasoning Detection + Per-Model Latency Mini-Benchmark

**Date**: 2026-06-21
**Status**: Proposed — decisions made, not yet implemented
**Branch**: `feat/akira-voseo-fix-and-cohost-adr`
**Author**: Claude Code orchestrator + owner ideation
**Scope (future implementation)**: `opencohost/core/llm_engine.py` (`_uses_reasoning_token_budget` + the generation path), a per-model qualification/cache layer, and a short cohost latency probe. UI trigger deferred to its own track. Companion to [ADR-011](./ADR-011-cohost-repetition-regenerate-on-duplicate.md) and [ADR-013](./ADR-013-model-latency-vs-repetition-benchmark-rtx3060.md).

---

## Context

Two problems block ADR-011 D4 ("use a bigger/better model to stop the repetition") from being practical:

1. **Reasoning-model detection does not scale.** `_uses_reasoning_token_budget` (llm_engine.py:1295) decides whether to remove the `num_predict` cap by matching the model name against a hardcoded list (`qwen3|e2b|e4b|think`). Larger gemma reasoning models (`gemma4:12b`, `gemma:26b`) are reasoning models but are **not** on the list, so they keep the cap, spend it on their internal thinking block, and return **empty output**. The owner's point: there are **hundreds** of models; nobody can download and classify them all by hand. The detection must be **O(1) per model**, not an O(n) enumeration.

2. **Latency is hardware-dependent and unknowable from metadata.** Whether a model is "fast enough for live" depends entirely on the user's GPU (see ADR-013: gemma4:12b is ~64 s/gen on an RTX 3060 — unusable). No static list can answer this for an arbitrary user.

**Discovery**: Ollama already exposes a `capabilities` array via `ollama.show(model)`, and every reasoning model tested declares `'thinking'` in it — e.g. `gemma4:e2b/e4b/12b → [...,'thinking']`, `qwen3:4b → ['completion','tools','thinking']`. So the O(1) reasoning signal **already exists** in metadata.

---

## Decisions

### D1 — Reasoning detection: AUGMENT with Ollama capabilities; do not DEPEND on it

Keep the current detection as the base (owner: *"mantené lo que tenemos"*) and **layer Ollama capabilities on top as additional information**, without making correctness depend on Ollama's metadata shape:

1. **Keep** the existing name heuristic (`_uses_reasoning_token_budget`) as a fast, dependency-free first signal.
2. **Augment** with `'thinking' in ollama.show(model).get('capabilities', [])` — used as *info* that broadens detection to any current/future reasoning model. Wrapped defensively (missing key / Ollama version / error → fall back to the heuristic, never crash). We consume the signal; we do not couple to it.
3. **Self-heal at runtime** (belt-and-suspenders): the engine already reads a `thinking` field from the *response* (llm_engine.py:1102). If a generation returns empty content **and** a non-empty `thinking`, treat the model as reasoning, retry without the cap, and **cache** that classification per model. This catches anything both layers above miss — independent of any metadata.
4. **Cache** the per-model classification so the decision is paid once (O(1) thereafter).

**Why this shape** (owner-directed): augmenting keeps OpenCohost working if Ollama changes/removes the field, preserves the existing behavior, and still gives the O(1), whitelist-free scaling the owner asked for. The runtime self-heal means a user can pick *any* model and never get silent empty output, even with zero metadata.

### D2 — Per-model latency MINI-BENCHMARK (so the user can qualify any model on their own hardware)

Metadata cannot tell us latency — it is hardware-specific. So provide a **mini-benchmark**: a short cohost probe (a handful of generations on a fixed seed/topic subset) that measures the model's **median / p90 latency on this machine** and surfaces a verdict (live-usable / borderline / too slow), plus an empty-rate and a quick repetition signal. This is the owner's *"un test para que el usuario tenga un minibenchmark."* Results are cached per model+machine.

### D3 — Any model is selectable

The answer to "can anyone choose any model?" is **yes**. The combination of D1 (auto reasoning-cap handling + self-heal) and D2 (latency mini-benchmark) makes free model choice **safe**: pick anything, OpenCohost auto-configures the token budget and tells you whether it is fast enough on your rig. No curated allow-list of "blessed" models is required.

### D4 — UI trigger is OUT of scope (own track)

A "test this model" button / where the mini-benchmark surfaces in the interface is **deferred to its own track** — owner: *"hoy no quiero entrar en la UI."* This ADR covers the engine/qualification mechanics only; the mini-benchmark must be runnable headlessly (e.g. via CLI / on first model use) independent of any UI.

---

## Edge Cases Considered

- **Ollama missing / older version / no `capabilities`**: D1.2 is wrapped; absence falls back to the name heuristic + runtime self-heal. No hard dependency.
- **Non-reasoning model that wrongly declares `thinking`**: removing its cap is harmless (it simply won't use the extra budget).
- **Reasoning model that does NOT declare `thinking`**: caught by D1.3 (empty-content + response-thinking → retry uncapped + cache).
- **Mini-benchmark cost**: a few generations only; on a slow model it is itself slow, but that *is* the signal ("this model is slow here"). Cache so it runs once per model+machine.
- **Latency cache invalidation**: hardware/driver changes could shift latency; allow a manual re-run (the deferred UI button, D4).

---

## Consequences

- **Positive**: model selection scales to any model (O(1) reasoning detection, augment-not-depend), never silently breaks (self-heal), and is hardware-honest (latency mini-benchmark). Directly unblocks ADR-011 D4 and operationalizes the ADR-013 frontier per-user.
- **No hard new dependency**: Ollama capabilities is consumed as optional info; the system keeps working without it.
- **Deferred**: the num_predict bug fix itself (its own track), the latency verdict thresholds (what counts as "too slow" — tied to the owner's latency ceiling), and all UI surfacing (own track).
- **Operating mode**: PROPOSAL — no implementation authorized; consistent with "validation, less expansion."

---

## Related ADRs
- [ADR-011](./ADR-011-cohost-repetition-regenerate-on-duplicate.md) — D4 (model is the lever); this ADR makes that lever practical.
- [ADR-013](./ADR-013-model-latency-vs-repetition-benchmark-rtx3060.md) — the hardware benchmark this qualification turns into a per-user, per-model check.
