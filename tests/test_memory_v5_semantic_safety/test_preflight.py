"""
WU0: Preflight and Environment Death-Proof Verification Test Suite.

Verifies:
1. Exact frozen runtime environment identity.
2. Availability of native runtime dependencies (onnxruntime, tokenizers, numpy, pytest).
3. Exact SHA-256 hashes of frozen fixtures, ONNX model artifact, shared config, spec, design, tasks.
4. Historical baseline reproducibility (Baseline A and Reference B scores).
5. Reconciled privacy semantics diagnosis and corrected 5/5 zero-leakage pass proof.
6. Availability of production symbol imports from opencohost.core.memory.memoria_store.
7. Zero modifications to production codebase (opencohost/ unmodified).
"""

import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from tools.memory_v5_semantic_benchmark import (
    _LexicalAdapter,
    compute_metrics_object,
    compute_shared_config_sha256,
    compute_file_sha256,
    compute_model_artifact_sha256,
)

FROZEN_LOCK_IDENTITY = "cpython-3.10.20-Windows-AMD64"
FROZEN_LOCKED_FIXTURE_HASH = "6e96049e76e471549b73d1606e714a888ee1f4ba64729423b6913739db5a435f"
FROZEN_CALIBRATION_FIXTURE_HASH = "1d262ab3b3daa275e9bee166e024ae2f91c8d6d8a3fe95b993d2bb06feee9472"
FROZEN_MODEL_ARTIFACT_HASH = "7cce6bd3df3eefe20757261165563b9f27b2397b5f0661bd42f804d545b07569"
FROZEN_SHARED_CONFIG_HASH = "c0d7110f0abe8dfae5e335dc1d931b4742c52e37ea67de296afbb22060912c43"
FROZEN_SPEC_HASH = "b2da2f55ed15a9907aad51e48c2e787fc9e68a10a5aa82152b67fcc68f924846"
FROZEN_DESIGN_HASH = "49c375553178d1d3c9de10b4f069e16bb734a6e63fa1b0086b04855191d27007"
FROZEN_TASKS_HASH = "5c5bbf0586165b58eb108219364db39683f83d1a36a9706f8edf169cf3c5c67e"

LOCKED_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "memory_v5_semantic_locked.json"
CAL_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "memory_v5_semantic_calibration.json"
MODEL_DIR = REPO_ROOT / "modelos_f5" / "minilm_l12_onnx"
LOCKFILE_PATH = REPO_ROOT / "tools" / "memory_v5_semantic_benchmark.lock"
HISTORICAL_RECEIPT_PATH = REPO_ROOT / "docs" / "memory_v5" / "generations" / "run-20260831-182044" / "receipt.json"
SPEC_PATH = REPO_ROOT / "openspec" / "changes" / "memory-v5-semantic-safety-refinement" / "specs" / "memory-semantic-safety-refinement" / "spec.md"
DESIGN_PATH = REPO_ROOT / "openspec" / "changes" / "memory-v5-semantic-safety-refinement" / "design.md"
TASKS_PATH = REPO_ROOT / "openspec" / "changes" / "memory-v5-semantic-safety-refinement" / "tasks.md"


def test_frozen_runtime_identity():
    """Verify running python environment matches the exact frozen lock identity."""
    actual_impl = sys.implementation.name
    actual_ver = platform.python_version()
    actual_os = platform.system()
    actual_arch = platform.machine()
    actual_identity = f"{actual_impl}-{actual_ver}-{actual_os}-{actual_arch}"

    assert actual_identity == FROZEN_LOCK_IDENTITY, (
        f"Runtime identity mismatch! Expected: {FROZEN_LOCK_IDENTITY}, Actual: {actual_identity}"
    )


def test_semantic_runtime_dependencies_available():
    """Verify native libraries load without error."""
    import onnxruntime as ort
    import tokenizers
    import numpy as np

    assert "CPUExecutionProvider" in ort.get_available_providers()
    assert tokenizers.__version__ is not None
    assert np.__version__ is not None


