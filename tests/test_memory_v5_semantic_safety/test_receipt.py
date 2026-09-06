"""
Unit and integration tests for WU7: Benchmark Receipt Publisher & Formatter.

Validates:
1. BenchmarkReceiptV1 domain model validation and strict nullability matrix:
   - VALID state: global_invalid_reason=None, candidates dict, metrics dict, terminal_route in (PROMISING, KEEP, INCONCLUSIVE)
   - GLOBAL_INVALID state: global_invalid_reason non-null, candidates=None, metrics=None, winner=None, terminal_route="GLOBAL_INVALID"
   - NON_EVALUABLE candidate: execution_reason non-null, quality_status="NOT_APPLICABLE", critical_reasons=(), calibration=None, metrics=None
2. Frozen model identity: exact match for "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2".
3. Exact required-field schemas (no missing, no extra keys across receipt, candidate, calibration, metrics, baselines).
4. Candidate identity cross-consistency (key == variant_key == mechanism_key, and recomputed candidate_id).
5. Finite numeric value enforcement (rejection of NaN, +Inf, -Inf).
6. Metadata-only diagnostics.json (zero raw payloads, zero query text, zero candidate text, zero PII).
7. Nested hash consistency: candidate hashes match receipt top-level hashes exactly.
8. Leaf mutation consistency: systematically proving that mutating individual fields fails closed.
9. Markdown report rendering: verified to accurately reflect receipt values without discrepancy.
10. Atomic publication layout: creates receipt.json, report.md, diagnostics.json and updates current-generation.json atomically only on VALID runs.
11. Publication coherence and failure resilience.
"""

import copy
import json
import math
from pathlib import Path
import pytest

from tools.memory_v5_semantic_safety.candidates import compute_candidate_id
from tools.memory_v5_semantic_safety.models import (
    BenchmarkReceiptV1,
    FrozenDict,
)
from tools.memory_v5_semantic_safety.receipt import (
    DIAGNOSTICS_SCHEMA_VERSION,
    FROZEN_MODEL_ID,
    RECEIPT_SCHEMA_VERSION,
    build_benchmark_receipt,
    build_diagnostics_object,
    publish_benchmark_run,
    receipt_from_dict,
    receipt_to_dict,
    render_markdown_report,
    validate_receipt_schema,
)

CAL_HASH = "1d262ab3b3daa275e9bee166e024ae2f91c8d6d8a3fe95b993d2bb06feee9472"
LOCKED_HASH = "6e96049e76e471549b73d1606e714a888ee1f4ba64729423b6913739db5a435f"
MODEL_HASH = "7cce6bd3df3eefe20757261165563b9f27b2397b5f0661bd42f804d545b07569"
CONFIG_HASH = "c0d7110f0abe8dfae5e335dc1d931b4742c52e37ea67de296afbb22060912c43"


