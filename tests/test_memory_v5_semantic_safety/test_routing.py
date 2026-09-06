"""
Unit and integration tests for WU6: Winner Selection, Terminal Routing & Lifecycle State Machine.

Validates:
1. Winner selection among QUALIFIED candidates using 4-tier tie-breaking:
   - highest retention_vs_b
   - highest mean Recall@3: (syn_recall_at_3 + para_recall_at_3) / 2
   - highest para_mrr_at_3
   - lexicographical variant_key
2. Disqualification of CRITICAL_REGRESSION and NON_EVALUABLE candidates.
3. Mutually exclusive and exhaustive decision bands (SAFETY_REFINEMENT_PROMISING, KEEP_LEXICAL, INCONCLUSIVE) with boundary tests.
4. Exact terminal route precedence (global_invalid > zero evaluable > zero qualified > promising > inconclusive > keep).
5. Closed global reason precedence hierarchy and strict fail-closed rejection of unknown global reasons.
6. Fail-closed structural validation of candidate inputs (rejection of duplicate keys, null metrics on EVALUABLE, NaN/Inf on QUALIFIED, metrics on NON_EVALUABLE).
7. Mandatory real WU5 regression scenario: deriving winner="H2_LEXICAL_CORROBORATION" and route="KEEP_LEXICAL".
8. Determinism, edge-case null handling, and lack of ordering bias.
"""

import math
from dataclasses import dataclass
from typing import Dict, Optional, Tuple
import pytest

from tools.memory_v5_semantic_safety.models import (
    CandidateResult,
    MetricsObject,
)
from tools.memory_v5_semantic_safety.routing import (
    GLOBAL_INVALID_PRECEDENCE,
    DECISION_BANDS,
    TERMINAL_ROUTES,
    LifecycleRoutingEngine,
    RoutingOutcome,
    classify_decision_band,
    resolve_global_invalid_reason,
    select_qualified_winner,
    validate_candidate_routing_inputs,
)


def _make_candidate(
    variant_key: str,
    execution_status: str = "EVALUABLE",
    execution_reason: Optional[str] = None,
    quality_status: str = "QUALIFIED",
    critical_reasons: Tuple[str, ...] = (),
    retention: Optional[float] = 0.60,
    gain: Optional[float] = 0.40,
    syn_r3: float = 1.0,
    para_r3: float = 1.0,
    para_mrr: float = 0.80,
) -> Tuple[CandidateResult, Optional[MetricsObject]]:
    c_res = CandidateResult(
        variant_key=variant_key,
        execution_status=execution_status,
        execution_reason=execution_reason,
        quality_status=quality_status,
        critical_reasons=critical_reasons,
        calibration=None,
    )
    if execution_status != "EVALUABLE":
        return c_res, None

    metrics = MetricsObject(
        hard_correct_at_1_count=16, hard_correct_at_3_count=16, hard_recall_at_1=1.0, hard_recall_at_3=1.0, hard_mrr_at_3=1.0,
        syn_correct_at_1_count=14, syn_correct_at_3_count=14, syn_recall_at_1=1.0, syn_recall_at_3=syn_r3, syn_mrr_at_3=1.0,
        para_correct_at_1_count=16, para_correct_at_3_count=16, para_recall_at_1=1.0, para_recall_at_3=para_r3, para_mrr_at_3=para_mrr,
        semantic_quality_score=0.80,
        no_memory_abstention_count=12, no_memory_false_injection_count=0 if quality_status == "QUALIFIED" else 1, no_memory_abstention_accuracy=1.0,
        near_wrong_correct_at_1_count=7, near_wrong_precision_at_1=7/12, near_wrong_target_mrr_at_3=7/12,
        profile_isolation_pass_count=5, profile_privacy_leakage_count=0, profile_private_leakage_count=0, profile_inactive_leakage_count=0, profile_cross_profile_leakage_count=0,
        stale_correct_count=5, stale_correct_rate=1.0,
        retention_vs_b=retention,
        gain_over_a=gain if gain is not None else 0.0,
    )
    return c_res, metrics


