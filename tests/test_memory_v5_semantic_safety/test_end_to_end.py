"""
End-to-End Pipeline Integration Tests for WU8 (Harness Integrity Enhanced).

Validates:
1. Full end-to-end execution of benchmark pipeline using real ONNX MiniLM, real fixtures, and real lexical adapter:
   - Preparation & Cryptographic Preflight validation
   - Calibration (24 cases) and Freeze
   - Locked evaluation (80 cases)
   - Baseline A reproduction (score=0.0, 16/16, 12/12 0 false, 7/12 near-wrong, 5/5 0 leak)
   - Baseline B reproduction (score=0.8392857, 16/16, 11/12 1 false, CRITICAL_REGRESSION ["NO_MEMORY_INJECTION"])
   - Candidate H1 reproduction (CRITICAL_REGRESSION ["NO_MEMORY_INJECTION"])
   - Candidate H2 reproduction (QUALIFIED, retention=0.093085, gain=0.078125)
   - Candidate H4 reproduction (CRITICAL_REGRESSION ["NO_MEMORY_INJECTION"])
   - Routing: derives winner = "H2_LEXICAL_CORROBORATION", terminal_route = "KEEP_LEXICAL"
   - Receipt publication: receipt.json, report.md, diagnostics.json, current-generation.json
2. Hermetic execution: using tmp_path causes zero mutations under repository docs/memory_v5/.
3. GLOBAL_INVALID scenarios:
   - Shared config invalid -> GLOBAL_INVALID / SHARED_CONFIG_INVALID
   - Runtime lock identity mismatch -> GLOBAL_INVALID / SHARED_CONFIG_INVALID
   - Locked fixture hash mismatch -> GLOBAL_INVALID / LOCKED_FIXTURE_INVALID
   - Calibration fixture hash mismatch -> GLOBAL_INVALID / CALIBRATION_FIXTURE_INVALID
   - Fixture overlap -> GLOBAL_INVALID / FIXTURE_OVERLAP
   - Baseline A invalid -> GLOBAL_INVALID / BASELINE_INVALID
4. Candidate-local Model Failure Scenarios (Fail-closed isolation without GLOBAL_INVALID):
   - Model directory missing -> Candidate NON_EVALUABLE / MODEL_UNAVAILABLE, Baseline A preserved, Baseline B preserves frozen historical reference, terminal_route = INCONCLUSIVE, state = VALID
   - Model file hash corrupted -> Candidate NON_EVALUABLE / MODEL_HASH_MISMATCH, Baseline A preserved, Baseline B preserves frozen historical reference, terminal_route = INCONCLUSIVE, state = VALID
"""

import copy
import json
from pathlib import Path
import pytest

from tools.memory_v5_semantic_safety.receipt import FROZEN_MODEL_ID
from tools.memory_v5_semantic_safety.runner import (
    BenchmarkPipelineCoordinator,
    FROZEN_CALIBRATION_FIXTURE_HASH,
    FROZEN_LOCKED_FIXTURE_HASH,
    _compute_file_sha256,
)