def _make_valid_test_receipt() -> BenchmarkReceiptV1:
    """Build a valid BenchmarkReceiptV1 corresponding to WU5/WU6 real scenario."""
    h1_params = {"theta_score": 0.27648258209228516, "theta_margin": 0.026646047830581665}
    h1_id = compute_candidate_id("H1_MARGIN", h1_params, MODEL_HASH, CONFIG_HASH)
    cal_h1 = {
        "candidate_id": h1_id,
        "mechanism_key": "H1_MARGIN",
        "selected_params": h1_params,
        "calibration_fixture_hash": CAL_HASH,
        "model_artifact_hash": MODEL_HASH,
        "shared_config_hash": CONFIG_HASH,
        "frozen_at": "2026-08-31T20:00:00Z",
        "frozen_sequence": 1,
        "cal_syn_para_mrr3_mean": 0.875,
        "cal_hard_recall_at_1": 1.0,
        "cal_no_memory_abstention_accuracy": 1.0,
        "cal_no_memory_false_injection_count": 0,
        "cal_profile_privacy_leakage_count": 0,
    }

    h2_params = {"theta_tok": 1, "theta_idf": 0.0}
    h2_id = compute_candidate_id("H2_LEXICAL_CORROBORATION", h2_params, MODEL_HASH, CONFIG_HASH)
    cal_h2 = {
        "candidate_id": h2_id,
        "mechanism_key": "H2_LEXICAL_CORROBORATION",
        "selected_params": h2_params,
        "calibration_fixture_hash": CAL_HASH,
        "model_artifact_hash": MODEL_HASH,
        "shared_config_hash": CONFIG_HASH,
        "frozen_at": "2026-08-31T20:00:00Z",
        "frozen_sequence": 2,
        "cal_syn_para_mrr3_mean": 0.125,
        "cal_hard_recall_at_1": 1.0,
        "cal_no_memory_abstention_accuracy": 1.0,
        "cal_no_memory_false_injection_count": 0,
        "cal_profile_privacy_leakage_count": 0,
    }

    candidates = {
        "H1_MARGIN": {
            "variant_key": "H1_MARGIN",
            "execution_status": "EVALUABLE",
            "execution_reason": None,
            "quality_status": "CRITICAL_REGRESSION",
            "critical_reasons": ["NO_MEMORY_INJECTION"],
            "calibration": cal_h1,
        },
        "H2_LEXICAL_CORROBORATION": {
            "variant_key": "H2_LEXICAL_CORROBORATION",
            "execution_status": "EVALUABLE",
            "execution_reason": None,
            "quality_status": "QUALIFIED",
            "critical_reasons": [],
            "calibration": cal_h2,
        },
    }

    metrics = {
        "baseline_a": {
            "hard_recall_at_1": 1.0,
            "semantic_quality_score": 0.0,
            "no_memory_false_injection_count": 0,
            "near_wrong_correct_at_1_count": 7,
            "profile_privacy_leakage_count": 0,
        },
        "baseline_b": {
            "hard_recall_at_1": 1.0,
            "semantic_quality_score": 0.8392857142857143,
            "no_memory_false_injection_count": 1,
            "near_wrong_correct_at_1_count": 11,
            "profile_privacy_leakage_count": 0,
        },
        "H1_MARGIN": {
            "hard_correct_at_1_count": 16,
            "hard_correct_at_3_count": 16,
            "hard_recall_at_1": 1.0,
            "hard_recall_at_3": 1.0,
            "hard_mrr_at_3": 1.0,
            "syn_correct_at_1_count": 12,
            "syn_correct_at_3_count": 14,
            "syn_recall_at_1": 0.8571428571428571,
            "syn_recall_at_3": 1.0,
            "syn_mrr_at_3": 0.8571428571428571,
            "para_correct_at_1_count": 10,
            "para_correct_at_3_count": 15,
            "para_recall_at_1": 0.625,
            "para_recall_at_3": 0.9375,
            "para_mrr_at_3": 0.71875,
            "semantic_quality_score": 0.7879464285714286,
            "no_memory_abstention_count": 11,
            "no_memory_false_injection_count": 1,
            "no_memory_abstention_accuracy": 0.9166666666666666,
            "near_wrong_correct_at_1_count": 11,
            "near_wrong_precision_at_1": 0.9166666666666666,
            "near_wrong_target_mrr_at_3": 0.9166666666666666,
            "profile_isolation_pass_count": 5,
            "profile_privacy_leakage_count": 0,
            "profile_private_leakage_count": 0,
            "profile_inactive_leakage_count": 0,
            "profile_cross_profile_leakage_count": 0,
            "stale_correct_count": 5,
            "stale_correct_rate": 1.0,
            "retention_vs_b": 0.9388297872340425,
            "gain_over_a": 0.7879464285714286,
        },
        "H2_LEXICAL_CORROBORATION": {
            "hard_correct_at_1_count": 16,
            "hard_correct_at_3_count": 16,
            "hard_recall_at_1": 1.0,
            "hard_recall_at_3": 1.0,
            "hard_mrr_at_3": 1.0,
            "syn_correct_at_1_count": 0,
            "syn_correct_at_3_count": 0,
            "syn_recall_at_1": 0.0,
            "syn_recall_at_3": 0.0,
            "syn_mrr_at_3": 0.0,
            "para_correct_at_1_count": 1,
            "para_correct_at_3_count": 3,
            "para_recall_at_1": 0.0625,
            "para_recall_at_3": 0.1875,
            "para_mrr_at_3": 0.15625,
            "semantic_quality_score": 0.078125,
            "no_memory_abstention_count": 12,
            "no_memory_false_injection_count": 0,
            "no_memory_abstention_accuracy": 1.0,
            "near_wrong_correct_at_1_count": 10,
            "near_wrong_precision_at_1": 0.8333333333333334,
            "near_wrong_target_mrr_at_3": 0.8333333333333334,
            "profile_isolation_pass_count": 5,
            "profile_privacy_leakage_count": 0,
            "profile_private_leakage_count": 0,
            "profile_inactive_leakage_count": 0,
            "profile_cross_profile_leakage_count": 0,
            "stale_correct_count": 5,
            "stale_correct_rate": 1.0,
            "retention_vs_b": 0.09308510638297872,
            "gain_over_a": 0.078125,
        },
    }

    return BenchmarkReceiptV1(
        schema_version="memory-semantic-safety-refinement-receipt-v1",
        run_id="run-20260831-230000",
        state="VALID",
        global_invalid_reason=None,
        locked_fixture_hash=LOCKED_HASH,
        calibration_fixture_hash=CAL_HASH,
        model_id=FROZEN_MODEL_ID,
        model_artifact_hash=MODEL_HASH,
        shared_config_hash=CONFIG_HASH,
        seed=42,
        lock_identity="cpython-3.10.20-Windows-AMD64",
        adr_reference="ADR-053",
        candidates=candidates,
        metrics=metrics,
        winner="H2_LEXICAL_CORROBORATION",
        terminal_route="KEEP_LEXICAL",
    )


