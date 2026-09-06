"""
Unit tests for WU1: Core Models & Opaque Evidence Interface Isolation.

Tests deep immutability and strict separation of InferenceEvidence from EvaluationTruth.
"""

from dataclasses import FrozenInstanceError
import pytest
from tools.memory_v5_semantic_safety.models import (
    RankedItem,
    InferenceEvidence,
    EvaluationTruth,
    CandidateConfig,
    CandidateResult,
    CalibrationRecord,
    MetricsObject,
    BenchmarkReceiptV1,
    FrozenDict,
)


def test_ranked_item_immutability():
    """RankedItem must be frozen/immutable."""
    item = RankedItem(
        memory_id="mem_001",
        score=0.95,
        rank=1,
        text="Sample memory text",
        user_id="user_123",
        is_private=False,
        is_inactive=False,
    )
    assert item.memory_id == "mem_001"
    assert item.score == 0.95
    with pytest.raises((FrozenInstanceError, AttributeError)):
        item.score = 0.50


def test_inference_evidence_zero_ground_truth_leakage():
    """InferenceEvidence must contain ONLY opaque evidence_key, query, lexical_ranking, semantic_ranking."""
    item_lex = RankedItem("mem_001", 10.5, 1, "text a", "user_1", False, False)
    item_sem = RankedItem("mem_001", 0.88, 1, "text a", "user_1", False, False)

    evidence = InferenceEvidence(
        evidence_key="opaque_hash_12345678",
        query="What is my favorite coffee?",
        lexical_ranking=(item_lex,),
        semantic_ranking=(item_sem,),
    )

    assert evidence.evidence_key == "opaque_hash_12345678"
    assert evidence.query == "What is my favorite coffee?"
    assert len(evidence.lexical_ranking) == 1
    assert len(evidence.semantic_ranking) == 1

    for forbidden_attr in [
        "case_id",
        "family",
        "target_ids",
        "expected_abstain",
        "target_user_id",
        "diagnostic_subtype",
        "is_no_memory",
        "expected_ids",
    ]:
        assert not hasattr(evidence, forbidden_attr), f"InferenceEvidence leaks forbidden attribute: {forbidden_attr}"

    with pytest.raises((FrozenInstanceError, AttributeError)):
        evidence.evidence_key = "mutated"


def test_evaluation_truth_structure():
    """EvaluationTruth contains all scoring truth required by evaluators."""
    truth = EvaluationTruth(
        case_id="tc-no_memory-004",
        family="no_memory",
        target_ids=("target_999",),
        expected_abstain=True,
        target_user_id="user_456",
        diagnostic_subtype="semantic_neighbor_trap",
    )
    assert truth.case_id == "tc-no_memory-004"
    assert truth.family == "no_memory"
    assert truth.target_ids == ("target_999",)
    assert truth.expected_abstain is True
    assert truth.target_user_id == "user_456"
    assert truth.diagnostic_subtype == "semantic_neighbor_trap"

    with pytest.raises((FrozenInstanceError, AttributeError)):
        truth.case_id = "mutated"


def test_deep_immutability_candidate_config():
    """Nested params in CandidateConfig must be deeply immutable and reject in-place mutations."""
    config = CandidateConfig(
        candidate_id="cand_abc123",
        mechanism_key="H1_MARGIN",
        params={
            "theta_score": 0.75,
            "theta_margin": 0.10,
            "nested_dict": {"k": 1},
            "nested_seq": [[{"deep": "val"}]],
        },
    )
    assert isinstance(config.params, FrozenDict)
    assert config.params["theta_score"] == 0.75

    # Top-level attribute reassignment blocked
    with pytest.raises((FrozenInstanceError, AttributeError)):
        config.params = {"theta_score": 0.99}

    # Nested dictionary mutations blocked
    with pytest.raises(TypeError):
        config.params["theta_score"] = 0.99

    with pytest.raises(TypeError):
        del config.params["theta_score"]

    with pytest.raises(TypeError):
        config.params |= {"theta_score": 0.99}

    with pytest.raises(TypeError):
        config.params.setdefault("new_key", 1)

    with pytest.raises(TypeError):
        config.params.pop("theta_score")

    with pytest.raises(TypeError):
        config.params.popitem()

    with pytest.raises(TypeError):
        config.params.clear()

    with pytest.raises(TypeError):
        config.params.update({"theta_score": 0.99})

    with pytest.raises(TypeError):
        config.params["nested_dict"]["k"] = 2

    # Arbitrary depth nesting immutability
    assert isinstance(config.params["nested_seq"][0][0], FrozenDict)
    with pytest.raises(TypeError):
        config.params["nested_seq"][0][0]["deep"] = "mutated"


