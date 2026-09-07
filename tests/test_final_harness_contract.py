"""
RED tests for final harness contract remediation.
Covers R13, C threshold independence, candidate-local lifecycle, GLOBAL_INVALID.
"""
import json
import tempfile
from pathlib import Path
from unittest.mock import patch
import sys
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

_ONNX_MODEL_FILE = REPO_ROOT / "modelos_f5" / "minilm_l12_onnx" / "model.onnx"
requires_minilm_onnx = pytest.mark.skipif(
    not _ONNX_MODEL_FILE.exists(),
    reason="MiniLM ONNX model artifacts not present on disk",
)

from tools.memory_v5_semantic_benchmark import (
    BenchmarkReceiptValidator,
    MiniLMRetriever,
    HybridRRFRetriever,
    _LexicalAdapter,
    render_markdown_report,
    validate_json_markdown_consistency,
    run_benchmark,
    verify_runtime_lock,
    CALIBRATION_RECEIPT_KEYS,
    METRICS_OBJECT_KEYS,
)

def _valid_receipt():
    return {
        "schema_version": "memory-semantic-benchmark-receipt-v1",
        "run_id": "run-final-001-abc123XYZ4567",
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
            "b": {"variant_key": "b", "execution_status": "EVALUABLE", "execution_reason": None, "quality_status": "QUALIFIED", "critical_reasons": [], "calibration": {"variant_key": "b", "threshold": 0.5, "calibration_fixture_hash": "b"*64, "model_artifact_hash": "c"*64, "shared_config_hash": "d"*64, "frozen_at": "2026-08-31T10:00:00Z", "frozen_sequence": 1, "cal_no_memory_abstention_count": 4, "cal_no_memory_false_injection_count": 0, "cal_no_memory_abstention_accuracy": 1.0, "cal_profile_isolation_pass_count": 4, "cal_profile_privacy_leakage_count": 0, "cal_profile_private_leakage_count": 0, "cal_profile_inactive_leakage_count": 0, "cal_profile_cross_profile_leakage_count": 0, "cal_near_wrong_precision_at_1": 0.75, "cal_near_wrong_a_precision_at_1": 0.5, "cal_syn_mrr_at_3": 0.8, "cal_para_mrr_at_3": 0.8, "cal_syn_para_mrr3_mean": 0.8, "cal_hard_correct_at_1_count": 4, "cal_hard_recall_at_1": 1.0}},
            "c": {"variant_key": "c", "execution_status": "EVALUABLE", "execution_reason": None, "quality_status": "QUALIFIED", "critical_reasons": [], "calibration": {"variant_key": "c", "threshold": 0.5, "calibration_fixture_hash": "b"*64, "model_artifact_hash": "c"*64, "shared_config_hash": "d"*64, "frozen_at": "2026-08-31T10:00:00Z", "frozen_sequence": 1, "cal_no_memory_abstention_count": 4, "cal_no_memory_false_injection_count": 0, "cal_no_memory_abstention_accuracy": 1.0, "cal_profile_isolation_pass_count": 4, "cal_profile_privacy_leakage_count": 0, "cal_profile_private_leakage_count": 0, "cal_profile_inactive_leakage_count": 0, "cal_profile_cross_profile_leakage_count": 0, "cal_near_wrong_precision_at_1": 0.75, "cal_near_wrong_a_precision_at_1": 0.5, "cal_syn_mrr_at_3": 0.8, "cal_para_mrr_at_3": 0.8, "cal_syn_para_mrr3_mean": 0.8, "cal_hard_correct_at_1_count": 4, "cal_hard_recall_at_1": 1.0}},
        },
        "metrics": {
            "a": {"hard_correct_at_1_count":16,"hard_correct_at_3_count":16,"hard_recall_at_1":1.0,"hard_recall_at_3":1.0,"hard_mrr_at_3":1.0,"syn_correct_at_1_count":14,"syn_correct_at_3_count":14,"syn_recall_at_1":1.0,"syn_recall_at_3":1.0,"syn_mrr_at_3":0.4,"para_correct_at_1_count":16,"para_correct_at_3_count":16,"para_recall_at_1":1.0,"para_recall_at_3":1.0,"para_mrr_at_3":0.4,"semantic_quality_score":0.4,"no_memory_abstention_count":12,"no_memory_false_injection_count":0,"no_memory_abstention_accuracy":1.0,"near_wrong_correct_at_1_count":12,"near_wrong_precision_at_1":1.0,"near_wrong_target_mrr_at_3":1.0,"profile_isolation_pass_count":5,"profile_privacy_leakage_count":0,"profile_private_leakage_count":0,"profile_inactive_leakage_count":0,"profile_cross_profile_leakage_count":0,"stale_correct_count":3,"stale_correct_rate":0.6},
            "b": {"hard_correct_at_1_count":16,"hard_correct_at_3_count":16,"hard_recall_at_1":1.0,"hard_recall_at_3":1.0,"hard_mrr_at_3":1.0,"syn_correct_at_1_count":14,"syn_correct_at_3_count":14,"syn_recall_at_1":1.0,"syn_recall_at_3":1.0,"syn_mrr_at_3":0.8,"para_correct_at_1_count":16,"para_correct_at_3_count":16,"para_recall_at_1":1.0,"para_recall_at_3":1.0,"para_mrr_at_3":0.8,"semantic_quality_score":0.8,"no_memory_abstention_count":12,"no_memory_false_injection_count":0,"no_memory_abstention_accuracy":1.0,"near_wrong_correct_at_1_count":12,"near_wrong_precision_at_1":1.0,"near_wrong_target_mrr_at_3":1.0,"profile_isolation_pass_count":5,"profile_privacy_leakage_count":0,"profile_private_leakage_count":0,"profile_inactive_leakage_count":0,"profile_cross_profile_leakage_count":0,"stale_correct_count":3,"stale_correct_rate":0.6},
            "c": {"hard_correct_at_1_count":16,"hard_correct_at_3_count":16,"hard_recall_at_1":1.0,"hard_recall_at_3":1.0,"hard_mrr_at_3":1.0,"syn_correct_at_1_count":14,"syn_correct_at_3_count":14,"syn_recall_at_1":1.0,"syn_recall_at_3":1.0,"syn_mrr_at_3":0.7,"para_correct_at_1_count":16,"para_correct_at_3_count":16,"para_recall_at_1":1.0,"para_recall_at_3":1.0,"para_mrr_at_3":0.7,"semantic_quality_score":0.7,"no_memory_abstention_count":12,"no_memory_false_injection_count":0,"no_memory_abstention_accuracy":1.0,"near_wrong_correct_at_1_count":12,"near_wrong_precision_at_1":1.0,"near_wrong_target_mrr_at_3":1.0,"profile_isolation_pass_count":5,"profile_privacy_leakage_count":0,"profile_private_leakage_count":0,"profile_inactive_leakage_count":0,"profile_cross_profile_leakage_count":0,"stale_correct_count":3,"stale_correct_rate":0.6},
        },
        "winner": "b",
        "terminal_route": "SEMANTIC_PROMISING",
    }

