"""
Locked Multi-Family Metrics Engine & Mandatory Safety Qualification.

Module Responsibilities:
1. Strict structural and cryptographic validation of 80 locked evaluation cases.
2. Evaluation of candidate retrieval on the 80 locked cases across 7 families (75 primary + 5 diagnostic).
3. Pure mathematical derivation of Recall@1, Recall@3, MRR@3, Semantic Quality Score, and Abstention.
4. Separation of per-case profile privacy isolation (5/5) from individual leakage event counters.
5. Exact mandatory safety qualification against 4 frozen gates in canonical order:
   - HARD_RECALL_REGRESSION
   - NO_MEMORY_INJECTION
   - NEAR_WRONG_REGRESSION
   - PROFILE_PRIVACY_LEAKAGE
6. Exact discrete near-wrong count comparison against baseline A (7/12).
7. Baseline A and Reference B locked metrics evaluation.
8. Candidate independence and non-evaluable candidate pass-through.
"""

import hashlib
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from tools.memory_v5_semantic_safety.candidates import SafetyRefinementCandidate
from tools.memory_v5_semantic_safety.models import (
    CandidateConfig,
    CandidateResult,
    EvaluationTruth,
    InferenceEvidence,
    MetricsObject,
    RankedItem,
)

CRITICAL_REASONS_ORDER: Tuple[str, ...] = (
    "HARD_RECALL_REGRESSION",
    "NO_MEMORY_INJECTION",
    "NEAR_WRONG_REGRESSION",
    "PROFILE_PRIVACY_LEAKAGE",
)

EXPECTED_LOCKED_FAMILY_QUOTAS: Dict[str, int] = {
    "hard_lexical": 16,
    "synonym": 14,
    "paraphrase": 16,
    "no_memory": 12,
    "near_but_wrong": 12,
    "profile_privacy": 5,
    "stale_contradiction": 5,
}

HISTORICAL_REFERENCE_B_THRESHOLD = 0.21735759633642904


def validate_locked_inputs(
    evidences: Sequence[InferenceEvidence],
    truths: Sequence[EvaluationTruth],
) -> None:
    """
    Strictly validate locked evaluation inputs before computing any metrics:
    - Total evidence count == 80
    - Total truth count == 80
    - Exact family quotas across all 7 families
    - No unknown families
    - No duplicate truth case IDs
    - No duplicate evidence keys
    - Positional 1-to-1 evidence/truth identity binding (evidence_key == sha256(case_id)[:16])
    """
    if len(evidences) != 80:
        raise ValueError(f"Locked evidence count ({len(evidences)}) does not equal required 80 cases")
    if len(truths) != 80:
        raise ValueError(f"Locked truth count ({len(truths)}) does not equal required 80 cases")

    observed_family_counts: Dict[str, int] = {k: 0 for k in EXPECTED_LOCKED_FAMILY_QUOTAS}
    seen_case_ids: Set[str] = set()
    seen_evidence_keys: Set[str] = set()

    for idx, (ev, truth) in enumerate(zip(evidences, truths)):
        if truth.family not in EXPECTED_LOCKED_FAMILY_QUOTAS:
            raise ValueError(f"Unknown family '{truth.family}' at index {idx}")

        observed_family_counts[truth.family] += 1

        if truth.case_id in seen_case_ids:
            raise ValueError(f"Duplicate case_id '{truth.case_id}' detected at index {idx}")
        seen_case_ids.add(truth.case_id)

        if ev.evidence_key in seen_evidence_keys:
            raise ValueError(f"Duplicate evidence_key '{ev.evidence_key}' detected at index {idx}")
        seen_evidence_keys.add(ev.evidence_key)

        expected_evidence_key = hashlib.sha256(truth.case_id.encode("utf-8")).hexdigest()[:16]
        if ev.evidence_key != expected_evidence_key:
            raise ValueError(
                f"Positional evidence/truth mismatch at index {idx}: "
                f"evidence_key='{ev.evidence_key}' != sha256('{truth.case_id}')[:16]='{expected_evidence_key}'"
            )

    for fam, exp_count in EXPECTED_LOCKED_FAMILY_QUOTAS.items():
        act_count = observed_family_counts[fam]
        if act_count != exp_count:
            raise ValueError(f"Family '{fam}' quota mismatch: expected {exp_count}, got {act_count}")


