"""
End-to-End Pipeline Coordinator & Authoritative Benchmark Runner.

Module Responsibilities:
1. Orchestrate complete benchmark lifecycle:
   PREPARE -> CALIBRATE -> FREEZE -> LOCKED EVALUATION -> METRICS -> SAFETY_QUALITY -> ROUTING -> RECEIPT -> PUBLICATION.
2. Cryptographic verification of fixtures, model artifacts, shared config, and runtime environment.
3. Candidate-local failure isolation (MODEL_UNAVAILABLE, MODEL_HASH_MISMATCH) preserving lexical baseline A.
4. Fail-closed baseline A verification (BASELINE_INVALID) protecting scientific authority.
5. Winner selection, terminal routing, and canonical BenchmarkReceiptV1 publication.
6. Support CLI entry point and hermetic directory overrides.
"""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from tools.memory_v5_semantic_safety.calibration import (
    DeterministicCalibrationEngine,
    FrozenCalibrationState,
    assert_no_calibration_leak,
)
from tools.memory_v5_semantic_safety.candidates import (
    CandidateManifest,
    compute_candidate_id,
)
from tools.memory_v5_semantic_safety.evidence import (
    MiniLMEmbedder,
    execute_lexical_search,
    generate_case_evidence,
    load_fixture,
    resolve_target_user_id,
    validate_fixture_quotas,
)
from tools.memory_v5_semantic_safety.metrics import (
    LockedQualificationEngine,
    evaluate_baseline_a,
    evaluate_baseline_b,
)
from tools.memory_v5_semantic_safety.models import (
    BenchmarkReceiptV1,
    CandidateResult,
    EvaluationTruth,
    InferenceEvidence,
    MetricsObject,
    RankedItem,
)
from tools.memory_v5_semantic_safety.receipt import (
    FROZEN_ADR_REFERENCE,
    FROZEN_LOCK_IDENTITY,
    FROZEN_MODEL_ID,
    FROZEN_SEED,
    RECEIPT_SCHEMA_VERSION,
    build_benchmark_receipt,
    publish_benchmark_run,
    validate_receipt_schema,
)
from tools.memory_v5_semantic_safety.routing import (
    LifecycleRoutingEngine,
)

FROZEN_CALIBRATION_FIXTURE_HASH = "1d262ab3b3daa275e9bee166e024ae2f91c8d6d8a3fe95b993d2bb06feee9472"
FROZEN_LOCKED_FIXTURE_HASH = "6e96049e76e471549b73d1606e714a888ee1f4ba64729423b6913739db5a435f"
FROZEN_MODEL_ARTIFACT_HASH = "7cce6bd3df3eefe20757261165563b9f27b2397b5f0661bd42f804d545b07569"
FROZEN_SHARED_CONFIG_HASH = "c0d7110f0abe8dfae5e335dc1d931b4742c52e37ea67de296afbb22060912c43"

REQUIRED_CONFIG_KEYS = {"rrf_k", "rrf_weight_lexical", "rrf_weight_semantic", "seed"}

FROZEN_HISTORICAL_BASELINE_B_METRICS = {
    "hard_recall_at_1": 1.0,
    "semantic_quality_score": 0.8392857142857143,
    "no_memory_false_injection_count": 1,
    "near_wrong_correct_at_1_count": 11,
    "profile_privacy_leakage_count": 0,
}

MODEL_FILES_MANIFEST = [
    {"path": "config.json", "sha256": "05b570bff786faa5c4604152aa16f19f77ed6dfc31e47dd0f3dd987078693ac7"},
    {"path": "model.onnx", "sha256": "185ae63f47e17a7e8d30d0e6a3cde6a6e4b79bc5b81666ecffc279a6856ca113"},
    {"path": "tokenizer.json", "sha256": "b60b6b43406a48bf3638526314f3d232d97058bc93472ff2de930d43686fa441"},
]