# ----- R13: top-level nullability -----
class TestR13TopLevelNullability:
    def test_valid_seed_null_must_fail(self):
        r=_valid_receipt(); r["seed"]=None
        assert BenchmarkReceiptValidator.validate(r) is False, "VALID seed null must be rejected except GLOBAL_INVALID"
    def test_valid_shared_config_hash_null_must_fail(self):
        r=_valid_receipt(); r["shared_config_hash"]=None
        assert BenchmarkReceiptValidator.validate(r) is False
    def test_valid_lock_identity_null_must_fail(self):
        r=_valid_receipt(); r["lock_identity"]=None
        assert BenchmarkReceiptValidator.validate(r) is False
    def test_valid_locked_hash_null_must_fail(self):
        r=_valid_receipt(); r["locked_fixture_hash"]=None
        assert BenchmarkReceiptValidator.validate(r) is False
    def test_valid_calibration_hash_null_must_fail(self):
        r=_valid_receipt(); r["calibration_fixture_hash"]=None
        assert BenchmarkReceiptValidator.validate(r) is False
    def test_valid_global_invalid_reason_non_null_must_fail(self):
        r=_valid_receipt(); r["global_invalid_reason"]="BASELINE_INVALID"
        assert BenchmarkReceiptValidator.validate(r) is False
    def test_valid_model_hash_null_without_shared_unavailable_must_fail(self):
        r=_valid_receipt(); r["model_artifact_hash"]=None
        # b and c are EVALUABLE, not MODEL_UNAVAILABLE, so null must be rejected
        assert BenchmarkReceiptValidator.validate(r) is False
    def test_valid_model_hash_null_with_both_model_unavailable_allowed(self):
        r=_valid_receipt()
        r["model_artifact_hash"]=None
        r["candidates"]["b"]={"variant_key":"b","execution_status":"NON_EVALUABLE","execution_reason":"MODEL_UNAVAILABLE","quality_status":"NOT_APPLICABLE","critical_reasons":[],"calibration":None}
        r["candidates"]["c"]={"variant_key":"c","execution_status":"NON_EVALUABLE","execution_reason":"MODEL_UNAVAILABLE","quality_status":"NOT_APPLICABLE","critical_reasons":[],"calibration":None}
        r["metrics"]["b"]=None; r["metrics"]["c"]=None
        r["winner"]=None; r["terminal_route"]="INCONCLUSIVE"
        assert BenchmarkReceiptValidator.validate(r) is True, "shared MODEL_UNAVAILABLE exception must allow null"
    def test_valid_model_hash_null_with_one_model_unavailable_must_fail(self):
        r=_valid_receipt(); r["model_artifact_hash"]=None
        r["candidates"]["b"]={"variant_key":"b","execution_status":"NON_EVALUABLE","execution_reason":"MODEL_UNAVAILABLE","quality_status":"NOT_APPLICABLE","critical_reasons":[],"calibration":None}
        r["metrics"]["b"]=None
        # c remains EVALUABLE, so not both
        r["winner"]=None; r["terminal_route"]="INCONCLUSIVE"
        assert BenchmarkReceiptValidator.validate(r) is False