def test_full_pipeline_end_to_end_authoritative_run(tmp_path: Path):
    """Task 8.1: Run complete authoritative benchmark pipeline end-to-end."""
    coordinator = BenchmarkPipelineCoordinator(output_dir=tmp_path)
    receipt, pub_paths = coordinator.execute_pipeline(run_id="test-run-e2e-001")

    # 1. State & Routing
    assert receipt.state == "VALID"
    assert receipt.global_invalid_reason is None
    assert receipt.terminal_route == "KEEP_LEXICAL"
    assert receipt.winner == "H2_LEXICAL_CORROBORATION"
    assert receipt.model_id == FROZEN_MODEL_ID
    assert receipt.seed == 42
    assert receipt.lock_identity == "cpython-3.10.20-Windows-AMD64"
    assert receipt.adr_reference == "ADR-053"

    # 2. Baseline A verification
    m_a = receipt.metrics["baseline_a"]
    assert m_a["hard_recall_at_1"] == 1.0
    assert m_a["semantic_quality_score"] == 0.0
    assert m_a["no_memory_false_injection_count"] == 0
    assert m_a["near_wrong_correct_at_1_count"] == 7
    assert m_a["profile_privacy_leakage_count"] == 0

    # 3. Baseline B verification
    m_b = receipt.metrics["baseline_b"]
    assert m_b["hard_recall_at_1"] == 1.0
    assert pytest.approx(m_b["semantic_quality_score"], rel=1e-6) == 0.8392857142857143
    assert m_b["no_memory_false_injection_count"] == 1
    assert m_b["near_wrong_correct_at_1_count"] == 11
    assert m_b["profile_privacy_leakage_count"] == 0

    # 4. Candidates verification
    c_h1 = receipt.candidates["H1_MARGIN"]
    assert c_h1["quality_status"] == "CRITICAL_REGRESSION"
    assert "NO_MEMORY_INJECTION" in list(c_h1["critical_reasons"])

    c_h2 = receipt.candidates["H2_LEXICAL_CORROBORATION"]
    assert c_h2["quality_status"] == "QUALIFIED"
    assert list(c_h2["critical_reasons"]) == []
    m_h2 = receipt.metrics["H2_LEXICAL_CORROBORATION"]
    assert pytest.approx(m_h2["retention_vs_b"], rel=1e-5) == 0.09308510638297872
    assert pytest.approx(m_h2["gain_over_a"], rel=1e-5) == 0.078125

    c_h4 = receipt.candidates["H4_ASYMMETRIC_GATE"]
    assert c_h4["quality_status"] == "CRITICAL_REGRESSION"
    assert "NO_MEMORY_INJECTION" in list(c_h4["critical_reasons"])

    # 5. Published Artifacts Verification
    receipt_p, report_p, diag_p, cur_p = pub_paths
    assert receipt_p.is_file()
    assert report_p.is_file()
    assert diag_p.is_file()
    assert cur_p is not None and cur_p.is_file()

    with open(receipt_p, "r", encoding="utf-8") as f:
        saved_receipt = json.load(f)
    assert saved_receipt["run_id"] == "test-run-e2e-001"
    assert saved_receipt["terminal_route"] == "KEEP_LEXICAL"

    with open(cur_p, "r", encoding="utf-8") as f:
        saved_cur = json.load(f)
    assert saved_cur["latest_valid_run_id"] == "test-run-e2e-001"


def test_pipeline_global_invalid_on_config_hash_mismatch(tmp_path: Path):
    """Task 8.2: Verify pipeline produces GLOBAL_INVALID when config hash is corrupted."""
    coordinator = BenchmarkPipelineCoordinator(
        output_dir=tmp_path,
        override_config_hash="0000000000000000000000000000000000000000000000000000000000000000",
    )
    receipt, pub_paths = coordinator.execute_pipeline(run_id="test-run-invalid-cfg")

    assert receipt.state == "GLOBAL_INVALID"
    assert receipt.global_invalid_reason == "SHARED_CONFIG_INVALID"
    assert receipt.terminal_route == "GLOBAL_INVALID"
    assert receipt.winner is None
    assert receipt.candidates is None
    assert receipt.metrics is None

    receipt_p, report_p, diag_p, cur_p = pub_paths
    assert receipt_p.is_file()
    assert report_p.is_file()
    assert diag_p.is_file()
    assert cur_p is None


def test_pipeline_global_invalid_on_runtime_lock_identity_mismatch(tmp_path: Path):
    """Task 8.3: Verify pipeline produces GLOBAL_INVALID when runtime lock identity fails."""
    coordinator = BenchmarkPipelineCoordinator(
        output_dir=tmp_path,
        override_lock_identity="cpython-3.11.0-Linux-x86_64",
    )
    receipt, pub_paths = coordinator.execute_pipeline(run_id="test-run-bad-runtime")

    assert receipt.state == "GLOBAL_INVALID"
    assert receipt.global_invalid_reason == "SHARED_CONFIG_INVALID"
    assert receipt.terminal_route == "GLOBAL_INVALID"
    assert receipt.winner is None
    assert receipt.candidates is None
    assert receipt.metrics is None


def test_pipeline_global_invalid_on_locked_fixture_mismatch(tmp_path: Path):
    """Task 8.4: Verify pipeline produces GLOBAL_INVALID when locked fixture hash is corrupted."""
    coordinator = BenchmarkPipelineCoordinator(
        output_dir=tmp_path,
        override_locked_hash="0000000000000000000000000000000000000000000000000000000000000000",
    )
    receipt, pub_paths = coordinator.execute_pipeline(run_id="test-run-invalid-locked")

    assert receipt.state == "GLOBAL_INVALID"
    assert receipt.global_invalid_reason == "LOCKED_FIXTURE_INVALID"
    assert receipt.terminal_route == "GLOBAL_INVALID"
    assert receipt.winner is None
    assert receipt.candidates is None
    assert receipt.metrics is None

    receipt_p, report_p, diag_p, cur_p = pub_paths
    assert receipt_p.is_file()
    assert report_p.is_file()
    assert diag_p.is_file()
    assert cur_p is None