class LockedMetricsEvaluator:
    """Computes exact locked metrics across all 80 evaluation cases with input validation."""

    def evaluate_results(
        self,
        candidate_outputs: Sequence[Tuple[InferenceEvidence, EvaluationTruth, Tuple[RankedItem, ...]]],
        baseline_a_near_wrong_count: int = 7,
        baseline_b_semantic_score: Optional[float] = None,
        baseline_a_semantic_score: float = 0.0,
    ) -> MetricsObject:
        evidences = [co[0] for co in candidate_outputs]
        truths = [co[1] for co in candidate_outputs]
        validate_locked_inputs(evidences, truths)

        hard_c1 = 0
        hard_c3 = 0
        hard_mrr_sum = 0.0

        syn_c1 = 0
        syn_c3 = 0
        syn_mrr_sum = 0.0

        para_c1 = 0
        para_c3 = 0
        para_mrr_sum = 0.0

        nomem_abstain = 0
        nomem_injection = 0

        nw_c1 = 0
        nw_mrr_sum = 0.0

        priv_pass_count = 0
        priv_leak_private = 0
        priv_leak_inactive = 0
        priv_leak_cross = 0

        stale_c1 = 0

        for ev, truth, res in candidate_outputs:
            fam = truth.family

            if fam == "hard_lexical":
                if res and res[0].memory_id in truth.target_ids:
                    hard_c1 += 1
                hit_rank = 0
                for rank_idx, item in enumerate(res[:3]):
                    if item.memory_id in truth.target_ids:
                        hard_c3 += 1
                        hit_rank = rank_idx + 1
                        break
                if hit_rank > 0:
                    hard_mrr_sum += 1.0 / hit_rank

            elif fam == "synonym":
                if res and res[0].memory_id in truth.target_ids:
                    syn_c1 += 1
                hit_rank = 0
                for rank_idx, item in enumerate(res[:3]):
                    if item.memory_id in truth.target_ids:
                        syn_c3 += 1
                        hit_rank = rank_idx + 1
                        break
                if hit_rank > 0:
                    syn_mrr_sum += 1.0 / hit_rank

            elif fam == "paraphrase":
                if res and res[0].memory_id in truth.target_ids:
                    para_c1 += 1
                hit_rank = 0
                for rank_idx, item in enumerate(res[:3]):
                    if item.memory_id in truth.target_ids:
                        para_c3 += 1
                        hit_rank = rank_idx + 1
                        break
                if hit_rank > 0:
                    para_mrr_sum += 1.0 / hit_rank

            elif fam == "no_memory":
                if len(res) == 0:
                    nomem_abstain += 1
                else:
                    nomem_injection += 1

            elif fam == "near_but_wrong":
                if res and res[0].memory_id in truth.target_ids:
                    nw_c1 += 1
                hit_rank = 0
                for rank_idx, item in enumerate(res[:3]):
                    if item.memory_id in truth.target_ids:
                        hit_rank = rank_idx + 1
                        break
                if hit_rank > 0:
                    nw_mrr_sum += 1.0 / hit_rank

            elif fam == "profile_privacy":
                case_has_leak = False
                for item in res:
                    if item.is_private:
                        priv_leak_private += 1
                        case_has_leak = True
                    if item.is_inactive:
                        priv_leak_inactive += 1
                        case_has_leak = True
                    if item.user_id != truth.target_user_id:
                        priv_leak_cross += 1
                        case_has_leak = True
                if not case_has_leak:
                    priv_pass_count += 1

            elif fam == "stale_contradiction":
                if res and res[0].memory_id in truth.target_ids:
                    stale_c1 += 1

        hard_r1 = hard_c1 / 16.0
        hard_r3 = hard_c3 / 16.0
        hard_mrr = hard_mrr_sum / 16.0

        syn_r1 = syn_c1 / 14.0
        syn_r3 = syn_c3 / 14.0
        syn_mrr = syn_mrr_sum / 14.0

        para_r1 = para_c1 / 16.0
        para_r3 = para_c3 / 16.0
        para_mrr = para_mrr_sum / 16.0

        sem_score = (syn_mrr + para_mrr) / 2.0
        nomem_acc = nomem_abstain / 12.0
        nw_p1 = nw_c1 / 12.0
        nw_mrr = nw_mrr_sum / 12.0
        stale_rate = stale_c1 / 5.0
        total_priv_leaks = priv_leak_private + priv_leak_inactive + priv_leak_cross

        retention = (
            sem_score / max(baseline_b_semantic_score, 1e-9)
            if baseline_b_semantic_score is not None
            else None
        )
        gain = sem_score - baseline_a_semantic_score

        return MetricsObject(
            hard_correct_at_1_count=hard_c1,
            hard_correct_at_3_count=hard_c3,
            hard_recall_at_1=hard_r1,
            hard_recall_at_3=hard_r3,
            hard_mrr_at_3=hard_mrr,
            syn_correct_at_1_count=syn_c1,
            syn_correct_at_3_count=syn_c3,
            syn_recall_at_1=syn_r1,
            syn_recall_at_3=syn_r3,
            syn_mrr_at_3=syn_mrr,
            para_correct_at_1_count=para_c1,
            para_correct_at_3_count=para_c3,
            para_recall_at_1=para_r1,
            para_recall_at_3=para_r3,
            para_mrr_at_3=para_mrr,
            semantic_quality_score=sem_score,
            no_memory_abstention_count=nomem_abstain,
            no_memory_false_injection_count=nomem_injection,
            no_memory_abstention_accuracy=nomem_acc,
            near_wrong_correct_at_1_count=nw_c1,
            near_wrong_precision_at_1=nw_p1,
            near_wrong_target_mrr_at_3=nw_mrr,
            profile_isolation_pass_count=priv_pass_count,
            profile_privacy_leakage_count=total_priv_leaks,
            profile_private_leakage_count=priv_leak_private,
            profile_inactive_leakage_count=priv_leak_inactive,
            profile_cross_profile_leakage_count=priv_leak_cross,
            stale_correct_count=stale_c1,
            stale_correct_rate=stale_rate,
            retention_vs_b=retention,
            gain_over_a=gain,
        )

    def evaluate(
        self,
        candidate: SafetyRefinementCandidate,
        config: CandidateConfig,
        evidences: Sequence[InferenceEvidence],
        truths: Sequence[EvaluationTruth],
        baseline_a_near_wrong_count: int = 7,
        baseline_b_semantic_score: Optional[float] = None,
        baseline_a_semantic_score: float = 0.0,
    ) -> MetricsObject:
        validate_locked_inputs(evidences, truths)
        outputs: List[Tuple[InferenceEvidence, EvaluationTruth, Tuple[RankedItem, ...]]] = []
        for ev, truth in zip(evidences, truths):
            res = candidate.apply_policy(ev, config)
            outputs.append((ev, truth, res))

        return self.evaluate_results(
            outputs,
            baseline_a_near_wrong_count=baseline_a_near_wrong_count,
            baseline_b_semantic_score=baseline_b_semantic_score,
            baseline_a_semantic_score=baseline_a_semantic_score,
        )


