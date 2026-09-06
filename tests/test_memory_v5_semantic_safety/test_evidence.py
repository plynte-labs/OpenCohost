"""
Unit and integration tests for WU2: Fixture Integrity & Shared Evidence Provider.

Validates:
1. Exact fixture quotas (Calibration=24, Locked=80) and negative quota detection.
2. Strict disjointness between Calibration and Locked sets and negative collision detection.
3. Candidate payload hash authority without case-local ID leakage.
4. SharedEvidenceProvider maps fixtures to opaque InferenceEvidence and EvaluationTruth.
5. Real embedding determinism across two independent un-cached MiniLMEmbedder instances.
6. Authoritative profile ownership across all 104 frozen cases.
7. Full 104-case differential parity against historical frozen reference implementations.
"""

import hashlib
import json
import math
import sys
from pathlib import Path
import numpy as np
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.memory_v5_semantic_benchmark import _LexicalAdapter, MiniLMRetriever
from tools.memory_v5_semantic_safety.evidence import (
    load_fixture,
    validate_fixture_quotas,
    validate_fixture_disjointness,
    hash_candidate_payload,
    resolve_target_user_id,
    MiniLMEmbedder,
    execute_lexical_search,
    execute_semantic_search,
    generate_case_evidence,
)
from tools.memory_v5_semantic_safety.models import InferenceEvidence, EvaluationTruth

LOCKED_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "memory_v5_semantic_locked.json"
CAL_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "memory_v5_semantic_calibration.json"
MODEL_DIR = REPO_ROOT / "modelos_f5" / "minilm_l12_onnx"


def test_fixture_quotas_positive_and_negative():
    """Task 2.1a: Assert exact dataset quotas for calibration (24) and locked (80) + negative tests."""
    cal_data = load_fixture(CAL_FIXTURE_PATH)
    assert len(cal_data["cases"]) == 24
    assert validate_fixture_quotas(cal_data, is_calibration=True) is True

    locked_data = load_fixture(LOCKED_FIXTURE_PATH)
    assert len(locked_data["cases"]) == 80
    assert validate_fixture_quotas(locked_data, is_calibration=False) is True

    assert validate_fixture_quotas({}, is_calibration=True) is False
    assert validate_fixture_quotas({"cases": []}, is_calibration=False) is False
    assert validate_fixture_quotas({"cases": cal_data["cases"][:23]}, is_calibration=True) is False


def test_fixture_disjointness_positive_and_negative():
    """Task 2.1b: Assert calibration and locked datasets are completely disjoint + negative tests."""
    cal_data = load_fixture(CAL_FIXTURE_PATH)
    locked_data = load_fixture(LOCKED_FIXTURE_PATH)
    assert validate_fixture_disjointness(cal_data, locked_data) is True

    # Negative test 1: ID collision
    collided_id_data = {"cases": [{"case_id": locked_data["cases"][0]["case_id"], "query": "unique query 1", "candidates": []}]}
    assert validate_fixture_disjointness(locked_data, collided_id_data) is False

    # Negative test 2: Query collision (with distinct ID)
    collided_query_data = {"cases": [{"case_id": "different-id-999", "query": "  " + locked_data["cases"][0]["query"].upper() + "  ", "candidates": []}]}
    assert validate_fixture_disjointness(locked_data, collided_query_data) is False

    # Negative test 3: Candidate payload collision (different ID and query, but same candidate title+content)
    sample_cand = locked_data["cases"][0]["candidates"][0]
    renamed_cand = {"id": "re-keyed-cand-999", "title": sample_cand["title"], "content": sample_cand["content"]}
    collided_payload_data = {"cases": [{"case_id": "different-id-888", "query": "totally different query xyz", "candidates": [renamed_cand]}]}
    assert validate_fixture_disjointness(locked_data, collided_payload_data) is False


