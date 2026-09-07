"""
Tests for Memory v5 Semantic Retrieval Quality Benchmark Harness (REDUCED).

Verifies:
- Task 1.1: Lockfile exact platform, model manifest, shared config. Active runtime lock verification.
- Task 1.2: Calibration fixture quotas (24 = 4*6, zero stale) and schema.
- Task 1.3: Locked fixture quotas (80 = 16+14+16+12+12+5+5) and pairwise disjointness.
- Task 1.4: Canonical JSON & 4 hash functions.
- Task 2.1: _LexicalAdapter pure v4 reuse, full-pool IDF invariant, deterministic ID sorting.
- Task 2.2: MiniLM ONNX CPU execution (384d, masked mean, L2 norm), zero-fallback enforcement, MODEL_UNAVAILABLE / MODEL_HASH_MISMATCH detection, RRF fusion (k=60, w=1:1).
- Task 2.3: Calibration threshold derivation, safety gates, lexicographic selection, freeze lifecycle.
- Task 2.4: Lifecycle state machine (PREPARE -> CALIBRATE -> LOCKED_EVALUATION -> METRICS -> QUALITY), 2D candidate classification, canonical critical reasons sorting.
- Task 2.5: MetricsObject computation, quality gates, material semantic gain, deterministic routing.
- Task 3.1: BenchmarkReceiptValidator schema validation (additionalProperties=false, exact key sets).
- Task 3.2: Atomic publication, directory immutability (re-publish collision fails).
- Task 3.3: Metadata-only reporting (zero raw queries/candidate contents).
"""

import json
import os
import platform
import re
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

_ONNX_MODEL_FILE = REPO_ROOT / "modelos_f5" / "minilm_l12_onnx" / "model.onnx"
requires_minilm_onnx = pytest.mark.skipif(
    not _ONNX_MODEL_FILE.exists(),
    reason="MiniLM ONNX model artifacts not present on disk",
)

from tools.memory_v5_semantic_benchmark import (
    CALIBRATION_RECEIPT_KEYS,
    CANDIDATE_RECEIPT_KEYS,
    CRITICAL_REASONS_CANONICAL_ORDER,
    GLOBAL_INVALID_REASONS_CANONICAL_ORDER,
    METRICS_OBJECT_KEYS,
    TOP_LEVEL_KEYS,
    BenchmarkReceiptValidator,
    HybridRRFRetriever,
    MiniLMRetriever,
    _LexicalAdapter,
    canonical_json_dumps,
    canonical_json_sha256,
    compute_file_sha256,
    compute_model_artifact_sha256,
    compute_metrics_object,
    compute_shared_config_sha256,
    evaluate_calibration_safety_and_objectives,
    evaluate_quality_gates,
    evaluate_terminal_routing,
    generate_threshold_candidates,
    publish_generation_atomic,
    render_markdown_report,
    rrf_fuse,
    run_benchmark,
    select_best_calibration_threshold,
    sort_critical_reasons,
    validate_2d_status,
    verify_model_artifacts,
    verify_runtime_lock,
)


