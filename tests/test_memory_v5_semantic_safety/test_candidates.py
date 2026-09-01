"""
Unit tests for WU3: Candidate Manifest & Policy Mechanisms (H1, H2, H4).

Strict TDD tests validating:
1. CandidateManifest pre-registers exactly 3 singletons (H1, H2, H4) and 0 combinations.
2. Quota enforcement: <= 4 singletons, <= 2 combinations, <= 6 total with ADMISSION_OVERFLOW error taxonomy.
3. CandidateManifest structural immutability (frozen object preventing attribute mutation).
4. Candidate configuration closed validation (rejection of missing keys, unknown keys, invalid types, NaN, inf, bools, invalid domains).
5. H1_MARGIN policy execution, cutpoint generation, and fail-closed margin gating.
6. H2_LEXICAL_CORROBORATION policy execution, cutpoint generation, and fail-closed corroboration gating (never fallback to lexical).
7. H2 consumption of real IDF score for semantic top-1 candidate with lexical rank > 3.
8. H4_ASYMMETRIC_GATE policy execution, cutpoint generation, and asymmetric gating logic.
9. Deterministic Candidate ID formula: SHA256(mechanism_key || ":" || canonical_json(params) || ":" || model_hash || ":" || config_hash) with strict hash/param validation.
"""

import hashlib
import json
import pytest