def test_decision_band_boundaries_and_exclusivity():
    """Task 6.1: Verify mutually exclusive and exhaustive decision bands at exact boundaries."""
    # Promising band: retention >= 0.50 and gain >= 0.35
    assert classify_decision_band(0.50, 0.35) == "SAFETY_REFINEMENT_PROMISING"
    assert classify_decision_band(0.60, 0.40) == "SAFETY_REFINEMENT_PROMISING"
    assert classify_decision_band(0.499999, 0.35) == "INCONCLUSIVE"
    assert classify_decision_band(0.50, 0.349999) == "INCONCLUSIVE"

    # Keep band: retention < 0.25 or gain < 0.20
    assert classify_decision_band(0.249999, 0.50) == "KEEP_LEXICAL"
    assert classify_decision_band(0.60, 0.199999) == "KEEP_LEXICAL"
    assert classify_decision_band(0.249999, 0.199999) == "KEEP_LEXICAL"
    assert classify_decision_band(0.093085, 0.078125) == "KEEP_LEXICAL"

    # Inconclusive band: retention in [0.25, 0.50) or gain in [0.20, 0.35)
    assert classify_decision_band(0.25, 0.20) == "INCONCLUSIVE"
    assert classify_decision_band(0.499999, 0.40) == "INCONCLUSIVE"
    assert classify_decision_band(0.60, 0.20) == "INCONCLUSIVE"
    assert classify_decision_band(0.30, 0.30) == "INCONCLUSIVE"


def test_winner_selection_tie_breaking_and_ineligibility():
    """Task 6.2: Test 4-tier winner ordering among QUALIFIED and ineligibility of CRITICAL/NON_EVALUABLE."""
    # 1. Higher retention wins
    c1, m1 = _make_candidate("H1", retention=0.55, gain=0.36)
    c2, m2 = _make_candidate("H2", retention=0.56, gain=0.36)
    assert select_qualified_winner([(c1, m1), (c2, m2)]) == "H2"

    # 2. Tie on retention -> Mean Recall@3 wins
    c1, m1 = _make_candidate("H1", retention=0.55, syn_r3=0.8, para_r3=0.8)  # mean 0.8
    c2, m2 = _make_candidate("H2", retention=0.55, syn_r3=0.9, para_r3=0.9)  # mean 0.9
    assert select_qualified_winner([(c1, m1), (c2, m2)]) == "H2"

    # 3. Tie on retention and Mean Recall@3 -> Para MRR@3 wins
    c1, m1 = _make_candidate("H1", retention=0.55, syn_r3=0.9, para_r3=0.9, para_mrr=0.75)
    c2, m2 = _make_candidate("H2", retention=0.55, syn_r3=0.9, para_r3=0.9, para_mrr=0.85)
    assert select_qualified_winner([(c1, m1), (c2, m2)]) == "H2"

    # 4. Tie on all metrics -> Lexicographical key wins (H1 < H2)
    c1, m1 = _make_candidate("H1", retention=0.55, syn_r3=0.9, para_r3=0.9, para_mrr=0.80)
    c2, m2 = _make_candidate("H2", retention=0.55, syn_r3=0.9, para_r3=0.9, para_mrr=0.80)
    assert select_qualified_winner([(c1, m1), (c2, m2)]) == "H1"
    assert select_qualified_winner([(c2, m2), (c1, m1)]) == "H1"  # Permutation invariant

    # 5. Ineligibility of CRITICAL_REGRESSION and NON_EVALUABLE
    c_crit, m_crit = _make_candidate("H_CRIT", quality_status="CRITICAL_REGRESSION", critical_reasons=("NO_MEMORY_INJECTION",), retention=0.99, gain=0.99)
    c_non, m_non = _make_candidate("H_NON", execution_status="NON_EVALUABLE", execution_reason="CALIBRATION_FAILED", quality_status="NOT_APPLICABLE")
    c_qual, m_qual = _make_candidate("H_QUAL", quality_status="QUALIFIED", retention=0.10, gain=0.08)

    assert select_qualified_winner([(c_crit, m_crit), (c_non, m_non), (c_qual, m_qual)]) == "H_QUAL"
    assert select_qualified_winner([(c_crit, m_crit), (c_non, m_non)]) is None


