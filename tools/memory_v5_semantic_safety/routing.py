"""
Winner Selection, Terminal Routing & Lifecycle State Machine.

Module Responsibilities:
1. Multi-tier deterministic winner selection among QUALIFIED candidates.
2. Ineligibility enforcement for CRITICAL_REGRESSION and NON_EVALUABLE candidates.
3. Mutually exclusive & exhaustive decision bands (SAFETY_REFINEMENT_PROMISING, KEEP_LEXICAL, INCONCLUSIVE).
4. Frozen precedence resolution for terminal lifecycle routing.
5. Closed global invalid hierarchy resolution (PRIVACY > BASELINE > LOCKED > CAL > OVERLAP > CONFIG > HARNESS).
6. Strict boundary validation preventing incomplete, duplicate, non-finite, or malformed candidate states from routing.
"""

import math
from dataclasses import dataclass
from typing import List, Optional, Sequence, Set, Tuple

from tools.memory_v5_semantic_safety.models import (
    CandidateResult,
    MetricsObject,
)

GLOBAL_INVALID_PRECEDENCE: Tuple[str, ...] = (
    "PRIVACY_INVARIANT_VIOLATION",
    "BASELINE_INVALID",
    "LOCKED_FIXTURE_INVALID",
    "CALIBRATION_FIXTURE_INVALID",
    "FIXTURE_OVERLAP",
    "SHARED_CONFIG_INVALID",
    "HARNESS_INVALID",
)

CLOSED_GLOBAL_INVALID_REASONS: Set[str] = set(GLOBAL_INVALID_PRECEDENCE)

DECISION_BANDS: Tuple[str, ...] = (
    "SAFETY_REFINEMENT_PROMISING",
    "KEEP_LEXICAL",
    "INCONCLUSIVE",
)

TERMINAL_ROUTES: Tuple[str, ...] = (
    "SAFETY_REFINEMENT_PROMISING",
    "KEEP_LEXICAL",
    "INCONCLUSIVE",
    "GLOBAL_INVALID",
)


@dataclass(frozen=True)
class RoutingOutcome:
    """Immutable terminal routing result of the benchmark lifecycle."""
    terminal_route: str
    winner_variant_key: Optional[str]
    global_invalid_reason: Optional[str]
    decision_band: Optional[str]
    evaluable_candidates_count: int
    qualified_candidates_count: int


def resolve_global_invalid_reason(reasons: Sequence[str]) -> Optional[str]:
    """
    Resolve the highest-precedence canonical global invalid reason from a closed enumeration.
    Fails closed (ValueError) if any reason is not in the closed enumeration.
    """
    if not reasons:
        return None

    for r in reasons:
        if r not in CLOSED_GLOBAL_INVALID_REASONS:
            raise ValueError(f"Unknown global invalid reason '{r}' not in closed enumeration {GLOBAL_INVALID_PRECEDENCE}")

    for canonical in GLOBAL_INVALID_PRECEDENCE:
        if canonical in reasons:
            return canonical

    return None


def validate_candidate_routing_inputs(
    candidates: Sequence[Tuple[CandidateResult, Optional[MetricsObject]]],
) -> None:
    """
    Strictly validate the candidate/metrics pairs before selection or routing:
    - Unique variant_key across all candidates
    - NON_EVALUABLE: metrics MUST be None, quality_status MUST be NOT_APPLICABLE
    - EVALUABLE: metrics MUST be non-null, quality_status in (QUALIFIED, CRITICAL_REGRESSION)
    - QUALIFIED: execution_status == EVALUABLE, metrics non-null, retention_vs_b non-null finite, gain_over_a non-null finite
    - CRITICAL_REGRESSION: execution_status == EVALUABLE, metrics non-null
    """
    seen_variant_keys: Set[str] = set()

    for idx, (c_res, metrics) in enumerate(candidates):
        if c_res.variant_key in seen_variant_keys:
            raise ValueError(
                f"Duplicate candidate variant_key '{c_res.variant_key}' detected at index {idx}"
            )
        seen_variant_keys.add(c_res.variant_key)

        if c_res.execution_status == "NON_EVALUABLE":
            if metrics is not None:
                raise ValueError(
                    f"Candidate '{c_res.variant_key}' at index {idx} has execution_status=NON_EVALUABLE but must have metrics=None"
                )
            if c_res.quality_status != "NOT_APPLICABLE":
                raise ValueError(
                    f"Candidate '{c_res.variant_key}' at index {idx} with execution_status=NON_EVALUABLE must have quality_status='NOT_APPLICABLE', got '{c_res.quality_status}'"
                )
        elif c_res.execution_status == "EVALUABLE":
            if metrics is None:
                raise ValueError(
                    f"Candidate '{c_res.variant_key}' at index {idx} has execution_status=EVALUABLE but must have non-null metrics"
                )
            if c_res.quality_status not in ("QUALIFIED", "CRITICAL_REGRESSION"):
                raise ValueError(
                    f"Candidate '{c_res.variant_key}' at index {idx} with execution_status=EVALUABLE has invalid quality_status='{c_res.quality_status}'"
                )

            if c_res.quality_status == "QUALIFIED":
                if metrics.retention_vs_b is None or not math.isfinite(metrics.retention_vs_b):
                    raise ValueError(
                        f"Qualified candidate '{c_res.variant_key}' at index {idx} must have non-null finite retention_vs_b, got {metrics.retention_vs_b}"
                    )
                if metrics.gain_over_a is None or not math.isfinite(metrics.gain_over_a):
                    raise ValueError(
                        f"Qualified candidate '{c_res.variant_key}' at index {idx} must have non-null finite gain_over_a, got {metrics.gain_over_a}"
                    )
        else:
            raise ValueError(
                f"Candidate '{c_res.variant_key}' at index {idx} has invalid execution_status='{c_res.execution_status}'"
            )


