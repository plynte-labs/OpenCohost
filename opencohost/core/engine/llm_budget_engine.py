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
# session-2107-fixes F3: reasoning allowance added on top of the answer budget when
# thinking is on and no per-model budget_tokens is configured (Auto). Hidden
# thinking tokens are spent from num_predict, so without it a thinking turn is
# cut off (done=length) before the answer starts.
DEFAULT_REASONING_BUDGET: int = 1024


class InferenceIntent(str, enum.Enum):
    CHAT = "chat"
    REASONING = "reasoning"
    DRAFTING = "drafting"
    AGENDA = "agenda"


class BudgetVerdict(str, enum.Enum):
    ALLOWED = "allowed"
    CLAMPED_LATENCY = "clamped_latency"
    CLAMPED_CONTEXT = "clamped_context"
    CLAMPED_MANUAL_CAP = "clamped_manual_cap"
    BUDGET_INFEASIBLE = "budget_infeasible"


# Target latencies in seconds per preset / intent
PRESET_TARGET_LATENCIES = {
    "fast": 2.0,
    "balanced": 4.0,
    "quality": 8.0,
}

INTENT_DEFAULT_LATENCY = {
    InferenceIntent.CHAT: 4.0,
    InferenceIntent.REASONING: 15.0,
    InferenceIntent.DRAFTING: 30.0,
    InferenceIntent.AGENDA: 20.0,
}

INTENT_TARGET_LATENCIES = {
    InferenceIntent.CHAT: {
        "fast": 2.0,
        "balanced": 4.0,
        "quality": 8.0,
    },
    InferenceIntent.REASONING: {
        "fast": 8.0,
        "balanced": 15.0,
        "quality": 30.0,
    },
    InferenceIntent.DRAFTING: {
        "fast": 15.0,
        "balanced": 30.0,
        "quality": 60.0,
    },
    InferenceIntent.AGENDA: {
        "fast": 10.0,
        "balanced": 20.0,
        "quality": 40.0,
    },
}

TARGET_LATENCIES = {
    "fast": 2.0,
    "balanced": 4.0,
    "quality": 8.0,
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
    prompt_tokens: int = 0


def resolve_generation_budget(
    *,
    intent: InferenceIntent = InferenceIntent.CHAT,
    allocated_context: int = 4096,
    prompt_tokens: int = 0,
    requested_budget: Optional[int] = None,
    preset: str = "balanced",
    reasoning_enabled: bool = False,
    tps_ewma: Optional[float] = 0.0,
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
    # Normalize intent robustly if string passed
    if not isinstance(intent, InferenceIntent):
        try:
            val = getattr(intent, "value", intent)
            intent = InferenceIntent(str(val).strip().lower())
        except (ValueError, TypeError, AttributeError):
            intent = InferenceIntent.CHAT

    safe_tps = float(tps_ewma or 0.0) if tps_ewma is not None else 0.0
    safe_tps = max(0.0, safe_tps)

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
            calibrated_tps=safe_tps,
            tts_eligible=tts_eligible,
            prompt_tokens=safe_prompt,
        )

    # 1. Base requested or preset budget
    clamp_reason: Optional[str] = None
    verdict = BudgetVerdict.ALLOWED

    if preset == "custom" and requested_budget is not None and requested_budget > 0:
        base_budget = int(requested_budget)
    else:
        # Determine target latency: check intent-specific preset, then general preset, then intent default
        clean_preset = str(preset or "balanced").strip().lower()
        intent_map = INTENT_TARGET_LATENCIES.get(intent)
        if intent_map and clean_preset in intent_map:
            target_lat = intent_map[clean_preset]
        elif clean_preset in PRESET_TARGET_LATENCIES:
            target_lat = PRESET_TARGET_LATENCIES[clean_preset]
        else:
            target_lat = INTENT_DEFAULT_LATENCY.get(intent, 4.0)
        if safe_tps > 0:
            base_budget = int(safe_tps * target_lat)
        else:
            # T2 (controls-cleanup-adaptive-reasoning-20260928): a manual
            # value is no longer substituted in directly here -- it is
            # applied uniformly as an upper-bound CAP below, alongside the
            # calibrated path, so a cap higher than the safe intent fallback
            # never lifts the budget above that fallback (min, not override).
            if intent == InferenceIntent.DRAFTING:
                base_budget = 2048
            elif intent == InferenceIntent.REASONING:
                base_budget = 1024
            else:
                base_budget = DEFAULT_FALLBACK_BUDGET

        # Residency degradation: if layers spilled to CPU RAM, scale down
        if residency_ratio is not None and residency_ratio < 1.0:
            scale = max(0.25, min(1.0, float(residency_ratio)))
            base_budget = int(base_budget * scale)
            clamp_reason = "RESIDENCY_SPILL_CLAMP"

        # T2 (controls-cleanup-adaptive-reasoning-20260928): the actual bug
        # fix. A manual `requested_budget` is now a REAL cap on the
        # adaptive/fallback estimate above, calibrated or not -- previously
        # it only ever won pre-calibration (and even then it replaced the
        # estimate rather than bounding it) and was silently ignored once
        # TPS calibrated (this whole branch only runs when preset !=
        # "custom", which is exactly the reported bug's condition). Auto
        # mode passes requested_budget=None and never reaches this branch.
        #
        # L2 (live-session-fixes-20261005): with thinking ON the per-model
        # budget is the REASONING allowance, spent on hidden tokens before the
        # answer starts. Capping the total with it let ~400 thinking tokens
        # starve a 512-token reply mid-sentence, so it is added to the answer
        # estimate instead. The context ceiling below still bounds the sum.
        #
        # F3 (session-2107-fixes): under Auto (no configured budget) the same
        # rule applies with DEFAULT_REASONING_BUDGET; before, the total was only
        # the latency-derived answer estimate (242 tokens live) and thinking
        # consumed all of it.
        if think_param and requested_budget is not None and requested_budget > 0:
            base_budget = base_budget + int(requested_budget)
        elif think_param:
            base_budget = base_budget + DEFAULT_REASONING_BUDGET
        elif (
            requested_budget is not None
            and requested_budget > 0
            and base_budget > requested_budget
        ):
            base_budget = int(requested_budget)
            clamp_reason = "MANUAL_CAP_CLAMP"
            verdict = BudgetVerdict.CLAMPED_MANUAL_CAP

    # 2. Check context ceiling clamping
    if base_budget > remaining_context:
        effective_budget = remaining_context
        verdict = BudgetVerdict.CLAMPED_CONTEXT
        clamp_reason = "CONTEXT_PRESSURE_CLAMP"
    else:
        effective_budget = base_budget
        if clamp_reason is None and safe_tps > 0 and preset != "custom" and requested_budget and base_budget < requested_budget:
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
        calibrated_tps=safe_tps,
        tts_eligible=tts_eligible,
        prompt_tokens=safe_prompt,
    )
