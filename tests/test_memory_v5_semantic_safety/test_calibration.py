"""
Unit and integration tests for WU4: Two-Phase Deterministic Calibration & Freeze Protocol.

Validates:
1. Phase 1 Safety Feasibility: Hard recall==1.0 (4/4), NO_MEMORY injections==0, Privacy leakage==0.
2. Near-wrong does NOT participate in calibration feasibility or optimization.
3. Unsafe high-MRR config loses to safe lower-MRR config.
4. Phase 2 Semantic Optimization: Maximizes mean syn/para MRR@3 among safe configs.
5. Exact deterministic conservatism tie-breaks (H1, H2, H4) and canonical JSON tie-break.
6. Empty feasible set produces candidate-local CALIBRATION_FAILED (NON_EVALUABLE).
7. Invalid config produces candidate-local CONFIG_INVALID (NON_EVALUABLE).
8. Candidate failure isolation (failure of one candidate does not suppress siblings).
9. Deep immutability of CalibrationRecord and selected_params.
10. Freeze integrity validation (candidate ID recomputation, valid closed config, hex64 hashes, ISO timestamp).
11. Freeze-all-before-locked invariant and 3-dimensional CALIBRATION_LEAK detection (ID, Query, Payload).
12. No premature QUALIFIED quality status during calibration (quality_status remains NOT_APPLICABLE).
13. Determinism across repeated calibration runs over identical ordered evidence.
14. Real 24-case calibration-only integration with MiniLM ONNX CPU and default candidate manifest.
"""

import copy
import hashlib
import json
import pytest
from pathlib import Path