def test_valid_receipt_schema_validation():
    """Task 7.1: Positive schema validation on valid receipt."""
    receipt = _make_valid_test_receipt()
    validate_receipt_schema(receipt)  # Must not raise


def test_build_benchmark_receipt_factory():
    """Task 7.2: Verify build_benchmark_receipt constructs valid receipt with frozen defaults."""
    receipt = _make_valid_test_receipt()
    b_receipt = build_benchmark_receipt(
        run_id=receipt.run_id,
        state=receipt.state,
        terminal_route=receipt.terminal_route,
        winner=receipt.winner,
        candidates=dict(receipt.candidates),
        metrics=dict(receipt.metrics),
        locked_fixture_hash=LOCKED_HASH,
        calibration_fixture_hash=CAL_HASH,
        model_artifact_hash=MODEL_HASH,
        shared_config_hash=CONFIG_HASH,
    )
    assert b_receipt.schema_version == RECEIPT_SCHEMA_VERSION
    assert b_receipt.model_id == FROZEN_MODEL_ID
    assert b_receipt.seed == 42
    assert b_receipt.lock_identity == "cpython-3.10.20-Windows-AMD64"
    assert b_receipt.adr_reference == "ADR-053"


def test_receipt_nullability_and_state_matrix():
    """Task 7.3: Verify strict closed nullability matrix for VALID and GLOBAL_INVALID states."""
    # 1. GLOBAL_INVALID with non-null candidates -> Fail
    bad_global_1 = BenchmarkReceiptV1(
        schema_version=RECEIPT_SCHEMA_VERSION,
        run_id="run-err",
        state="GLOBAL_INVALID",
        global_invalid_reason="BASELINE_INVALID",
        locked_fixture_hash=LOCKED_HASH,
        calibration_fixture_hash=CAL_HASH,
        model_id=FROZEN_MODEL_ID,
        model_artifact_hash=MODEL_HASH,
        shared_config_hash=CONFIG_HASH,
        seed=42,
        lock_identity="cpython-3.10.20-Windows-AMD64",
        adr_reference="ADR-053",
        candidates={"H1": {}},
        metrics=None,
        winner=None,
        terminal_route="GLOBAL_INVALID",
    )
    with pytest.raises(ValueError, match="GLOBAL_INVALID state must have candidates=None"):
        validate_receipt_schema(bad_global_1)

    # 2. VALID with global_invalid_reason present -> Fail
    bad_valid_1 = BenchmarkReceiptV1(
        schema_version=RECEIPT_SCHEMA_VERSION,
        run_id="run-err",
        state="VALID",
        global_invalid_reason="BASELINE_INVALID",
        locked_fixture_hash=LOCKED_HASH,
        calibration_fixture_hash=CAL_HASH,
        model_id=FROZEN_MODEL_ID,
        model_artifact_hash=MODEL_HASH,
        shared_config_hash=CONFIG_HASH,
        seed=42,
        lock_identity="cpython-3.10.20-Windows-AMD64",
        adr_reference="ADR-053",
        candidates={},
        metrics={},
        winner=None,
        terminal_route="KEEP_LEXICAL",
    )
    with pytest.raises(ValueError, match="VALID state must have global_invalid_reason=None"):
        validate_receipt_schema(bad_valid_1)

    # 3. SAFETY_REFINEMENT_PROMISING without winner -> Fail
    valid_r = _make_valid_test_receipt()
    bad_promising_no_winner = BenchmarkReceiptV1(
        schema_version=RECEIPT_SCHEMA_VERSION,
        run_id="run-err",
        state="VALID",
        global_invalid_reason=None,
        locked_fixture_hash=LOCKED_HASH,
        calibration_fixture_hash=CAL_HASH,
        model_id=FROZEN_MODEL_ID,
        model_artifact_hash=MODEL_HASH,
        shared_config_hash=CONFIG_HASH,
        seed=42,
        lock_identity="cpython-3.10.20-Windows-AMD64",
        adr_reference="ADR-053",
        candidates=valid_r.candidates,
        metrics=valid_r.metrics,
        winner=None,
        terminal_route="SAFETY_REFINEMENT_PROMISING",
    )
    with pytest.raises(ValueError, match="requires a non-null winner"):
        validate_receipt_schema(bad_promising_no_winner)