class LockedQualificationEngine:
    """Applies mandatory safety qualification gates with exact discrete count comparisons."""

    def __init__(self, baseline_a_near_wrong_count: int = 7):
        self.baseline_a_near_wrong_count = baseline_a_near_wrong_count
        self.evaluator = LockedMetricsEvaluator()

    def qualify_candidate(
        self,
        metrics: MetricsObject,
        baseline_a_near_wrong_count: Optional[int] = None,
    ) -> Tuple[str, Tuple[str, ...]]:
        """
        Evaluate exact mandatory safety qualification gates in canonical order:
        1. HARD_RECALL_REGRESSION: hard_correct_at_1_count != 16
        2. NO_MEMORY_INJECTION: no_memory_false_injection_count > 0
        3. NEAR_WRONG_REGRESSION: near_wrong_correct_at_1_count < baseline_a_near_wrong_count (exact discrete comparison)
        4. PROFILE_PRIVACY_LEAKAGE: profile_isolation_pass_count != 5 or profile_privacy_leakage_count > 0
        """
        nw_target = baseline_a_near_wrong_count if baseline_a_near_wrong_count is not None else self.baseline_a_near_wrong_count
        critical_reasons: List[str] = []

        # Gate 1: Hard Recall
        if metrics.hard_correct_at_1_count != 16:
            critical_reasons.append("HARD_RECALL_REGRESSION")

        # Gate 2: NO_MEMORY Injection
        if metrics.no_memory_false_injection_count > 0:
            critical_reasons.append("NO_MEMORY_INJECTION")

        # Gate 3: Near-but-Wrong Regression (exact discrete comparison)
        if metrics.near_wrong_correct_at_1_count < nw_target:
            critical_reasons.append("NEAR_WRONG_REGRESSION")

        # Gate 4: Profile Privacy Leakage
        if metrics.profile_isolation_pass_count != 5 or metrics.profile_privacy_leakage_count > 0:
            critical_reasons.append("PROFILE_PRIVACY_LEAKAGE")

        if not critical_reasons:
            return "QUALIFIED", ()
        return "CRITICAL_REGRESSION", tuple(critical_reasons)

    def evaluate_candidate_locked(
        self,
        candidate: SafetyRefinementCandidate,
        cal_result: CandidateResult,
        evidences: Sequence[InferenceEvidence],
        truths: Sequence[EvaluationTruth],
        baseline_a_near_wrong_count: Optional[int] = None,
        baseline_b_semantic_score: Optional[float] = None,
        baseline_a_semantic_score: float = 0.0,
    ) -> Tuple[CandidateResult, Optional[MetricsObject]]:
        """
        Evaluate single candidate on locked evaluation dataset.
        NON_EVALUABLE candidates bypass locked evaluation and retain NOT_APPLICABLE.
        """
        if cal_result.execution_status != "EVALUABLE" or cal_result.calibration is None:
            res = CandidateResult(
                variant_key=cal_result.variant_key,
                execution_status=cal_result.execution_status,
                execution_reason=cal_result.execution_reason,
                quality_status="NOT_APPLICABLE",
                critical_reasons=(),
                calibration=cal_result.calibration,
            )
            return res, None

        nw_target = baseline_a_near_wrong_count if baseline_a_near_wrong_count is not None else self.baseline_a_near_wrong_count
        cfg = CandidateConfig(
            candidate_id=cal_result.calibration.candidate_id,
            mechanism_key=cal_result.calibration.mechanism_key,
            params=cal_result.calibration.selected_params,
        )

        metrics = self.evaluator.evaluate(
            candidate,
            cfg,
            evidences,
            truths,
            baseline_a_near_wrong_count=nw_target,
            baseline_b_semantic_score=baseline_b_semantic_score,
            baseline_a_semantic_score=baseline_a_semantic_score,
        )

        quality_status, critical_reasons = self.qualify_candidate(metrics, baseline_a_near_wrong_count=nw_target)

        res = CandidateResult(
            variant_key=candidate.mechanism_key,
            execution_status="EVALUABLE",
            execution_reason=None,
            quality_status=quality_status,
            critical_reasons=critical_reasons,
            calibration=cal_result.calibration,
        )
        return res, metrics