def test_global_invalid_precedence_and_closed_enumeration():
    """Task 6.3: Resolve highest-priority global invalid reason and reject unknown global reasons."""
    # Precedence resolution among canonical reasons
    assert resolve_global_invalid_reason(["FIXTURE_OVERLAP", "PRIVACY_INVARIANT_VIOLATION"]) == "PRIVACY_INVARIANT_VIOLATION"
    assert resolve_global_invalid_reason(["SHARED_CONFIG_INVALID", "BASELINE_INVALID"]) == "BASELINE_INVALID"
    assert resolve_global_invalid_reason(["HARNESS_INVALID", "CALIBRATION_FIXTURE_INVALID"]) == "CALIBRATION_FIXTURE_INVALID"
    assert resolve_global_invalid_reason(["LOCKED_FIXTURE_INVALID", "FIXTURE_OVERLAP"]) == "LOCKED_FIXTURE_INVALID"
    assert resolve_global_invalid_reason([]) is None

    # Fail closed on unknown global invalid reasons
    with pytest.raises(ValueError, match="Unknown global invalid reason"):
        resolve_global_invalid_reason(["WHATEVER_BROKE"])

    with pytest.raises(ValueError, match="Unknown global invalid reason"):
        resolve_global_invalid_reason(["PRIVACY_INVARIANT_VIOLATION", "UNKNOWN_REASON"])


def test_candidate_input_shape_fail_closed_validation():
    """Task 6.4: Validate that impossible candidate/metrics shapes fail closed."""
    c_valid, m_valid = _make_candidate("H_VALID", retention=0.50, gain=0.35)
    validate_candidate_routing_inputs([(c_valid, m_valid)])  # Clean

    # 1. EVALUABLE with metrics = None
    c_no_m = CandidateResult("H_ERR", "EVALUABLE", None, "QUALIFIED", ())
    with pytest.raises(ValueError, match="must have non-null metrics"):
        validate_candidate_routing_inputs([(c_no_m, None)])

    # 2. NON_EVALUABLE with metrics present
    c_non = CandidateResult("H_NON", "NON_EVALUABLE", "CALIBRATION_FAILED", "NOT_APPLICABLE", ())
    with pytest.raises(ValueError, match="must have metrics=None"):
        validate_candidate_routing_inputs([(c_non, m_valid)])

    # 3. NON_EVALUABLE with quality_status != NOT_APPLICABLE
    c_non_bad_q = CandidateResult("H_NON_BAD", "NON_EVALUABLE", "CALIBRATION_FAILED", "QUALIFIED", ())
    with pytest.raises(ValueError, match="must have quality_status='NOT_APPLICABLE'"):
        validate_candidate_routing_inputs([(c_non_bad_q, None)])

    # 4. Duplicate variant_key
    c_dup1, m_dup1 = _make_candidate("H_DUP", retention=0.50, gain=0.35)
    c_dup2, m_dup2 = _make_candidate("H_DUP", retention=0.55, gain=0.36)
    with pytest.raises(ValueError, match="Duplicate candidate variant_key"):
        validate_candidate_routing_inputs([(c_dup1, m_dup1), (c_dup2, m_dup2)])

    # 5. QUALIFIED with retention_vs_b = None
    _, m_no_ret = _make_candidate("H_NO_RET", retention=None, gain=0.40)
    c_qual = CandidateResult("H_NO_RET", "EVALUABLE", None, "QUALIFIED", ())
    with pytest.raises(ValueError, match="must have non-null finite retention_vs_b"):
        validate_candidate_routing_inputs([(c_qual, m_no_ret)])

    # 6. QUALIFIED with retention_vs_b = NaN or Inf
    _, m_nan_ret = _make_candidate("H_NAN_RET", retention=float("nan"), gain=0.40)
    with pytest.raises(ValueError, match="finite"):
        validate_candidate_routing_inputs([(c_qual, m_nan_ret)])

    _, m_inf_ret = _make_candidate("H_INF_RET", retention=float("inf"), gain=0.40)
    with pytest.raises(ValueError, match="finite"):
        validate_candidate_routing_inputs([(c_qual, m_inf_ret)])

    # 7. QUALIFIED with gain_over_a = NaN
    _, m_nan_gain = _make_candidate("H_NAN_GAIN", retention=0.50, gain=float("nan"))
    with pytest.raises(ValueError, match="finite"):
        validate_candidate_routing_inputs([(c_qual, m_nan_gain)])