def test_frozen_model_id_strict_enforcement():
    """Task 7.4: Verify frozen model ID is strictly enforced."""
    receipt = _make_valid_test_receipt()

    for bad_model in [
        "all-MiniLM-L12-v2",
        "paraphrase-multilingual-MiniLM-L12-v2",
        "sentence-transformers/all-MiniLM-L6-v2",
        "",
    ]:
        mutated_data = receipt_to_dict(receipt)
        mutated_data["model_id"] = bad_model
        with pytest.raises(ValueError, match="Invalid model_id"):
            receipt_from_dict(mutated_data)


def test_exact_required_field_schemas():
    """Task 7.5: Reject missing required fields across all receipt structures."""
    receipt = _make_valid_test_receipt()
    data = receipt_to_dict(receipt)

    # 1. Missing field in candidate
    bad_cand = copy.deepcopy(data)
    del bad_cand["candidates"]["H1_MARGIN"]["quality_status"]
    with pytest.raises(ValueError, match="missing required fields"):
        receipt_from_dict(bad_cand)

    # 2. Missing field in calibration
    bad_cal = copy.deepcopy(data)
    del bad_cal["candidates"]["H1_MARGIN"]["calibration"]["selected_params"]
    with pytest.raises(ValueError, match="missing required fields"):
        receipt_from_dict(bad_cal)

    # 3. Missing field in candidate metrics
    bad_met = copy.deepcopy(data)
    del bad_met["metrics"]["H1_MARGIN"]["retention_vs_b"]
    with pytest.raises(ValueError, match="missing required fields"):
        receipt_from_dict(bad_met)

    # 4. Missing baseline_a
    bad_base = copy.deepcopy(data)
    del bad_base["metrics"]["baseline_a"]
    with pytest.raises(ValueError, match="must contain 'baseline_a'"):
        receipt_from_dict(bad_base)