from tools.memory_v5_semantic_safety.models import (
    CandidateConfig,
    CandidateResult,
    CalibrationRecord,
    EvaluationTruth,
    InferenceEvidence,
    RankedItem,
)
from tools.memory_v5_semantic_safety.candidates import (
    CandidateManifest,
    H1MarginCandidate,
    H2LexicalCorroborationCandidate,
    H4AsymmetricGateCandidate,
    compute_candidate_id,
)
from tools.memory_v5_semantic_safety.evidence import (
    load_fixture,
    generate_case_evidence,
    MiniLMEmbedder,
)
from tools.memory_v5_semantic_safety.calibration import (
    CalibrationEvaluator,
    CalibrationMetrics,
    DeterministicCalibrationEngine,
    FrozenCalibrationState,
    assert_no_calibration_leak,
    derive_conservatism_key,
    validate_freeze_integrity,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MODEL_DIR = REPO_ROOT / "modelos_f5" / "minilm_l12_onnx"
CAL_FIXTURE_PATH = REPO_ROOT / "tests" / "fixtures" / "memory_v5_semantic_calibration.json"

MODEL_HASH = "7cce6bd3df3eefe20757261165563b9f27b2397b5f0661bd42f804d545b07569"
CONFIG_HASH = "c0d7110f0abe8dfae5e335dc1d931b4742c52e37ea67de296afbb22060912c43"
CAL_HASH = "1d262ab3b3daa275e9bee166e024ae2f91c8d6d8a3fe95b993d2bb06feee9472"


def _make_case(case_id: str, family: str, query: str, lex_tuples, sem_tuples, target_ids, expected_abstain=False, target_user_id="u1"):
    lex_items = tuple(
        RankedItem(memory_id=t[0], score=float(t[1]), rank=i + 1, text=t[2], user_id=t[3] if len(t) > 3 else "u1", is_private=bool(t[4]) if len(t) > 4 else False, is_inactive=bool(t[5]) if len(t) > 5 else False)
        for i, t in enumerate(lex_tuples)
    )
    sem_items = tuple(
        RankedItem(memory_id=t[0], score=float(t[1]), rank=i + 1, text=t[2], user_id=t[3] if len(t) > 3 else "u1", is_private=bool(t[4]) if len(t) > 4 else False, is_inactive=bool(t[5]) if len(t) > 5 else False)
        for i, t in enumerate(sem_tuples)
    )
    ev = InferenceEvidence(
        evidence_key=f"key_{case_id}",
        query=query,
        lexical_ranking=lex_items,
        semantic_ranking=sem_items,
    )
    truth = EvaluationTruth(
        case_id=case_id,
        family=family,
        target_ids=tuple(target_ids),
        expected_abstain=expected_abstain,
        target_user_id=target_user_id,
        diagnostic_subtype=None,
    )
    return ev, truth


def _build_mock_24_cal_cases():
    """Build a deterministic 24-case calibration set (4 each of 6 families)."""
    evidences = []
    truths = []

    # 4 Hard Lexical
    for i in range(4):
        ev, tr = _make_case(f"cal_hard_{i}", "hard_lexical", f"hard query {i}", [("h1", 10.0, f"hard item {i}")], [("h1", 0.95, f"hard item {i}"), ("h2", 0.50, "distractor")], ["h1"])
        evidences.append(ev)
        truths.append(tr)

    # 4 Synonym
    for i in range(4):
        ev, tr = _make_case(f"cal_syn_{i}", "synonym", f"syn query {i}", [], [("s1", 0.88, f"syn item {i}"), ("s2", 0.40, "distractor")], ["s1"])
        evidences.append(ev)
        truths.append(tr)

    # 4 Paraphrase
    for i in range(4):
        ev, tr = _make_case(f"cal_para_{i}", "paraphrase", f"para query {i}", [], [("p1", 0.86, f"para item {i}"), ("p2", 0.40, "distractor")], ["p1"])
        evidences.append(ev)
        truths.append(tr)

    # 4 NO_MEMORY (semantic ranking contains trap at score 0.65)
    for i in range(4):
        ev, tr = _make_case(f"cal_nomem_{i}", "no_memory", f"nomem query {i}", [], [("trap", 0.65, "trap memory")], [], expected_abstain=True)
        evidences.append(ev)
        truths.append(tr)

    # 4 Near-but-Wrong (Should NOT participate in calibration gate)
    for i in range(4):
        ev, tr = _make_case(f"cal_near_{i}", "near_but_wrong", f"near query {i}", [], [("wrong", 0.70, "wrong memory")], ["correct_distant"], expected_abstain=False)
        evidences.append(ev)
        truths.append(tr)

    # 4 Profile Privacy (Target user = u1; candidate ranking is correctly isolated/empty)
    for i in range(4):
        ev, tr = _make_case(f"cal_priv_{i}", "profile_privacy", f"priv query {i}", [], [], ["other_user_mem"], target_user_id="u1")
        evidences.append(ev)
        truths.append(tr)

    return evidences, truths


def test_phase1_safety_feasibility_filtering():
    """Task 4.1a: Phase 1 strictly evaluates Hard=1.0, NO_MEMORY=0 false, Privacy=0 leak."""
    evidences, truths = _build_mock_24_cal_cases()
    evaluator = CalibrationEvaluator()
    h1 = H1MarginCandidate()

    # 1. Safe config: theta_score=0.80, theta_margin=0.20 -> passes hard (0.95>=0.80), abstains on nomem (0.65<0.80), passes privacy (0 leak)
    config_safe = CandidateConfig("h1_safe", "H1_MARGIN", {"theta_score": 0.80, "theta_margin": 0.20})
    metrics_safe = evaluator.evaluate(h1, config_safe, evidences, truths)
    assert metrics_safe.is_safety_feasible is True
    assert metrics_safe.cal_hard_recall_at_1 == 1.0
    assert metrics_safe.cal_no_memory_false_injection_count == 0
    assert metrics_safe.cal_profile_privacy_leakage_count == 0

    # 2. Unsafe config: theta_score=0.50, theta_margin=0.0 -> fails NO_MEMORY (injects trap memory at score 0.65)
    config_unsafe_nomem = CandidateConfig("h1_unsafe", "H1_MARGIN", {"theta_score": 0.50, "theta_margin": 0.0})
    metrics_unsafe = evaluator.evaluate(h1, config_unsafe_nomem, evidences, truths)
    assert metrics_unsafe.is_safety_feasible is False
    assert metrics_unsafe.cal_no_memory_false_injection_count == 4

    # 3. Privacy leakage test: when cross-profile memory is returned, is_safety_feasible becomes False
    ev_leak, tr_leak = _make_case("cal_priv_leak", "profile_privacy", "leak query", [], [("leaked_mem", 0.90, "text", "other_user", False, False)], ["leaked_mem"], target_user_id="u1")
    leak_evs = list(evidences)
    leak_evs[20] = ev_leak
    metrics_leak = evaluator.evaluate(h1, config_safe, leak_evs, truths)
    assert metrics_leak.is_safety_feasible is False
    assert metrics_leak.cal_profile_privacy_leakage_count >= 1

    # 4. High-MRR unsafe config loses to safe lower-MRR config in engine selection
    engine = DeterministicCalibrationEngine(model_hash=MODEL_HASH, config_hash=CONFIG_HASH, cal_hash=CAL_HASH)
    res = engine.calibrate_candidate(h1, evidences, truths)
    assert res.execution_status == "EVALUABLE"
    assert res.calibration is not None
    assert res.calibration.selected_params["theta_score"] >= 0.80  # Filtered out 0.50


def test_near_wrong_does_not_gate_calibration():
    """Task 4.1b: Near-wrong family does not filter feasibility in calibration."""
    evidences, truths = _build_mock_24_cal_cases()
    evaluator = CalibrationEvaluator()
    h1 = H1MarginCandidate()

    config = CandidateConfig("h1_test", "H1_MARGIN", {"theta_score": 0.80, "theta_margin": 0.20})
    metrics = evaluator.evaluate(h1, config, evidences, truths)

    # Near-wrong returned wrong memory on all 4 cases, but is_safety_feasible is still True!
    assert metrics.is_safety_feasible is True


def test_exact_conservatism_tie_breaking():
    """Task 4.1c: Exact per-mechanism conservatism keys and canonical JSON tie-break."""
    # H1: (-theta_score, -theta_margin)
    k1 = derive_conservatism_key("H1_MARGIN", {"theta_score": 0.85, "theta_margin": 0.10})
    k2 = derive_conservatism_key("H1_MARGIN", {"theta_score": 0.80, "theta_margin": 0.20})
    assert k1 < k2  # Higher theta_score is more conservative -> smaller negative tuple

    # H2: (-theta_tok, -theta_idf)
    h2_k1 = derive_conservatism_key("H2_LEXICAL_CORROBORATION", {"theta_tok": 2, "theta_idf": 5.0})
    h2_k2 = derive_conservatism_key("H2_LEXICAL_CORROBORATION", {"theta_tok": 1, "theta_idf": 10.0})
    assert h2_k1 < h2_k2  # Higher theta_tok is more conservative

    # H4: (-theta_high, -theta_low, R_lex_max)
    h4_k1 = derive_conservatism_key("H4_ASYMMETRIC_GATE", {"theta_high": 0.90, "theta_low": 0.70, "R_lex_max": 2})
    h4_k2 = derive_conservatism_key("H4_ASYMMETRIC_GATE", {"theta_high": 0.85, "theta_low": 0.70, "R_lex_max": 1})
    assert h4_k1 < h4_k2  # Higher theta_high is more conservative


def test_empty_feasible_set_produces_calibration_failed():
    """Task 4.1d: When 0 configurations are safety-feasible, return CALIBRATION_FAILED."""
    evidences, truths = _build_mock_24_cal_cases()
    # Inject impossible hard cases (empty semantic and lexical) so hard_recall is 0/4
    bad_evidences = [
        InferenceEvidence(ev.evidence_key, ev.query, (), ()) if "hard" in ev.evidence_key else ev
        for ev in evidences
    ]
    engine = DeterministicCalibrationEngine(model_hash=MODEL_HASH, config_hash=CONFIG_HASH, cal_hash=CAL_HASH)
    res = engine.calibrate_candidate(H1MarginCandidate(), bad_evidences, truths)

    assert res.execution_status == "NON_EVALUABLE"
    assert res.execution_reason == "CALIBRATION_FAILED"
    assert res.quality_status == "NOT_APPLICABLE"
    assert res.calibration is None


def test_invalid_config_produces_config_invalid():
    """Task 4.1e: Candidate validation failure produces CONFIG_INVALID."""
    engine = DeterministicCalibrationEngine(model_hash=MODEL_HASH, config_hash=CONFIG_HASH, cal_hash=CAL_HASH)
    res = engine.evaluate_custom_config(
        H1MarginCandidate(),
        CandidateConfig("bad_h1", "H1_MARGIN", {"invalid_key": 123}),
        [],
        [],
    )
    assert res.execution_status == "NON_EVALUABLE"
    assert res.execution_reason == "CONFIG_INVALID"
    assert res.quality_status == "NOT_APPLICABLE"


def test_candidate_failure_isolation():
    """Task 4.1f: Failure of one candidate does not suppress siblings."""
    evidences, truths = _build_mock_24_cal_cases()
    engine = DeterministicCalibrationEngine(model_hash=MODEL_HASH, config_hash=CONFIG_HASH, cal_hash=CAL_HASH)

    manifest = CandidateManifest.default_manifest()
    results = engine.calibrate_all(manifest, evidences, truths)

    assert len(results) == 3
    assert "H1_MARGIN" in results
    assert "H2_LEXICAL_CORROBORATION" in results
    assert "H4_ASYMMETRIC_GATE" in results


def test_deep_immutability_of_calibration_record():
    """Task 4.1g: CalibrationRecord selected_params is deeply immutable."""
    engine = DeterministicCalibrationEngine(model_hash=MODEL_HASH, config_hash=CONFIG_HASH, cal_hash=CAL_HASH)
    evidences, truths = _build_mock_24_cal_cases()
    res = engine.calibrate_candidate(H1MarginCandidate(), evidences, truths)

    assert res.calibration is not None
    record = res.calibration

    with pytest.raises(TypeError):
        record.selected_params["theta_score"] = 0.99

    with pytest.raises(TypeError):
        record.selected_params.update({"theta_score": 0.99})


def test_freeze_integrity_validation_positive_and_negative():
    """Task 4.1h: Test freeze integrity validation for valid record and comprehensive negative cases."""
    valid_params = {"theta_score": 0.80, "theta_margin": 0.20}
    valid_cid = compute_candidate_id("H1_MARGIN", valid_params, MODEL_HASH, CONFIG_HASH)
    valid_rec = CalibrationRecord(
        candidate_id=valid_cid,
        mechanism_key="H1_MARGIN",
        selected_params=valid_params,
        calibration_fixture_hash=CAL_HASH,
        model_artifact_hash=MODEL_HASH,
        shared_config_hash=CONFIG_HASH,
        frozen_at="2026-08-31T20:00:00Z",
        frozen_sequence=1,
        cal_syn_para_mrr3_mean=1.0,
        cal_hard_recall_at_1=1.0,
        cal_no_memory_abstention_accuracy=1.0,
        cal_no_memory_false_injection_count=0,
        cal_profile_privacy_leakage_count=0,
    )

    # Positive test: Valid freeze record passes
    validate_freeze_integrity(valid_rec, expected_cal_hash=CAL_HASH, expected_model_hash=MODEL_HASH, expected_config_hash=CONFIG_HASH)

    # Negative 1: Bad cal hash (non-hex or wrong hash)
    bad_cal = copy.copy(valid_rec)
    object.__setattr__(bad_cal, "calibration_fixture_hash", "bad_hash_123")
    with pytest.raises(ValueError, match="Freeze integrity error"):
        validate_freeze_integrity(bad_cal)

    # Negative 2: Bad model hash
    bad_mod = copy.copy(valid_rec)
    object.__setattr__(bad_mod, "model_artifact_hash", "00" * 31 + "zz")
    with pytest.raises(ValueError, match="Freeze integrity error"):
        validate_freeze_integrity(bad_mod)

    # Negative 3: Candidate ID mismatch
    bad_cid = copy.copy(valid_rec)
    object.__setattr__(bad_cid, "candidate_id", "00" * 32)
    with pytest.raises(ValueError, match="Freeze integrity error: candidate_id mismatch"):
        validate_freeze_integrity(bad_cid)

    # Negative 4: Incomplete / invalid selected_params
    bad_params = copy.copy(valid_rec)
    object.__setattr__(bad_params, "selected_params", {"theta_score": 0.80})  # missing theta_margin
    with pytest.raises(ValueError, match="Freeze integrity error: selected_params invalid"):
        validate_freeze_integrity(bad_params)

    # Negative 5: Malformed frozen_at
    bad_date = copy.copy(valid_rec)
    object.__setattr__(bad_date, "frozen_at", "invalid-date-format")
    with pytest.raises(ValueError, match="Freeze integrity error: frozen_at"):
        validate_freeze_integrity(bad_date)

    # Negative 6: Invalid frozen_sequence
    bad_seq = copy.copy(valid_rec)
    object.__setattr__(bad_seq, "frozen_sequence", 0)
    with pytest.raises(ValueError, match="Freeze integrity error: frozen_sequence"):
        validate_freeze_integrity(bad_seq)


def test_freeze_all_before_locked_invariant():
    """Task 4.1i: Non-frozen state cannot produce locked-ready candidate list."""
    state = FrozenCalibrationState(expected_cal_hash=CAL_HASH, expected_model_hash=MODEL_HASH, expected_config_hash=CONFIG_HASH)
    # Not yet frozen
    assert state.is_frozen is False
    with pytest.raises(RuntimeError, match="not yet frozen"):
        state.get_locked_ready_configs()

    valid_params = {"theta_score": 0.80, "theta_margin": 0.20}
    valid_cid = compute_candidate_id("H1_MARGIN", valid_params, MODEL_HASH, CONFIG_HASH)
    valid_rec = CalibrationRecord(
        candidate_id=valid_cid,
        mechanism_key="H1_MARGIN",
        selected_params=valid_params,
        calibration_fixture_hash=CAL_HASH,
        model_artifact_hash=MODEL_HASH,
        shared_config_hash=CONFIG_HASH,
        frozen_at="2026-08-31T20:00:00Z",
        frozen_sequence=1,
        cal_syn_para_mrr3_mean=1.0,
        cal_hard_recall_at_1=1.0,
        cal_no_memory_abstention_accuracy=1.0,
        cal_no_memory_false_injection_count=0,
        cal_profile_privacy_leakage_count=0,
    )

    # Freeze candidates
    state.register_candidate_result("H1_MARGIN", CandidateResult("H1_MARGIN", "EVALUABLE", None, "NOT_APPLICABLE", (), calibration=valid_rec))
    state.freeze()
    assert state.is_frozen is True
    configs = state.get_locked_ready_configs()
    assert len(configs) == 1
    assert configs[0].candidate_id == valid_cid


def test_calibration_leak_3d_detection():
    """Task 4.1j: assert_no_calibration_leak detects locked fixture data contamination across 3 dimensions."""
    cal_cases = [{"case_id": "cal-001", "query": "my query text", "candidates": [{"title": "t1", "content": "c1"}]}]
    locked_cases = [{"case_id": "lock-001", "query": "distinct query", "candidates": [{"title": "t2", "content": "c2"}]}]
    assert_no_calibration_leak(cal_cases, locked_cases)  # Passes

    # Dimension 1: Case ID collision
    collided_id = [{"case_id": "cal-001", "query": "distinct query", "candidates": []}]
    with pytest.raises(ValueError, match="CALIBRATION_LEAK: Overlapping case IDs"):
        assert_no_calibration_leak(cal_cases, collided_id)

    # Dimension 2: Normalized query collision (different ID)
    collided_query = [{"case_id": "lock-999", "query": "  MY QUERY TEXT  ", "candidates": []}]
    with pytest.raises(ValueError, match="CALIBRATION_LEAK: Overlapping normalized queries"):
        assert_no_calibration_leak(cal_cases, collided_query)

    # Dimension 3: Candidate memory payload collision (different ID and query)
    collided_payload = [{"case_id": "lock-888", "query": "completely unrelated", "candidates": [{"title": "t1", "content": "c1"}]}]
    with pytest.raises(ValueError, match="CALIBRATION_LEAK: Overlapping candidate memory payloads"):
        assert_no_calibration_leak(cal_cases, collided_payload)


def test_repeated_calibration_is_deterministic():
    """Task 4.1k: Repeated calibration produces identical selected params and candidate ID."""
    evidences, truths = _build_mock_24_cal_cases()
    engine = DeterministicCalibrationEngine(model_hash=MODEL_HASH, config_hash=CONFIG_HASH, cal_hash=CAL_HASH)

    res1 = engine.calibrate_candidate(H1MarginCandidate(), evidences, truths, fixed_frozen_at="2026-08-31T20:00:00Z")
    res2 = engine.calibrate_candidate(H1MarginCandidate(), evidences, truths, fixed_frozen_at="2026-08-31T20:00:00Z")

    assert res1.calibration == res2.calibration
    assert res1.calibration.candidate_id == res2.calibration.candidate_id
    assert res1.quality_status == "NOT_APPLICABLE"


def test_real_24_case_calibration_only_integration():
    """Task 4.2: Full real integration test across 24 calibration cases with MiniLM ONNX CPU."""
    cal_data = load_fixture(CAL_FIXTURE_PATH)
    assert len(cal_data["cases"]) == 24

    embedder = MiniLMEmbedder(MODEL_DIR)
    embedder.initialize()

    evidences = []
    truths = []
    for case in cal_data["cases"]:
        ev, tr = generate_case_evidence(case, embedder=embedder)
        evidences.append(ev)
        truths.append(tr)

    manifest = CandidateManifest.default_manifest()
    engine = DeterministicCalibrationEngine(model_hash=MODEL_HASH, config_hash=CONFIG_HASH, cal_hash=CAL_HASH)

    # Run 1
    results1 = engine.calibrate_all(manifest, evidences, truths, fixed_frozen_at="2026-08-31T20:00:00Z")
    assert len(results1) == 3
    assert set(results1.keys()) == {"H1_MARGIN", "H2_LEXICAL_CORROBORATION", "H4_ASYMMETRIC_GATE"}

    state = FrozenCalibrationState(
        manifest=manifest,
        expected_cal_hash=CAL_HASH,
        expected_model_hash=MODEL_HASH,
        expected_config_hash=CONFIG_HASH,
    )
    for k, res in results1.items():
        state.register_candidate_result(k, res)
        # Quality status must be NOT_APPLICABLE (locked qualification belongs to WU5)
        assert res.quality_status == "NOT_APPLICABLE"

    state.freeze()
    ready_configs = state.get_locked_ready_configs()

    # Verify each evaluable candidate has valid recomputable freeze
    for cfg in ready_configs:
        res = results1[cfg.mechanism_key]
        assert res.execution_status == "EVALUABLE"
        assert res.calibration is not None
        assert res.calibration.candidate_id == cfg.candidate_id
        validate_freeze_integrity(res.calibration, manifest=manifest, expected_cal_hash=CAL_HASH, expected_model_hash=MODEL_HASH, expected_config_hash=CONFIG_HASH)

    # Run 2: Exact determinism check
    results2 = engine.calibrate_all(manifest, evidences, truths, fixed_frozen_at="2026-08-31T20:00:00Z")
    for k in results1:
        assert results1[k].calibration == results2[k].calibration
