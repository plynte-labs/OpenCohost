"""
Candidate policy mechanisms and pre-registered manifest for Memory v5 Semantic Safety.

Implements:
1. SafetyRefinementCandidate abstract base class with closed config validation.
2. H1: Score-Confidence & Margin Gating (H1_MARGIN).
3. H2: Lexical Corroboration Veto (H2_LEXICAL_CORROBORATION) - strict fail-closed abstention.
4. H4: Asymmetric Lexical-Semantic Gating (H4_ASYMMETRIC_GATE).
5. Pre-registered CandidateManifest enforcing exact quotas and structural immutability.
6. Deterministic Candidate ID generation formula with strict schema validation.
"""

from abc import ABC, abstractmethod
import hashlib
import json
import math
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from opencohost.core.memory.memoria_store import (
    _SCORING_STOPWORDS,
    _significant_tokens,
)
from tools.memory_v5_semantic_safety.models import (
    CandidateConfig,
    InferenceEvidence,
    RankedItem,
)


def compute_candidate_id(
    mechanism_key: str,
    params: Dict[str, Any],
    model_hash: str,
    config_hash: str,
) -> str:
    """
    Deterministic candidate identifier formula:
    SHA256(mechanism_key || ":" || canonical_json(params) || ":" || model_hash || ":" || config_hash)
    """
    if not isinstance(mechanism_key, str) or not mechanism_key.strip():
        raise ValueError(f"Invalid mechanism_key: {mechanism_key!r}")
    if not isinstance(model_hash, str) or len(model_hash) != 64 or not all(c in "0123456789abcdef" for c in model_hash):
        raise ValueError(f"Invalid model_hash: must be 64-char lowercase hex string, got {model_hash!r}")
    if not isinstance(config_hash, str) or len(config_hash) != 64 or not all(c in "0123456789abcdef" for c in config_hash):
        raise ValueError(f"Invalid config_hash: must be 64-char lowercase hex string, got {config_hash!r}")

    for k, v in params.items():
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            raise ValueError(f"Invalid parameter {k}={v}: NaN and Infinity are rejected")

    canon_params = json.dumps(dict(params), sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)
    raw = f"{mechanism_key}:{canon_params}:{model_hash}:{config_hash}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _derive_cutpoints(values: Sequence[float], sentinels: Sequence[float] = (0.0, 1.0)) -> List[float]:
    """
    Deterministic cutpoint generation:
    1. Sort unique observed values.
    2. Compute arithmetic midpoints between adjacent sorted unique values.
    3. Add sentinels, filter to unique sorted float values.
    """
    uniques = sorted(set(float(v) for v in values))
    cutpoints: Set[float] = set(float(s) for s in sentinels)

    if uniques:
        for u in uniques:
            cutpoints.add(u)
        for i in range(len(uniques) - 1):
            mid = (uniques[i] + uniques[i + 1]) / 2.0
            cutpoints.add(mid)

    return sorted(cutpoints)


class SafetyRefinementCandidate(ABC):
    """Abstract base class for pure semantic safety candidate policies."""

    @property
    @abstractmethod
    def mechanism_key(self) -> str:
        """Unique uppercase mechanism identifier."""
        pass

    @abstractmethod
    def validate_config(self, config: CandidateConfig) -> None:
        """Validate that candidate config has exact valid keys, types, and constraints."""
        pass

    @abstractmethod
    def generate_parameter_space(self, cal_evidences: List[InferenceEvidence]) -> List[Dict[str, Any]]:
        """Generate finite, deterministic parameter space derived from calibration evidence."""
        pass

    @abstractmethod
    def apply_policy(self, evidence: InferenceEvidence, config: CandidateConfig) -> Tuple[RankedItem, ...]:
        """Pure decision rule: returns filtered ranking or empty tuple (abstain)."""
        pass