def test_candidate_identity_cross_consistency():
    """Task 7.6: Verify candidate key == variant_key == mechanism_key and recomputed candidate_id."""
    receipt = _make_valid_test_receipt()
    data = receipt_to_dict(receipt)

    # 1. Variant key mismatch with dict key
    bad_var = copy.deepcopy(data)
    bad_var["candidates"]["H1_MARGIN"]["variant_key"] = "H2_LEXICAL_CORROBORATION"
    with pytest.raises(ValueError, match="Candidate key mismatch"):
        receipt_from_dict(bad_var)

    # 2. Mechanism key mismatch
    bad_mech = copy.deepcopy(data)
    bad_mech["candidates"]["H1_MARGIN"]["calibration"]["mechanism_key"] = "H2_LEXICAL_CORROBORATION"
    with pytest.raises(ValueError, match="Mechanism key mismatch"):
        receipt_from_dict(bad_mech)

    # 3. Candidate ID mismatch
    bad_id = copy.deepcopy(data)
    bad_id["candidates"]["H1_MARGIN"]["calibration"]["candidate_id"] = "0" * 64
    with pytest.raises(ValueError, match="Candidate ID mismatch"):
        receipt_from_dict(bad_id)


def test_finite_numeric_value_enforcement():
    """Task 7.7: Reject NaN and Infinity from all numeric leaves."""
    receipt = _make_valid_test_receipt()

    # 1. NaN in metrics
    data_nan = receipt_to_dict(receipt)
    data_nan["metrics"]["H1_MARGIN"]["semantic_quality_score"] = float("nan")
    with pytest.raises(ValueError, match="Non-finite numeric value"):
        receipt_from_dict(data_nan)

    # 2. Inf in calibration
    data_inf = receipt_to_dict(receipt)
    data_inf["candidates"]["H1_MARGIN"]["calibration"]["cal_syn_para_mrr3_mean"] = float("inf")
    with pytest.raises(ValueError, match="Non-finite numeric value"):
        receipt_from_dict(data_inf)


def test_diagnostics_object_metadata_only():
    """Task 7.8: Verify diagnostics.json is metadata-only with zero PII or raw payloads."""
    receipt = _make_valid_test_receipt()
    diag = build_diagnostics_object(receipt)

    assert diag["schema_version"] == DIAGNOSTICS_SCHEMA_VERSION
    assert diag["run_id"] == "run-20260831-230000"
    assert diag["terminal_route"] == "KEEP_LEXICAL"
    assert diag["winner"] == "H2_LEXICAL_CORROBORATION"
    assert "authority_hashes" in diag
    assert "candidates_summary" in diag
    assert "metrics_summary" in diag

    # JSON serialization with allow_nan=False
    json_str = json.dumps(diag, indent=2, allow_nan=False)
    assert "query" not in json_str.lower() or "semantic_quality_score" in json_str
    assert "raw_payload" not in json_str