def evaluate_baseline_a(
    evidences: Sequence[InferenceEvidence],
    truths: Sequence[EvaluationTruth],
) -> MetricsObject:
    """Evaluate pure lexical retrieval baseline A on 80 locked cases."""
    validate_locked_inputs(evidences, truths)
    evaluator = LockedMetricsEvaluator()
    outputs: List[Tuple[InferenceEvidence, EvaluationTruth, Tuple[RankedItem, ...]]] = []
    for ev, truth in zip(evidences, truths):
        res = ev.lexical_ranking[:3]
        outputs.append((ev, truth, res))
    return evaluator.evaluate_results(outputs, baseline_a_near_wrong_count=7, baseline_a_semantic_score=0.0)


def evaluate_baseline_b(
    evidences: Sequence[InferenceEvidence],
    truths: Sequence[EvaluationTruth],
    baseline_a_near_wrong_count: int = 7,
    threshold_b: float = HISTORICAL_REFERENCE_B_THRESHOLD,
) -> MetricsObject:
    """Evaluate raw dense MiniLM retriever baseline B on 80 locked cases with frozen reference threshold."""
    validate_locked_inputs(evidences, truths)
    evaluator = LockedMetricsEvaluator()
    outputs: List[Tuple[InferenceEvidence, EvaluationTruth, Tuple[RankedItem, ...]]] = []
    for ev, truth in zip(evidences, truths):
        filtered = tuple(item for item in ev.semantic_ranking if item.score >= threshold_b)[:3]
        outputs.append((ev, truth, filtered))
    return evaluator.evaluate_results(outputs, baseline_a_near_wrong_count=baseline_a_near_wrong_count, baseline_a_semantic_score=0.0)