def test_pipeline_global_invalid_on_cal_fixture_mismatch(tmp_path: Path):
    """Task 8.5: Verify pipeline produces GLOBAL_INVALID when calibration fixture hash is corrupted."""
    coordinator = BenchmarkPipelineCoordinator(
        output_dir=tmp_path,
        override_cal_hash="0000000000000000000000000000000000000000000000000000000000000000",
    )
    receipt, pub_paths = coordinator.execute_pipeline(run_id="test-run-invalid-cal")

    assert receipt.state == "GLOBAL_INVALID"
    assert receipt.global_invalid_reason == "CALIBRATION_FIXTURE_INVALID"
    assert receipt.terminal_route == "GLOBAL_INVALID"
    assert receipt.winner is None
    assert receipt.candidates is None
    assert receipt.metrics is None

    receipt_p, report_p, diag_p, cur_p = pub_paths
    assert receipt_p.is_file()
    assert report_p.is_file()
    assert diag_p.is_file()
    assert cur_p is None


def test_pipeline_global_invalid_on_fixture_overlap(tmp_path: Path):
    """Task 8.6: Verify pipeline produces GLOBAL_INVALID / FIXTURE_OVERLAP on contaminated fixture cases."""
    repo_root = Path(__file__).resolve().parents[2]
    real_cal_path = repo_root / "tests" / "fixtures" / "memory_v5_semantic_calibration.json"
    real_locked_path = repo_root / "tests" / "fixtures" / "memory_v5_semantic_locked.json"

    with open(real_cal_path, "r", encoding="utf-8") as f:
        cal_data = json.load(f)

    with open(real_locked_path, "r", encoding="utf-8") as f:
        locked_data = json.load(f)

    # Contaminate: replace locked case 0 query with calibration case 0 query
    bad_locked_data = copy.deepcopy(locked_data)
    bad_locked_data["cases"][0]["query"] = cal_data["cases"][0]["query"]

    bad_locked_path = tmp_path / "bad_locked.json"
    with open(bad_locked_path, "w", encoding="utf-8") as f:
        json.dump(bad_locked_data, f)

    bad_locked_hash = _compute_file_sha256(bad_locked_path)

    coordinator = BenchmarkPipelineCoordinator(
        output_dir=tmp_path,
        override_locked_fixture_path=bad_locked_path,
        override_locked_hash=bad_locked_hash,
    )
    receipt, pub_paths = coordinator.execute_pipeline(run_id="test-run-overlap")

    assert receipt.state == "GLOBAL_INVALID"
    assert receipt.global_invalid_reason == "FIXTURE_OVERLAP"
    assert receipt.terminal_route == "GLOBAL_INVALID"
    assert receipt.winner is None
    assert receipt.candidates is None
    assert receipt.metrics is None

    receipt_p, report_p, diag_p, cur_p = pub_paths
    assert receipt_p.is_file()
    assert report_p.is_file()
    assert diag_p.is_file()
    assert cur_p is None


def test_pipeline_global_invalid_on_baseline_a_failure(tmp_path: Path):
    """Task 8.7: Verify pipeline produces GLOBAL_INVALID / BASELINE_INVALID if Baseline A integrity is broken."""
    coordinator = BenchmarkPipelineCoordinator(
        output_dir=tmp_path,
        synthetic_baseline_a_fail=True,
    )
    receipt, pub_paths = coordinator.execute_pipeline(run_id="test-run-bad-baseline-a")

    assert receipt.state == "GLOBAL_INVALID"
    assert receipt.global_invalid_reason == "BASELINE_INVALID"
    assert receipt.terminal_route == "GLOBAL_INVALID"
    assert receipt.winner is None
    assert receipt.candidates is None
    assert receipt.metrics is None

    receipt_p, report_p, diag_p, cur_p = pub_paths
    assert receipt_p.is_file()
    assert report_p.is_file()
    assert diag_p.is_file()
    assert cur_p is None