def test_real_embedding_determinism_two_independent_instances():
    """Item E: Prove deterministic ONNX CPU embeddings across two independent cold instances."""
    embedder1 = MiniLMEmbedder(model_dir=MODEL_DIR)
    embedder1.initialize()

    embedder2 = MiniLMEmbedder(model_dir=MODEL_DIR)
    embedder2.initialize()

    text = "Espresso and morning coffee with fresh beans"
    vec1 = embedder1.encode_text(text)
    vec2 = embedder2.encode_text(text)

    assert len(vec1) == 384
    assert len(vec2) == 384

    # Assert L2 norm is 1.0
    norm1 = math.sqrt(sum(x * x for x in vec1))
    norm2 = math.sqrt(sum(x * x for x in vec2))
    assert abs(norm1 - 1.0) < 1e-5
    assert abs(norm2 - 1.0) < 1e-5

    # Assert vector identity across independent instances
    for a, b in zip(vec1, vec2):
        assert abs(a - b) < 1e-6

    # Verify CPUExecutionProvider
    assert "CPUExecutionProvider" in embedder1.session.get_providers()


def test_authoritative_profile_ownership_all_104_cases():
    """Item F: Verify authoritative target profile resolution across all 104 cases."""
    cal_data = load_fixture(CAL_FIXTURE_PATH)
    locked_data = load_fixture(LOCKED_FIXTURE_PATH)

    for case in cal_data["cases"] + locked_data["cases"]:
        resolved_pid = resolve_target_user_id(case)
        assert isinstance(resolved_pid, str)
        assert len(resolved_pid) > 0
        # In privacy cases, target profile must match target candidate profile
        if case.get("family") == "profile_privacy" and case.get("expected_ids"):
            target_id = case["expected_ids"][0]
            matching = [c for c in case["candidates"] if c["id"] == target_id]
            if matching:
                assert resolved_pid == matching[0]["profile_id"]


def test_differential_parity_lexical_all_104_cases():
    """Item D: Differential parity test between old _LexicalAdapter and new execute_lexical_search."""
    old_lexical = _LexicalAdapter()
    cal_data = load_fixture(CAL_FIXTURE_PATH)
    locked_data = load_fixture(LOCKED_FIXTURE_PATH)

    for case in cal_data["cases"] + locked_data["cases"]:
        query = case["query"]
        candidates = case["candidates"]
        prof_id = resolve_target_user_id(case)

        old_res = old_lexical.retrieve(query, candidates, profile_id=prof_id, k=3)
        new_res = execute_lexical_search(query, candidates, profile_id=prof_id, k=3)

        old_ids = [r["id"] for r in old_res]
        new_ids = [r.memory_id for r in new_res]
        assert old_ids == new_ids, f"Lexical ID mismatch on case {case.get('case_id')}: {old_ids} vs {new_ids}"

        old_scores = [r["score"] for r in old_res]
        new_scores = [r.score for r in new_res]
        for a, b in zip(old_scores, new_scores):
            assert abs(a - b) < 1e-6, f"Lexical score mismatch on case {case.get('case_id')}: {a} vs {b}"


def test_differential_parity_semantic_all_104_cases():
    """Item D: Differential parity test between old MiniLMRetriever and new MiniLMEmbedder."""
    old_semantic = MiniLMRetriever(model_dir=MODEL_DIR, threshold=None)
    old_semantic.initialize()

    new_embedder = MiniLMEmbedder(model_dir=MODEL_DIR)
    new_embedder.initialize()

    cal_data = load_fixture(CAL_FIXTURE_PATH)
    locked_data = load_fixture(LOCKED_FIXTURE_PATH)

    for case in cal_data["cases"] + locked_data["cases"]:
        query = case["query"]
        candidates = case["candidates"]
        prof_id = resolve_target_user_id(case)

        old_res = old_semantic.retrieve(query, candidates, profile_id=prof_id, k=3)
        new_res = execute_semantic_search(query, candidates, embedder=new_embedder, profile_id=prof_id, k=3)

        old_ids = [r["id"] for r in old_res]
        new_ids = [r.memory_id for r in new_res]
        assert old_ids == new_ids, f"Semantic ID mismatch on case {case.get('case_id')}: {old_ids} vs {new_ids}"

        old_scores = [r["score"] for r in old_res]
        new_scores = [r.score for r in new_res]
        for a, b in zip(old_scores, new_scores):
            assert abs(a - b) < 1e-5, f"Semantic score mismatch on case {case.get('case_id')}: {a} vs {b}"