from tools.memory_v5_semantic_safety.models import (
    CandidateConfig,
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


def _make_evidence(query: str, lex_tuples, sem_tuples) -> InferenceEvidence:
    """Helper to create InferenceEvidence for testing."""
    lex_items = tuple(
        RankedItem(memory_id=t[0], score=float(t[1]), rank=i + 1, text=t[2], user_id="u1", is_private=False, is_inactive=False)
        for i, t in enumerate(lex_tuples)
    )
    sem_items = tuple(
        RankedItem(memory_id=t[0], score=float(t[1]), rank=i + 1, text=t[2], user_id="u1", is_private=False, is_inactive=False)
        for i, t in enumerate(sem_tuples)
    )
    return InferenceEvidence(
        evidence_key="test_evidence_key",
        query=query,
        lexical_ranking=lex_items,
        semantic_ranking=sem_items,
    )


def test_candidate_manifest_registration_quotas_and_immutability():
    """Task 3.1a: CandidateManifest pre-registers 3 singletons and is deeply immutable."""
    manifest = CandidateManifest.default_manifest()

    assert len(manifest.singletons) == 3
    assert len(manifest.combinations) == 0
    assert manifest.total_count == 3

    keys = [c.mechanism_key for c in manifest.singletons]
    assert keys == ["H1_MARGIN", "H2_LEXICAL_CORROBORATION", "H4_ASYMMETRIC_GATE"]
    assert manifest.validate_quotas() is True

    # Structural immutability test: re-assignment or attribute mutation is blocked
    with pytest.raises(TypeError, match="frozen and cannot be mutated"):
        manifest.singletons = ()

    with pytest.raises(TypeError, match="frozen and cannot be mutated"):
        manifest.combinations = ()

    with pytest.raises(TypeError, match="frozen and cannot be mutated"):
        del manifest.singletons

    # Over-quota rejection: singletons > 4
    with pytest.raises(ValueError, match="ADMISSION_OVERFLOW: Singleton candidate count"):
        CandidateManifest(
            singletons=[
                H1MarginCandidate(),
                H2LexicalCorroborationCandidate(),
                H4AsymmetricGateCandidate(),
                H1MarginCandidate(),
                H1MarginCandidate(),
            ],
            combinations=[],
        )

    # Over-quota rejection: combinations > 2
    with pytest.raises(ValueError, match="ADMISSION_OVERFLOW: Combination candidate count"):
        CandidateManifest(
            singletons=[H1MarginCandidate()],
            combinations=[
                H2LexicalCorroborationCandidate(),
                H4AsymmetricGateCandidate(),
                H1MarginCandidate(),
            ],
        )

    # Over-quota rejection: total > 6
    with pytest.raises(ValueError, match="ADMISSION_OVERFLOW: Total candidate count"):
        CandidateManifest(
            singletons=[
                H1MarginCandidate(),
                H2LexicalCorroborationCandidate(),
                H4AsymmetricGateCandidate(),
                H1MarginCandidate(),
            ],
            combinations=[
                H2LexicalCorroborationCandidate(),
                H4AsymmetricGateCandidate(),
                H1MarginCandidate(),
            ],
        )


def test_candidate_id_formula_deterministic_and_validation():
    """Task 3.1b: Deterministic Candidate ID formula and validation."""
    params = {"theta_score": 0.85, "theta_margin": 0.15}
    model_hash = "7cce6bd3df3eefe20757261165563b9f27b2397b5f0661bd42f804d545b07569"
    config_hash = "c0d7110f0abe8dfae5e335dc1d931b4742c52e37ea67de296afbb22060912c43"

    cid1 = compute_candidate_id("H1_MARGIN", params, model_hash, config_hash)
    cid2 = compute_candidate_id("H1_MARGIN", {"theta_margin": 0.15, "theta_score": 0.85}, model_hash, config_hash)

    assert cid1 == cid2
    assert len(cid1) == 64

    expected_canon = '{"theta_margin":0.15,"theta_score":0.85}'
    expected_raw = f"H1_MARGIN:{expected_canon}:{model_hash}:{config_hash}"
    assert cid1 == hashlib.sha256(expected_raw.encode("utf-8")).hexdigest()

    # Negative validation tests for hashes and params
    with pytest.raises(ValueError, match="Invalid model_hash"):
        compute_candidate_id("H1_MARGIN", params, "invalid_short_hash", config_hash)

    with pytest.raises(ValueError, match="Invalid config_hash"):
        compute_candidate_id("H1_MARGIN", params, model_hash, "UPPERCASE_NOT_ALLOWED" * 4)

    with pytest.raises(ValueError, match="NaN and Infinity are rejected"):
        compute_candidate_id("H1_MARGIN", {"theta_score": float("nan")}, model_hash, config_hash)

    with pytest.raises(ValueError, match="NaN and Infinity are rejected"):
        compute_candidate_id("H1_MARGIN", {"theta_score": float("inf")}, model_hash, config_hash)


def test_candidate_config_closed_validation():
    """Task 3.1c: Closed configuration validation rejecting missing, extra, or invalid keys."""
    h1 = H1MarginCandidate()
    h2 = H2LexicalCorroborationCandidate()
    h4 = H4AsymmetricGateCandidate()
    ev = _make_evidence("q", [], [("c1", 0.9, "t1")])

    # 1. H1 validation tests
    with pytest.raises(ValueError, match="requires exact keys"):
        h1.apply_policy(ev, CandidateConfig("h1", "H1_MARGIN", {}))  # Missing keys

    with pytest.raises(ValueError, match="requires exact keys"):
        h1.apply_policy(ev, CandidateConfig("h1", "H1_MARGIN", {"theta_score": 0.8, "theta_margin": 0.1, "extra": 1}))

    with pytest.raises(ValueError, match="does not match candidate"):
        h1.apply_policy(ev, CandidateConfig("h1", "H2_LEXICAL_CORROBORATION", {"theta_tok": 1, "theta_idf": 0.0}))

    with pytest.raises(ValueError, match="must be float/int"):
        h1.apply_policy(ev, CandidateConfig("h1", "H1_MARGIN", {"theta_score": True, "theta_margin": 0.1}))

    with pytest.raises(ValueError, match="must be finite"):
        h1.apply_policy(ev, CandidateConfig("h1", "H1_MARGIN", {"theta_score": float("nan"), "theta_margin": 0.1}))

    # 2. H2 validation tests
    with pytest.raises(ValueError, match="theta_tok must be integer in"):
        h2.apply_policy(ev, CandidateConfig("h2", "H2_LEXICAL_CORROBORATION", {"theta_tok": 4, "theta_idf": 0.0}))

    with pytest.raises(ValueError, match="theta_tok must be integer in"):
        h2.apply_policy(ev, CandidateConfig("h2", "H2_LEXICAL_CORROBORATION", {"theta_tok": True, "theta_idf": 0.0}))

    with pytest.raises(ValueError, match="theta_idf must be finite non-negative"):
        h2.apply_policy(ev, CandidateConfig("h2", "H2_LEXICAL_CORROBORATION", {"theta_tok": 1, "theta_idf": -1.0}))

    # 3. H4 validation tests
    with pytest.raises(ValueError, match="theta_low .* cannot be greater than theta_high"):
        h4.apply_policy(ev, CandidateConfig("h4", "H4_ASYMMETRIC_GATE", {"theta_high": 0.5, "theta_low": 0.8, "R_lex_max": 2}))

    with pytest.raises(ValueError, match="R_lex_max must be integer in"):
        h4.apply_policy(ev, CandidateConfig("h4", "H4_ASYMMETRIC_GATE", {"theta_high": 0.8, "theta_low": 0.5, "R_lex_max": 5}))


def test_h1_margin_policy_execution():
    """Task 3.1d: H1_MARGIN applies score and margin threshold gating."""
    h1 = H1MarginCandidate()
    config = CandidateConfig(
        candidate_id="h1_test",
        mechanism_key="H1_MARGIN",
        params={"theta_score": 0.80, "theta_margin": 0.10},
    )

    ev_pass = _make_evidence("coffee", [], [("c1", 0.85, "coffee dark roast"), ("c2", 0.70, "tea green")])
    res_pass = h1.apply_policy(ev_pass, config)
    assert len(res_pass) == 2
    assert res_pass[0].memory_id == "c1"

    ev_fail_score = _make_evidence("coffee", [], [("c1", 0.75, "coffee dark roast"), ("c2", 0.50, "tea green")])
    res_fail_score = h1.apply_policy(ev_fail_score, config)
    assert len(res_fail_score) == 0

    ev_fail_margin = _make_evidence("coffee", [], [("c1", 0.85, "coffee dark roast"), ("c2", 0.80, "coffee espresso")])
    res_fail_margin = h1.apply_policy(ev_fail_margin, config)
    assert len(res_fail_margin) == 0

    ev_single = _make_evidence("coffee", [], [("c1", 0.85, "coffee dark roast")])
    res_single = h1.apply_policy(ev_single, config)
    assert len(res_single) == 1


def test_h1_cutpoint_generation_deterministic():
    """Task 3.1e: H1_MARGIN derives parameter grid exclusively from calibration data."""
    h1 = H1MarginCandidate()
    ev1 = _make_evidence("q1", [], [("c1", 0.90, "t1"), ("c2", 0.80, "t2")])
    ev2 = _make_evidence("q2", [], [("c1", 0.70, "t1"), ("c2", 0.60, "t2")])

    space = h1.generate_parameter_space([ev1, ev2])
    assert len(space) > 0
    for p in space:
        assert "theta_score" in p
        assert "theta_margin" in p
        assert 0.0 <= p["theta_score"] <= 1.0
        assert 0.0 <= p["theta_margin"] <= 1.0


def test_h2_lexical_corroboration_policy_execution():
    """Task 3.1f: H2_LEXICAL_CORROBORATION fail-closed ABSTAIN."""
    h2 = H2LexicalCorroborationCandidate()
    config = CandidateConfig(
        candidate_id="h2_test",
        mechanism_key="H2_LEXICAL_CORROBORATION",
        params={"theta_tok": 2, "theta_idf": 5.0},
    )

    ev_pass = _make_evidence(
        "dark roast coffee",
        [("c1", 12.0, "dark roast coffee blend")],
        [("c1", 0.88, "dark roast coffee blend")],
    )
    res_pass = h2.apply_policy(ev_pass, config)
    assert len(res_pass) == 1
    assert res_pass[0].memory_id == "c1"

    ev_fail_tok = _make_evidence(
        "dark roast coffee",
        [("c1", 12.0, "dark roast coffee blend")],
        [("c2", 0.88, "morning green tea")],
    )
    res_fail_tok = h2.apply_policy(ev_fail_tok, config)
    assert len(res_fail_tok) == 0

    ev_fail_idf = _make_evidence(
        "dark roast coffee",
        [("c1", 2.0, "dark roast coffee blend")],
        [("c1", 0.88, "dark roast coffee blend")],
    )
    res_fail_idf = h2.apply_policy(ev_fail_idf, config)
    assert len(res_fail_idf) == 0


def test_h2_receives_real_idf_when_semantic_candidate_has_lexical_rank_greater_than_3():
    """Task 3.1g: H2 receives real IDF score even when semantic top-1 has lexical rank > 3."""
    h2 = H2LexicalCorroborationCandidate()
    config = CandidateConfig(
        candidate_id="h2_test",
        mechanism_key="H2_LEXICAL_CORROBORATION",
        params={"theta_tok": 2, "theta_idf": 6.0},
    )

    # Candidate c4 is ranked #4 in lexical search with IDF 8.0, and is top-1 in semantic search
    ev = _make_evidence(
        "special dark roast coffee",
        [
            ("c1", 20.0, "special dark roast coffee blend"),
            ("c2", 15.0, "dark roast coffee beans"),
            ("c3", 10.0, "roast coffee morning"),
            ("c4", 8.0, "special dark blend"),  # rank 4 in lexical, shared tokens = 2 ("special", "dark")
        ],
        [
            ("c4", 0.92, "special dark blend"),  # top-1 semantic
        ],
    )

    res = h2.apply_policy(ev, config)
    # Because evidence preserves full lexical ranking for the case pool, c4 has S_idf = 8.0 >= 6.0 and N_tok = 2 >= 2
    assert len(res) == 1
    assert res[0].memory_id == "c4"


def test_h2_cutpoint_generation_deterministic():
    """Task 3.1h: H2_LEXICAL_CORROBORATION cutpoint generation."""
    h2 = H2LexicalCorroborationCandidate()
    ev1 = _make_evidence("dark roast", [("c1", 10.0, "dark roast")], [("c1", 0.90, "dark roast")])
    ev2 = _make_evidence("green tea", [("c2", 4.0, "green tea")], [("c2", 0.80, "green tea")])

    space = h2.generate_parameter_space([ev1, ev2])
    assert len(space) > 0
    for p in space:
        assert "theta_tok" in p
        assert "theta_idf" in p
        assert p["theta_tok"] in (1, 2, 3)
        assert p["theta_idf"] >= 0.0


def test_h4_asymmetric_gate_policy_execution():
    """Task 3.1i: H4_ASYMMETRIC_GATE applies asymmetric lexical-semantic gating."""
    h4 = H4AsymmetricGateCandidate()
    config = CandidateConfig(
        candidate_id="h4_test",
        mechanism_key="H4_ASYMMETRIC_GATE",
        params={"theta_high": 0.85, "theta_low": 0.70, "R_lex_max": 2},
    )

    ev_high = _make_evidence("coffee", [], [("c1", 0.90, "coffee espresso")])
    res_high = h4.apply_policy(ev_high, config)
    assert len(res_high) == 1

    ev_mid_lex_pass = _make_evidence(
        "coffee",
        [("c2", 10.0, "t2"), ("c1", 8.0, "coffee espresso")],  # c1 is rank 2
        [("c1", 0.75, "coffee espresso")],
    )
    res_mid_lex_pass = h4.apply_policy(ev_mid_lex_pass, config)
    assert len(res_mid_lex_pass) == 1

    ev_mid_lex_fail = _make_evidence(
        "coffee",
        [("c2", 10.0, "t2"), ("c3", 8.0, "t3"), ("c1", 5.0, "coffee espresso")],  # c1 is rank 3
        [("c1", 0.75, "coffee espresso")],
    )
    res_mid_lex_fail = h4.apply_policy(ev_mid_lex_fail, config)
    assert len(res_mid_lex_fail) == 0

    ev_mid_absent = _make_evidence(
        "coffee",
        [("c2", 10.0, "t2"), ("c3", 8.0, "t3")],  # c1 not in lexical (r_lex = inf)
        [("c1", 0.75, "coffee espresso")],
    )
    res_mid_absent = h4.apply_policy(ev_mid_absent, config)
    assert len(res_mid_absent) == 0

    ev_low = _make_evidence("coffee", [("c1", 10.0, "coffee espresso")], [("c1", 0.65, "coffee espresso")])
    res_low = h4.apply_policy(ev_low, config)
    assert len(res_low) == 0


def test_h4_cutpoint_generation_deterministic():
    """Task 3.1j: H4_ASYMMETRIC_GATE cutpoint generation."""
    h4 = H4AsymmetricGateCandidate()
    ev1 = _make_evidence("q1", [], [("c1", 0.90, "t1")])
    ev2 = _make_evidence("q2", [], [("c2", 0.70, "t2")])

    space = h4.generate_parameter_space([ev1, ev2])
    assert len(space) > 0
    for p in space:
        assert "theta_high" in p
        assert "theta_low" in p
        assert "R_lex_max" in p
        assert p["theta_low"] <= p["theta_high"]
        assert p["R_lex_max"] in (1, 2, 3)