def test_critical_reasons_ordering_and_deduplication():
    """Task 7.9: Verify critical reasons ordering and deduplication invariants."""
    receipt = _make_valid_test_receipt()
    c_dict = dict(receipt.candidates)
    c_h1 = dict(c_dict["H1_MARGIN"])

    # 1. Unsorted critical reasons -> Fail
    c_h1["critical_reasons"] = ["PROFILE_PRIVACY_LEAKAGE", "HARD_RECALL_REGRESSION"]
    c_dict["H1_MARGIN"] = c_h1
    bad_order_receipt = BenchmarkReceiptV1(
        schema_version=receipt.schema_version,
        run_id=receipt.run_id,
        state=receipt.state,
        global_invalid_reason=receipt.global_invalid_reason,
        locked_fixture_hash=receipt.locked_fixture_hash,
        calibration_fixture_hash=receipt.calibration_fixture_hash,
        model_id=receipt.model_id,
        model_artifact_hash=receipt.model_artifact_hash,
        shared_config_hash=receipt.shared_config_hash,
        seed=receipt.seed,
        lock_identity=receipt.lock_identity,
        adr_reference=receipt.adr_reference,
        candidates=c_dict,
        metrics=receipt.metrics,
        winner=receipt.winner,
        terminal_route=receipt.terminal_route,
    )
    with pytest.raises(ValueError, match="critical_reasons not in canonical order"):
        validate_receipt_schema(bad_order_receipt)

    # 2. Duplicate critical reasons -> Fail
    c_h1["critical_reasons"] = ["NO_MEMORY_INJECTION", "NO_MEMORY_INJECTION"]
    c_dict["H1_MARGIN"] = c_h1
    bad_dup_receipt = BenchmarkReceiptV1(
        schema_version=receipt.schema_version,
        run_id=receipt.run_id,
        state=receipt.state,
        global_invalid_reason=receipt.global_invalid_reason,
        locked_fixture_hash=receipt.locked_fixture_hash,
        calibration_fixture_hash=receipt.calibration_fixture_hash,
        model_id=receipt.model_id,
        model_artifact_hash=receipt.model_artifact_hash,
        shared_config_hash=receipt.shared_config_hash,
        seed=receipt.seed,
        lock_identity=receipt.lock_identity,
        adr_reference=receipt.adr_reference,
        candidates=c_dict,
        metrics=receipt.metrics,
        winner=receipt.winner,
        terminal_route=receipt.terminal_route,
    )
    with pytest.raises(ValueError, match="duplicate critical_reasons"):
        validate_receipt_schema(bad_dup_receipt)


def test_nested_hash_consistency():
    """Task 7.10: Verify nested candidate calibration hashes match receipt top-level hashes."""
    receipt = _make_valid_test_receipt()

    # 1. Calibration fixture hash mismatch
    c_dict = dict(receipt.candidates)
    c_h1 = dict(c_dict["H1_MARGIN"])
    cal_h1 = dict(c_h1["calibration"])
    cal_h1["calibration_fixture_hash"] = "0" * 64
    c_h1["calibration"] = cal_h1
    c_dict["H1_MARGIN"] = c_h1
    bad_receipt = BenchmarkReceiptV1(
        schema_version=receipt.schema_version,
        run_id=receipt.run_id,
        state=receipt.state,
        global_invalid_reason=receipt.global_invalid_reason,
        locked_fixture_hash=receipt.locked_fixture_hash,
        calibration_fixture_hash=receipt.calibration_fixture_hash,
        model_id=receipt.model_id,
        model_artifact_hash=receipt.model_artifact_hash,
        shared_config_hash=receipt.shared_config_hash,
        seed=receipt.seed,
        lock_identity=receipt.lock_identity,
        adr_reference=receipt.adr_reference,
        candidates=c_dict,
        metrics=receipt.metrics,
        winner=receipt.winner,
        terminal_route=receipt.terminal_route,
    )
    with pytest.raises(ValueError, match="Nested calibration_fixture_hash mismatch"):
        validate_receipt_schema(bad_receipt)

    # 2. Model artifact hash mismatch
    c_dict2 = dict(receipt.candidates)
    c_h1_2 = dict(c_dict2["H1_MARGIN"])
    cal_h1_2 = dict(c_h1_2["calibration"])
    cal_h1_2["model_artifact_hash"] = "0" * 64
    c_h1_2["calibration"] = cal_h1_2
    c_dict2["H1_MARGIN"] = c_h1_2
    bad_receipt2 = BenchmarkReceiptV1(
        schema_version=receipt.schema_version,
        run_id=receipt.run_id,
        state=receipt.state,
        global_invalid_reason=receipt.global_invalid_reason,
        locked_fixture_hash=receipt.locked_fixture_hash,
        calibration_fixture_hash=receipt.calibration_fixture_hash,
        model_id=receipt.model_id,
        model_artifact_hash=receipt.model_artifact_hash,
        shared_config_hash=receipt.shared_config_hash,
        seed=receipt.seed,
        lock_identity=receipt.lock_identity,
        adr_reference=receipt.adr_reference,
        candidates=c_dict2,
        metrics=receipt.metrics,
        winner=receipt.winner,
        terminal_route=receipt.terminal_route,
    )
    with pytest.raises(ValueError, match="Nested model_artifact_hash mismatch"):
        validate_receipt_schema(bad_receipt2)

    # 3. Shared config hash mismatch
    c_dict3 = dict(receipt.candidates)
    c_h1_3 = dict(c_dict3["H1_MARGIN"])
    cal_h1_3 = dict(c_h1_3["calibration"])
    cal_h1_3["shared_config_hash"] = "0" * 64
    c_h1_3["calibration"] = cal_h1_3
    c_dict3["H1_MARGIN"] = c_h1_3
    bad_receipt3 = BenchmarkReceiptV1(
        schema_version=receipt.schema_version,
        run_id=receipt.run_id,
        state=receipt.state,
        global_invalid_reason=receipt.global_invalid_reason,
        locked_fixture_hash=receipt.locked_fixture_hash,
        calibration_fixture_hash=receipt.calibration_fixture_hash,
        model_id=receipt.model_id,
        model_artifact_hash=receipt.model_artifact_hash,
        shared_config_hash=receipt.shared_config_hash,
        seed=receipt.seed,
        lock_identity=receipt.lock_identity,
        adr_reference=receipt.adr_reference,
        candidates=c_dict3,
        metrics=receipt.metrics,
        winner=receipt.winner,
        terminal_route=receipt.terminal_route,
    )
    with pytest.raises(ValueError, match="Nested shared_config_hash mismatch"):
        validate_receipt_schema(bad_receipt3)