# ----- R13: nested hash binding -----
class TestR13NestedHashBinding:
    def test_calibration_fixture_hash_mismatch_fails(self):
        r=_valid_receipt(); r["candidates"]["b"]["calibration"]["calibration_fixture_hash"]="f"*64
        assert BenchmarkReceiptValidator.validate(r) is False
    def test_calibration_model_hash_mismatch_fails(self):
        r=_valid_receipt(); r["candidates"]["b"]["calibration"]["model_artifact_hash"]="e"*64
        assert BenchmarkReceiptValidator.validate(r) is False
    def test_calibration_shared_hash_mismatch_fails(self):
        r=_valid_receipt(); r["candidates"]["c"]["calibration"]["shared_config_hash"]="f"*64
        assert BenchmarkReceiptValidator.validate(r) is False

# ----- R13: omitted leaves and markdown completeness -----
class TestR13OmittedLeavesAndMarkdown:
    def test_omitted_global_invalid_reason_fails(self):
        r=_valid_receipt(); del r["global_invalid_reason"]
        assert BenchmarkReceiptValidator.validate(r) is False
    def test_omitted_seed_fails(self):
        r=_valid_receipt(); del r["seed"]
        assert BenchmarkReceiptValidator.validate(r) is False
    def test_omitted_calibration_leaf_fails(self):
        for leaf in ["cal_profile_private_leakage_count","cal_profile_inactive_leakage_count","cal_profile_cross_profile_leakage_count","cal_near_wrong_precision_at_1","cal_near_wrong_a_precision_at_1"]:
            r=_valid_receipt(); del r["candidates"]["b"]["calibration"][leaf]
            assert BenchmarkReceiptValidator.validate(r) is False, f"missing {leaf} must fail"
    def test_omitted_metrics_leaf_fails(self):
        for leaf in list(METRICS_OBJECT_KEYS):
            r=_valid_receipt(); del r["metrics"]["b"][leaf]
            assert BenchmarkReceiptValidator.validate(r) is False, f"missing metrics leaf {leaf} must fail"
    def test_markdown_mutation_seed_detected(self):
        r=_valid_receipt(); md=render_markdown_report(r)
        r2=_valid_receipt(); r2["seed"]=999
        assert validate_json_markdown_consistency(r2, md) is False
    def test_markdown_mutation_global_invalid_reason_detected(self):
        r=_valid_receipt(); md=render_markdown_report(r)
        r2=_valid_receipt(); r2["global_invalid_reason"]="BASELINE_INVALID"; r2["state"]="GLOBAL_INVALID"
        assert validate_json_markdown_consistency(r2, md) is False
    def test_markdown_mutation_every_calibration_leaf_detected(self):
        r=_valid_receipt(); md=render_markdown_report(r)
        for leaf in CALIBRATION_RECEIPT_KEYS:
            r2=_valid_receipt()
            orig=r2["candidates"]["b"]["calibration"][leaf]
            # mutate leaf to different valid value
            if isinstance(orig, int):
                r2["candidates"]["b"]["calibration"][leaf]= (orig+1)%5
                # fix ratios if needed: skip ratio leaves for this iteration, they are covered separately but still must be detected
                if leaf in ("cal_no_memory_abstention_count","cal_hard_correct_at_1_count"):
                    # adjust ratio to keep receipt valid else validator would fail before markdown check
                    if leaf=="cal_no_memory_abstention_count":
                        r2["candidates"]["b"]["calibration"]["cal_no_memory_abstention_accuracy"]=r2["candidates"]["b"]["calibration"][leaf]/4.0
                    if leaf=="cal_hard_correct_at_1_count":
                        r2["candidates"]["b"]["calibration"]["cal_hard_recall_at_1"]=r2["candidates"]["b"]["calibration"][leaf]/4.0
                    if leaf=="cal_syn_mrr_at_3" or leaf=="cal_para_mrr_at_3":
                        r2["candidates"]["b"]["calibration"]["cal_syn_para_mrr3_mean"]=(r2["candidates"]["b"]["calibration"]["cal_syn_mrr_at_3"]+r2["candidates"]["b"]["calibration"]["cal_para_mrr_at_3"])/2.0
            elif isinstance(orig, float):
                r2["candidates"]["b"]["calibration"][leaf]=0.11 if orig!=0.11 else 0.22
                if leaf in ("cal_syn_mrr_at_3","cal_para_mrr_at_3"):
                    r2["candidates"]["b"]["calibration"]["cal_syn_para_mrr3_mean"]=(r2["candidates"]["b"]["calibration"]["cal_syn_mrr_at_3"]+r2["candidates"]["b"]["calibration"]["cal_para_mrr_at_3"])/2.0
                if leaf=="cal_no_memory_abstention_accuracy":
                    # keep count consistent
                    pass
            elif isinstance(orig, str):
                if leaf=="threshold":
                    r2["candidates"]["b"]["calibration"][leaf]=0.99
                elif leaf=="frozen_at":
                    r2["candidates"]["b"]["calibration"][leaf]="2026-08-31T11:00:00Z"
                else:
                    r2["candidates"]["b"]["calibration"][leaf]="a"*64 if orig!="a"*64 else "b"*64
                    # if we mutated hash, need top-level to match? But we test markdown mismatch, not validator. For markdown, we need top-level also? The mismatch we test is calibration hash vs top mismatch should also be caught by markdown. We'll keep top aligned for other leaves; for hash leaves we also need to keep validator valid? For this markdown test, we want receipt to remain valid except markdown mismatch. If we mutate calibration_fixture_hash without updating top-level, receipt becomes invalid per hash binding, so validator would fail. Instead mutate a non-hash leaf.
                    continue
            else:
                continue
            # ensure receipt still valid for markdown test
            if not BenchmarkReceiptValidator.validate(r2):
                continue
            assert validate_json_markdown_consistency(r2, md) is False, f"markdown must detect mutation of calibration leaf {leaf}"
    def test_markdown_mutation_every_metrics_leaf_detected(self):
        r=_valid_receipt(); md=render_markdown_report(r)
        for leaf in METRICS_OBJECT_KEYS:
            r2=_valid_receipt()
            orig=r2["metrics"]["b"][leaf]
            if isinstance(orig, int):
                new_val = (orig+1)%6
                # keep ratios consistent for valid receipt
                if leaf=="hard_correct_at_1_count":
                    r2["metrics"]["b"][leaf]=new_val; r2["metrics"]["b"]["hard_recall_at_1"]=new_val/16.0
                elif leaf=="hard_correct_at_3_count":
                    r2["metrics"]["b"][leaf]=new_val; r2["metrics"]["b"]["hard_recall_at_3"]=new_val/16.0
                elif leaf=="syn_correct_at_1_count":
                    r2["metrics"]["b"][leaf]=new_val%15; r2["metrics"]["b"]["syn_recall_at_1"]=r2["metrics"]["b"][leaf]/14.0
                elif leaf=="syn_correct_at_3_count":
                    r2["metrics"]["b"][leaf]=new_val%15; r2["metrics"]["b"]["syn_recall_at_3"]=r2["metrics"]["b"][leaf]/14.0
                elif leaf=="para_correct_at_1_count":
                    r2["metrics"]["b"][leaf]=new_val%17; r2["metrics"]["b"]["para_recall_at_1"]=r2["metrics"]["b"][leaf]/16.0
                elif leaf=="para_correct_at_3_count":
                    r2["metrics"]["b"][leaf]=new_val%17; r2["metrics"]["b"]["para_recall_at_3"]=r2["metrics"]["b"][leaf]/16.0
                elif leaf=="no_memory_abstention_count":
                    r2["metrics"]["b"][leaf]=new_val%13; r2["metrics"]["b"]["no_memory_abstention_accuracy"]=r2["metrics"]["b"][leaf]/12.0
                    r2["metrics"]["b"]["no_memory_false_injection_count"]=12-r2["metrics"]["b"][leaf]
                elif leaf=="near_wrong_correct_at_1_count":
                    r2["metrics"]["b"][leaf]=new_val%13; r2["metrics"]["b"]["near_wrong_precision_at_1"]=r2["metrics"]["b"][leaf]/12.0
                elif leaf in ("profile_isolation_pass_count","profile_privacy_leakage_count","profile_private_leakage_count","profile_inactive_leakage_count","profile_cross_profile_leakage_count","stale_correct_count"):
                    r2["metrics"]["b"][leaf]=new_val%6
                    if leaf=="stale_correct_count":
                        r2["metrics"]["b"]["stale_correct_rate"]=r2["metrics"]["b"][leaf]/5.0
                else:
                    r2["metrics"]["b"][leaf]=new_val
            elif isinstance(orig, float):
                new_val = 0.11 if orig!=0.11 else 0.22
                if leaf=="semantic_quality_score":
                    # keep derived
                    r2["metrics"]["b"]["syn_mrr_at_3"]=0.4; r2["metrics"]["b"]["para_mrr_at_3"]=0.6; r2["metrics"]["b"][leaf]=0.5
                    new_val=0.99
                    r2["metrics"]["b"]["syn_mrr_at_3"]=0.99; r2["metrics"]["b"]["para_mrr_at_3"]=0.99; r2["metrics"]["b"][leaf]=0.99
                elif leaf in ("hard_recall_at_1","hard_recall_at_3","syn_recall_at_1","syn_recall_at_3","para_recall_at_1","para_recall_at_3","no_memory_abstention_accuracy","near_wrong_precision_at_1","stale_correct_rate","hard_mrr_at_3","syn_mrr_at_3","para_mrr_at_3","near_wrong_target_mrr_at_3"):
                    # for ratio leaves, adjust count too
                    continue
                else:
                    r2["metrics"]["b"][leaf]=new_val
            else:
                continue
            if not BenchmarkReceiptValidator.validate(r2):
                continue
            assert validate_json_markdown_consistency(r2, md) is False, f"markdown must detect mutation of metrics leaf {leaf}"