def test_frozen_hashes():
    """Verify all frozen dataset, model, config, spec, design, and task hashes."""
    assert compute_file_sha256(LOCKED_FIXTURE_PATH) == FROZEN_LOCKED_FIXTURE_HASH
    assert compute_file_sha256(CAL_FIXTURE_PATH) == FROZEN_CALIBRATION_FIXTURE_HASH

    with open(LOCKFILE_PATH, "r", encoding="utf-8") as f:
        lock_data = json.load(f)

    manifest = lock_data.get("model", {}).get("files", [])
    assert compute_model_artifact_sha256(manifest) == FROZEN_MODEL_ARTIFACT_HASH

    shared_cfg = lock_data.get("shared_config", {})
    assert compute_shared_config_sha256(shared_cfg) == FROZEN_SHARED_CONFIG_HASH

    assert compute_file_sha256(SPEC_PATH) == FROZEN_SPEC_HASH
    assert compute_file_sha256(DESIGN_PATH) == FROZEN_DESIGN_HASH
    assert compute_file_sha256(TASKS_PATH) == FROZEN_TASKS_HASH


def test_historical_baseline_reproducibility():
    """Verify Baseline A reproduces exact historical metrics over locked fixture."""
    with open(LOCKED_FIXTURE_PATH, "r", encoding="utf-8") as f:
        locked_data = json.load(f)
    locked_cases = locked_data.get("cases", [])

    adapter_a = _LexicalAdapter()
    a_metrics = compute_metrics_object(adapter_a, locked_cases)

    with open(HISTORICAL_RECEIPT_PATH, "r", encoding="utf-8") as f:
        hist_receipt = json.load(f)
    hist_a = hist_receipt["metrics"]["a"]

    assert a_metrics["hard_correct_at_1_count"] == hist_a["hard_correct_at_1_count"] == 16
    assert a_metrics["hard_recall_at_1"] == hist_a["hard_recall_at_1"] == 1.0
    assert a_metrics["no_memory_abstention_count"] == hist_a["no_memory_abstention_count"] == 12
    assert a_metrics["no_memory_false_injection_count"] == hist_a["no_memory_false_injection_count"] == 0
    assert a_metrics["near_wrong_precision_at_1"] == hist_a["near_wrong_precision_at_1"] == 0.5833333333333334
    assert a_metrics["semantic_quality_score"] == hist_a["semantic_quality_score"] == 0.0


def test_reconciled_privacy_semantics():
    """Verify legacy privacy diagnostic pass count and prove 5/5 zero-leakage passes on real retrieval."""
    with open(HISTORICAL_RECEIPT_PATH, "r", encoding="utf-8") as f:
        hist_receipt = json.load(f)
    legacy_pass_count = hist_receipt["metrics"]["a"]["profile_isolation_pass_count"]
    assert legacy_pass_count == 0

    with open(LOCKED_FIXTURE_PATH, "r", encoding="utf-8") as f:
        locked_data = json.load(f)
    privacy_cases = [c for c in locked_data.get("cases", []) if c.get("family") == "profile_privacy"]
    assert len(privacy_cases) == 5

    adapter_a = _LexicalAdapter()
    passed_isolation_cases = 0
    for case in privacy_cases:
        candidates = case["candidates"]
        prof_id = candidates[0].get("profile_id")
        results = adapter_a.retrieve(case["query"], candidates, profile_id=prof_id, k=3)
        cand_map = {c["id"]: c for c in candidates}
        leakage = 0
        for item in results:
            c_data = cand_map.get(item["id"], {})
            if c_data.get("private", 0) == 1:
                leakage += 1
            elif c_data.get("inactive", 0) == 1:
                leakage += 1
            elif prof_id is not None and c_data.get("profile_id") != prof_id:
                leakage += 1
        if leakage == 0:
            passed_isolation_cases += 1

    assert passed_isolation_cases == 5


def test_production_helpers_import():
    """Verify opencohost production memory helpers are cleanly importable without modification."""
    from opencohost.core.memory.memoria_store import (
        _compute_candidate_idf,
        _significant_tokens,
        select_top_k,
    )
    assert callable(_compute_candidate_idf)
    assert callable(_significant_tokens)
    assert callable(select_top_k)


def test_opencohost_unmodified():
    """Verify git working tree under opencohost/ is clean."""
    res = subprocess.run(
        ["git", "status", "--porcelain", "--", "opencohost"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert res.returncode == 0
    assert res.stdout.strip() == "", f"opencohost/ has modifications: {res.stdout}"