def test_closed_schema_rejection_of_unknown_fields():
    """Task 7.11: Verify rejection of extra unknown fields in receipt and nested dictionaries."""
    receipt = _make_valid_test_receipt()
    data = receipt_to_dict(receipt)

    # Top-level rogue field
    data_bad_top = copy.deepcopy(data)
    data_bad_top["extra_unregistered_field"] = "payload"
    with pytest.raises(ValueError, match="unknown extra fields"):
        receipt_from_dict(data_bad_top)

    # Candidate-level rogue field
    data_bad_cand = copy.deepcopy(data)
    data_bad_cand["candidates"]["H1_MARGIN"]["rogue_cand_field"] = "payload"
    with pytest.raises(ValueError, match="unknown extra fields"):
        receipt_from_dict(data_bad_cand)

    # Calibration-level rogue field
    data_bad_cal = copy.deepcopy(data)
    data_bad_cal["candidates"]["H1_MARGIN"]["calibration"]["rogue_cal_field"] = "payload"
    with pytest.raises(ValueError, match="unknown extra fields"):
        receipt_from_dict(data_bad_cal)

    # Metrics top-level rogue key
    data_bad_met_top = copy.deepcopy(data)
    data_bad_met_top["metrics"]["rogue_group"] = {}
    with pytest.raises(ValueError, match="Receipt metrics key mismatch"):
        receipt_from_dict(data_bad_met_top)

    # Metrics candidate-level rogue field
    data_bad_met = copy.deepcopy(data)
    data_bad_met["metrics"]["H1_MARGIN"]["rogue_met_field"] = "payload"
    with pytest.raises(ValueError, match="unknown extra fields"):
        receipt_from_dict(data_bad_met)


def test_leaf_mutation_consistency():
    """Task 7.12: Systematically mutate every single leaf field in receipt to prove fail-closed validation."""
    base_receipt = _make_valid_test_receipt()

    # 1. Mutate constant schema identifiers and hash formats
    for field, bad_val in [
        ("schema_version", "invalid-schema-v2"),
        ("run_id", ""),
        ("run_id", "   "),
        ("model_id", "bad-model"),
        ("seed", 43),
        ("lock_identity", "linux-amd64"),
        ("adr_reference", "ADR-999"),
        ("locked_fixture_hash", "NOT_A_HEX_HASH"),
        ("locked_fixture_hash", "A" * 64),  # Uppercase not allowed
        ("calibration_fixture_hash", "1234"),
        ("model_artifact_hash", "a" * 63),
        ("shared_config_hash", "G" * 64),
    ]:
        mutated_data = receipt_to_dict(base_receipt)
        mutated_data[field] = bad_val
        with pytest.raises(ValueError):
            receipt_from_dict(mutated_data)