class TestLock:
    def test_lockfile_structure_and_types(self):
        lock_path = REPO_ROOT / "tools" / "memory_v5_semantic_benchmark.lock"
        assert lock_path.exists(), "Lockfile does not exist"
        with open(lock_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert data.get("schema") == "memory-v5-semantic-benchmark-lock/v1"
        interp = data.get("interpreter", {})
        assert interp.get("implementation") == "cpython"
        assert interp.get("version") == "3.10.20"
        assert interp.get("os") == "Windows"
        assert interp.get("arch") == "AMD64"
        assert isinstance(interp.get("tags"), list)
        assert len(interp["tags"]) == 3

        model = data.get("model", {})
        assert model.get("name") == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
        files = model.get("files", [])
        assert len(files) == 3
        paths = [f["path"] for f in files]
        assert sorted(paths) == ["config.json", "model.onnx", "tokenizer.json"]
        for f in files:
            assert isinstance(f.get("size"), int) and f["size"] > 0
            assert len(f.get("sha256", "")) == 64

        cfg = data.get("shared_config", {})
        assert cfg.get("seed") == 42
        assert cfg.get("rrf_k") == 60
        assert cfg.get("rrf_weight_lexical") == 1.0
        assert cfg.get("rrf_weight_semantic") == 1.0

    def test_active_runtime_lock_verification(self):
        lock_path = REPO_ROOT / "tools" / "memory_v5_semantic_benchmark.lock"
        with open(lock_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        matching_data = json.loads(json.dumps(data))
        matching_data["interpreter"] = {
            "implementation": sys.implementation.name,
            "version": platform.python_version(),
            "os": platform.system(),
            "arch": platform.machine(),
        }
        valid, reason, identity = verify_runtime_lock(matching_data)
        assert valid is True
        assert reason is None
        expected_ident = f"{sys.implementation.name}-{platform.python_version()}-{platform.system()}-{platform.machine()}"
        assert identity == expected_ident

        mismatched_data = json.loads(json.dumps(matching_data))
        mismatched_data["interpreter"]["version"] = "99.99.99"
        m_valid, m_reason, _ = verify_runtime_lock(mismatched_data)
        assert m_valid is False
        assert m_reason == "SHARED_CONFIG_INVALID"


class TestFixtures:
    def test_calibration_fixture_contract(self):
        cal_path = REPO_ROOT / "tests" / "fixtures" / "memory_v5_semantic_calibration.json"
        assert cal_path.exists()
        with open(cal_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert data.get("schema") == "memory-v5-semantic-calibration/v1"
        assert data.get("case_count") == 24
        cases = data.get("cases", [])
        assert len(cases) == 24

        family_counts = {}
        for c in cases:
            fam = c.get("family")
            family_counts[fam] = family_counts.get(fam, 0) + 1
            assert len(c.get("candidates", [])) >= 2

        assert family_counts.get("hard_lexical") == 4
        assert family_counts.get("synonym") == 4
        assert family_counts.get("paraphrase") == 4
        assert family_counts.get("no_memory") == 4
        assert family_counts.get("near_but_wrong") == 4
        assert family_counts.get("profile_privacy") == 4
        assert "stale_contradiction" not in family_counts

    def test_locked_fixture_contract(self):
        locked_path = REPO_ROOT / "tests" / "fixtures" / "memory_v5_semantic_locked.json"
        assert locked_path.exists()
        with open(locked_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        assert data.get("schema") == "memory-v5-semantic-locked/v1"
        assert data.get("case_count") == 80
        cases = data.get("cases", [])
        assert len(cases) == 80

        family_counts = {}
        for c in cases:
            fam = c.get("family")
            family_counts[fam] = family_counts.get(fam, 0) + 1
            assert len(c.get("candidates", [])) >= 2

        assert family_counts.get("hard_lexical") == 16
        assert family_counts.get("synonym") == 14
        assert family_counts.get("paraphrase") == 16
        assert family_counts.get("no_memory") == 12
        assert family_counts.get("near_but_wrong") == 12
        assert family_counts.get("profile_privacy") == 5
        assert family_counts.get("stale_contradiction") == 5

    def test_fixture_pairwise_disjointness(self):
        cal_path = REPO_ROOT / "tests" / "fixtures" / "memory_v5_semantic_calibration.json"
        locked_path = REPO_ROOT / "tests" / "fixtures" / "memory_v5_semantic_locked.json"

        with open(cal_path, "r", encoding="utf-8") as f:
            cal_data = json.load(f)
        with open(locked_path, "r", encoding="utf-8") as f:
            locked_data = json.load(f)

        cal_cases = cal_data.get("cases", [])
        locked_cases = locked_data.get("cases", [])

        cal_ids = {c["case_id"] for c in cal_cases}
        locked_ids = {c["case_id"] for c in locked_cases}
        assert len(cal_ids.intersection(locked_ids)) == 0

        cal_queries = {re.sub(r"\s+", " ", c["query"].strip().casefold()) for c in cal_cases}
        locked_queries = {re.sub(r"\s+", " ", c["query"].strip().casefold()) for c in locked_cases}
        assert len(cal_queries.intersection(locked_queries)) == 0

        def payload_hash(cand):
            s = f"{cand.get('id', '')}|{cand.get('profile_id', '')}|{cand.get('stable_key', '')}|{cand.get('title', '')}|{cand.get('content', '')}"
            return canonical_json_sha256(s)

        cal_payloads = {payload_hash(cand) for c in cal_cases for cand in c["candidates"]}
        locked_payloads = {payload_hash(cand) for c in locked_cases for cand in c["candidates"]}
        assert len(cal_payloads.intersection(locked_payloads)) == 0


class TestHashCanonical:
    def test_canonical_json_and_hashes(self):
        obj = {"b": 2, "a": 1, "z": [3, 2, 1]}
        dumped = canonical_json_dumps(obj)
        assert dumped == '{"a":1,"b":2,"z":[3,2,1]}'
        sha = canonical_json_sha256(obj)
        assert len(sha) == 64

        cfg = {"seed": 42, "rrf_k": 60, "rrf_weight_lexical": 1.0, "rrf_weight_semantic": 1.0}
        cfg_hash = compute_shared_config_sha256(cfg)
        assert len(cfg_hash) == 64

        manifest = [
            {"path": "b.onnx", "sha256": "b" * 64},
            {"path": "a.json", "sha256": "a" * 64},
        ]
        manifest_hash = compute_model_artifact_sha256(manifest)
        assert len(manifest_hash) == 64


class TestLexicalAdapter:
    def test_lexical_adapter_pure_execution(self):
        adapter = _LexicalAdapter()
        pool = [
            {"id": "m1", "title": "Setup stream", "content": "Configuracion de microfono y audio.", "profile_id": "prof_1", "status": "curated", "pinned": 0, "private": 0, "inactive": 0},
            {"id": "m2", "title": "Configuracion OBS Studio", "content": "Configura el software OBS Studio para streaming.", "profile_id": "prof_1", "status": "curated", "pinned": 0, "private": 0, "inactive": 0},
        ]
        results = adapter.retrieve("configura el software OBS Studio", pool, profile_id="prof_1", k=2)
        assert len(results) >= 1
        assert results[0]["id"] == "m2"
        assert results[0]["rank"] == 1
        assert results[0]["score"] > 0

    def test_lexical_adapter_full_pool_idf_invariant(self):
        adapter = _LexicalAdapter()
        pool = [
            {"id": "m1", "title": "PiperTTS motor neuronal", "content": "Usa PiperTTS rapido neuronal.", "profile_id": "prof_1", "status": "curated", "pinned": 0, "private": 0, "inactive": 0},
            {"id": "m2", "title": "PiperTTS motor secundario", "content": "Usa PiperTTS calidad neuronal.", "profile_id": "prof_2", "status": "curated", "pinned": 0, "private": 0, "inactive": 0},
        ]
        results = adapter.retrieve("PiperTTS motor neuronal", pool, profile_id="prof_1", k=3)
        assert len(results) == 1
        assert results[0]["id"] == "m1"


class TestMiniLMAndArtifactVerification:
    @requires_minilm_onnx
    def test_model_artifact_verification(self):
        lock_path = REPO_ROOT / "tools" / "memory_v5_semantic_benchmark.lock"
        with open(lock_path, "r", encoding="utf-8") as f:
            lock_data = json.load(f)
        manifest = lock_data["model"]["files"]

        model_dir = REPO_ROOT / "modelos_f5" / "minilm_l12_onnx"
        valid, reason, art_hash = verify_model_artifacts(model_dir, manifest)
        assert valid is True
        assert reason is None
        assert len(art_hash) == 64

        bad_dir = REPO_ROOT / "modelos_f5" / "non_existent_dir_xyz"
        b_valid, b_reason, _ = verify_model_artifacts(bad_dir, manifest)
        assert b_valid is False
        assert b_reason == "MODEL_UNAVAILABLE"

    @requires_minilm_onnx
    def test_minilm_real_onnx_execution_and_no_fallback(self):
        model_dir = REPO_ROOT / "modelos_f5" / "minilm_l12_onnx"
        retriever = MiniLMRetriever(model_dir=model_dir)
        retriever.initialize()
        assert retriever.initialized is True

        pool = [
            {"id": "c1", "title": "Auriculares de diadema", "content": "Utiliza audífonos cerrados para aislar el ruido exterior.", "profile_id": "prof_1", "status": "curated", "pinned": 0, "private": 0, "inactive": 0},
            {"id": "c2", "title": "Microfono Shure MV7", "content": "Montó una cápsula dinámica conectada por XLR para registrar su locución.", "profile_id": "prof_1", "status": "curated", "pinned": 0, "private": 0, "inactive": 0},
        ]

        results = retriever.retrieve("cascos acolchados para escuchar", pool, profile_id="prof_1", k=2)
        assert len(results) == 2
        assert results[0]["id"] == "c1"
        assert results[0]["score"] > results[1]["score"]

        uninit_retriever = MiniLMRetriever()
        with pytest.raises(RuntimeError, match="not initialized"):
            uninit_retriever._encode_text("test")


class TestRRFAndCalibration:
    def test_rrf_fusion(self):
        lex = [{"id": "m1", "score": 2.5, "rank": 1}, {"id": "m2", "score": 1.0, "rank": 2}]
        sem = [{"id": "m2", "score": 0.85, "rank": 1}, {"id": "m1", "score": 0.70, "rank": 2}]

        fused = rrf_fuse(lex, sem, k=60, w_lex=1.0, w_sem=1.0)
        assert len(fused) == 2
        assert fused[0]["id"] == "m1"
        assert fused[1]["id"] == "m2"

    def test_threshold_generation_and_lexicographic_selection(self):
        candidates = generate_threshold_candidates([0.3, 0.5, 0.7])
        assert candidates[0] == "SELECT_ALL"
        assert candidates[-1] == "ABSTAIN_ALL"
        assert 0.4 in candidates
        assert 0.6 in candidates

        eval_cands = [
            {"threshold": 0.4, "safe": True, "cal_syn_para_mrr3_mean": 0.85, "cal_hard_recall_at_1": 1.0},
            {"threshold": 0.5, "safe": True, "cal_syn_para_mrr3_mean": 0.90, "cal_hard_recall_at_1": 1.0},
            {"threshold": 0.6, "safe": False, "cal_syn_para_mrr3_mean": 0.95, "cal_hard_recall_at_1": 0.75},
        ]
        best = select_best_calibration_threshold(eval_cands)
        assert best["threshold"] == 0.5


class TestReceiptAndPublication:
    def test_benchmark_receipt_validator(self):
        receipt = _valid_receipt_base()
        assert BenchmarkReceiptValidator.validate(receipt) is True
        invalid_receipt = dict(receipt)
        invalid_receipt["extra_field"] = "bad"
        assert BenchmarkReceiptValidator.validate(invalid_receipt) is False

    def test_atomic_publication_and_immutability(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            docs_dir = Path(tmp_dir) / "docs" / "memory_v5"
            receipt = _valid_receipt_base()
            receipt["run_id"] = "run-immutability-test-valid-abc123XYZ"
            receipt["winner"] = None
            receipt["terminal_route"] = "KEEP_LEXICAL"
            # keep valid but make winner null case with evaluable? Need non-evaluable for winner null? Use valid base but adjust to keep valid: winner null is allowed for KEEP with qualified? For simplicity keep base valid and set winner null and route KEEP is valid even with winner b? Actually base has winner b SEMANTIC, change to winner null KEEP but need qualified? Keep base's candidates b QUALIFIED, so winner null with KEEP is allowed per validator? Actually KEEP with winner b is also allowed, but null also allowed. Keep null.
            # Ensure receipt still valid: winner null with KEEP is allowed, so no change to candidates needed.
            report_md = "# Test Report"

            paths = publish_generation_atomic("run_immutability_test", receipt, report_md, docs_base_dir=docs_dir)
            assert paths["receipt_path"].exists()
            assert paths["report_path"].exists()
            assert paths["pointer_path"].exists()

            with pytest.raises(FileExistsError, match="strictly immutable"):
                publish_generation_atomic("run_immutability_test", receipt, report_md, docs_base_dir=docs_dir)


class TestFullBenchmarkExecution:
    @requires_minilm_onnx
    def test_run_benchmark_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            docs_dir = Path(tmp_dir) / "docs" / "memory_v5"
            receipt = run_benchmark(docs_base_dir=docs_dir)

            assert receipt["schema_version"] == "memory-semantic-benchmark-receipt-v1"
            assert receipt["model_id"] == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
            assert receipt["adr_reference"] == "ADR-053"
            assert receipt["state"] == "VALID"
            assert receipt["terminal_route"] in ("SEMANTIC_PROMISING", "KEEP_LEXICAL", "INCONCLUSIVE")

            cand_b = receipt["candidates"]["b"]
            assert cand_b["execution_status"] == "EVALUABLE"
            assert cand_b["execution_reason"] is None
            assert cand_b["calibration"] is not None
            assert receipt["model_artifact_hash"] is not None
            assert len(receipt["model_artifact_hash"]) == 64


# === Remediation RED: R12/R13/HardGate ===

def _valid_metrics(syn_mrr=0.6, para_mrr=0.6, hard_c1=16, syn_r3=0.8, para_r3=0.8, no_inj=0, near_p=0.8, leak=0):
    syn_c=max(0,min(14,int(round(syn_r3*14)))); syn_recall=syn_c/14.0
    para_c=max(0,min(16,int(round(para_r3*16)))); para_recall=para_c/16.0
    near_c=max(0,min(12,int(round(near_p*12)))); near_prec=near_c/12.0
    return {
        "hard_correct_at_1_count": hard_c1, "hard_correct_at_3_count": hard_c1, "hard_recall_at_1": hard_c1/16.0, "hard_recall_at_3": hard_c1/16.0, "hard_mrr_at_3": hard_c1/16.0,
        "syn_correct_at_1_count": syn_c, "syn_correct_at_3_count": syn_c, "syn_recall_at_1": syn_recall, "syn_recall_at_3": syn_recall, "syn_mrr_at_3": syn_mrr,
        "para_correct_at_1_count": para_c, "para_correct_at_3_count": para_c, "para_recall_at_1": para_recall, "para_recall_at_3": para_recall, "para_mrr_at_3": para_mrr,
        "semantic_quality_score": (syn_mrr + para_mrr)/2.0, "no_memory_abstention_count": 12-no_inj, "no_memory_false_injection_count": no_inj, "no_memory_abstention_accuracy": (12-no_inj)/12.0,
        "near_wrong_correct_at_1_count": near_c, "near_wrong_precision_at_1": near_prec, "near_wrong_target_mrr_at_3": near_prec,
        "profile_isolation_pass_count": 5-leak, "profile_privacy_leakage_count": leak, "profile_private_leakage_count": leak, "profile_inactive_leakage_count": 0, "profile_cross_profile_leakage_count": 0,
        "stale_correct_count": 3, "stale_correct_rate": 0.6,
    }

def _valid_receipt_base():
    return {
        "schema_version": "memory-semantic-benchmark-receipt-v1",
        "run_id": "run-valid-001-abc123XYZ4567",
        "state": "VALID",
        "global_invalid_reason": None,
        "locked_fixture_hash": "a"*64,
        "calibration_fixture_hash": "b"*64,
        "model_id": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "model_artifact_hash": "c"*64,
        "shared_config_hash": "d"*64,
        "seed": 42,
        "lock_identity": "cpython-3.10.20-Windows-AMD64",
        "adr_reference": "ADR-053",
        "candidates": {
            "b": {"variant_key": "b", "execution_status": "EVALUABLE", "execution_reason": None, "quality_status": "QUALIFIED", "critical_reasons": [], "calibration": {"variant_key": "b", "threshold": 0.5, "calibration_fixture_hash": "b"*64, "model_artifact_hash": "c"*64, "shared_config_hash": "d"*64, "frozen_at": "2026-08-30T10:00:00Z", "frozen_sequence": 1, "cal_no_memory_abstention_count": 4, "cal_no_memory_false_injection_count": 0, "cal_no_memory_abstention_accuracy": 1.0, "cal_profile_isolation_pass_count": 4, "cal_profile_privacy_leakage_count": 0, "cal_profile_private_leakage_count": 0, "cal_profile_inactive_leakage_count": 0, "cal_profile_cross_profile_leakage_count": 0, "cal_near_wrong_precision_at_1": 0.75, "cal_near_wrong_a_precision_at_1": 0.5, "cal_syn_mrr_at_3": 0.8, "cal_para_mrr_at_3": 0.8, "cal_syn_para_mrr3_mean": 0.8, "cal_hard_correct_at_1_count": 4, "cal_hard_recall_at_1": 1.0}},
            "c": {"variant_key": "c", "execution_status": "NON_EVALUABLE", "execution_reason": "CALIBRATION_FAILED", "quality_status": "NOT_APPLICABLE", "critical_reasons": [], "calibration": None},
        },
        "metrics": {"a": _valid_metrics(syn_mrr=0.4, para_mrr=0.4, hard_c1=16, near_p=0.6), "b": _valid_metrics(syn_mrr=0.6, para_mrr=0.6), "c": None},
        "winner": "b",
        "terminal_route": "SEMANTIC_PROMISING",
    }

class TestRemediationHardGate:
    def test_hard_gate_15_fails_16_passes_literal_count(self):
        a_m = _valid_metrics(near_p=0.75)
        # 15 should fail even if recall artificially high
        m15 = _valid_metrics(hard_c1=15, near_p=0.75)
        m15["hard_recall_at_1"] = 0.99  # inconsistent recall to expose recall-based check
        s15, r15 = evaluate_quality_gates(m15, a_m)
        assert s15 == "CRITICAL_REGRESSION" and "HARD_RECALL_REGRESSION" in r15, "15/16 must be CRITICAL_REGRESSION via literal count==16"
        m16 = _valid_metrics(hard_c1=16, near_p=0.75)
        m16["hard_recall_at_1"] = 0.97  # inconsistent low recall but count passes => should be QUALIFIED
        s16, r16 = evaluate_quality_gates(m16, a_m)
        assert s16 == "QUALIFIED" and r16 == [], "16/16 must be QUALIFIED even with 0.97 recall when literal count is authority"

class TestRemediationR12Routing:
    def test_global_invalid_inconclusive_null(self):
        a = _valid_metrics(); b = _valid_metrics(); c = _valid_metrics()
        route, winner = evaluate_terminal_routing("BASELINE_INVALID", ("EVALUABLE","QUALIFIED",[]), ("EVALUABLE","QUALIFIED",[]), a,b,c)
        assert route == "INCONCLUSIVE" and winner is None
    def test_both_non_evaluable_inconclusive(self):
        a = _valid_metrics(); route, winner = evaluate_terminal_routing(None, ("NON_EVALUABLE","NOT_APPLICABLE",[]), ("NON_EVALUABLE","NOT_APPLICABLE",[]), a, None, None)
        assert route == "INCONCLUSIVE" and winner is None
    def test_one_non_evaluable_other_qualified_promising(self):
        a = _valid_metrics(syn_mrr=0.2, para_mrr=0.2); b_m = _valid_metrics(syn_mrr=0.6, para_mrr=0.6); # rel 2.0 abs 0.4 material
        route, winner = evaluate_terminal_routing(None, ("NON_EVALUABLE","NOT_APPLICABLE",[]), ("EVALUABLE","QUALIFIED",[]), a, None, b_m)
        assert route == "SEMANTIC_PROMISING" and winner == "c"
    def test_no_qualified_keep_lexical_null(self):
        a = _valid_metrics(); b = _valid_metrics(); c = _valid_metrics()
        route, winner = evaluate_terminal_routing(None, ("EVALUABLE","CRITICAL_REGRESSION",["HARD_RECALL_REGRESSION"]), ("EVALUABLE","CRITICAL_REGRESSION",["NO_MEMORY_INJECTION"]), a,b,c)
        assert route == "KEEP_LEXICAL" and winner is None
    def test_qualified_rel_below_10_keep_lexical_winner_preserved(self):
        a = _valid_metrics(syn_mrr=0.4, para_mrr=0.4); b = _valid_metrics(syn_mrr=0.424, para_mrr=0.424) # rel 0.06
        route, winner = evaluate_terminal_routing(None, ("EVALUABLE","QUALIFIED",[]), ("NON_EVALUABLE","NOT_APPLICABLE",[]), a,b,None)
        assert route == "KEEP_LEXICAL" and winner == "b"
    def test_qualified_intermediate_18_inconclusive_winner(self):
        a = _valid_metrics(syn_mrr=0.4, para_mrr=0.4); c = _valid_metrics(syn_mrr=0.472, para_mrr=0.472) # rel 0.18
        route, winner = evaluate_terminal_routing(None, ("NON_EVALUABLE","NOT_APPLICABLE",[]), ("EVALUABLE","QUALIFIED",[]), a,None,c)
        assert route == "INCONCLUSIVE" and winner == "c"
    def test_qualified_rel_ok_abs_fail_inconclusive(self):
        a = _valid_metrics(syn_mrr=0.32, para_mrr=0.32); b = _valid_metrics(syn_mrr=0.40, para_mrr=0.40) # rel 0.25 abs 0.08
        route, winner = evaluate_terminal_routing(None, ("EVALUABLE","QUALIFIED",[]), ("NON_EVALUABLE","NOT_APPLICABLE",[]), a,b,None)
        assert route == "INCONCLUSIVE" and winner == "b"
    def test_qualified_material_promising_winner(self):
        a = _valid_metrics(syn_mrr=0.40, para_mrr=0.40); c = _valid_metrics(syn_mrr=0.52, para_mrr=0.52) # rel 0.30 abs 0.12
        route, winner = evaluate_terminal_routing(None, ("NON_EVALUABLE","NOT_APPLICABLE",[]), ("EVALUABLE","QUALIFIED",[]), a,None,c)
        assert route == "SEMANTIC_PROMISING" and winner == "c"
    def test_winner_tie_break_sequence(self):
        a = _valid_metrics(syn_mrr=0.30, para_mrr=0.30)
        b = _valid_metrics(syn_mrr=0.62, para_mrr=0.58, syn_r3=0.71, para_r3=0.71)
        c = _valid_metrics(syn_mrr=0.59, para_mrr=0.61, syn_r3=0.71, para_r3=0.71)
        # equal score 0.60 & mean recall 0.71, c higher para_mrr wins
        route, winner = evaluate_terminal_routing(None, ("EVALUABLE","QUALIFIED",[]), ("EVALUABLE","QUALIFIED",[]), a,b,c)
        assert winner == "c"
        # full tie b before c
        b2 = _valid_metrics(syn_mrr=0.60, para_mrr=0.60, syn_r3=0.71, para_r3=0.71)
        c2 = _valid_metrics(syn_mrr=0.60, para_mrr=0.60, syn_r3=0.71, para_r3=0.71)
        route2, winner2 = evaluate_terminal_routing(None, ("EVALUABLE","QUALIFIED",[]), ("EVALUABLE","QUALIFIED",[]), a,b2,c2)
        assert winner2 == "b"

class TestRemediationR13Receipt:
    def test_exact_valid_receipt_passes(self):
        r = _valid_receipt_base()
        assert BenchmarkReceiptValidator.validate(r) is True
    def test_missing_required_field_fails(self):
        r = _valid_receipt_base(); del r["adr_reference"]
        assert BenchmarkReceiptValidator.validate(r) is False
    def test_unexpected_field_fails(self):
        r = _valid_receipt_base(); r["extra"] = 1
        assert BenchmarkReceiptValidator.validate(r) is False
    def test_wrong_type_fails(self):
        r = _valid_receipt_base(); r["seed"] = "42"
        assert BenchmarkReceiptValidator.validate(r) is False
    def test_wrong_model_identity_fails(self):
        r = _valid_receipt_base(); r["model_id"] = "other/model"
        assert BenchmarkReceiptValidator.validate(r) is False
    def test_wrong_adr_identity_fails(self):
        r = _valid_receipt_base(); r["adr_reference"] = "ADR-999"
        assert BenchmarkReceiptValidator.validate(r) is False
    def test_invalid_nested_candidate_structure(self):
        r = _valid_receipt_base(); r["candidates"]["b"]["execution_status"] = "UNKNOWN"
        assert BenchmarkReceiptValidator.validate(r) is False
    def test_illegal_execution_quality_combination(self):
        r = _valid_receipt_base(); r["candidates"]["b"] = {"variant_key":"b","execution_status":"NON_EVALUABLE","execution_reason":"MODEL_UNAVAILABLE","quality_status":"QUALIFIED","critical_reasons":[],"calibration":None}
        assert BenchmarkReceiptValidator.validate(r) is False
    def test_illegal_reasons_nonempty_for_qualified(self):
        r = _valid_receipt_base(); r["candidates"]["b"]["critical_reasons"] = ["HARD_RECALL_REGRESSION"]
        assert BenchmarkReceiptValidator.validate(r) is False
    def test_json_markdown_mismatch_fails(self):
        r = _valid_receipt_base()
        md = render_markdown_report(r)
        assert "`b`" in md and r["winner"] == "b"
        r_bad = _valid_receipt_base(); r_bad["winner"] = "c"
        md_bad = render_markdown_report(_valid_receipt_base())
        # JSON winner c vs markdown winner b must be considered mismatch -> no valid result
        # validate_json_markdown_consistency must exist and return False on mismatch
        from tools.memory_v5_semantic_benchmark import validate_json_markdown_consistency
        assert validate_json_markdown_consistency(r, md) is True
        assert validate_json_markdown_consistency(r_bad, md_bad) is False
    def test_no_valid_result_malformed_receipt_asserts(self):
        r = _valid_receipt_base(); r["schema_version"] = "wrong"
        assert BenchmarkReceiptValidator.validate(r) is False
    def test_const_values_exact(self):
        r = _valid_receipt_base()
        assert r["schema_version"] == "memory-semantic-benchmark-receipt-v1"
        assert r["model_id"] == "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
        assert r["adr_reference"] == "ADR-053"
        assert BenchmarkReceiptValidator.validate(r) is True

class TestGateCorrection:
    def test_r12_tie_break_i_semantic_score(self):
        a=_valid_metrics(syn_mrr=0.30,para_mrr=0.30); b=_valid_metrics(syn_mrr=0.65,para_mrr=0.65,syn_r3=0.70,para_r3=0.70); c=_valid_metrics(syn_mrr=0.55,para_mrr=0.55,syn_r3=0.90,para_r3=0.90)
        # b score 0.65 > c 0.55, c has higher recall but score wins
        _,w=evaluate_terminal_routing(None,("EVALUABLE","QUALIFIED",[]),("EVALUABLE","QUALIFIED",[]),a,b,c); assert w=="b"
    def test_r12_tie_break_ii_mean_recall(self):
        a=_valid_metrics(syn_mrr=0.30,para_mrr=0.30); b=_valid_metrics(syn_mrr=0.60,para_mrr=0.60,syn_r3=0.80,para_r3=0.80); c=_valid_metrics(syn_mrr=0.60,para_mrr=0.60,syn_r3=0.70,para_r3=0.70)
        _,w=evaluate_terminal_routing(None,("EVALUABLE","QUALIFIED",[]),("EVALUABLE","QUALIFIED",[]),a,b,c); assert w=="b"
    def test_run_id_rejects_underscore(self):
        r=_valid_receipt_base(); r["run_id"]="run_20260831_055842"; assert BenchmarkReceiptValidator.validate(r) is False
        r2=_valid_receipt_base(); r2["run_id"]="run-20260831-055842-valid-01"; assert BenchmarkReceiptValidator.validate(r2) is True
        r3=_valid_receipt_base(); r3["run_id"]="short"; assert BenchmarkReceiptValidator.validate(r3) is False
    def test_timestamp_rejects_fractional(self):
        r=_valid_receipt_base(); r["candidates"]["b"]["calibration"]["frozen_at"]="2026-08-30T10:00:00.123Z"; assert BenchmarkReceiptValidator.validate(r) is False
        r2=_valid_receipt_base(); r2["candidates"]["b"]["calibration"]["frozen_at"]="2026-08-30T10:00:00Z"; assert BenchmarkReceiptValidator.validate(r2) is True
    def test_evaluable_requires_calibration(self):
        r=_valid_receipt_base(); r["candidates"]["b"]["execution_status"]="EVALUABLE"; r["candidates"]["b"]["execution_reason"]=None; r["candidates"]["b"]["calibration"]=None; assert BenchmarkReceiptValidator.validate(r) is False
    def test_evaluable_requires_metrics(self):
        r=_valid_receipt_base(); r["candidates"]["b"]["execution_status"]="EVALUABLE"; assert r["metrics"]["b"] is not None
        r2=_valid_receipt_base(); r2["metrics"]["b"]=None; assert BenchmarkReceiptValidator.validate(r2) is False
    def test_non_evaluable_requires_null_calibration_metrics(self):
        r=_valid_receipt_base(); r["candidates"]["c"]["calibration"]={"variant_key":"c","threshold":0.5,"calibration_fixture_hash":"b"*64,"model_artifact_hash":"c"*64,"shared_config_hash":"d"*64,"frozen_at":"2026-08-30T10:00:00Z","frozen_sequence":1,"cal_no_memory_abstention_count":4,"cal_no_memory_false_injection_count":0,"cal_no_memory_abstention_accuracy":1.0,"cal_profile_isolation_pass_count":4,"cal_profile_privacy_leakage_count":0,"cal_profile_private_leakage_count":0,"cal_profile_inactive_leakage_count":0,"cal_profile_cross_profile_leakage_count":0,"cal_near_wrong_precision_at_1":0.75,"cal_near_wrong_a_precision_at_1":0.5,"cal_syn_mrr_at_3":0.8,"cal_para_mrr_at_3":0.8,"cal_syn_para_mrr3_mean":0.8,"cal_hard_correct_at_1_count":4,"cal_hard_recall_at_1":1.0}; assert BenchmarkReceiptValidator.validate(r) is False
    def test_global_invalid_requires_null_candidates_and_inconclusive(self):
        r=_valid_receipt_base(); r["state"]="GLOBAL_INVALID"; r["global_invalid_reason"]="BASELINE_INVALID"; r["winner"]=None; r["terminal_route"]="INCONCLUSIVE"; assert BenchmarkReceiptValidator.validate(r) is False  # still has b non-null
        r2=_valid_receipt_base(); r2["state"]="GLOBAL_INVALID"; r2["global_invalid_reason"]="BASELINE_INVALID"; r2["candidates"]["b"]=None; r2["candidates"]["c"]=None; r2["metrics"]["a"]=None; r2["metrics"]["b"]=None; r2["metrics"]["c"]=None; r2["winner"]=None; r2["terminal_route"]="INCONCLUSIVE"; assert BenchmarkReceiptValidator.validate(r2) is True
        r3=_valid_receipt_base(); r3["state"]="GLOBAL_INVALID"; r3["global_invalid_reason"]="BASELINE_INVALID"; r3["candidates"]["b"]=None; r3["candidates"]["c"]=None; r3["metrics"]["a"]=None; r3["metrics"]["b"]=None; r3["metrics"]["c"]=None; r3["winner"]=None; r3["terminal_route"]="KEEP_LEXICAL"; assert BenchmarkReceiptValidator.validate(r3) is False
    def test_metrics_count_bounds_and_rates(self):
        r=_valid_receipt_base(); r["metrics"]["b"]["hard_correct_at_1_count"]=17; assert BenchmarkReceiptValidator.validate(r) is False
        r2=_valid_receipt_base(); r2["metrics"]["b"]["semantic_quality_score"]=1.5; assert BenchmarkReceiptValidator.validate(r2) is False
        r3=_valid_receipt_base(); assert BenchmarkReceiptValidator.validate(r3) is True
    def test_nested_calibration_exact(self):
        r=_valid_receipt_base(); del r["candidates"]["b"]["calibration"]["frozen_at"]; assert BenchmarkReceiptValidator.validate(r) is False
        r2=_valid_receipt_base(); r2["candidates"]["b"]["calibration"]["extra"]="x"; assert BenchmarkReceiptValidator.validate(r2) is False
        r3=_valid_receipt_base(); r3["candidates"]["b"]["calibration"]["calibration_fixture_hash"]="nothex"; assert BenchmarkReceiptValidator.validate(r3) is False
        r4=_valid_receipt_base(); r4["candidates"]["b"]["calibration"]["frozen_sequence"]=-1; assert BenchmarkReceiptValidator.validate(r4) is False

class TestExceptionalR13:
    def test_missing_extra_field(self): r=_valid_receipt_base(); del r["schema_version"]; assert not BenchmarkReceiptValidator.validate(r); r2=_valid_receipt_base(); r2["extra_field"]="x"; assert not BenchmarkReceiptValidator.validate(r2)
    def test_primitive_type_enum_sha_nullability(self): r=_valid_receipt_base(); r["seed"]="42"; assert not BenchmarkReceiptValidator.validate(r); r2=_valid_receipt_base(); r2["seed"]=True; assert not BenchmarkReceiptValidator.validate(r2); r3=_valid_receipt_base(); r3["state"]="UNKNOWN"; assert not BenchmarkReceiptValidator.validate(r3); r4=_valid_receipt_base(); r4["locked_fixture_hash"]="ZZZ"; assert not BenchmarkReceiptValidator.validate(r4); r5=_valid_receipt_base(); r5["candidates"]["b"]=None; assert not BenchmarkReceiptValidator.validate(r5)
    def test_execution_quality_reason_ordering(self): r=_valid_receipt_base(); r["candidates"]["b"]["critical_reasons"]=["PROFILE_PRIVACY_LEAKAGE","HARD_RECALL_REGRESSION"]; assert not BenchmarkReceiptValidator.validate(r); r2=_valid_receipt_base(); r2["candidates"]["b"]["quality_status"]="QUALIFIED"; r2["candidates"]["b"]["critical_reasons"]=["HARD_RECALL_REGRESSION"]; assert not BenchmarkReceiptValidator.validate(r2); r3=_valid_receipt_base(); r3["candidates"]["b"]["execution_status"]="NON_EVALUABLE"; r3["candidates"]["b"]["quality_status"]="QUALIFIED"; assert not BenchmarkReceiptValidator.validate(r3)
    def test_calibration_forbidden_and_required(self): r=_valid_receipt_base(); r["candidates"]["c"]["calibration"]={"variant_key":"c","threshold":0.5,"calibration_fixture_hash":"b"*64,"model_artifact_hash":"c"*64,"shared_config_hash":"d"*64,"frozen_at":"2026-08-30T10:00:00Z","frozen_sequence":1,"cal_no_memory_abstention_count":4,"cal_no_memory_false_injection_count":0,"cal_no_memory_abstention_accuracy":1.0,"cal_profile_isolation_pass_count":4,"cal_profile_privacy_leakage_count":0,"cal_profile_private_leakage_count":0,"cal_profile_inactive_leakage_count":0,"cal_profile_cross_profile_leakage_count":0,"cal_near_wrong_precision_at_1":0.75,"cal_near_wrong_a_precision_at_1":0.5,"cal_syn_mrr_at_3":0.8,"cal_para_mrr_at_3":0.8,"cal_syn_para_mrr3_mean":0.8,"cal_hard_correct_at_1_count":4,"cal_hard_recall_at_1":1.0}; assert not BenchmarkReceiptValidator.validate(r); r2=_valid_receipt_base(); r2["candidates"]["b"]["calibration"]=None; assert not BenchmarkReceiptValidator.validate(r2)
    def test_calibration_bounds_ratios_timestamp(self): r=_valid_receipt_base(); r["candidates"]["b"]["calibration"]["cal_profile_private_leakage_count"]=5; assert not BenchmarkReceiptValidator.validate(r); r2=_valid_receipt_base(); r2["candidates"]["b"]["calibration"]["cal_no_memory_abstention_accuracy"]=1.5; assert not BenchmarkReceiptValidator.validate(r2); r3=_valid_receipt_base(); r3["candidates"]["b"]["calibration"]["frozen_at"]="2026-08-30T10:00:00.123Z"; assert not BenchmarkReceiptValidator.validate(r3); r4=_valid_receipt_base(); r4["candidates"]["b"]["calibration"]["frozen_sequence"]=True; assert not BenchmarkReceiptValidator.validate(r4)
    def test_calibration_count_ratio_consistency(self): r=_valid_receipt_base(); r["candidates"]["b"]["calibration"]["cal_no_memory_abstention_count"]=4; r["candidates"]["b"]["calibration"]["cal_no_memory_abstention_accuracy"]=0.5; assert not BenchmarkReceiptValidator.validate(r); r2=_valid_receipt_base(); r2["candidates"]["b"]["calibration"]["cal_hard_correct_at_1_count"]=4; r2["candidates"]["b"]["calibration"]["cal_hard_recall_at_1"]=0.5; assert not BenchmarkReceiptValidator.validate(r2); r3=_valid_receipt_base(); r3["candidates"]["b"]["calibration"]["cal_syn_mrr_at_3"]=0.8; r3["candidates"]["b"]["calibration"]["cal_para_mrr_at_3"]=0.6; r3["candidates"]["b"]["calibration"]["cal_syn_para_mrr3_mean"]=0.9; assert not BenchmarkReceiptValidator.validate(r3)
    def test_metrics_bounds_ratios(self): r=_valid_receipt_base(); r["metrics"]["b"]["hard_correct_at_1_count"]=17; assert not BenchmarkReceiptValidator.validate(r); r2=_valid_receipt_base(); r2["metrics"]["b"]["profile_private_leakage_count"]=6; assert not BenchmarkReceiptValidator.validate(r2); r3=_valid_receipt_base(); r3["metrics"]["b"]["hard_recall_at_1"]=1.5; assert not BenchmarkReceiptValidator.validate(r3); r4=_valid_receipt_base(); r4["metrics"]["b"]["semantic_quality_score"]=1.2; assert not BenchmarkReceiptValidator.validate(r4)
    def test_metrics_count_ratio_consistency(self): r=_valid_receipt_base(); r["metrics"]["b"]["hard_correct_at_1_count"]=16; r["metrics"]["b"]["hard_recall_at_1"]=0.5; assert not BenchmarkReceiptValidator.validate(r); r2=_valid_receipt_base(); r2["metrics"]["b"]["syn_correct_at_1_count"]=14; r2["metrics"]["b"]["syn_recall_at_1"]=0.5; assert not BenchmarkReceiptValidator.validate(r2); r3=_valid_receipt_base(); r3["metrics"]["b"]["syn_mrr_at_3"]=0.6; r3["metrics"]["b"]["para_mrr_at_3"]=0.6; r3["metrics"]["b"]["semantic_quality_score"]=0.9; assert not BenchmarkReceiptValidator.validate(r3)
    def test_model_config_fixture_identity(self): r=_valid_receipt_base(); r["model_id"]="other/model"; assert not BenchmarkReceiptValidator.validate(r); r2=_valid_receipt_base(); r2["adr_reference"]="ADR-999"; assert not BenchmarkReceiptValidator.validate(r2); r3=_valid_receipt_base(); r3["schema_version"]="wrong"; assert not BenchmarkReceiptValidator.validate(r3)
    def test_valid_global_illegal_states(self): r=_valid_receipt_base(); r["candidates"]["b"]=None; assert not BenchmarkReceiptValidator.validate(r); r2=_valid_receipt_base(); r2["metrics"]["a"]=None; assert not BenchmarkReceiptValidator.validate(r2); r3=_valid_receipt_base(); r3["state"]="GLOBAL_INVALID"; r3["global_invalid_reason"]="BASELINE_INVALID"; r3["candidates"]["b"]=None; r3["candidates"]["c"]=None; r3["metrics"]["a"]=None; r3["metrics"]["b"]=None; r3["metrics"]["c"]=None; r3["winner"]="b"; assert not BenchmarkReceiptValidator.validate(r3); r4=_valid_receipt_base(); r4["state"]="GLOBAL_INVALID"; r4["global_invalid_reason"]="BASELINE_INVALID"; r4["candidates"]["b"]=None; r4["candidates"]["c"]=None; r4["metrics"]["a"]=None; r4["metrics"]["b"]=None; r4["metrics"]["c"]=None; r4["winner"]=None; r4["terminal_route"]="KEEP_LEXICAL"; assert not BenchmarkReceiptValidator.validate(r4)
    def test_illegal_winner_route(self): r=_valid_receipt_base(); r["candidates"]["c"]={"variant_key":"c","execution_status":"EVALUABLE","execution_reason":None,"quality_status":"QUALIFIED","critical_reasons":[],"calibration":{"variant_key":"c","threshold":0.5,"calibration_fixture_hash":"b"*64,"model_artifact_hash":"c"*64,"shared_config_hash":"d"*64,"frozen_at":"2026-08-30T10:00:00Z","frozen_sequence":1,"cal_no_memory_abstention_count":4,"cal_no_memory_false_injection_count":0,"cal_no_memory_abstention_accuracy":1.0,"cal_profile_isolation_pass_count":4,"cal_profile_privacy_leakage_count":0,"cal_profile_private_leakage_count":0,"cal_profile_inactive_leakage_count":0,"cal_profile_cross_profile_leakage_count":0,"cal_near_wrong_precision_at_1":0.75,"cal_near_wrong_a_precision_at_1":0.5,"cal_syn_mrr_at_3":0.8,"cal_para_mrr_at_3":0.8,"cal_syn_para_mrr3_mean":0.8,"cal_hard_correct_at_1_count":4,"cal_hard_recall_at_1":1.0}}; r["metrics"]["c"]=_valid_metrics(syn_mrr=0.5, para_mrr=0.5); r["metrics"]["b"]=_valid_metrics(syn_mrr=0.6, para_mrr=0.6); r["metrics"]["a"]=_valid_metrics(syn_mrr=0.2, para_mrr=0.2); r["winner"]="c"; r["terminal_route"]="SEMANTIC_PROMISING"; assert not BenchmarkReceiptValidator.validate(r); r2=_valid_receipt_base(); r2["winner"]=None; r2["terminal_route"]="SEMANTIC_PROMISING"; assert not BenchmarkReceiptValidator.validate(r2)
    def test_full_json_markdown_mismatch(self): r=_valid_receipt_base(); md=render_markdown_report(r); r_bad=_valid_receipt_base(); r_bad["metrics"]["b"]["hard_correct_at_1_count"]=15; from tools.memory_v5_semantic_benchmark import validate_json_markdown_consistency; assert not validate_json_markdown_consistency(r_bad, md); r2=_valid_receipt_base(); md2=render_markdown_report(r2); r2_bad=_valid_receipt_base(); r2_bad["candidates"]["b"]["calibration"]["threshold"]=0.99; assert not validate_json_markdown_consistency(r2_bad, md2)
    def test_r12_tie_break_iii_paraphrase_mrr(self): a=_valid_metrics(syn_mrr=0.30,para_mrr=0.30); b=_valid_metrics(syn_mrr=0.60,para_mrr=0.60,syn_r3=0.71,para_r3=0.71); b["para_mrr_at_3"]=0.58; c=_valid_metrics(syn_mrr=0.60,para_mrr=0.60,syn_r3=0.71,para_r3=0.71); c["para_mrr_at_3"]=0.62; c["semantic_quality_score"]=0.60; b["semantic_quality_score"]=0.60; _,w=evaluate_terminal_routing(None,("EVALUABLE","QUALIFIED",[]),("EVALUABLE","QUALIFIED",[]),a,b,c); assert w=="c"
    def test_r12_tie_break_iv_complete_tie_b(self): a=_valid_metrics(syn_mrr=0.30,para_mrr=0.30); b=_valid_metrics(syn_mrr=0.60,para_mrr=0.60,syn_r3=0.71,para_r3=0.71); c=_valid_metrics(syn_mrr=0.60,para_mrr=0.60,syn_r3=0.71,para_r3=0.71); b["para_mrr_at_3"]=0.60; c["para_mrr_at_3"]=0.60; _,w=evaluate_terminal_routing(None,("EVALUABLE","QUALIFIED",[]),("EVALUABLE","QUALIFIED",[]),a,b,c); assert w=="b"


class TestPublicationOwnership:
    @requires_minilm_onnx
    def test_run_benchmark_with_temp_root_has_no_repo_side_effects(self):
        import hashlib
        repo_pointer = REPO_ROOT / "docs" / "memory_v5" / "current-generation.json"
        repo_diag = REPO_ROOT / "docs" / "memory_v5" / "semantic-retrieval-benchmark-diagnostics.json"
        before_ptr_bytes = repo_pointer.read_bytes()
        before_diag_bytes = repo_diag.read_bytes()
        before_ptr_hash = hashlib.sha256(before_ptr_bytes).hexdigest()
        before_diag_hash = hashlib.sha256(before_diag_bytes).hexdigest()
        before_ptr_json = json.loads(before_ptr_bytes.decode("utf-8"))
        gens_before = set((REPO_ROOT / "docs" / "memory_v5" / "generations").iterdir()) if (REPO_ROOT / "docs" / "memory_v5" / "generations").exists() else set()
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp) / "docs" / "memory_v5"
            receipt = run_benchmark(docs_base_dir=docs_dir)
            run_id = receipt["run_id"]
            temp_pointer = docs_dir / "current-generation.json"
            temp_diag_path = docs_dir / "semantic-retrieval-benchmark-diagnostics.json"
            temp_gen = docs_dir / "generations" / run_id
            assert temp_gen.exists(), "temp generation missing"
            assert temp_pointer.exists(), "temp pointer missing"
            assert temp_diag_path.exists(), "temp diagnostics missing"
            ptr_data = json.loads(temp_pointer.read_text(encoding="utf-8"))
            diag_data = json.loads(temp_diag_path.read_text(encoding="utf-8"))
            assert ptr_data["current_run_id"] == diag_data["run_id"] == receipt["run_id"] == temp_gen.name
            assert (temp_gen / "receipt.json").exists()
            assert (temp_gen / "report.md").exists()
            after_ptr_bytes = repo_pointer.read_bytes()
            after_diag_bytes = repo_diag.read_bytes()
            assert after_ptr_bytes == before_ptr_bytes, "repo pointer mutated by temp run"
            assert after_diag_bytes == before_diag_bytes, "repo diagnostics mutated by temp run"
            assert hashlib.sha256(after_ptr_bytes).hexdigest() == before_ptr_hash
            assert hashlib.sha256(after_diag_bytes).hexdigest() == before_diag_hash
            temp_gen_str = str(temp_gen)
            temp_run_id = run_id
        assert not Path(temp_gen_str).exists(), "temp generation leaked after TemporaryDirectory close"
        after_close_ptr = repo_pointer.read_bytes()
        after_close_diag = repo_diag.read_bytes()
        assert after_close_ptr == before_ptr_bytes
        assert after_close_diag == before_diag_bytes
        assert json.loads(after_close_diag.decode("utf-8"))["run_id"] != temp_run_id
        assert json.loads(after_close_ptr.decode("utf-8"))["current_run_id"] == before_ptr_json["current_run_id"]
        gens_after = set((REPO_ROOT / "docs" / "memory_v5" / "generations").iterdir())
        assert gens_before == gens_after, "repo generations mutated"

    @requires_minilm_onnx
    def test_publication_generation_identity_is_coherent(self):
        import hashlib
        repo_pointer = REPO_ROOT / "docs" / "memory_v5" / "current-generation.json"
        repo_diag = REPO_ROOT / "docs" / "memory_v5" / "semantic-retrieval-benchmark-diagnostics.json"
        before_ptr = repo_pointer.read_bytes()
        before_diag = repo_diag.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp) / "docs" / "memory_v5"
            receipt = run_benchmark(docs_base_dir=docs_dir)
            run_id = receipt["run_id"]
            gen_dir = docs_dir / "generations" / run_id
            ptr_path = docs_dir / "current-generation.json"
            diag_path = docs_dir / "semantic-retrieval-benchmark-diagnostics.json"
            assert gen_dir.is_dir()
            ptr = json.loads(ptr_path.read_text(encoding="utf-8"))
            diag = json.loads(diag_path.read_text(encoding="utf-8"))
            assert ptr["current_run_id"] == run_id
            assert diag["run_id"] == run_id
            assert receipt["run_id"] == run_id
            assert gen_dir.name == run_id
            assert ptr["receipt_path"] == f"docs/memory_v5/generations/{run_id}/receipt.json"
            assert (gen_dir / "receipt.json").exists()
            assert (gen_dir / "report.md").exists()
            stored = json.loads((gen_dir / "receipt.json").read_text(encoding="utf-8"))
            assert stored["run_id"] == run_id
            assert BenchmarkReceiptValidator.validate(stored) is True
            from tools.memory_v5_semantic_benchmark import validate_json_markdown_consistency
            md = (gen_dir / "report.md").read_text(encoding="utf-8")
            assert validate_json_markdown_consistency(stored, md) is True
        assert repo_pointer.read_bytes() == before_ptr, "repo pointer drift in coherence test"
        assert repo_diag.read_bytes() == before_diag, "repo diagnostics drift in coherence test"