def _compute_file_sha256(path: Path) -> str:
    """Compute SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def compute_canonical_shared_config_hash(cfg: Dict[str, Any]) -> str:
    """Compute canonical SHA-256 hash for shared configuration. Fails closed on extra/missing keys."""
    if not isinstance(cfg, dict) or set(cfg.keys()) != REQUIRED_CONFIG_KEYS:
        raise ValueError(
            f"Shared config must contain exact keys {REQUIRED_CONFIG_KEYS}, "
            f"got {set(cfg.keys()) if isinstance(cfg, dict) else type(cfg)}"
        )

    canonical_cfg = {
        "rrf_k": int(cfg["rrf_k"]),
        "rrf_weight_lexical": float(cfg["rrf_weight_lexical"]),
        "rrf_weight_semantic": float(cfg["rrf_weight_semantic"]),
        "seed": int(cfg["seed"]),
    }
    raw_bytes = json.dumps(canonical_cfg, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw_bytes).hexdigest()


def verify_model_artifacts_directory(model_dir: Path) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Verify presence and cryptographic SHA-256 hashes of actual model files.
    Returns (is_valid, fail_reason, computed_artifact_hash).
    Candidate-local reasons: MODEL_UNAVAILABLE, MODEL_HASH_MISMATCH.
    """
    if not model_dir.exists() or not model_dir.is_dir():
        return False, "MODEL_UNAVAILABLE", None

    for item in MODEL_FILES_MANIFEST:
        rel_path = item["path"]
        expected_sha = item["sha256"]
        file_p = model_dir / rel_path
        if not file_p.exists() or not file_p.is_file():
            return False, "MODEL_UNAVAILABLE", None
        actual_sha = _compute_file_sha256(file_p)
        if actual_sha.lower() != expected_sha.lower():
            return False, "MODEL_HASH_MISMATCH", None

    # Compute concatenated artifact hash
    concat = "".join(item["sha256"] for item in sorted(MODEL_FILES_MANIFEST, key=lambda x: x["path"]))
    computed_artifact_hash = hashlib.sha256(concat.encode("utf-8")).hexdigest()
    if computed_artifact_hash != FROZEN_MODEL_ARTIFACT_HASH:
        return False, "MODEL_HASH_MISMATCH", None

    return True, None, computed_artifact_hash


