# ADR-056 - Adaptive Reasoning Governance, Context Decoupling, and Workload-Aware Generation Budgets

**Date:** 2026-09-05  
**Status:** Accepted - Architectural Blueprint (Amended for WU1 Observational Seam)  
**Decision Scope:** Decoupling Context Window from Model Tiers, Intent-Driven Generation Budgets, Real-Time Hardware & Residency Guardrails (NVML + Ollama PS), and Adaptive Latency Calibration.

---

## 1. Decision Summary

OpenCohost is refactoring its inference resource governance to replace static, heuristic-driven limits with a decoupled, hardware-portable, and workload-aware architecture.

This decision enforces three primary architectural mandates:
1. **Decouple Three Orthogonal Concerns:**
   * **Context Allocation (`allocated_context`)**: Formally distinguishes `native_context_max` (model capability from `/api/show`), `requested_context` (operator configuration), and `allocated_context` (runtime active context from `/api/ps`). Allocated per model configuration/session, bounded by hardware residency, never thrashing per turn.
   * **Generation Budget (`num_predict`)**: Resolved per inference request based on caller intent, target latency, and hardware guardrails.
   * **Reasoning Policy (`think`)**: Managed as an independent toggle or depth level (`false`, `true`, `low`, `medium`, `high`, `max`).
2. **Abolish Tier-Bound Context Limits:** Retires `LLM_TIER_EFFECTIVE_CTX_CAPS` (which falsely married Quality/Balanced/Fast tiers to arbitrary context numbers like 4096). Context length is a property of the **model architecture + runtime allocation + hardware**, not of the model selection tier.
3. **Empirical Adaptive Calibration & Multi-Layer Guardrails:**
   * **Dynamic Token Velocity**: Calibrates generation budgets using runtime Exponentially Weighted Moving Average (EWMA) of actual decode tokens/second (`eval_count / eval_duration_ns`) provided directly by Ollama.
   * **Relative Hardware Guardrails**: Scans NVML for relative VRAM headroom (`max(1.0 GiB, total_vram * 0.15)`) and queries `ollama.ps()` (`OllamaResidencyProbe`) to track `residency_ratio = size_vram / size` and `spill_bytes = max(0, size - size_vram)`.
   * **Workload-Aware Intent Routing**: Differentiates `InferenceIntent.CHAT` (latency-first), `InferenceIntent.REASONING` (depth-first), and `InferenceIntent.DRAFTING` / `AGENDA` (long-form generation), decoupling long generations from direct Piper TTS playback.

---

## 2. Context & Problem Statement

### 2.1 The 100% GPU Utilization Misconception
In local LLM execution, 100% GPU compute core utilization during inference is expected matrix-multiplication behavior. It does not indicate hardware degradation or overload. The true failure modes requiring control are:
* **Thermal and Power Saturation**: Sustained high-wattage execution over long periods.
* **VRAM Spilling / CPU Offload Thrashing**: When the model weights or the attention Key-Value (KV) Cache exceed dedicated GPU memory, causing layers or tensors to be dumped into system RAM via PCIe, dropping memory bandwidth by ~90% and freezing system responsiveness.
* **Interactive Latency Blowup**: Unbounded chain-of-thought (`<think>`) generation locking conversational turns for dozens of seconds.

### 2.2 Flaws in Prior Assumptions
Earlier iterations suffered from multiple design shortcuts:
* **Arbitrary Hardcoded Limits**: Imposing fixed numbers (e.g. 512, 1024, 2048) or fixed VRAM thresholds (e.g. `free_vram > 6 GB`), which do not scale across different GPUs (RTX 3060 vs. RTX 4090 vs. A100).
* **Conflation of Context and Quality**: Hardcoding `quality: 4096` in `LLM_TIER_EFFECTIVE_CTX_CAPS`, ignoring that models like `gemma4:e4b` natively support 128K context.
* **Single-Path TTS Coupling**: Routing every generation directly to text-to-speech, artificially constraining long-form generation (e.g., Agenda summaries, research drafting) to conversational speech lengths.

---

## 3. Core Architecture Decisions

### 3.1 Orthogonal Separation of Inference Parameters

```text
+-----------------------------------------------------------------------------------------+
| 1. CONTEXT ALLOCATION (triad: native_context_max, requested_context, allocated_context)  |
|    - Bound per-model / per-session                                                      |
|    - Derived from model /api/show, Ollama /api/ps, and VRAM residency                   |
+-----------------------------------------------------------------------------------------+
                                             |
                                             v
+-----------------------------------------------------------------------------------------+
| 2. GENERATION BUDGET (num_predict)                                                      |
|    - Calculated dynamically per turn                                                    |
|    - Input: caller intent, target latency, empirical tokens/sec                         |
+-----------------------------------------------------------------------------------------+
                                             |
                                             v
+-----------------------------------------------------------------------------------------+
| 3. REASONING POLICY (think)                                                             |
|    - think: false (direct fast reply, suppresses <think>)                                |
|    - think: true (allows cognitive scratchpad up to budget)                              |
+-----------------------------------------------------------------------------------------+
```

### 3.2 Dynamic Calibration via Empirical Decode Velocity
Rather than assuming hardware speed with arbitrary magic constants, OpenCohost measures actual observed generation throughput:
$$\text{TPS}_{\text{turn}} = \frac{\text{eval\_count}}{\text{eval\_duration\_ns} / 10^{9}}$$
$$\text{EWMA}_{\text{TPS}} = \alpha \cdot \text{TPS}_{\text{turn}} + (1 - \alpha) \cdot \text{EWMA}_{\text{TPS}}$$