def test_deep_immutability_calibration_record():
    """Nested selected_params in CalibrationRecord must be deeply immutable."""
    cal = CalibrationRecord(
        candidate_id="cand_abc123",
        mechanism_key="H1_MARGIN",
        selected_params={"theta_score": 0.75, "theta_margin": 0.10},
        calibration_fixture_hash="1d262ab3b3daa275e9bee166e024ae2f91c8d6d8a3fe95b993d2bb06feee9472",
        model_artifact_hash="7cce6bd3df3eefe20757261165563b9f27b2397b5f0661bd42f804d545b07569",
        shared_config_hash="c0d7110f0abe8dfae5e335dc1d931b4742c52e37ea67de296afbb22060912c43",
        frozen_at="2026-08-31T20:00:00Z",
        frozen_sequence=1,
        cal_syn_para_mrr3_mean=0.85,
        cal_hard_recall_at_1=1.0,
        cal_no_memory_abstention_accuracy=1.0,
        cal_no_memory_false_injection_count=0,
        cal_profile_privacy_leakage_count=0,
    )
    assert isinstance(cal.selected_params, FrozenDict)
    with pytest.raises(TypeError):
        cal.selected_params["theta_score"] = 0.99
    with pytest.raises(TypeError):
        cal.selected_params |= {"theta_score": 0.99}


def test_candidate_result_and_receipt_immutability():
    """CandidateResult and BenchmarkReceiptV1 must be frozen and deeply immutable."""
    res = CandidateResult(
        variant_key="cand_abc123",
        execution_status="EVALUABLE",
        execution_reason=None,
        quality_status="QUALIFIED",
        critical_reasons=(),
        calibration=None,
    )
    with pytest.raises((FrozenInstanceError, AttributeError)):
        res.execution_status = "MUTATED"

    receipt = BenchmarkReceiptV1(
        schema_version="memory-semantic-safety-refinement-receipt-v1",
        run_id="run-test",
        state="VALID",
        global_invalid_reason=None,
        locked_fixture_hash="6e96049e76e471549b73d1606e714a888ee1f4ba64729423b6913739db5a435f",
        calibration_fixture_hash="1d262ab3b3daa275e9bee166e024ae2f91c8d6d8a3fe95b993d2bb06feee9472",
        model_id="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        model_artifact_hash="7cce6bd3df3eefe20757261165563b9f27b2397b5f0661bd42f804d545b07569",
        shared_config_hash="c0d7110f0abe8dfae5e335dc1d931b4742c52e37ea67de296afbb22060912c43",
        seed=42,
        lock_identity="cpython-3.10.20-Windows-AMD64",
        adr_reference="ADR-053",
        candidates={"h1": {"status": "QUALIFIED"}},
        metrics={"h1": {"semantic_quality_score": 0.8}},
        winner=None,
        terminal_route="KEEP_LEXICAL",
    )
    assert isinstance(receipt.candidates, FrozenDict)
    with pytest.raises(TypeError):
        receipt.candidates["h1"] = "MUTATED"
    with pytest.raises(TypeError):
        receipt.candidates |= {"h2": {}}