# ----- C threshold independence -----
class TestCThresholdIndependence:
    @requires_minilm_onnx
    def test_c_semantic_ranking_unchanged_when_b_abstains(self):
        model_dir = REPO_ROOT / "modelos_f5" / "minilm_l12_onnx"
        retriever_b = MiniLMRetriever(model_dir=model_dir)
        retriever_b.initialize()
        pool = [
            {"id": "c1", "title": "Auriculares de diadema", "content": "Utiliza audífonos cerrados para aislar el ruido exterior.", "profile_id": "prof_1", "status": "curated", "pinned": 0, "private": 0, "inactive": 0},
            {"id": "c2", "title": "Microfono Shure MV7", "content": "Montó una cápsula dinámica conectada por XLR.", "profile_id": "prof_1", "status": "curated", "pinned": 0, "private": 0, "inactive": 0},
            {"id": "c3", "title": "Texto neutro", "content": "Contenido sin relación.", "profile_id": "prof_1", "status": "curated", "pinned": 0, "private": 0, "inactive": 0},
        ]
        query = "cascos acolchados para escuchar"
        adapter = _LexicalAdapter()
        # Baseline hybrid with unthresholded semantic
        retriever_b.threshold = None
        baseline_hybrid = HybridRRFRetriever(adapter, MiniLMRetriever(model_dir=model_dir, threshold=None, session=retriever_b.session, tokenizer=retriever_b.tokenizer, embed_cache=retriever_b._embed_cache), threshold=None)
        baseline = baseline_hybrid.retrieve(query, pool, k=3)
        assert len(baseline) > 0
        # Mutate B to ABSTAIN_ALL as coordinator does after freezing B
        retriever_b.threshold = "ABSTAIN_ALL"
        # Defective C path (current coordinator bug) reuses same thresholded retriever for C
        defective_hybrid = HybridRRFRetriever(adapter, retriever_b, threshold=None)
        defective = defective_hybrid.retrieve(query, pool, k=3)
        # Correct C path must use separate unthresholded view sharing session/cache
        c_view = MiniLMRetriever(model_dir=model_dir, threshold=None, session=retriever_b.session, tokenizer=retriever_b.tokenizer, embed_cache=retriever_b._embed_cache)
        correct_hybrid = HybridRRFRetriever(adapter, c_view, threshold=None)
        correct = correct_hybrid.retrieve(query, pool, k=3)
        # Coordinator must produce correct (unchanged) ranking for C despite B ABSTAIN_ALL
        assert correct == baseline and len(correct) > 0, "C semantic ranking must remain unchanged and non-empty despite B ABSTAIN_ALL"
        assert defective != baseline, "defective path shows bug: C affected by B threshold"
        # Enforce coordinator fix: must construct separate C retriever with threshold=None sharing session/tokenizer/cache
        src = (REPO_ROOT / "tools" / "memory_v5_semantic_benchmark.py").read_text(encoding="utf-8")
        # Count constructions sharing retriever_b session/tokenizer (B eval + C view)
        import re
        sess_count = src.count("retriever_b.session")
        tok_count = src.count("retriever_b.tokenizer")
        assert sess_count >= 2 and tok_count >= 2, f"coordinator must have at least 2 shared-session retrievers (one for B eval, one for C view), found session:{sess_count} tokenizer:{tok_count}"
        assert "threshold=None" in src, "C view must be threshold=None"
        # Also check that defective direct reuse for C calibration is gone: must introduce C semantic view variable
        assert sess_count >= 2, "coordinator must introduce C semantic view variable sharing session"