Samples with $\text{eval\_count} \le 0$, $\text{eval\_duration\_ns} \le 0$, errors, or cancellations are strictly dropped.

### 3.3 Partitioned Tracker & Calibration Lifecycle
The throughput profile is partitioned by:
$$\text{PartitionKey} = (\text{provider}, \text{model\_digest}, \text{allocated\_context})$$
This prevents cross-model or cross-quantization pollution.

The tracker moves through an explicit calibration lifecycle:
* **`COLD`**: Fewer than $N_{\text{min}}$ valid samples (default 3). The governor does NOT make runtime decisions based on EWMA.
* **`WARMING`**: Between $N_{\text{min}}$ and $N_{\text{cal}}$ samples (default 3 to 10). EWMA is stabilizing.
* **`CALIBRATED`**: $\ge N_{\text{cal}}$ samples. Statistically stable for adaptive targets.

### 3.4 Hardware Residency & Safe Budget Clamping

Ollama residency metrics from `/api/ps` are represented structurally without magic number cuts:
* $\text{residency\_ratio} = \frac{\text{size\_vram}}{\max(1, \text{size})}$
* $\text{spill\_bytes} = \max(0, \text{size} - \text{size\_vram})$

```python
def resolve_effective_budget(
    requested_budget: Optional[int],
    preset: str,
    intent: InferenceIntent,
    allocated_context: int,
    prompt_tokens: int,
    tps_ewma: float,
    vram_headroom_mb: float,
    residency_ratio: float,
) -> int | BudgetInfeasible:
    remaining_context = allocated_context - prompt_tokens - SAFETY_RESERVE_TOKENS
    if remaining_context <= 0:
        return BUDGET_INFEASIBLE

    if preset == "custom" and requested_budget is not None:
        # User explicit choice: never clamped for latency; only clamped for hard limits
        if requested_budget > remaining_context:
            return min(requested_budget, remaining_context)
        return requested_budget

    target_latency = TARGET_LATENCIES.get(preset, 8.0)
    perf_cap = int(tps_ewma * target_latency) if tps_ewma > 0 else DEFAULT_FALLBACK_BUDGET
    
    # Residency degradation guard: scale back proportionally if offloaded to host RAM
    if residency_ratio < 1.0:
        perf_cap = int(perf_cap * max(0.25, residency_ratio))

    effective = min(perf_cap, remaining_context)
    if effective <= 0:
        return BUDGET_INFEASIBLE
    return effective
```

### 3.5 Telemetry & Observability Receipts
Every completed generation returns metadata for runtime audit without logging user prompt or thinking text:
```json
{
  "reasoning_mode": "auto",
  "intent": "chat",
  "requested_budget": "auto",
  "effective_budget": 512,
  "allocated_context": 4096,
  "prompt_tokens": 1280,
  "eval_tokens": 420,
  "eval_duration_ns": 9500000000,
  "tokens_per_second": 44.2,
  "residency_ratio": 1.0,
  "spill_bytes": 0,
  "vram_used_mb": 9400,
  "vram_total_mb": 12288,
  "calibration_state": "CALIBRATED",
  "clamping_reason": "LATENCY_TARGET"
}
```

---

## 4. Work Breakdown Structure (WBS)

* **Phase 1: Telemetry & Profiler Seam (WU1 - STRICTLY OBSERVATIONAL)**
  - Implement `opencohost/core/engine/inference_telemetry.py` (thread-safe, in-memory, zero SQLite).
  - Extract `eval_count` and `eval_duration` from the final response chunk in both streaming and non-streaming attempts.
  - Snapshot `OllamaResidencyProbe` residency ratio without affecting budget.
  - Zero modifications to `num_predict`, `num_ctx`, `think`, TTS, or routing.
  - 100% behavioral parity.
* **Phase 2: Context Decoupling**
  - Formalize context triad: `native_context_max` (`/api/show`), `requested_context`, and `allocated_context` (`/api/ps`).
  - Deprecate `LLM_TIER_EFFECTIVE_CTX_CAPS`.
* **Phase 3: Adaptive Budget Engine**
  - Implement `opencohost/core/engine/llm_budget_engine.py` with intent routing and guardrail clamping.
* **Phase 4: API & Persistence Migration**
  - Persist `reasoning` and `context` preferences in `model_parameters.json`.
* **Phase 5: UI Refinement in `ModelCard`**
  - Expose presets and runtime telemetry diagnostics in `ModelCard.tsx`.

---

## 5. Acceptance Gate for WU1 (Telemetry Foundation)

```text
[x] Streaming and non-streaming produce exactly one final telemetry sample.
[x] eval_duration_ns properly converted to seconds via / 1e9.
[x] Errored, canceled, or invalid (eval_count <= 0) attempts do not pollute EWMA.
[x] Partitioning by (provider, model_digest, allocated_context) prevents cross-contamination.
[x] Calibration state machine (COLD -> WARMING -> CALIBRATED) is deterministic.
[x] Division by zero prevented on empty durations or sizes.
[x] Ollama/NVML downtime fails open to UNAVAILABLE telemetry, never crashes inference.
[x] Absolute behavioral parity: num_ctx, num_predict, and think remain completely unchanged.
[x] Zero storage of prompt, thinking, or response text in logs or telemetry structures.
[x] All existing unit, integration, and regression suites pass GREEN.
```