class H1MarginCandidate(SafetyRefinementCandidate):
    """
    H1: Score-Confidence & Margin Gating.
    Accepts semantic ranking IFF Top-1 score >= theta_score AND (s1 - s2) >= theta_margin.
    Else: Fail-closed ABSTAIN (returns empty tuple).
    """

    @property
    def mechanism_key(self) -> str:
        return "H1_MARGIN"

    def validate_config(self, config: CandidateConfig) -> None:
        if config.mechanism_key != self.mechanism_key:
            raise ValueError(f"Config mechanism_key {config.mechanism_key!r} does not match candidate {self.mechanism_key!r}")

        expected_keys = {"theta_score", "theta_margin"}
        actual_keys = set(config.params.keys())
        if actual_keys != expected_keys:
            raise ValueError(f"H1_MARGIN requires exact keys {expected_keys}, got {actual_keys}")

        for k in ("theta_score", "theta_margin"):
            v = config.params[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"H1 parameter {k} must be float/int, got {type(v).__name__}: {v!r}")
            if math.isnan(float(v)) or math.isinf(float(v)):
                raise ValueError(f"H1 parameter {k} must be finite, got {v}")

    def generate_parameter_space(self, cal_evidences: List[InferenceEvidence]) -> List[Dict[str, Any]]:
        scores: List[float] = []
        margins: List[float] = []

        for ev in cal_evidences:
            if ev.semantic_ranking:
                s1 = ev.semantic_ranking[0].score
                s2 = ev.semantic_ranking[1].score if len(ev.semantic_ranking) >= 2 else 0.0
                scores.append(s1)
                margins.append(s1 - s2)

        score_cutpoints = _derive_cutpoints(scores, sentinels=(0.0, 1.0))
        margin_cutpoints = _derive_cutpoints(margins, sentinels=(0.0, 1.0))

        param_space: List[Dict[str, Any]] = []
        for s in score_cutpoints:
            for m in margin_cutpoints:
                param_space.append({"theta_score": s, "theta_margin": m})

        return param_space

    def apply_policy(self, evidence: InferenceEvidence, config: CandidateConfig) -> Tuple[RankedItem, ...]:
        self.validate_config(config)

        if not evidence.semantic_ranking:
            return ()

        s1 = evidence.semantic_ranking[0].score
        s2 = evidence.semantic_ranking[1].score if len(evidence.semantic_ranking) >= 2 else 0.0
        margin = s1 - s2

        theta_score = float(config.params["theta_score"])
        theta_margin = float(config.params["theta_margin"])

        if s1 >= theta_score and margin >= theta_margin:
            return evidence.semantic_ranking
        return ()