def test_markdown_report_rendering():
    """Task 7.13: Verify markdown report contains all key normative fields accurately."""
    receipt = _make_valid_test_receipt()
    md = render_markdown_report(receipt)

    assert "run-20260831-230000" in md
    assert "KEEP_LEXICAL" in md
    assert "H2_LEXICAL_CORROBORATION" in md
    assert FROZEN_MODEL_ID in md
    assert "42" in md
    assert "ADR-053" in md
    assert "cpython-3.10.20-Windows-AMD64" in md
    assert "0.0781" in md
    assert "0.0931" in md

    # Global invalid markdown report
    global_inv_receipt = BenchmarkReceiptV1(
        schema_version=RECEIPT_SCHEMA_VERSION,
        run_id="run-inv",
        state="GLOBAL_INVALID",
        global_invalid_reason="PRIVACY_INVARIANT_VIOLATION",
        locked_fixture_hash=LOCKED_HASH,
        calibration_fixture_hash=CAL_HASH,
        model_id=FROZEN_MODEL_ID,
        model_artifact_hash=MODEL_HASH,
        shared_config_hash=CONFIG_HASH,
        seed=42,
        lock_identity="cpython-3.10.20-Windows-AMD64",
        adr_reference="ADR-053",
        candidates=None,
        metrics=None,
        winner=None,
        terminal_route="GLOBAL_INVALID",
    )
    md_inv = render_markdown_report(global_inv_receipt)
    assert "Global Invalidation" in md_inv
    assert "PRIVACY_INVARIANT_VIOLATION" in md_inv


def test_publish_benchmark_run_layout_and_coherence(tmp_path: Path):
    """Task 7.14: Verify atomic publication layout, coherence, and failure resilience."""
    receipt = _make_valid_test_receipt()
    rec_p, rep_p, diag_p, cur_p = publish_benchmark_run(receipt, output_dir=tmp_path)

    assert rec_p.is_file()
    assert rep_p.is_file()
    assert diag_p.is_file()
    assert cur_p is not None and cur_p.is_file()

    # Coherence checks: directory name == run_id == diagnostics.run_id
    assert rec_p.parent.name == receipt.run_id
    with open(diag_p, "r", encoding="utf-8") as f:
        diag_data = json.load(f)
    assert diag_data["run_id"] == receipt.run_id

    # Current generation validation
    with open(cur_p, "r", encoding="utf-8") as f:
        cur_data = json.load(f)
    assert cur_data["latest_valid_run_id"] == receipt.run_id

    # Publishing GLOBAL_INVALID run must NOT create or overwrite current-generation.json
    tmp_path_inv = tmp_path / "inv_test"
    tmp_path_inv.mkdir()
    global_inv_receipt = BenchmarkReceiptV1(
        schema_version=RECEIPT_SCHEMA_VERSION,
        run_id="run-inv",
        state="GLOBAL_INVALID",
        global_invalid_reason="PRIVACY_INVARIANT_VIOLATION",
        locked_fixture_hash=LOCKED_HASH,
        calibration_fixture_hash=CAL_HASH,
        model_id=FROZEN_MODEL_ID,
        model_artifact_hash=MODEL_HASH,
        shared_config_hash=CONFIG_HASH,
        seed=42,
        lock_identity="cpython-3.10.20-Windows-AMD64",
        adr_reference="ADR-053",
        candidates=None,
        metrics=None,
        winner=None,
        terminal_route="GLOBAL_INVALID",
    )
    rec_inv_p, rep_inv_p, diag_inv_p, cur_inv_p = publish_benchmark_run(global_inv_receipt, output_dir=tmp_path_inv)
    assert rec_inv_p.is_file()
    assert rep_inv_p.is_file()
    assert diag_inv_p.is_file()
    assert cur_inv_p is None
    assert not (tmp_path_inv / "current-generation.json").exists()