class BenchmarkPipelineCoordinator:
    """Coordinates end-to-end benchmark execution with fail-closed failure isolation."""

    def __init__(
        self,
        output_dir: Optional[Path] = None,
        override_cal_hash: Optional[str] = None,
        override_locked_hash: Optional[str] = None,
        override_model_hash: Optional[str] = None,
        override_config_hash: Optional[str] = None,
        override_cal_fixture_path: Optional[Path] = None,
        override_locked_fixture_path: Optional[Path] = None,
        override_model_dir: Optional[Path] = None,
        override_shared_config: Optional[Dict[str, Any]] = None,
        override_lock_identity: Optional[str] = None,
        synthetic_baseline_a_fail: bool = False,
    ):
        self.repo_root = Path(__file__).resolve().parents[2]
        self.output_dir = output_dir if output_dir is not None else (self.repo_root / "docs" / "memory_v5")
        self.cal_fixture_path = override_cal_fixture_path or (self.repo_root / "tests" / "fixtures" / "memory_v5_semantic_calibration.json")
        self.locked_fixture_path = override_locked_fixture_path or (self.repo_root / "tests" / "fixtures" / "memory_v5_semantic_locked.json")
        self.model_dir = override_model_dir or (self.repo_root / "modelos_f5" / "minilm_l12_onnx")

        self.expected_cal_hash = override_cal_hash or FROZEN_CALIBRATION_FIXTURE_HASH
        self.expected_locked_hash = override_locked_hash or FROZEN_LOCKED_FIXTURE_HASH
        self.expected_model_hash = override_model_hash or FROZEN_MODEL_ARTIFACT_HASH
        self.expected_config_hash = override_config_hash or FROZEN_SHARED_CONFIG_HASH

        self.override_shared_config = override_shared_config
        self.override_lock_identity = override_lock_identity
        self.synthetic_baseline_a_fail = synthetic_baseline_a_fail

    def execute_pipeline(self, run_id: Optional[str] = None) -> Tuple[BenchmarkReceiptV1, Tuple[Path, Path, Path, Optional[Path]]]:
        """
        Execute full benchmark pipeline:
        1. Preparation and Cryptographic Preflight Check
        2. Candidate-local Model Verification
        3. Calibration and Freeze
        4. Locked Evaluation
        5. Baseline A Fail-Closed Integrity Check
        6. Candidate Qualification & Routing
        7. Receipt Construction & Atomic Publication
        """
        effective_run_id = run_id or f"run-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        global_invalid_reasons: List[str] = []

        # 1. PREPARE: Preflight & Fixture Integrity
        cal_data = {}
        locked_data = {}

        # 1a. Runtime lock identity verification
        actual_impl = sys.implementation.name
        actual_ver = platform.python_version()
        actual_os = platform.system()
        actual_arch = platform.machine()
        actual_lock_identity = self.override_lock_identity or f"{actual_impl}-{actual_ver}-{actual_os}-{actual_arch}"
        if actual_lock_identity != FROZEN_LOCK_IDENTITY:
            global_invalid_reasons.append("SHARED_CONFIG_INVALID")

        # 1b. Shared config hash computation & verification (strict fail-closed, no silent reconstruction)
        shared_cfg_data = self.override_shared_config
        if shared_cfg_data is None:
            lock_path = self.repo_root / "tools" / "memory_v5_semantic_benchmark.lock"
            if not lock_path.exists():
                global_invalid_reasons.append("SHARED_CONFIG_INVALID")
            else:
                try:
                    with open(lock_path, "r", encoding="utf-8") as f:
                        lock_obj = json.load(f)
                    shared_cfg_data = lock_obj.get("shared_config")
                    if not isinstance(shared_cfg_data, dict):
                        global_invalid_reasons.append("SHARED_CONFIG_INVALID")
                except Exception:
                    global_invalid_reasons.append("SHARED_CONFIG_INVALID")

        if shared_cfg_data is not None:
            try:
                actual_config_hash = compute_canonical_shared_config_hash(shared_cfg_data)
                if actual_config_hash != self.expected_config_hash or actual_config_hash != FROZEN_SHARED_CONFIG_HASH:
                    global_invalid_reasons.append("SHARED_CONFIG_INVALID")
            except Exception:
                global_invalid_reasons.append("SHARED_CONFIG_INVALID")

        # 1c. Calibration fixture check
        if self.cal_fixture_path.exists():
            actual_cal_hash = _compute_file_sha256(self.cal_fixture_path)
            if actual_cal_hash != self.expected_cal_hash:
                global_invalid_reasons.append("CALIBRATION_FIXTURE_INVALID")
            else:
                cal_data = load_fixture(self.cal_fixture_path)
                if not validate_fixture_quotas(cal_data, is_calibration=True):
                    global_invalid_reasons.append("CALIBRATION_FIXTURE_INVALID")
        else:
            global_invalid_reasons.append("CALIBRATION_FIXTURE_INVALID")

        # 1d. Locked fixture check
        if self.locked_fixture_path.exists():
            actual_locked_hash = _compute_file_sha256(self.locked_fixture_path)
            if actual_locked_hash != self.expected_locked_hash:
                global_invalid_reasons.append("LOCKED_FIXTURE_INVALID")
            else:
                locked_data = load_fixture(self.locked_fixture_path)
                if not validate_fixture_quotas(locked_data, is_calibration=False):
                    global_invalid_reasons.append("LOCKED_FIXTURE_INVALID")
        else:
            global_invalid_reasons.append("LOCKED_FIXTURE_INVALID")

        # 1e. Disjointness check
        if not global_invalid_reasons and cal_data and locked_data:
            try:
                assert_no_calibration_leak(cal_data.get("cases", []), locked_data.get("cases", []))
            except Exception:
                global_invalid_reasons.append("FIXTURE_OVERLAP")

        # If global invalid at preparation, build and publish GLOBAL_INVALID receipt immediately
        if global_invalid_reasons:
            engine = LifecycleRoutingEngine()
            outcome = engine.route_lifecycle([], global_invalid_reasons=global_invalid_reasons)
            receipt = build_benchmark_receipt(
                run_id=effective_run_id,
                state="GLOBAL_INVALID",
                terminal_route=outcome.terminal_route,
                winner=None,
                global_invalid_reason=outcome.global_invalid_reason,
                candidates=None,
                metrics=None,
                locked_fixture_hash=FROZEN_LOCKED_FIXTURE_HASH,
                calibration_fixture_hash=FROZEN_CALIBRATION_FIXTURE_HASH,
                model_artifact_hash=FROZEN_MODEL_ARTIFACT_HASH,
                shared_config_hash=FROZEN_SHARED_CONFIG_HASH,
                model_id=FROZEN_MODEL_ID,
            )
            pub_paths = publish_benchmark_run(receipt, output_dir=self.output_dir)
            return receipt, pub_paths

        # 2. CANDIDATE-LOCAL MODEL ARTIFACT VERIFICATION
        model_valid, model_fail_reason, actual_model_hash = verify_model_artifacts_directory(self.model_dir)
        embedder: Optional[MiniLMEmbedder] = None
        if model_valid:
            try:
                embedder = MiniLMEmbedder(self.model_dir)
                embedder.initialize()
            except Exception:
                model_valid = False
                model_fail_reason = "MODEL_UNAVAILABLE"

        manifest = CandidateManifest.default_manifest()

        # 3. EVIDENCE GENERATION & CALIBRATION
        cal_evidences: List[InferenceEvidence] = []
        cal_truths: List[EvaluationTruth] = []
        cal_results: Dict[str, Any] = {}

        if model_valid and embedder is not None:
            for case in cal_data.get("cases", []):
                ev, tr = generate_case_evidence(case, embedder=embedder)
                cal_evidences.append(ev)
                cal_truths.append(tr)

            cal_engine = DeterministicCalibrationEngine(
                model_hash=FROZEN_MODEL_ARTIFACT_HASH,
                config_hash=FROZEN_SHARED_CONFIG_HASH,
                cal_hash=FROZEN_CALIBRATION_FIXTURE_HASH,
            )

            cal_results = cal_engine.calibrate_all(
                manifest=manifest,
                cal_evidences=cal_evidences,
                cal_truths=cal_truths,
                fixed_frozen_at="2026-08-31T20:00:00Z",
            )

            frozen_state = FrozenCalibrationState(
                manifest=manifest,
                expected_cal_hash=FROZEN_CALIBRATION_FIXTURE_HASH,
                expected_model_hash=FROZEN_MODEL_ARTIFACT_HASH,
                expected_config_hash=FROZEN_SHARED_CONFIG_HASH,
            )
            for k, res in cal_results.items():
                frozen_state.register_candidate_result(k, res)
            frozen_state.freeze()
            frozen_state.get_locked_ready_configs()

        # 4. LOCKED EVALUATION
        locked_evidences: List[InferenceEvidence] = []
        locked_truths: List[EvaluationTruth] = []

        if model_valid and embedder is not None:
            for case in locked_data.get("cases", []):
                ev, tr = generate_case_evidence(case, embedder=embedder)
                locked_evidences.append(ev)
                locked_truths.append(tr)
        else:
            # Generate lexical-only evidence for Baseline A evaluation
            for case in locked_data.get("cases", []):
                case_id = str(case.get("case_id") or case.get("id", ""))
                opaque_key = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:16]
                query = str(case.get("query", ""))
                candidates = case.get("candidates", [])
                target_user_id = resolve_target_user_id(case)
                lex_ranking = execute_lexical_search(query, candidates, profile_id=target_user_id, k=None)
                inf_ev = InferenceEvidence(
                    evidence_key=opaque_key,
                    query=query,
                    lexical_ranking=lex_ranking,
                    semantic_ranking=(),
                )
                targets = case.get("target_ids") or case.get("expected_ids") or []
                eval_tr = EvaluationTruth(
                    case_id=case_id,
                    family=str(case.get("family", "")),
                    target_ids=tuple(str(tid) for tid in targets),
                    expected_abstain=bool(case.get("expected_abstain", False)),
                    target_user_id=target_user_id,
                    diagnostic_subtype=case.get("diagnostic_subtype"),
                )
                locked_evidences.append(inf_ev)
                locked_truths.append(eval_tr)

        # Baseline A evaluation
        metrics_a = evaluate_baseline_a(locked_evidences, locked_truths)

        # 5. BASELINE A INTEGRITY CHECK (Fail-closed to BASELINE_INVALID)
        baseline_a_valid = (
            not self.synthetic_baseline_a_fail
            and metrics_a.hard_correct_at_1_count == 16
            and metrics_a.semantic_quality_score == 0.0
            and metrics_a.no_memory_abstention_count == 12
            and metrics_a.no_memory_false_injection_count == 0
            and metrics_a.near_wrong_correct_at_1_count == 7
            and metrics_a.profile_isolation_pass_count == 5
            and metrics_a.profile_privacy_leakage_count == 0
            and metrics_a.profile_private_leakage_count == 0
            and metrics_a.profile_inactive_leakage_count == 0
            and metrics_a.profile_cross_profile_leakage_count == 0
        )

        if not baseline_a_valid:
            engine = LifecycleRoutingEngine()
            outcome = engine.route_lifecycle([], global_invalid_reasons=["BASELINE_INVALID"])
            receipt = build_benchmark_receipt(
                run_id=effective_run_id,
                state="GLOBAL_INVALID",
                terminal_route=outcome.terminal_route,
                winner=None,
                global_invalid_reason=outcome.global_invalid_reason,
                candidates=None,
                metrics=None,
                locked_fixture_hash=FROZEN_LOCKED_FIXTURE_HASH,
                calibration_fixture_hash=FROZEN_CALIBRATION_FIXTURE_HASH,
                model_artifact_hash=FROZEN_MODEL_ARTIFACT_HASH,
                shared_config_hash=FROZEN_SHARED_CONFIG_HASH,
                model_id=FROZEN_MODEL_ID,
            )
            pub_paths = publish_benchmark_run(receipt, output_dir=self.output_dir)
            return receipt, pub_paths

        # Baseline B evaluation (or frozen reference baseline if model unavailable)
        metrics_b: Optional[MetricsObject] = None
        if model_valid and embedder is not None:
            metrics_b = evaluate_baseline_b(
                locked_evidences,
                locked_truths,
                baseline_a_near_wrong_count=metrics_a.near_wrong_correct_at_1_count,
            )

        qual_engine = LockedQualificationEngine(baseline_a_near_wrong_count=metrics_a.near_wrong_correct_at_1_count)

        candidates_dict: Dict[str, Any] = {}
        metrics_dict: Dict[str, Any] = {
            "baseline_a": {
                "hard_recall_at_1": metrics_a.hard_recall_at_1,
                "semantic_quality_score": metrics_a.semantic_quality_score,
                "no_memory_false_injection_count": metrics_a.no_memory_false_injection_count,
                "near_wrong_correct_at_1_count": metrics_a.near_wrong_correct_at_1_count,
                "profile_privacy_leakage_count": metrics_a.profile_privacy_leakage_count,
            },
            "baseline_b": {
                "hard_recall_at_1": metrics_b.hard_recall_at_1,
                "semantic_quality_score": metrics_b.semantic_quality_score,
                "no_memory_false_injection_count": metrics_b.no_memory_false_injection_count,
                "near_wrong_correct_at_1_count": metrics_b.near_wrong_correct_at_1_count,
                "profile_privacy_leakage_count": metrics_b.profile_privacy_leakage_count,
            } if metrics_b is not None else dict(FROZEN_HISTORICAL_BASELINE_B_METRICS),
        }

        routing_candidate_pairs: List[Tuple[CandidateResult, Optional[MetricsObject]]] = []

        for candidate in list(manifest.singletons) + list(manifest.combinations):
            v_key = candidate.mechanism_key

            if not model_valid:
                # Candidate-local model failure: preserve lexical baseline, mark semantic candidate NON_EVALUABLE
                non_eval_res = CandidateResult(
                    variant_key=v_key,
                    execution_status="NON_EVALUABLE",
                    execution_reason=model_fail_reason,
                    quality_status="NOT_APPLICABLE",
                    critical_reasons=(),
                    calibration=None,
                )
                candidates_dict[v_key] = {
                    "variant_key": v_key,
                    "execution_status": "NON_EVALUABLE",
                    "execution_reason": model_fail_reason,
                    "quality_status": "NOT_APPLICABLE",
                    "critical_reasons": [],
                    "calibration": None,
                }
                metrics_dict[v_key] = None
                routing_candidate_pairs.append((non_eval_res, None))
                continue

            cal_res = cal_results.get(v_key)
            evaluated_res, c_metrics = qual_engine.evaluate_candidate_locked(
                candidate=candidate,
                cal_result=cal_res,
                evidences=locked_evidences,
                truths=locked_truths,
                baseline_a_near_wrong_count=metrics_a.near_wrong_correct_at_1_count,
                baseline_b_semantic_score=metrics_b.semantic_quality_score if metrics_b else 0.0,
                baseline_a_semantic_score=metrics_a.semantic_quality_score,
            )

            if evaluated_res.execution_status == "NON_EVALUABLE":
                candidates_dict[v_key] = {
                    "variant_key": v_key,
                    "execution_status": "NON_EVALUABLE",
                    "execution_reason": evaluated_res.execution_reason,
                    "quality_status": "NOT_APPLICABLE",
                    "critical_reasons": [],
                    "calibration": None,
                }
                metrics_dict[v_key] = None
            else:
                record = evaluated_res.calibration
                cal_serialized = {
                    "candidate_id": record.candidate_id,
                    "mechanism_key": record.mechanism_key,
                    "selected_params": dict(record.selected_params),
                    "calibration_fixture_hash": record.calibration_fixture_hash,
                    "model_artifact_hash": record.model_artifact_hash,
                    "shared_config_hash": record.shared_config_hash,
                    "frozen_at": record.frozen_at,
                    "frozen_sequence": record.frozen_sequence,
                    "cal_syn_para_mrr3_mean": record.cal_syn_para_mrr3_mean,
                    "cal_hard_recall_at_1": record.cal_hard_recall_at_1,
                    "cal_no_memory_abstention_accuracy": record.cal_no_memory_abstention_accuracy,
                    "cal_no_memory_false_injection_count": record.cal_no_memory_false_injection_count,
                    "cal_profile_privacy_leakage_count": record.cal_profile_privacy_leakage_count,
                }

                candidates_dict[v_key] = {
                    "variant_key": v_key,
                    "execution_status": "EVALUABLE",
                    "execution_reason": None,
                    "quality_status": evaluated_res.quality_status,
                    "critical_reasons": list(evaluated_res.critical_reasons),
                    "calibration": cal_serialized,
                }

                metrics_dict[v_key] = {
                    "hard_correct_at_1_count": c_metrics.hard_correct_at_1_count,
                    "hard_correct_at_3_count": c_metrics.hard_correct_at_3_count,
                    "hard_recall_at_1": c_metrics.hard_recall_at_1,
                    "hard_recall_at_3": c_metrics.hard_recall_at_3,
                    "hard_mrr_at_3": c_metrics.hard_mrr_at_3,
                    "syn_correct_at_1_count": c_metrics.syn_correct_at_1_count,
                    "syn_correct_at_3_count": c_metrics.syn_correct_at_3_count,
                    "syn_recall_at_1": c_metrics.syn_recall_at_1,
                    "syn_recall_at_3": c_metrics.syn_recall_at_3,
                    "syn_mrr_at_3": c_metrics.syn_mrr_at_3,
                    "para_correct_at_1_count": c_metrics.para_correct_at_1_count,
                    "para_correct_at_3_count": c_metrics.para_correct_at_3_count,
                    "para_recall_at_1": c_metrics.para_recall_at_1,
                    "para_recall_at_3": c_metrics.para_recall_at_3,
                    "para_mrr_at_3": c_metrics.para_mrr_at_3,
                    "semantic_quality_score": c_metrics.semantic_quality_score,
                    "no_memory_abstention_count": c_metrics.no_memory_abstention_count,
                    "no_memory_false_injection_count": c_metrics.no_memory_false_injection_count,
                    "no_memory_abstention_accuracy": c_metrics.no_memory_abstention_accuracy,
                    "near_wrong_correct_at_1_count": c_metrics.near_wrong_correct_at_1_count,
                    "near_wrong_precision_at_1": c_metrics.near_wrong_precision_at_1,
                    "near_wrong_target_mrr_at_3": c_metrics.near_wrong_target_mrr_at_3,
                    "profile_isolation_pass_count": c_metrics.profile_isolation_pass_count,
                    "profile_privacy_leakage_count": c_metrics.profile_privacy_leakage_count,
                    "profile_private_leakage_count": c_metrics.profile_private_leakage_count,
                    "profile_inactive_leakage_count": c_metrics.profile_inactive_leakage_count,
                    "profile_cross_profile_leakage_count": c_metrics.profile_cross_profile_leakage_count,
                    "stale_correct_count": c_metrics.stale_correct_count,
                    "stale_correct_rate": c_metrics.stale_correct_rate,
                    "retention_vs_b": c_metrics.retention_vs_b,
                    "gain_over_a": c_metrics.gain_over_a,
                }

            routing_candidate_pairs.append((evaluated_res, c_metrics))

        # 6. ROUTING & WINNER SELECTION
        routing_engine = LifecycleRoutingEngine()
        outcome = routing_engine.route_lifecycle(routing_candidate_pairs)

        # 7. RECEIPT BUILD & ATOMIC PUBLICATION
        receipt = build_benchmark_receipt(
            run_id=effective_run_id,
            state="VALID",
            terminal_route=outcome.terminal_route,
            winner=outcome.winner_variant_key,
            global_invalid_reason=None,
            candidates=candidates_dict,
            metrics=metrics_dict,
            locked_fixture_hash=FROZEN_LOCKED_FIXTURE_HASH,
            calibration_fixture_hash=FROZEN_CALIBRATION_FIXTURE_HASH,
            model_artifact_hash=actual_model_hash or FROZEN_MODEL_ARTIFACT_HASH,
            shared_config_hash=actual_config_hash,
            model_id=FROZEN_MODEL_ID,
        )

        validate_receipt_schema(receipt)
        pub_paths = publish_benchmark_run(receipt, output_dir=self.output_dir)
        return receipt, pub_paths


def run_authoritative_benchmark(output_dir: Optional[Path] = None) -> BenchmarkReceiptV1:
    """CLI entry point helper to run the authoritative benchmark."""
    coordinator = BenchmarkPipelineCoordinator(output_dir=output_dir)
    receipt, pub_paths = coordinator.execute_pipeline()
    receipt_p, report_p, diag_p, cur_p = pub_paths

    print(f"[BENCHMARK COMPLETE]")
    print(f"  Run ID:         {receipt.run_id}")
    print(f"  State:          {receipt.state}")
    print(f"  Terminal Route: {receipt.terminal_route}")
    print(f"  Winner:         {receipt.winner}")
    print(f"  Receipt Path:   {receipt_p}")
    print(f"  Report Path:    {report_p}")
    print(f"  Diagnostics:    {diag_p}")
    if cur_p:
        print(f"  Current Pointer:{cur_p}")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Memory v5 Semantic Safety Benchmark Runner")
    parser.add_argument("--output-dir", type=str, default=None, help="Output directory for benchmark publication")
    args = parser.parse_args()

    out_p = Path(args.output_dir) if args.output_dir else None
    run_authoritative_benchmark(output_dir=out_p)
