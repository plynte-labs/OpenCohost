"""Adaptive Inference Budget Engine for OpenCohost (ADR-056 WU3).

Orchestrates intent-driven generation budgets (num_predict), reasoning policy (think),
and context governance with empirical decode throughput (EWMA TPS) and hardware residency.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Optional, Any

SAFETY_RESERVE_TOKENS: int = 128
DEFAULT_FALLBACK_BUDGET: int = 768
MIN_EFFECTIVE_BUDGET: int = 32


class InferenceIntent(str, enum.Enum):
    CHAT = "chat"
    REASONING = "reasoning"
    DRAFTING = "drafting"
    AGENDA = "agenda"


class BudgetVerdict(str, enum.Enum):
    ALLOWED = "allowed"
    CLAMPED_LATENCY = "clamped_latency"
    CLAMPED_CONTEXT = "clamped_context"
    BUDGET_INFEASIBLE = "budget_infeasible"


# Target latencies in seconds per preset / intent
TARGET_LATENCIES = {
    "fast": 3.0,
    "balanced": 6.0,
    "quality": 12.0,
    InferenceIntent.CHAT: 4.0,
    InferenceIntent.REASONING: 15.0,
    InferenceIntent.DRAFTING: 30.0,
    InferenceIntent.AGENDA: 20.0,
}


@dataclass(frozen=True)
class BudgetResolution:
    intent: InferenceIntent
    verdict: BudgetVerdict
    effective_budget: int
    think: Optional[bool]
    allocated_context: int
    remaining_context: int
    clamp_reason: Optional[str]
    calibrated_tps: float
    tts_eligible: bool


def resolve_generation_budget(
    *,
    intent: InferenceIntent = InferenceIntent.CHAT,
    allocated_context: int = 4096,
    prompt_tokens: int = 0,
    requested_budget: Optional[int] = None,
    preset: str = "balanced",
    reasoning_enabled: bool = False,
    tps_ewma: float = 0.0,
    residency_ratio: Optional[float] = None,
    safety_reserve: int = SAFETY_RESERVE_TOKENS,
) -> BudgetResolution:
    """Resolve the effective num_predict budget, think parameter, and TTS eligibility.

    Guarantees:
    - Never fabricates capacity: returns BUDGET_INFEASIBLE with effective_budget=0 if remaining <= 0
    - If intent == DRAFTING: tts_eligible is False, and generous latency budget
    - If intent == CHAT: tts_eligible is True
    - Clamps to remaining_context if budget exceeds headroom
    - Clamps to latency target if calibrated (tps_ewma > 0 and preset != "custom")
    - Accounts for VRAM spill / residency_ratio degradation
    """
    safe_ctx = max(0, int(allocated_context or 0))
    safe_prompt = max(0, int(prompt_tokens or 0))
    remaining_context = safe_ctx - safe_prompt - max(0, int(safety_reserve))

    tts_eligible = (intent != InferenceIntent.DRAFTING)
    think_param = True if (reasoning_enabled or intent == InferenceIntent.REASONING) else False

    if remaining_context <= 0 or remaining_context < MIN_EFFECTIVE_BUDGET:
        return BudgetResolution(
            intent=intent,
            verdict=BudgetVerdict.BUDGET_INFEASIBLE,
            effective_budget=0,
            think=think_param,
            allocated_context=safe_ctx,
            remaining_context=max(0, remaining_context),
            clamp_reason="CONTEXT_EXHAUSTED",
            calibrated_tps=tps_ewma,
            tts_eligible=tts_eligible,
        )

    # 1. Base requested or preset budget
    clamp_reason: Optional[str] = None
    verdict = BudgetVerdict.ALLOWED

    if preset == "custom" and requested_budget is not None and requested_budget > 0:
        base_budget = int(requested_budget)
    else:
        # Determine target latency: check intent first, then preset
        target_lat = TARGET_LATENCIES.get(intent, TARGET_LATENCIES.get(preset, 8.0))
        if tps_ewma > 0:
            base_budget = int(tps_ewma * target_lat)
            # Residency degradation: if layers spilled to CPU RAM, scale down
            if residency_ratio is not None and residency_ratio < 1.0:
                scale = max(0.25, min(1.0, float(residency_ratio)))
                base_budget = int(base_budget * scale)
                clamp_reason = "RESIDENCY_SPILL_CLAMP"
        else:
            if requested_budget is not None and requested_budget > 0:
                base_budget = int(requested_budget)
            elif intent == InferenceIntent.DRAFTING:
                base_budget = 2048
            elif intent == InferenceIntent.REASONING:
                base_budget = 1024
            else:
                base_budget = DEFAULT_FALLBACK_BUDGET

    # 2. Check context ceiling clamping
    if base_budget > remaining_context:
        effective_budget = remaining_context
        verdict = BudgetVerdict.CLAMPED_CONTEXT
        clamp_reason = "CONTEXT_PRESSURE_CLAMP"
    else:
        effective_budget = base_budget
        if clamp_reason is None and tps_ewma > 0 and preset != "custom" and requested_budget and base_budget < requested_budget:
            verdict = BudgetVerdict.CLAMPED_LATENCY
            clamp_reason = "LATENCY_TARGET_CLAMP"
        elif clamp_reason is None:
            verdict = BudgetVerdict.ALLOWED

    # Ensure minimal viable budget
    effective_budget = max(MIN_EFFECTIVE_BUDGET, effective_budget)
    # But strictly never exceed remaining_context
    effective_budget = min(effective_budget, remaining_context)

    return BudgetResolution(
        intent=intent,
        verdict=verdict,
        effective_budget=effective_budget,
        think=think_param,
        allocated_context=safe_ctx,
        remaining_context=remaining_context,
        clamp_reason=clamp_reason,
        calibrated_tps=tps_ewma,
        tts_eligible=tts_eligible,
    )