def test_pipeline_candidate_local_model_unavailable(tmp_path: Path):
    """Task 8.8: Verify missing model is candidate-local MODEL_UNAVAILABLE, NOT GLOBAL_INVALID."""
    empty_model_dir = tmp_path / "empty_models"
    empty_model_dir.mkdir()

    coordinator = BenchmarkPipelineCoordinator(
        output_dir=tmp_path,
        override_model_dir=empty_model_dir,
    )
    receipt, pub_paths = coordinator.execute_pipeline(run_id="test-run-model-unavail")

    # Receipt remains VALID (not GLOBAL_INVALID) because lexical baseline A succeeds
    assert receipt.state == "VALID"
    assert receipt.global_invalid_reason is None
    assert receipt.terminal_route == "INCONCLUSIVE"
    assert receipt.winner is None

    # Baseline A is evaluated and valid
    m_a = receipt.metrics["baseline_a"]
    assert m_a["hard_recall_at_1"] == 1.0

    # Baseline B reflects the frozen reference baseline (0.8392857...), not arbitrary zeros
    m_b = receipt.metrics["baseline_b"]
    assert m_b["hard_recall_at_1"] == 1.0
    assert pytest.approx(m_b["semantic_quality_score"], rel=1e-6) == 0.8392857142857143

    # Semantic candidates are NON_EVALUABLE with MODEL_UNAVAILABLE
    for cand_key in ("H1_MARGIN", "H2_LEXICAL_CORROBORATION", "H4_ASYMMETRIC_GATE"):
        c = receipt.candidates[cand_key]
        assert c["execution_status"] == "NON_EVALUABLE"
        assert c["execution_reason"] == "MODEL_UNAVAILABLE"
        assert c["quality_status"] == "NOT_APPLICABLE"
        assert receipt.metrics[cand_key] is None


def test_pipeline_candidate_local_model_hash_mismatch(tmp_path: Path):
    """Task 8.9: Verify corrupted model file is candidate-local MODEL_HASH_MISMATCH, NOT GLOBAL_INVALID."""
    corrupted_model_dir = tmp_path / "corrupted_models"
    corrupted_model_dir.mkdir()

    # Create dummy model files with wrong hashes
    (corrupted_model_dir / "config.json").write_text("{}", encoding="utf-8")
    (corrupted_model_dir / "model.onnx").write_bytes(b"corrupted_onnx_bytes")
    (corrupted_model_dir / "tokenizer.json").write_text("{}", encoding="utf-8")

    coordinator = BenchmarkPipelineCoordinator(
        output_dir=tmp_path,
        override_model_dir=corrupted_model_dir,
    )
    receipt, pub_paths = coordinator.execute_pipeline(run_id="test-run-model-mismatch")

    # Receipt remains VALID with INCONCLUSIVE routing
    assert receipt.state == "VALID"
    assert receipt.global_invalid_reason is None
    assert receipt.terminal_route == "INCONCLUSIVE"
    assert receipt.winner is None

    # Baseline B reflects the frozen reference baseline
    m_b = receipt.metrics["baseline_b"]
    assert m_b["hard_recall_at_1"] == 1.0

    # Semantic candidates are NON_EVALUABLE with MODEL_HASH_MISMATCH
    for cand_key in ("H1_MARGIN", "H2_LEXICAL_CORROBORATION", "H4_ASYMMETRIC_GATE"):
        c = receipt.candidates[cand_key]
        assert c["execution_status"] == "NON_EVALUABLE"
        assert c["execution_reason"] == "MODEL_HASH_MISMATCH"
        assert c["quality_status"] == "NOT_APPLICABLE"
        assert receipt.metrics[cand_key] is None


def test_pipeline_zero_repo_docs_mutation_when_custom_output_dir(tmp_path: Path):
    """Task 8.10: Hermetic execution: custom output_dir leaves default repo docs/memory_v5 pristine."""
    repo_docs_dir = Path(__file__).resolve().parents[2] / "docs" / "memory_v5"
    files_before = {}
    if repo_docs_dir.exists():
        files_before = {str(p): p.stat().st_mtime for p in repo_docs_dir.rglob("*") if p.is_file()}

    coordinator = BenchmarkPipelineCoordinator(output_dir=tmp_path)
    receipt, pub_paths = coordinator.execute_pipeline(run_id="test-hermetic-run")
    assert receipt.state == "VALID"

    if repo_docs_dir.exists():
        files_after = {str(p): p.stat().st_mtime for p in repo_docs_dir.rglob("*") if p.is_file()}
        assert set(files_after.keys()) == set(files_before.keys())
        for path_str, mtime in files_before.items():
            assert files_after[path_str] == mtime