class H2LexicalCorroborationCandidate(SafetyRefinementCandidate):
    """
    H2: Lexical Corroboration Veto.
    Verifies top-1 semantic candidate against raw lexical features (token overlap N_tok and IDF sum S_idf).
    Accepts semantic ranking IFF N_tok >= theta_tok AND S_idf >= theta_idf.
    Else: Fail-closed ABSTAIN (never fallback to lexical baseline).
    """

    @property
    def mechanism_key(self) -> str:
        return "H2_LEXICAL_CORROBORATION"

    def validate_config(self, config: CandidateConfig) -> None:
        if config.mechanism_key != self.mechanism_key:
            raise ValueError(f"Config mechanism_key {config.mechanism_key!r} does not match candidate {self.mechanism_key!r}")

        expected_keys = {"theta_tok", "theta_idf"}
        actual_keys = set(config.params.keys())
        if actual_keys != expected_keys:
            raise ValueError(f"H2_LEXICAL_CORROBORATION requires exact keys {expected_keys}, got {actual_keys}")

        tok = config.params["theta_tok"]
        if isinstance(tok, bool) or not isinstance(tok, int) or tok not in (1, 2, 3):
            raise ValueError(f"H2 parameter theta_tok must be integer in {{1, 2, 3}}, got {tok!r}")

        idf = config.params["theta_idf"]
        if isinstance(idf, bool) or not isinstance(idf, (int, float)):
            raise ValueError(f"H2 parameter theta_idf must be float/int, got {type(idf).__name__}: {idf!r}")
        if math.isnan(float(idf)) or math.isinf(float(idf)) or float(idf) < 0.0:
            raise ValueError(f"H2 parameter theta_idf must be finite non-negative float, got {idf}")

    def generate_parameter_space(self, cal_evidences: List[InferenceEvidence]) -> List[Dict[str, Any]]:
        idf_scores: List[float] = []

        for ev in cal_evidences:
            if ev.semantic_ranking:
                top_sem_id = ev.semantic_ranking[0].memory_id
                lex_map = {r.memory_id: r.score for r in ev.lexical_ranking}
                idf_scores.append(lex_map.get(top_sem_id, 0.0))

        idf_cutpoints = _derive_cutpoints(idf_scores, sentinels=(0.0,))

        param_space: List[Dict[str, Any]] = []
        for tok in (1, 2, 3):
            for idf in idf_cutpoints:
                param_space.append({"theta_tok": tok, "theta_idf": idf})

        return param_space

    def apply_policy(self, evidence: InferenceEvidence, config: CandidateConfig) -> Tuple[RankedItem, ...]:
        self.validate_config(config)

        if not evidence.semantic_ranking:
            return ()

        top_sem = evidence.semantic_ranking[0]
        q_tokens = set(_significant_tokens(evidence.query)) - _SCORING_STOPWORDS
        c_tokens = set(_significant_tokens(top_sem.text)) - _SCORING_STOPWORDS
        shared_tokens = q_tokens & c_tokens
        n_tok = len(shared_tokens)

        lex_map = {r.memory_id: r.score for r in evidence.lexical_ranking}
        s_idf = lex_map.get(top_sem.memory_id, 0.0)

        theta_tok = int(config.params["theta_tok"])
        theta_idf = float(config.params["theta_idf"])

        if n_tok >= theta_tok and s_idf >= theta_idf:
            return evidence.semantic_ranking
        return ()


class H4AsymmetricGateCandidate(SafetyRefinementCandidate):
    """
    H4: Asymmetric Lexical-Semantic Gating.
    Accepts semantic ranking IFF s1 >= theta_high OR (s1 >= theta_low AND r_lex <= R_lex_max).
    Else: Fail-closed ABSTAIN.
    """

    @property
    def mechanism_key(self) -> str:
        return "H4_ASYMMETRIC_GATE"

    def validate_config(self, config: CandidateConfig) -> None:
        if config.mechanism_key != self.mechanism_key:
            raise ValueError(f"Config mechanism_key {config.mechanism_key!r} does not match candidate {self.mechanism_key!r}")

        expected_keys = {"theta_high", "theta_low", "R_lex_max"}
        actual_keys = set(config.params.keys())
        if actual_keys != expected_keys:
            raise ValueError(f"H4_ASYMMETRIC_GATE requires exact keys {expected_keys}, got {actual_keys}")

        for k in ("theta_high", "theta_low"):
            v = config.params[k]
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"H4 parameter {k} must be float/int, got {type(v).__name__}: {v!r}")
            if math.isnan(float(v)) or math.isinf(float(v)):
                raise ValueError(f"H4 parameter {k} must be finite, got {v}")

        if float(config.params["theta_low"]) > float(config.params["theta_high"]):
            raise ValueError(f"H4 theta_low ({config.params['theta_low']}) cannot be greater than theta_high ({config.params['theta_high']})")

        r = config.params["R_lex_max"]
        if isinstance(r, bool) or not isinstance(r, int) or r not in (1, 2, 3):
            raise ValueError(f"H4 parameter R_lex_max must be integer in {{1, 2, 3}}, got {r!r}")

    def generate_parameter_space(self, cal_evidences: List[InferenceEvidence]) -> List[Dict[str, Any]]:
        scores: List[float] = []
        for ev in cal_evidences:
            if ev.semantic_ranking:
                scores.append(ev.semantic_ranking[0].score)

        score_cutpoints = _derive_cutpoints(scores, sentinels=(0.0, 1.0))

        param_space: List[Dict[str, Any]] = []
        for th_high in score_cutpoints:
            for th_low in score_cutpoints:
                if th_low <= th_high:
                    for r_lex in (1, 2, 3):
                        param_space.append({
                            "theta_high": th_high,
                            "theta_low": th_low,
                            "R_lex_max": r_lex,
                        })

        return param_space

    def apply_policy(self, evidence: InferenceEvidence, config: CandidateConfig) -> Tuple[RankedItem, ...]:
        self.validate_config(config)

        if not evidence.semantic_ranking:
            return ()

        top_sem = evidence.semantic_ranking[0]
        s1 = top_sem.score

        lex_ranks = {r.memory_id: r.rank for r in evidence.lexical_ranking}
        r_lex = lex_ranks.get(top_sem.memory_id, float("inf"))

        theta_high = float(config.params["theta_high"])
        theta_low = float(config.params["theta_low"])
        r_lex_max = int(config.params["R_lex_max"])

        if s1 >= theta_high:
            return evidence.semantic_ranking
        elif s1 >= theta_low and r_lex <= r_lex_max:
            return evidence.semantic_ranking
        return ()