def classify_decision_band(
    retention_vs_b: Optional[float],
    gain_over_a: Optional[float],
) -> str:
    """
    Classify a qualified candidate's performance into mutually exclusive decision bands:
    - KEEP_LEXICAL: retention < 0.25 OR gain < 0.20
    - SAFETY_REFINEMENT_PROMISING: retention >= 0.50 AND gain >= 0.35
    - INCONCLUSIVE: retention >= 0.25 AND gain >= 0.20 AND (retention < 0.50 OR gain < 0.35)
    """
    if retention_vs_b is None or not math.isfinite(retention_vs_b):
        raise ValueError(f"retention_vs_b must be a non-null finite float, got {retention_vs_b}")
    if gain_over_a is None or not math.isfinite(gain_over_a):
        raise ValueError(f"gain_over_a must be a non-null finite float, got {gain_over_a}")

    ret = retention_vs_b
    gain = gain_over_a

    if ret < 0.25 or gain < 0.20:
        return "KEEP_LEXICAL"
    if ret >= 0.50 and gain >= 0.35:
        return "SAFETY_REFINEMENT_PROMISING"
    return "INCONCLUSIVE"


def select_qualified_winner(
    candidates: Sequence[Tuple[CandidateResult, Optional[MetricsObject]]],
) -> Optional[str]:
    """
    Select winning variant key among QUALIFIED candidates using strict 4-tier tie-breaking:
    1. Highest retention_vs_b
    2. Highest mean Recall@3: (syn_recall_at_3 + para_recall_at_3) / 2
    3. Highest para_mrr_at_3
    4. Lexicographically smallest candidate key (ascending)
    """
    validate_candidate_routing_inputs(candidates)
    qualified_list: List[Tuple[Tuple[float, float, float, str], str]] = []

    for c_res, metrics in candidates:
        if c_res.execution_status == "EVALUABLE" and c_res.quality_status == "QUALIFIED" and metrics is not None:
            ret = metrics.retention_vs_b
            assert ret is not None and math.isfinite(ret)
            mean_r3 = (metrics.syn_recall_at_3 + metrics.para_recall_at_3) / 2.0
            para_mrr = metrics.para_mrr_at_3
            key = c_res.variant_key
            # Sort tuple: (-ret, -mean_r3, -para_mrr, key)
            sort_key = (-ret, -mean_r3, -para_mrr, key)
            qualified_list.append((sort_key, key))

    if not qualified_list:
        return None

    qualified_list.sort(key=lambda item: item[0])
    return qualified_list[0][1]


class LifecycleRoutingEngine:
    """Evaluates lifecycle outcome and terminal routing with strict precedence."""

    def route_lifecycle(
        self,
        candidates: Sequence[Tuple[CandidateResult, Optional[MetricsObject]]],
        global_invalid_reasons: Optional[Sequence[str]] = None,
    ) -> RoutingOutcome:
        """
        Execute terminal route precedence:
        1. global_invalid_reason != None -> GLOBAL_INVALID, winner = None
        2. zero EVALUABLE candidates -> INCONCLUSIVE, winner = None
        3. zero QUALIFIED candidates -> KEEP_LEXICAL, winner = None
        4. best QUALIFIED in promising band -> SAFETY_REFINEMENT_PROMISING, winner = best
        5. best QUALIFIED in middle band -> INCONCLUSIVE, winner = best
        6. best QUALIFIED in keep band -> KEEP_LEXICAL, winner = best
        """
        validate_candidate_routing_inputs(candidates)
        global_reason = resolve_global_invalid_reason(global_invalid_reasons or ())

        evaluable_count = sum(1 for c, _ in candidates if c.execution_status == "EVALUABLE")
        qualified_count = sum(
            1 for c, m in candidates
            if c.execution_status == "EVALUABLE" and c.quality_status == "QUALIFIED" and m is not None
        )

        # Precedence 1: Global invalid reasons
        if global_reason is not None:
            return RoutingOutcome(
                terminal_route="GLOBAL_INVALID",
                winner_variant_key=None,
                global_invalid_reason=global_reason,
                decision_band=None,
                evaluable_candidates_count=evaluable_count,
                qualified_candidates_count=qualified_count,
            )

        # Precedence 2: Zero evaluable candidates
        if evaluable_count == 0:
            return RoutingOutcome(
                terminal_route="INCONCLUSIVE",
                winner_variant_key=None,
                global_invalid_reason=None,
                decision_band=None,
                evaluable_candidates_count=0,
                qualified_candidates_count=0,
            )

        # Precedence 3: Zero qualified candidates (all failed safety)
        if qualified_count == 0:
            return RoutingOutcome(
                terminal_route="KEEP_LEXICAL",
                winner_variant_key=None,
                global_invalid_reason=None,
                decision_band=None,
                evaluable_candidates_count=evaluable_count,
                qualified_candidates_count=0,
            )

        # Select Winner among qualified candidates
        winner_key = select_qualified_winner(candidates)
        winner_metrics: Optional[MetricsObject] = None
        for c, m in candidates:
            if c.variant_key == winner_key and m is not None:
                winner_metrics = m
                break

        assert winner_metrics is not None
        band = classify_decision_band(
            winner_metrics.retention_vs_b,
            winner_metrics.gain_over_a,
        )

        # Precedence 4, 5, 6: Route corresponds to winner's decision band
        return RoutingOutcome(
            terminal_route=band,
            winner_variant_key=winner_key,
            global_invalid_reason=None,
            decision_band=band,
            evaluable_candidates_count=evaluable_count,
            qualified_candidates_count=qualified_count,
        )
