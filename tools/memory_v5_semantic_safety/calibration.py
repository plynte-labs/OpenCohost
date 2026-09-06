"""
Two-Phase Deterministic Calibration Engine & Freeze Protocol.

Module Responsibilities:
1. Derivation of exact deterministic per-mechanism conservatism keys.
2. Common CalibrationEvaluator deriving 13 calibration metrics with exact per-case privacy semantics.
3. Phase 1 Safety Feasibility Filter: Hard recall==1.0 (4/4), NO_MEMORY false injections==0 (4/4), Privacy leakage==0.
4. Phase 2 Semantic Optimization: Maximizing mean syn/para MRR@3 among safe configurations.
5. Deterministic tie-breaking (Objective -> Hard Recall -> Conservatism -> Canonical JSON).
6. Candidate-local CALIBRATION_FAILED and CONFIG_INVALID handling with failure isolation.
7. Structural Freeze boundary producing immutable CalibrationRecord snapshots.
8. Freeze validation (candidate ID recomputation, closed config validation, hex64 hashes, ISO timestamp, sequence).
9. Freeze-all-before-locked invariant and 3-dimensional CALIBRATION_LEAK detection.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import re
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from tools.memory_v5_semantic_safety.candidates import (
    CandidateManifest,
    SafetyRefinementCandidate,
    compute_candidate_id,
)
from tools.memory_v5_semantic_safety.evidence import (
    hash_candidate_payload,
    normalize_query_for_disjointness,
)
from tools.memory_v5_semantic_safety.models import (
    CandidateConfig,
    CandidateResult,
    CalibrationRecord,
    EvaluationTruth,
    FrozenDict,
    InferenceEvidence,
    RankedItem,
)

HEX64_REGEX = re.compile(r"^[0-9a-f]{64}$")


def derive_conservatism_key(mechanism_key: str, params: Dict[str, Any]) -> Tuple[Any, ...]:
    """
    Derive exact deterministic conservatism sorting key:
    - H1_MARGIN: (-theta_score, -theta_margin) -> prefers higher score and margin threshold
    - H2_LEXICAL_CORROBORATION: (-theta_tok, -theta_idf) -> prefers higher token and IDF requirement
    - H4_ASYMMETRIC_GATE: (-theta_high, -theta_low, R_lex_max) -> prefers higher thresholds and smaller lexical rank allowance
    """
    if mechanism_key == "H1_MARGIN":
        return (-float(params.get("theta_score", 0.0)), -float(params.get("theta_margin", 0.0)))
    elif mechanism_key == "H2_LEXICAL_CORROBORATION":
        return (-int(params.get("theta_tok", 1)), -float(params.get("theta_idf", 0.0)))
    elif mechanism_key == "H4_ASYMMETRIC_GATE":
        return (
            -float(params.get("theta_high", 1.0)),
            -float(params.get("theta_low", 0.0)),
            int(params.get("R_lex_max", 3)),
        )
    return ()


@dataclass(frozen=True)
class CalibrationMetrics:
    """Evaluation metrics for a candidate on the 24-case calibration set."""
    cal_hard_correct_at_1_count: int
    cal_hard_recall_at_1: float
    cal_no_memory_abstention_count: int
    cal_no_memory_false_injection_count: int
    cal_no_memory_abstention_accuracy: float
    cal_profile_isolation_pass_count: int
    cal_profile_privacy_leakage_count: int
    cal_profile_private_leakage_count: int
    cal_profile_inactive_leakage_count: int
    cal_profile_cross_profile_leakage_count: int
    cal_syn_mrr_at_3: float
    cal_para_mrr_at_3: float
    cal_syn_para_mrr3_mean: float
    is_safety_feasible: bool


class CalibrationEvaluator:
    """Evaluates candidate policy outputs against calibration ground truth."""

    def evaluate(
        self,
        candidate: SafetyRefinementCandidate,
        config: CandidateConfig,
        evidences: Sequence[InferenceEvidence],
        truths: Sequence[EvaluationTruth],
    ) -> CalibrationMetrics:
        if len(evidences) != len(truths):
            raise ValueError(f"Evidence count ({len(evidences)}) does not match truth count ({len(truths)})")

        hard_correct_1 = 0
        nomem_abstain = 0
        nomem_injections = 0
        priv_passes = 0
        priv_leak_private = 0
        priv_leak_inactive = 0
        priv_leak_cross = 0
        syn_rr_sum = 0.0
        para_rr_sum = 0.0

        for ev, truth in zip(evidences, truths):
            res = candidate.apply_policy(ev, config)
            fam = truth.family

            if fam == "hard_lexical":
                if res and res[0].memory_id in truth.target_ids:
                    hard_correct_1 += 1

            elif fam == "no_memory":
                if len(res) == 0:
                    nomem_abstain += 1
                else:
                    nomem_injections += 1

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
                    priv_passes += 1

            elif fam == "synonym":
                reciprocal_rank = 0.0
                for rank_idx, item in enumerate(res[:3]):
                    if item.memory_id in truth.target_ids:
                        reciprocal_rank = 1.0 / (rank_idx + 1)
                        break
                syn_rr_sum += reciprocal_rank

            elif fam == "paraphrase":
                reciprocal_rank = 0.0
                for rank_idx, item in enumerate(res[:3]):
                    if item.memory_id in truth.target_ids:
                        reciprocal_rank = 1.0 / (rank_idx + 1)
                        break
                para_rr_sum += reciprocal_rank

        total_privacy_leaks = priv_leak_private + priv_leak_inactive + priv_leak_cross
        hard_recall = hard_correct_1 / 4.0
        nomem_acc = nomem_abstain / 4.0
        syn_mrr = syn_rr_sum / 4.0
        para_mrr = para_rr_sum / 4.0
        syn_para_mean = (syn_mrr + para_mrr) / 2.0

        is_feasible = (
            hard_recall == 1.0
            and nomem_injections == 0
            and total_privacy_leaks == 0
        )

        return CalibrationMetrics(
            cal_hard_correct_at_1_count=hard_correct_1,
            cal_hard_recall_at_1=hard_recall,
            cal_no_memory_abstention_count=nomem_abstain,
            cal_no_memory_false_injection_count=nomem_injections,
            cal_no_memory_abstention_accuracy=nomem_acc,
            cal_profile_isolation_pass_count=priv_passes,
            cal_profile_privacy_leakage_count=total_privacy_leaks,
            cal_profile_private_leakage_count=priv_leak_private,
            cal_profile_inactive_leakage_count=priv_leak_inactive,
            cal_profile_cross_profile_leakage_count=priv_leak_cross,
            cal_syn_mrr_at_3=syn_mrr,
            cal_para_mrr_at_3=para_mrr,
            cal_syn_para_mrr3_mean=syn_para_mean,
            is_safety_feasible=is_feasible,
        )


def validate_freeze_integrity(
    record: CalibrationRecord,
    manifest: Optional[CandidateManifest] = None,
    expected_cal_hash: Optional[str] = None,
    expected_model_hash: Optional[str] = None,
    expected_config_hash: Optional[str] = None,
) -> None:
    """
    Validate that a CalibrationRecord is fully integral, closed, and valid for locked evaluation handoff.
    Raises ValueError on any integrity violation.
    """
    if not isinstance(record, CalibrationRecord):
        raise ValueError(f"Freeze integrity error: expected CalibrationRecord, got {type(record)}")

    active_manifest = manifest if manifest is not None else CandidateManifest.default_manifest()
    candidates_by_key = {c.mechanism_key: c for c in list(active_manifest.singletons) + list(active_manifest.combinations)}

    if record.mechanism_key not in candidates_by_key:
        raise ValueError(f"Freeze integrity error: unknown mechanism_key '{record.mechanism_key}' not in manifest")

    candidate = candidates_by_key[record.mechanism_key]

    # Validate parameters satisfy candidate's closed validation contract
    try:
        candidate.validate_config(CandidateConfig(record.candidate_id, record.mechanism_key, record.selected_params))
    except ValueError as e:
        raise ValueError(f"Freeze integrity error: selected_params invalid for {record.mechanism_key}: {e}") from e

    # Validate 64-char lowercase hex hashes
    for hash_name, hash_val in [
        ("calibration_fixture_hash", record.calibration_fixture_hash),
        ("model_artifact_hash", record.model_artifact_hash),
        ("shared_config_hash", record.shared_config_hash),
    ]:
        if not isinstance(hash_val, str) or not HEX64_REGEX.match(hash_val):
            raise ValueError(f"Freeze integrity error: {hash_name} '{hash_val}' is not a valid 64-char lowercase hex string")

    # Validate against expected authority hashes if provided
    if expected_cal_hash is not None and record.calibration_fixture_hash != expected_cal_hash:
        raise ValueError(f"Freeze integrity error: calibration_fixture_hash mismatch. Expected {expected_cal_hash}, got {record.calibration_fixture_hash}")
    if expected_model_hash is not None and record.model_artifact_hash != expected_model_hash:
        raise ValueError(f"Freeze integrity error: model_artifact_hash mismatch. Expected {expected_model_hash}, got {record.model_artifact_hash}")
    if expected_config_hash is not None and record.shared_config_hash != expected_config_hash:
        raise ValueError(f"Freeze integrity error: shared_config_hash mismatch. Expected {expected_config_hash}, got {record.shared_config_hash}")

    # Validate candidate ID recomputation
    expected_cid = compute_candidate_id(
        record.mechanism_key,
        record.selected_params,
        record.model_artifact_hash,
        record.shared_config_hash,
    )
    if record.candidate_id != expected_cid:
        raise ValueError(f"Freeze integrity error: candidate_id mismatch. Expected {expected_cid}, got {record.candidate_id}")

    # Validate frozen_at ISO 8601 string
    if not isinstance(record.frozen_at, str):
        raise ValueError("Freeze integrity error: frozen_at must be an ISO 8601 string")
    try:
        datetime.fromisoformat(record.frozen_at.replace("Z", "+00:00"))
    except ValueError as e:
        raise ValueError(f"Freeze integrity error: frozen_at '{record.frozen_at}' is not valid ISO 8601: {e}") from e

    # Validate frozen_sequence is integer >= 1
    if not isinstance(record.frozen_sequence, int) or isinstance(record.frozen_sequence, bool) or record.frozen_sequence < 1:
        raise ValueError(f"Freeze integrity error: frozen_sequence '{record.frozen_sequence}' must be integer >= 1")


class DeterministicCalibrationEngine:
    """Two-phase deterministic calibration and freeze engine."""

    def __init__(
        self,
        model_hash: str,
        config_hash: str,
        cal_hash: str,
    ):
        if not HEX64_REGEX.match(model_hash):
            raise ValueError(f"model_hash '{model_hash}' must be 64-char lowercase hex")
        if not HEX64_REGEX.match(config_hash):
            raise ValueError(f"config_hash '{config_hash}' must be 64-char lowercase hex")
        if not HEX64_REGEX.match(cal_hash):
            raise ValueError(f"cal_hash '{cal_hash}' must be 64-char lowercase hex")

        self.model_hash = model_hash
        self.config_hash = config_hash
        self.cal_hash = cal_hash
        self.evaluator = CalibrationEvaluator()

    def calibrate_candidate(
        self,
        candidate: SafetyRefinementCandidate,
        cal_evidences: Sequence[InferenceEvidence],
        cal_truths: Sequence[EvaluationTruth],
        frozen_sequence: int = 1,
        fixed_frozen_at: Optional[str] = None,
    ) -> CandidateResult:
        """
        Execute two-phase calibration:
        1. Enumerate candidate parameter space.
        2. Filter Phase 1 feasible configurations.
        3. Optimize Phase 2 semantic objective with deterministic conservatism tie-breaks.
        4. Produce immutable CalibrationRecord.
        Quality status is initialized as NOT_APPLICABLE (locked qualification in WU5).
        """
        param_space = candidate.generate_parameter_space(list(cal_evidences))

        feasible_candidates: List[Tuple[Any, CandidateConfig, CalibrationMetrics]] = []

        for params in param_space:
            try:
                candidate.validate_config(CandidateConfig("temp", candidate.mechanism_key, params))
            except ValueError:
                continue

            cid = compute_candidate_id(
                candidate.mechanism_key,
                params,
                self.model_hash,
                self.config_hash,
            )
            config = CandidateConfig(
                candidate_id=cid,
                mechanism_key=candidate.mechanism_key,
                params=params,
            )

            metrics = self.evaluator.evaluate(candidate, config, cal_evidences, cal_truths)
            if metrics.is_safety_feasible:
                conservatism_key = derive_conservatism_key(candidate.mechanism_key, params)
                canon_json = json.dumps(dict(params), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
                sort_key = (
                    -metrics.cal_syn_para_mrr3_mean,
                    -metrics.cal_hard_recall_at_1,
                    conservatism_key,
                    canon_json,
                )
                feasible_candidates.append((sort_key, config, metrics))

        if not feasible_candidates:
            return CandidateResult(
                variant_key=candidate.mechanism_key,
                execution_status="NON_EVALUABLE",
                execution_reason="CALIBRATION_FAILED",
                quality_status="NOT_APPLICABLE",
                critical_reasons=(),
                calibration=None,
            )

        feasible_candidates.sort(key=lambda item: item[0])
        _, best_config, best_metrics = feasible_candidates[0]

        frozen_at = fixed_frozen_at if fixed_frozen_at is not None else datetime.now(timezone.utc).isoformat()

        record = CalibrationRecord(
            candidate_id=best_config.candidate_id,
            mechanism_key=candidate.mechanism_key,
            selected_params=best_config.params,
            calibration_fixture_hash=self.cal_hash,
            model_artifact_hash=self.model_hash,
            shared_config_hash=self.config_hash,
            frozen_at=frozen_at,
            frozen_sequence=frozen_sequence,
            cal_syn_para_mrr3_mean=best_metrics.cal_syn_para_mrr3_mean,
            cal_hard_recall_at_1=best_metrics.cal_hard_recall_at_1,
            cal_no_memory_abstention_accuracy=best_metrics.cal_no_memory_abstention_accuracy,
            cal_no_memory_false_injection_count=best_metrics.cal_no_memory_false_injection_count,
            cal_profile_privacy_leakage_count=best_metrics.cal_profile_privacy_leakage_count,
        )

        return CandidateResult(
            variant_key=candidate.mechanism_key,
            execution_status="EVALUABLE",
            execution_reason=None,
            quality_status="NOT_APPLICABLE",
            critical_reasons=(),
            calibration=record,
        )

    def evaluate_custom_config(
        self,
        candidate: SafetyRefinementCandidate,
        config: CandidateConfig,
        cal_evidences: Sequence[InferenceEvidence],
        cal_truths: Sequence[EvaluationTruth],
    ) -> CandidateResult:
        """Evaluate custom config with CONFIG_INVALID handling."""
        try:
            candidate.validate_config(config)
        except ValueError:
            return CandidateResult(
                variant_key=candidate.mechanism_key,
                execution_status="NON_EVALUABLE",
                execution_reason="CONFIG_INVALID",
                quality_status="NOT_APPLICABLE",
                critical_reasons=(),
                calibration=None,
            )

        metrics = self.evaluator.evaluate(candidate, config, cal_evidences, cal_truths)
        if not metrics.is_safety_feasible:
            return CandidateResult(
                variant_key=candidate.mechanism_key,
                execution_status="NON_EVALUABLE",
                execution_reason="CALIBRATION_FAILED",
                quality_status="NOT_APPLICABLE",
                critical_reasons=(),
                calibration=None,
            )

        record = CalibrationRecord(
            candidate_id=config.candidate_id,
            mechanism_key=candidate.mechanism_key,
            selected_params=config.params,
            calibration_fixture_hash=self.cal_hash,
            model_artifact_hash=self.model_hash,
            shared_config_hash=self.config_hash,
            frozen_at=datetime.now(timezone.utc).isoformat(),
            frozen_sequence=1,
            cal_syn_para_mrr3_mean=metrics.cal_syn_para_mrr3_mean,
            cal_hard_recall_at_1=metrics.cal_hard_recall_at_1,
            cal_no_memory_abstention_accuracy=metrics.cal_no_memory_abstention_accuracy,
            cal_no_memory_false_injection_count=metrics.cal_no_memory_false_injection_count,
            cal_profile_privacy_leakage_count=metrics.cal_profile_privacy_leakage_count,
        )
        return CandidateResult(
            variant_key=candidate.mechanism_key,
            execution_status="EVALUABLE",
            execution_reason=None,
            quality_status="NOT_APPLICABLE",
            critical_reasons=(),
            calibration=record,
        )

    def calibrate_all(
        self,
        manifest: CandidateManifest,
        cal_evidences: Sequence[InferenceEvidence],
        cal_truths: Sequence[EvaluationTruth],
        fixed_frozen_at: Optional[str] = None,
    ) -> Dict[str, CandidateResult]:
        """Calibrate all admitted candidates independently."""
        results: Dict[str, CandidateResult] = {}
        for seq, cand in enumerate(list(manifest.singletons) + list(manifest.combinations), start=1):
            res = self.calibrate_candidate(
                cand,
                cal_evidences,
                cal_truths,
                frozen_sequence=seq,
                fixed_frozen_at=fixed_frozen_at,
            )
            results[cand.mechanism_key] = res
        return results


class FrozenCalibrationState:
    """Holds calibration state and enforces freeze-all-before-locked invariant and freeze validation."""

    def __init__(
        self,
        manifest: Optional[CandidateManifest] = None,
        expected_cal_hash: Optional[str] = None,
        expected_model_hash: Optional[str] = None,
        expected_config_hash: Optional[str] = None,
    ):
        self._results: Dict[str, CandidateResult] = {}
        self._is_frozen: bool = False
        self._manifest = manifest
        self._expected_cal_hash = expected_cal_hash
        self._expected_model_hash = expected_model_hash
        self._expected_config_hash = expected_config_hash

    @property
    def is_frozen(self) -> bool:
        return self._is_frozen

    def register_candidate_result(self, variant_key: str, result: CandidateResult) -> None:
        if self._is_frozen:
            raise RuntimeError("Cannot register candidate result after calibration state is frozen")
        self._results[variant_key] = result

    def freeze(self) -> None:
        """Freeze calibration state prior to locked evaluation."""
        self._is_frozen = True

    def get_locked_ready_configs(self) -> List[CandidateConfig]:
        """
        Return list of CandidateConfig ready for locked evaluation.
        Enforces freeze-all-before-locked invariant and validates freeze integrity.
        """
        if not self._is_frozen:
            raise RuntimeError("Calibration state is not yet frozen. Freeze must occur before locked evaluation.")

        configs: List[CandidateConfig] = []
        for res in self._results.values():
            if res.execution_status == "EVALUABLE":
                if res.calibration is None:
                    raise ValueError(f"EVALUABLE candidate '{res.variant_key}' missing CalibrationRecord")

                validate_freeze_integrity(
                    res.calibration,
                    manifest=self._manifest,
                    expected_cal_hash=self._expected_cal_hash,
                    expected_model_hash=self._expected_model_hash,
                    expected_config_hash=self._expected_config_hash,
                )

                cfg = CandidateConfig(
                    candidate_id=res.calibration.candidate_id,
                    mechanism_key=res.calibration.mechanism_key,
                    params=res.calibration.selected_params,
                )
                configs.append(cfg)
        return configs


def assert_no_calibration_leak(
    cal_cases: Sequence[Dict[str, Any]],
    locked_cases: Sequence[Dict[str, Any]],
) -> None:
    """
    Validate that no locked evaluation cases or identities have leaked into calibration across 3 dimensions:
    1. Case IDs are disjoint.
    2. Normalized query texts are disjoint.
    3. Memory candidate payload hashes are disjoint.
    Raises ValueError('CALIBRATION_LEAK: ...') on contamination.
    """
    # Dimension 1: Case IDs
    cal_ids = {str(c.get("case_id") or c.get("id")) for c in cal_cases}
    locked_ids = {str(c.get("case_id") or c.get("id")) for c in locked_cases}
    id_overlap = cal_ids & locked_ids
    if id_overlap:
        raise ValueError(f"CALIBRATION_LEAK: Overlapping case IDs detected between calibration and locked: {id_overlap}")

    # Dimension 2: Normalized queries
    cal_queries = {normalize_query_for_disjointness(str(c.get("query", ""))) for c in cal_cases}
    locked_queries = {normalize_query_for_disjointness(str(c.get("query", ""))) for c in locked_cases}
    query_overlap = cal_queries & locked_queries
    if query_overlap:
        raise ValueError(f"CALIBRATION_LEAK: Overlapping normalized queries detected between calibration and locked: {query_overlap}")

    # Dimension 3: Memory candidate payloads
    cal_payloads: Set[str] = set()
    for c in cal_cases:
        for cand in c.get("candidates", []):
            cal_payloads.add(hash_candidate_payload(cand))

    locked_payloads: Set[str] = set()
    for c in locked_cases:
        for cand in c.get("candidates", []):
            locked_payloads.add(hash_candidate_payload(cand))

    payload_overlap = cal_payloads & locked_payloads
    if payload_overlap:
        raise ValueError(f"CALIBRATION_LEAK: Overlapping candidate memory payloads detected ({len(payload_overlap)} hashes)")