class CandidateManifest:
    """
    Pre-registered candidate manifest enforcing strict hypothesis admission quotas:
    - Singletons <= 4
    - Combinations <= 2
    - Total candidates <= 6
    Structurally frozen: attributes cannot be re-assigned or mutated after initialization.
    """
    __slots__ = ("_singletons", "_combinations", "_frozen")

    def __init__(
        self,
        singletons: Optional[Sequence[SafetyRefinementCandidate]] = None,
        combinations: Optional[Sequence[SafetyRefinementCandidate]] = None,
    ):
        object.__setattr__(self, "_singletons", tuple(singletons) if singletons is not None else ())
        object.__setattr__(self, "_combinations", tuple(combinations) if combinations is not None else ())
        self.validate_quotas(raise_error=True)
        object.__setattr__(self, "_frozen", True)

    @property
    def singletons(self) -> Tuple[SafetyRefinementCandidate, ...]:
        return self._singletons

    @property
    def combinations(self) -> Tuple[SafetyRefinementCandidate, ...]:
        return self._combinations

    @property
    def total_count(self) -> int:
        return len(self._singletons) + len(self._combinations)

    def validate_quotas(self, raise_error: bool = False) -> bool:
        if self.total_count > 6:
            if raise_error:
                raise ValueError(f"ADMISSION_OVERFLOW: Total candidate count {self.total_count} exceeds maximum quota of 6")
            return False
        if len(self._singletons) > 4:
            if raise_error:
                raise ValueError(f"ADMISSION_OVERFLOW: Singleton candidate count {len(self._singletons)} exceeds quota of 4")
            return False
        if len(self._combinations) > 2:
            if raise_error:
                raise ValueError(f"ADMISSION_OVERFLOW: Combination candidate count {len(self._combinations)} exceeds quota of 2")
            return False
        return True

    def __setattr__(self, name: str, value: Any) -> None:
        if getattr(self, "_frozen", False):
            raise TypeError("CandidateManifest is frozen and cannot be mutated after construction")
        super().__setattr__(name, value)

    def __delattr__(self, name: str) -> None:
        if getattr(self, "_frozen", False):
            raise TypeError("CandidateManifest is frozen and cannot be mutated after construction")
        super().__delattr__(name)

    @classmethod
    def default_manifest(cls) -> "CandidateManifest":
        """Pre-registers the 3 authoritative singletons: H1, H2, H4 and 0 combinations."""
        return cls(
            singletons=(
                H1MarginCandidate(),
                H2LexicalCorroborationCandidate(),
                H4AsymmetricGateCandidate(),
            ),
            combinations=(),
        )