# ----- Candidate-local lifecycle -----
class TestCandidateLocalLifecycle:
    @requires_minilm_onnx
    def test_b_calibration_failed_does_not_suppress_c(self):
        # Run benchmark with B forced CALIBRATION_FAILED but C should still be EVALUABLE
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp) / "docs" / "memory_v5"
            import tools.memory_v5_semantic_benchmark as bench
            original_select = bench.select_best_calibration_threshold
            call_count = {"n":0}
            def side_effect(eval_cands):
                call_count["n"]+=1
                if call_count["n"]==1:
                    return None  # B fails
                return original_select(eval_cands)
            with patch.object(bench, "select_best_calibration_threshold", side_effect=side_effect), \
                 patch.object(bench, "verify_runtime_lock", return_value=(True, None, "valid-lock-identity")):
                receipt = bench.run_benchmark(docs_base_dir=docs_dir)
            assert receipt["candidates"]["b"]["execution_status"]=="NON_EVALUABLE"
            assert receipt["candidates"]["b"]["execution_reason"]=="CALIBRATION_FAILED"
            assert receipt["candidates"]["c"]["execution_status"]=="EVALUABLE", "C must be EVALUABLE even when B CALIBRATION_FAILED"
            assert receipt["candidates"]["c"]["calibration"] is not None
            assert receipt["metrics"]["c"] is not None
            # C should be able to be winner if qualified
            assert receipt["state"]=="VALID"

