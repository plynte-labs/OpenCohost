"""
Unit and integration tests for WU5: Locked Multi-Family Metrics & Mandatory Safety Qualification.

Validates:
1. Exact family denominators and quotas (Hard=16, Syn=14, Para=16, NoMem=12, NearWrong=12, Privacy=5, Stale=5).
2. Strict locked input validation and fail-closed behavior for malformed inputs.
3. Pure mathematical calculation of Recall@1, Recall@3, MRR@3, semantic_quality_score.
4. Privacy case isolation (5/5) vs leakage event counting (private, inactive, cross-profile).
5. Near-wrong exact discrete threshold comparison against baseline A (7/12).
6. Exact critical reason taxonomy and canonical ordering:
   - HARD_RECALL_REGRESSION
   - NO_MEMORY_INJECTION
   - NEAR_WRONG_REGRESSION
   - PROFILE_PRIVACY_LEAKAGE
7. Quality status determination: QUALIFIED iff zero critical reasons, else CRITICAL_REGRESSION.
8. NON_EVALUABLE candidates are never evaluated on locked data (metrics=None, quality_status=NOT_APPLICABLE).
9. Exact binding to pre-locked experimental snapshot from Engram #6313 (SHA256: 36fac4a7250e4e9169009a6630a206da9ab5d13bfe00e0839f17f3174c751d35).
10. Golden regression assertions for Baseline A, Reference B, H1_MARGIN, H2_LEXICAL_CORROBORATION, and H4_ASYMMETRIC_GATE.
"""

import copy
import hashlib
import json
from pathlib import Path
import pytest