def test_lifecycle_terminal_route_precedence():
    """Task 6.5: Test full precedence order of terminal routing."""
    engine = LifecycleRoutingEngine()

    c_prom, m_prom = _make_candidate("H_PROM", retention=0.60, gain=0.40)
    c_incon, m_incon = _make_candidate("H_INCON", retention=0.30, gain=0.25)
    c_keep, m_keep = _make_candidate("H_KEEP", retention=0.10, gain=0.08)
    c_crit, m_crit = _make_candidate("H_CRIT", quality_status="CRITICAL_REGRESSION", critical_reasons=("NO_MEMORY_INJECTION",), retention=0.95, gain=0.80)
    c_non, m_non = _make_candidate("H_NON", execution_status="NON_EVALUABLE", execution_reason="CALIBRATION_FAILED", quality_status="NOT_APPLICABLE")

    # 1. Global invalid reason present -> GLOBAL_INVALID, winner=None
    out = engine.route_lifecycle([(c_prom, m_prom)], global_invalid_reasons=["BASELINE_INVALID"])
    assert out.terminal_route == "GLOBAL_INVALID"
    assert out.winner_variant_key is None
    assert out.global_invalid_reason == "BASELINE_INVALID"
    assert out.evaluable_candidates_count == 1
    assert out.qualified_candidates_count == 1

    # 2. Zero EVALUABLE candidates -> INCONCLUSIVE, winner=None
    out = engine.route_lifecycle([(c_non, m_non)])
    assert out.terminal_route == "INCONCLUSIVE"
    assert out.winner_variant_key is None
    assert out.evaluable_candidates_count == 0
    assert out.qualified_candidates_count == 0

    # 3. Zero QUALIFIED candidates (all critical) -> KEEP_LEXICAL, winner=None
    out = engine.route_lifecycle([(c_crit, m_crit)])
    assert out.terminal_route == "KEEP_LEXICAL"
    assert out.winner_variant_key is None
    assert out.evaluable_candidates_count == 1
    assert out.qualified_candidates_count == 0

    # 4. Best QUALIFIED in Promising Band -> SAFETY_REFINEMENT_PROMISING, winner=best
    out = engine.route_lifecycle([(c_prom, m_prom), (c_keep, m_keep)])
    assert out.terminal_route == "SAFETY_REFINEMENT_PROMISING"
    assert out.winner_variant_key == "H_PROM"
    assert out.evaluable_candidates_count == 2
    assert out.qualified_candidates_count == 2

    # 5. Best QUALIFIED in Inconclusive Band -> INCONCLUSIVE, winner=best
    out = engine.route_lifecycle([(c_incon, m_incon), (c_keep, m_keep)])
    assert out.terminal_route == "INCONCLUSIVE"
    assert out.winner_variant_key == "H_INCON"

    # 6. Best QUALIFIED in Keep Band -> KEEP_LEXICAL, winner=best
    out = engine.route_lifecycle([(c_keep, m_keep)])
    assert out.terminal_route == "KEEP_LEXICAL"
    assert out.winner_variant_key == "H_KEEP"


def test_mandatory_real_wu5_scenario_derivation():
    """
    Task 6.6: Authoritative test on real WU5 golden outputs:
    - H1: CRITICAL_REGRESSION (retention=0.9388, gain=0.7879)
    - H2: QUALIFIED (retention=0.093085, gain=0.078125)
    - H4: CRITICAL_REGRESSION (retention=0.9761, gain=0.8192)
    Must deterministically derive:
    - winner = "H2_LEXICAL_CORROBORATION"
    - terminal_route = "KEEP_LEXICAL"
    """
    engine = LifecycleRoutingEngine()

    c_h1, m_h1 = _make_candidate(
        "H1_MARGIN",
        quality_status="CRITICAL_REGRESSION",
        critical_reasons=("NO_MEMORY_INJECTION",),
        retention=0.9388297872340425,
        gain=0.7879464285714286,
    )
    c_h2, m_h2 = _make_candidate(
        "H2_LEXICAL_CORROBORATION",
        quality_status="QUALIFIED",
        critical_reasons=(),
        retention=0.09308510638297872,
        gain=0.078125,
    )
    c_h4, m_h4 = _make_candidate(
        "H4_ASYMMETRIC_GATE",
        quality_status="CRITICAL_REGRESSION",
        critical_reasons=("NO_MEMORY_INJECTION",),
        retention=0.976063829787234,
        gain=0.8191964285714286,
    )

    out = engine.route_lifecycle([(c_h1, m_h1), (c_h2, m_h2), (c_h4, m_h4)])

    assert out.winner_variant_key == "H2_LEXICAL_CORROBORATION"
    assert out.terminal_route == "KEEP_LEXICAL"
    assert out.global_invalid_reason is None
    assert out.decision_band == "KEEP_LEXICAL"
    assert out.evaluable_candidates_count == 3
    assert out.qualified_candidates_count == 1