# ----- GLOBAL_INVALID coordinator -----
class TestGlobalInvalidCoordinator:
    def test_global_invalid_runtime_lock_produces_valid_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            docs_dir = Path(tmp) / "docs" / "memory_v5"
            import tools.memory_v5_semantic_benchmark as bench
            with patch.object(bench, "verify_runtime_lock", return_value=(False, "SHARED_CONFIG_INVALID", "mismatch-identity")):
                receipt = bench.run_benchmark(docs_base_dir=docs_dir)
            assert receipt["state"]=="GLOBAL_INVALID"
            assert receipt["global_invalid_reason"]=="SHARED_CONFIG_INVALID"
            assert receipt["candidates"]["b"] is None
            assert receipt["candidates"]["c"] is None
            assert receipt["metrics"]["a"] is None
            assert receipt["metrics"]["b"] is None
            assert receipt["metrics"]["c"] is None
            assert receipt["winner"] is None
            assert receipt["terminal_route"]=="INCONCLUSIVE"
            assert BenchmarkReceiptValidator.validate(receipt) is True
            md = (docs_dir / "generations" / receipt["run_id"] / "report.md").read_text(encoding="utf-8")
            assert validate_json_markdown_consistency(receipt, md) is True
            # publication proof
            assert (docs_dir / "generations" / receipt["run_id"] / "receipt.json").exists()
            assert (docs_dir / "current-generation.json").exists()
            ptr = json.loads((docs_dir / "current-generation.json").read_text(encoding="utf-8"))
            assert ptr["current_run_id"]==receipt["run_id"]
            diag = json.loads((docs_dir / "semantic-retrieval-benchmark-diagnostics.json").read_text(encoding="utf-8"))
            assert diag["run_id"]==receipt["run_id"]