from tools.memory_v5_semantic_safety.models import (
    CandidateConfig,
    CandidateResult,
    CalibrationRecord,
    EvaluationTruth,
    InferenceEvidence,
    MetricsObject,
    RankedItem,
)
from tools.memory_v5_semantic_safety.candidates import (
    CandidateManifest,
    H1MarginCandidate,
    H2LexicalCorroborationCandidate,
    H4AsymmetricGateCandidate,
)
from tools.memory_v5_semantic_safety.evidence import (
    load_fixture,
    generate_case_evidence,
    MiniLMEmbedder,
)
from tools.memory_v5_semantic_safety.metrics import (
    CRITICAL_REASONS_ORDER,
    LockedMetricsEvaluator,
    LockedQualificationEngine,
    evaluate_baseline_a,
    evaluate_baseline_b,
    validate_locked_inputs,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MODEL_DIR = REPO_ROOT / "modelos_f5" / "minilm_l12_onnx"
LOCKED_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "memory_v5_semantic_locked.json"
CAL_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "memory_v5_semantic_calibration.json"
PRE_LOCKED_SNAPSHOT_PATH = REPO_ROOT / "tests" / "fixtures" / "pre_locked_snapshot_6313.json"

MODEL_HASH = "7cce6bd3df3eefe20757261165563b9f27b2397b5f0661bd42f804d545b07569"
CONFIG_HASH = "c0d7110f0abe8dfae5e335dc1d931b4742c52e37ea67de296afbb22060912c43"
CAL_HASH = "1d262ab3b3daa275e9bee166e024ae2f91c8d6d8a3fe95b993d2bb06feee9472"
LOCKED_HASH = "6e96049e76e471549b73d1606e714a888ee1f4ba64729423b6913739db5a435f"
PRE_LOCKED_SNAPSHOT_HASH = "36fac4a7250e4e9169009a6630a206da9ab5d13bfe00e0839f17f3174c751d35"


def _compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _make_mock_80_locked_cases():
    """Build a deterministic 80-case mock locked dataset matching exact quotas."""
    evidences = []
    truths = []

    # 16 Hard Lexical
    for i in range(16):
        cid = f"l_hard_{i}"
        ekey = hashlib.sha256(cid.encode("utf-8")).hexdigest()[:16]
        ev = InferenceEvidence(
            ekey, f"hard q {i}",
            (RankedItem(f"h_{i}", 10.0, 1, "text", "u1", False, False),),
            (RankedItem(f"h_{i}", 0.95, 1, "text", "u1", False, False),),
        )
        tr = EvaluationTruth(cid, "hard_lexical", (f"h_{i}",), False, "u1", None)
        evidences.append(ev)
        truths.append(tr)

    # 14 Synonym
    for i in range(14):
        cid = f"l_syn_{i}"
        ekey = hashlib.sha256(cid.encode("utf-8")).hexdigest()[:16]
        ev = InferenceEvidence(
            ekey, f"syn q {i}",
            (),
            (RankedItem(f"s_{i}", 0.90, 1, "text", "u1", False, False),),
        )
        tr = EvaluationTruth(cid, "synonym", (f"s_{i}",), False, "u1", None)
        evidences.append(ev)
        truths.append(tr)

    # 16 Paraphrase
    for i in range(16):
        cid = f"l_para_{i}"
        ekey = hashlib.sha256(cid.encode("utf-8")).hexdigest()[:16]
        ev = InferenceEvidence(
            ekey, f"para q {i}",
            (),
            (RankedItem(f"p_{i}", 0.88, 1, "text", "u1", False, False),),
        )
        tr = EvaluationTruth(cid, "paraphrase", (f"p_{i}",), False, "u1", None)
        evidences.append(ev)
        truths.append(tr)

    # 12 NO_MEMORY
    for i in range(12):
        cid = f"l_nomem_{i}"
        ekey = hashlib.sha256(cid.encode("utf-8")).hexdigest()[:16]
        ev = InferenceEvidence(
            ekey, f"nomem q {i}",
            (),
            (RankedItem("trap", 0.60, 1, "trap text", "u1", False, False),),
        )
        tr = EvaluationTruth(cid, "no_memory", (), True, "u1", None)
        evidences.append(ev)
        truths.append(tr)

    # 12 Near-but-Wrong (7 correct lexical top-1, 5 distractors)
    for i in range(12):
        is_correct = (i < 7)
        cid = f"l_near_{i}"
        ekey = hashlib.sha256(cid.encode("utf-8")).hexdigest()[:16]
        target_id = f"nw_target_{i}"
        top_cand_id = target_id if is_correct else "nw_distractor"
        ev = InferenceEvidence(
            ekey, f"near q {i}",
            (RankedItem(top_cand_id, 10.0, 1, "text", "u1", False, False),),
            (RankedItem(top_cand_id, 0.80, 1, "text", "u1", False, False),),
        )
        tr = EvaluationTruth(cid, "near_but_wrong", (target_id,), False, "u1", None)
        evidences.append(ev)
        truths.append(tr)

    # 5 Profile Privacy
    for i in range(5):
        cid = f"l_priv_{i}"
        ekey = hashlib.sha256(cid.encode("utf-8")).hexdigest()[:16]
        ev = InferenceEvidence(
            ekey, f"priv q {i}",
            (),
            (),
        )
        tr = EvaluationTruth(cid, "profile_privacy", (f"priv_target_{i}",), False, "u1", None)
        evidences.append(ev)
        truths.append(tr)

    # 5 Stale Contradiction (Diagnostic)
    for i in range(5):
        cid = f"l_stale_{i}"
        ekey = hashlib.sha256(cid.encode("utf-8")).hexdigest()[:16]
        ev = InferenceEvidence(
            ekey, f"stale q {i}",
            (RankedItem(f"stale_{i}", 10.0, 1, "text", "u1", False, False),),
            (RankedItem(f"stale_{i}", 0.85, 1, "text", "u1", False, False),),
        )
        tr = EvaluationTruth(cid, "stale_contradiction", (f"stale_{i}",), False, "u1", "stale_update")
        evidences.append(ev)
        truths.append(tr)

    return evidences, truths


def test_locked_input_validation_failure_modes():
    """Task 5.1: Negative tests for all structural and identity input validation failures."""
    evs, trs = _make_mock_80_locked_cases()
    validate_locked_inputs(evs, trs)  # Must pass on clean 80 cases

    # 1. Missing case (79 cases)
    with pytest.raises(ValueError, match="does not equal required 80 cases"):
        validate_locked_inputs(evs[:79], trs[:79])

    # 2. Extra case (81 cases)
    extra_ev = copy.deepcopy(evs[0])
    extra_tr = copy.deepcopy(trs[0])
    with pytest.raises(ValueError, match="does not equal required 80 cases"):
        validate_locked_inputs(evs + [extra_ev], trs + [extra_tr])

    # 3. Wrong family quota (replace 1 synonym with 1 hard_lexical -> 17 hard, 13 syn)
    bad_trs_quota = list(trs)
    bad_cid = "l_syn_0"
    bad_trs_quota[16] = EvaluationTruth(bad_cid, "hard_lexical", ("h_extra",), False, "u1", None)
    with pytest.raises(ValueError, match="quota mismatch"):
        validate_locked_inputs(evs, bad_trs_quota)

    # 4. Unknown family
    bad_trs_unknown = list(trs)
    bad_trs_unknown[0] = EvaluationTruth("l_hard_0", "unknown_family", ("h_0",), False, "u1", None)
    with pytest.raises(ValueError, match="Unknown family"):
        validate_locked_inputs(evs, bad_trs_unknown)

    # 5. Swapped evidence/truth pair (identity mismatch)
    bad_evs_swap = list(evs)
    bad_evs_swap[0], bad_evs_swap[1] = bad_evs_swap[1], bad_evs_swap[0]
    with pytest.raises(ValueError, match="Positional evidence/truth mismatch"):
        validate_locked_inputs(bad_evs_swap, trs)

    # 6. Duplicate truth case_id
    bad_trs_dup = list(trs)
    bad_trs_dup[1] = copy.deepcopy(bad_trs_dup[0])
    with pytest.raises(ValueError, match="Duplicate case_id"):
        validate_locked_inputs(evs, bad_trs_dup)

    # 7. Duplicate evidence_key
    bad_evs_dup = list(evs)
    bad_evs_dup[1] = copy.deepcopy(bad_evs_dup[0])
    with pytest.raises(ValueError, match="Duplicate evidence_key"):
        validate_locked_inputs(bad_evs_dup, trs)


def test_metrics_evaluator_family_denominators_and_calculations():
    """Task 5.2: Validate exact denominators and score derivations."""
    evidences, truths = _make_mock_80_locked_cases()
    evaluator = LockedMetricsEvaluator()
    h1 = H1MarginCandidate()
    cfg = CandidateConfig("h1_test", "H1_MARGIN", {"theta_score": 0.75, "theta_margin": 0.10})

    metrics = evaluator.evaluate(h1, cfg, evidences, truths, baseline_a_near_wrong_count=7)

    assert metrics.hard_correct_at_1_count == 16
    assert metrics.hard_recall_at_1 == 1.0
    assert metrics.syn_correct_at_1_count == 14
    assert metrics.syn_recall_at_1 == 1.0
    assert metrics.para_correct_at_1_count == 16
    assert metrics.para_recall_at_1 == 1.0
    assert metrics.semantic_quality_score == 1.0
    assert metrics.no_memory_abstention_count == 12
    assert metrics.no_memory_false_injection_count == 0
    assert metrics.near_wrong_correct_at_1_count == 7
    assert metrics.near_wrong_precision_at_1 == 7 / 12
    assert metrics.profile_isolation_pass_count == 5
    assert metrics.profile_privacy_leakage_count == 0
    assert metrics.stale_correct_count == 5


def test_privacy_case_isolation_vs_leak_event_distinction():
    """Task 5.3: Assert per-case isolation is not 5 - leak_events when multiple leaks occur in one case."""
    evidences, truths = _make_mock_80_locked_cases()
    evaluator = LockedMetricsEvaluator()
    h1 = H1MarginCandidate()
    cfg = CandidateConfig("h1_test", "H1_MARGIN", {"theta_score": 0.50, "theta_margin": 0.0})

    # Inject 3 leakage items into case 0 of privacy (index 70: 16 hard + 14 syn + 16 para + 12 nomem + 12 near = 70)
    p_cid = "l_priv_0"
    p_ekey = hashlib.sha256(p_cid.encode("utf-8")).hexdigest()[:16]
    leak_case = InferenceEvidence(
        p_ekey, "priv q 0",
        (),
        (
            RankedItem("m1", 0.90, 1, "p text", "u1", is_private=True, is_inactive=False),
            RankedItem("m2", 0.85, 2, "i text", "u1", is_private=False, is_inactive=True),
            RankedItem("m3", 0.80, 3, "c text", "u2", is_private=False, is_inactive=False),
        )
    )
    mod_evidences = list(evidences)
    mod_evidences[70] = leak_case

    metrics = evaluator.evaluate(h1, cfg, mod_evidences, truths, baseline_a_near_wrong_count=7)

    # 1 case failed out of 5 -> pass count is exactly 4
    assert metrics.profile_isolation_pass_count == 4
    # Event counters: 1 private, 1 inactive, 1 cross-profile = 3 total leaks
    assert metrics.profile_private_leakage_count == 1
    assert metrics.profile_inactive_leakage_count == 1
    assert metrics.profile_cross_profile_leakage_count == 1
    assert metrics.profile_privacy_leakage_count == 3
    # Demonstrating: isolation_pass_count (4) != 5 - total_leaks (5 - 3 = 2)!
    assert metrics.profile_isolation_pass_count != 5 - metrics.profile_privacy_leakage_count


def test_mandatory_safety_qualification_and_canonical_reason_ordering():
    """Task 5.4: Assert exact critical reason ordering and QUALIFIED vs CRITICAL_REGRESSION."""
    engine = LockedQualificationEngine(baseline_a_near_wrong_count=7)

    # 1. Clean metrics -> QUALIFIED, critical_reasons empty
    clean_metrics = MetricsObject(
        hard_correct_at_1_count=16, hard_correct_at_3_count=16, hard_recall_at_1=1.0, hard_recall_at_3=1.0, hard_mrr_at_3=1.0,
        syn_correct_at_1_count=14, syn_correct_at_3_count=14, syn_recall_at_1=1.0, syn_recall_at_3=1.0, syn_mrr_at_3=1.0,
        para_correct_at_1_count=16, para_correct_at_3_count=16, para_recall_at_1=1.0, para_recall_at_3=1.0, para_mrr_at_3=1.0,
        semantic_quality_score=1.0,
        no_memory_abstention_count=12, no_memory_false_injection_count=0, no_memory_abstention_accuracy=1.0,
        near_wrong_correct_at_1_count=7, near_wrong_precision_at_1=7/12, near_wrong_target_mrr_at_3=7/12,
        profile_isolation_pass_count=5, profile_privacy_leakage_count=0, profile_private_leakage_count=0, profile_inactive_leakage_count=0, profile_cross_profile_leakage_count=0,
        stale_correct_count=5, stale_correct_rate=1.0,
    )
    status, reasons = engine.qualify_candidate(clean_metrics)
    assert status == "QUALIFIED"
    assert reasons == ()

    # 2. Multiple failures: Hard=15, NoMem=1 injection, NearWrong=6/12, Privacy=1 leak
    dirty_metrics = MetricsObject(
        hard_correct_at_1_count=15, hard_correct_at_3_count=16, hard_recall_at_1=15/16, hard_recall_at_3=1.0, hard_mrr_at_3=0.95,
        syn_correct_at_1_count=14, syn_correct_at_3_count=14, syn_recall_at_1=1.0, syn_recall_at_3=1.0, syn_mrr_at_3=1.0,
        para_correct_at_1_count=16, para_correct_at_3_count=16, para_recall_at_1=1.0, para_recall_at_3=1.0, para_mrr_at_3=1.0,
        semantic_quality_score=1.0,
        no_memory_abstention_count=11, no_memory_false_injection_count=1, no_memory_abstention_accuracy=11/12,
        near_wrong_correct_at_1_count=6, near_wrong_precision_at_1=6/12, near_wrong_target_mrr_at_3=6/12,
        profile_isolation_pass_count=4, profile_privacy_leakage_count=1, profile_private_leakage_count=1, profile_inactive_leakage_count=0, profile_cross_profile_leakage_count=0,
        stale_correct_count=5, stale_correct_rate=1.0,
    )
    status_d, reasons_d = engine.qualify_candidate(dirty_metrics)
    assert status_d == "CRITICAL_REGRESSION"
    assert reasons_d == (
        "HARD_RECALL_REGRESSION",
        "NO_MEMORY_INJECTION",
        "NEAR_WRONG_REGRESSION",
        "PROFILE_PRIVACY_LEAKAGE",
    )


def test_near_wrong_discrete_boundary_comparisons():
    """Task 5.5: Exact discrete count comparisons against baseline A (7)."""
    engine = LockedQualificationEngine(baseline_a_near_wrong_count=7)

    def _make_nw_metrics(nw_count):
        return MetricsObject(
            hard_correct_at_1_count=16, hard_correct_at_3_count=16, hard_recall_at_1=1.0, hard_recall_at_3=1.0, hard_mrr_at_3=1.0,
            syn_correct_at_1_count=14, syn_correct_at_3_count=14, syn_recall_at_1=1.0, syn_recall_at_3=1.0, syn_mrr_at_3=1.0,
            para_correct_at_1_count=16, para_correct_at_3_count=16, para_recall_at_1=1.0, para_recall_at_3=1.0, para_mrr_at_3=1.0,
            semantic_quality_score=1.0,
            no_memory_abstention_count=12, no_memory_false_injection_count=0, no_memory_abstention_accuracy=1.0,
            near_wrong_correct_at_1_count=nw_count, near_wrong_precision_at_1=nw_count/12, near_wrong_target_mrr_at_3=nw_count/12,
            profile_isolation_pass_count=5, profile_privacy_leakage_count=0, profile_private_leakage_count=0, profile_inactive_leakage_count=0, profile_cross_profile_leakage_count=0,
            stale_correct_count=5, stale_correct_rate=1.0,
        )

    # 7/12 -> Passes (no NEAR_WRONG_REGRESSION)
    assert engine.qualify_candidate(_make_nw_metrics(7))[0] == "QUALIFIED"
    # 8/12 -> Passes
    assert engine.qualify_candidate(_make_nw_metrics(8))[0] == "QUALIFIED"
    # 6/12 -> Fails with NEAR_WRONG_REGRESSION
    st_6, r_6 = engine.qualify_candidate(_make_nw_metrics(6))
    assert st_6 == "CRITICAL_REGRESSION"
    assert r_6 == ("NEAR_WRONG_REGRESSION",)


def test_non_evaluable_candidates_not_evaluated_on_locked():
    """Task 5.6: NON_EVALUABLE candidates remain NOT_APPLICABLE and metrics=None."""
    engine = LockedQualificationEngine(baseline_a_near_wrong_count=7)
    evidences, truths = _make_mock_80_locked_cases()

    non_eval_res = CandidateResult("H1_MARGIN", "NON_EVALUABLE", "CALIBRATION_FAILED", "NOT_APPLICABLE", ())
    evaluated_res, metrics = engine.evaluate_candidate_locked(H1MarginCandidate(), non_eval_res, evidences, truths)

    assert evaluated_res.execution_status == "NON_EVALUABLE"
    assert evaluated_res.execution_reason == "CALIBRATION_FAILED"
    assert evaluated_res.quality_status == "NOT_APPLICABLE"
    assert evaluated_res.critical_reasons == ()
    assert metrics is None


def test_pre_locked_snapshot_identity_binding_and_golden_locked_evaluation():
    """
    Task 5.7: Authoritative 80-case evaluation bound directly to pre-locked snapshot (Engram #6313).
    Asserts exact golden observations for A, B, H1, H2, and H4.
    """
    # 1. Verify fixture and snapshot file SHA-256 hashes
    assert _compute_file_sha256(LOCKED_FIXTURE_PATH) == LOCKED_HASH
    assert _compute_file_sha256(CAL_FIXTURE_PATH) == CAL_HASH

    with open(PRE_LOCKED_SNAPSHOT_PATH, "r", encoding="utf-8") as f:
        snapshot_data = json.load(f)

    canon_bytes = json.dumps(snapshot_data, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode('utf-8')
    assert hashlib.sha256(canon_bytes).hexdigest() == PRE_LOCKED_SNAPSHOT_HASH

    assert snapshot_data["git_commit"] == "8ad1f7e"
    assert snapshot_data["calibration_fixture_hash"] == CAL_HASH
    assert snapshot_data["model_artifact_hash"] == MODEL_HASH
    assert snapshot_data["shared_config_hash"] == CONFIG_HASH
    assert snapshot_data["runtime_lock_identity"] == "cpython-3.10.20-Windows-AMD64"

    # 2. Build candidate instances and match against pre-locked snapshot
    manifest = CandidateManifest.default_manifest()
    cand_map = {c.mechanism_key: c for c in manifest.singletons}

    snap_candidates = snapshot_data["candidates"]
    assert set(snap_candidates.keys()) == {"H1_MARGIN", "H2_LEXICAL_CORROBORATION", "H4_ASYMMETRIC_GATE"}

    # 3. Load locked evidence
    locked_data = load_fixture(LOCKED_FIXTURE_PATH)
    assert len(locked_data["cases"]) == 80

    embedder = MiniLMEmbedder(MODEL_DIR)
    embedder.initialize()

    locked_evs, locked_trs = [], []
    for c in locked_data["cases"]:
        ev, tr = generate_case_evidence(c, embedder=embedder)
        locked_evs.append(ev)
        locked_trs.append(tr)

    # 4. Evaluate Baseline A
    metrics_a = evaluate_baseline_a(locked_evs, locked_trs)
    assert metrics_a.hard_correct_at_1_count == 16
    assert metrics_a.hard_recall_at_1 == 1.0
    assert metrics_a.no_memory_abstention_count == 12
    assert metrics_a.no_memory_false_injection_count == 0
    assert metrics_a.near_wrong_correct_at_1_count == 7
    assert metrics_a.near_wrong_precision_at_1 == 7 / 12
    assert metrics_a.profile_isolation_pass_count == 5
    assert metrics_a.profile_privacy_leakage_count == 0
    assert metrics_a.semantic_quality_score == 0.0

    # 5. Evaluate Baseline B Reference
    metrics_b = evaluate_baseline_b(locked_evs, locked_trs, baseline_a_near_wrong_count=metrics_a.near_wrong_correct_at_1_count)
    assert metrics_b.hard_correct_at_1_count == 16
    assert abs(metrics_b.syn_mrr_at_3 - 0.9285714285714286) < 1e-6
    assert abs(metrics_b.para_mrr_at_3 - 0.75) < 1e-6
    assert abs(metrics_b.semantic_quality_score - 0.8392857142857143) < 1e-6
    assert metrics_b.no_memory_abstention_count == 11
    assert metrics_b.no_memory_false_injection_count == 1

    qual_engine = LockedQualificationEngine(baseline_a_near_wrong_count=metrics_a.near_wrong_correct_at_1_count)
    status_b, reasons_b = qual_engine.qualify_candidate(metrics_b)
    assert status_b == "CRITICAL_REGRESSION"
    assert reasons_b == ("NO_MEMORY_INJECTION",)

    # 6. Evaluate Pre-Locked Sealed Candidates

    # --- H1_MARGIN ---
    snap_h1 = snap_candidates["H1_MARGIN"]
    rec_h1 = CalibrationRecord(
        candidate_id=snap_h1["candidate_id"],
        mechanism_key=snap_h1["mechanism_key"],
        selected_params=snap_h1["selected_params"],
        cal_syn_para_mrr3_mean=snap_h1["metrics"]["cal_syn_para_mrr3_mean"],
        cal_hard_recall_at_1=snap_h1["metrics"]["cal_hard_recall_at_1"],
        cal_no_memory_abstention_accuracy=snap_h1["metrics"]["cal_no_memory_abstention_accuracy"],
        cal_no_memory_false_injection_count=snap_h1["metrics"]["cal_no_memory_false_injection_count"],
        cal_profile_privacy_leakage_count=snap_h1["metrics"]["cal_profile_privacy_leakage_count"],
        calibration_fixture_hash=snap_h1["calibration_fixture_hash"],
        model_artifact_hash=snap_h1["model_artifact_hash"],
        shared_config_hash=snap_h1["shared_config_hash"],
        frozen_at=snap_h1["frozen_at"],
        frozen_sequence=snap_h1["frozen_sequence"],
    )
    cal_res_h1 = CandidateResult("H1_MARGIN", "EVALUABLE", None, "NOT_APPLICABLE", (), rec_h1)
    res_h1, mets_h1 = qual_engine.evaluate_candidate_locked(
        cand_map["H1_MARGIN"],
        cal_res_h1,
        locked_evs,
        locked_trs,
        baseline_b_semantic_score=metrics_b.semantic_quality_score,
        baseline_a_semantic_score=metrics_a.semantic_quality_score,
    )
    assert res_h1.quality_status == "CRITICAL_REGRESSION"
    assert res_h1.critical_reasons == ("NO_MEMORY_INJECTION",)
    assert mets_h1.hard_correct_at_1_count == 16
    assert abs(mets_h1.syn_mrr_at_3 - 0.8571428571428571) < 1e-6
    assert abs(mets_h1.para_mrr_at_3 - 0.71875) < 1e-6
    assert abs(mets_h1.semantic_quality_score - 0.7879464285714286) < 1e-6
    assert mets_h1.no_memory_abstention_count == 11
    assert mets_h1.no_memory_false_injection_count == 1
    assert mets_h1.near_wrong_correct_at_1_count == 11
    assert mets_h1.profile_isolation_pass_count == 5
    assert mets_h1.profile_privacy_leakage_count == 0
    assert abs(mets_h1.retention_vs_b - 0.9388297872340425) < 1e-6
    assert abs(mets_h1.gain_over_a - 0.7879464285714286) < 1e-6

    # --- H2_LEXICAL_CORROBORATION ---
    snap_h2 = snap_candidates["H2_LEXICAL_CORROBORATION"]
    rec_h2 = CalibrationRecord(
        candidate_id=snap_h2["candidate_id"],
        mechanism_key=snap_h2["mechanism_key"],
        selected_params=snap_h2["selected_params"],
        cal_syn_para_mrr3_mean=snap_h2["metrics"]["cal_syn_para_mrr3_mean"],
        cal_hard_recall_at_1=snap_h2["metrics"]["cal_hard_recall_at_1"],
        cal_no_memory_abstention_accuracy=snap_h2["metrics"]["cal_no_memory_abstention_accuracy"],
        cal_no_memory_false_injection_count=snap_h2["metrics"]["cal_no_memory_false_injection_count"],
        cal_profile_privacy_leakage_count=snap_h2["metrics"]["cal_profile_privacy_leakage_count"],
        calibration_fixture_hash=snap_h2["calibration_fixture_hash"],
        model_artifact_hash=snap_h2["model_artifact_hash"],
        shared_config_hash=snap_h2["shared_config_hash"],
        frozen_at=snap_h2["frozen_at"],
        frozen_sequence=snap_h2["frozen_sequence"],
    )
    cal_res_h2 = CandidateResult("H2_LEXICAL_CORROBORATION", "EVALUABLE", None, "NOT_APPLICABLE", (), rec_h2)
    res_h2, mets_h2 = qual_engine.evaluate_candidate_locked(
        cand_map["H2_LEXICAL_CORROBORATION"],
        cal_res_h2,
        locked_evs,
        locked_trs,
        baseline_b_semantic_score=metrics_b.semantic_quality_score,
        baseline_a_semantic_score=metrics_a.semantic_quality_score,
    )
    assert res_h2.quality_status == "QUALIFIED"
    assert res_h2.critical_reasons == ()
    assert mets_h2.hard_correct_at_1_count == 16
    assert mets_h2.syn_mrr_at_3 == 0.0
    assert abs(mets_h2.para_mrr_at_3 - 0.15625) < 1e-6
    assert abs(mets_h2.semantic_quality_score - 0.078125) < 1e-6
    assert mets_h2.no_memory_abstention_count == 12
    assert mets_h2.no_memory_false_injection_count == 0
    assert mets_h2.near_wrong_correct_at_1_count == 10
    assert mets_h2.profile_isolation_pass_count == 5
    assert mets_h2.profile_privacy_leakage_count == 0
    assert abs(mets_h2.retention_vs_b - 0.09308510638297872) < 1e-6
    assert abs(mets_h2.gain_over_a - 0.078125) < 1e-6

    # --- H4_ASYMMETRIC_GATE ---
    snap_h4 = snap_candidates["H4_ASYMMETRIC_GATE"]
    rec_h4 = CalibrationRecord(
        candidate_id=snap_h4["candidate_id"],
        mechanism_key=snap_h4["mechanism_key"],
        selected_params=snap_h4["selected_params"],
        cal_syn_para_mrr3_mean=snap_h4["metrics"]["cal_syn_para_mrr3_mean"],
        cal_hard_recall_at_1=snap_h4["metrics"]["cal_hard_recall_at_1"],
        cal_no_memory_abstention_accuracy=snap_h4["metrics"]["cal_no_memory_abstention_accuracy"],
        cal_no_memory_false_injection_count=snap_h4["metrics"]["cal_no_memory_false_injection_count"],
        cal_profile_privacy_leakage_count=snap_h4["metrics"]["cal_profile_privacy_leakage_count"],
        calibration_fixture_hash=snap_h4["calibration_fixture_hash"],
        model_artifact_hash=snap_h4["model_artifact_hash"],
        shared_config_hash=snap_h4["shared_config_hash"],
        frozen_at=snap_h4["frozen_at"],
        frozen_sequence=snap_h4["frozen_sequence"],
    )
    cal_res_h4 = CandidateResult("H4_ASYMMETRIC_GATE", "EVALUABLE", None, "NOT_APPLICABLE", (), rec_h4)
    res_h4, mets_h4 = qual_engine.evaluate_candidate_locked(
        cand_map["H4_ASYMMETRIC_GATE"],
        cal_res_h4,
        locked_evs,
        locked_trs,
        baseline_b_semantic_score=metrics_b.semantic_quality_score,
        baseline_a_semantic_score=metrics_a.semantic_quality_score,
    )
    assert res_h4.quality_status == "CRITICAL_REGRESSION"
    assert res_h4.critical_reasons == ("NO_MEMORY_INJECTION",)
    assert mets_h4.hard_correct_at_1_count == 16
    assert abs(mets_h4.syn_mrr_at_3 - 0.8571428571428571) < 1e-6
    assert abs(mets_h4.para_mrr_at_3 - 0.78125) < 1e-6
    assert abs(mets_h4.semantic_quality_score - 0.8191964285714286) < 1e-6
    assert mets_h4.no_memory_abstention_count == 11
    assert mets_h4.no_memory_false_injection_count == 1
    assert mets_h4.near_wrong_correct_at_1_count == 11
    assert mets_h4.profile_isolation_pass_count == 5
    assert mets_h4.profile_privacy_leakage_count == 0
    assert abs(mets_h4.retention_vs_b - 0.976063829787234) < 1e-6
    assert abs(mets_h4.gain_over_a - 0.8191964285714286) < 1e-6
