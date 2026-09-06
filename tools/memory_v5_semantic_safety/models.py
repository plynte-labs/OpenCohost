"""
Core immutable models and schemas for memory-v5-semantic-safety-refinement.

Enforces deep immutability and strict separation between candidate-facing
InferenceEvidence and evaluator-only EvaluationTruth.
"""

from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple


def _freeze_val(val: Any) -> Any:
    """Recursively convert nested dicts to FrozenDict and lists/sets to tuples."""
    if isinstance(val, dict):
        return FrozenDict(val) if not isinstance(val, FrozenDict) else val
    if isinstance(val, (list, tuple, set)):
        return tuple(_freeze_val(x) for x in val)
    return val


class FrozenDict(dict):
    """
    Repository-native deeply immutable dictionary.
    Blocks all mutating operations (including Python 3.9+ __ior__) and
    recursively freezes nested mappings/collections at arbitrary depth.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for k, v in list(self.items()):
            super().__setitem__(k, _freeze_val(v))

    def __setitem__(self, key, value):
        raise TypeError(f"Cannot modify immutable {self.__class__.__name__}")

    def __delitem__(self, key):
        raise TypeError(f"Cannot delete from immutable {self.__class__.__name__}")

    def __ior__(self, other):
        raise TypeError(f"Cannot modify immutable {self.__class__.__name__}")

    def pop(self, *args, **kwargs):
        raise TypeError(f"Cannot modify immutable {self.__class__.__name__}")

    def popitem(self):
        raise TypeError(f"Cannot modify immutable {self.__class__.__name__}")

    def clear(self):
        raise TypeError(f"Cannot modify immutable {self.__class__.__name__}")

    def update(self, *args, **kwargs):
        raise TypeError(f"Cannot modify immutable {self.__class__.__name__}")

    def setdefault(self, *args, **kwargs):
        raise TypeError(f"Cannot modify immutable {self.__class__.__name__}")


@dataclass(frozen=True)
class RankedItem:
    """Immutable representation of a single ranked memory item."""
    memory_id: str
    score: float
    rank: int
    text: str
    user_id: str
    is_private: bool
    is_inactive: bool


@dataclass(frozen=True)
class InferenceEvidence:
    """
    Evidence delivered to candidates for policy inference.
    Contains ONLY query and observable ranking evidence.
    NO ground truth, NO family, NO case_id, NO target_ids.
    """
    evidence_key: str  # Opaque hash identifier (e.g. sha256(case_id)[:16])
    query: str
    lexical_ranking: Tuple[RankedItem, ...]
    semantic_ranking: Tuple[RankedItem, ...]


@dataclass(frozen=True)
class EvaluationTruth:
    """
    Ground-truth expectations delivered ONLY to metrics/evaluators.
    Never accessible to candidate policies.
    """
    case_id: str
    family: str  # 'hard_lexical', 'synonym', 'paraphrase', 'no_memory', 'near_but_wrong', 'profile_privacy', 'stale_contradiction'
    target_ids: Tuple[str, ...]
    expected_abstain: bool
    target_user_id: str
    diagnostic_subtype: Optional[str] = None


@dataclass(frozen=True)
class CandidateConfig:
    """Immutable configuration for an admitted candidate with deeply frozen params."""
    candidate_id: str
    mechanism_key: str
    params: Dict[str, Any]

    def __post_init__(self):
        if not isinstance(self.params, FrozenDict):
            object.__setattr__(self, "params", FrozenDict(self.params) if self.params is not None else FrozenDict())


@dataclass(frozen=True)
class CalibrationRecord:
    """Immutable record of frozen calibration parameters and metrics."""
    candidate_id: str
    mechanism_key: str
    selected_params: Dict[str, Any]
    calibration_fixture_hash: str
    model_artifact_hash: str
    shared_config_hash: str
    frozen_at: str
    frozen_sequence: int
    cal_syn_para_mrr3_mean: float
    cal_hard_recall_at_1: float
    cal_no_memory_abstention_accuracy: float
    cal_no_memory_false_injection_count: int
    cal_profile_privacy_leakage_count: int

    def __post_init__(self):
        if not isinstance(self.selected_params, FrozenDict):
            object.__setattr__(
                self,
                "selected_params",
                FrozenDict(self.selected_params) if self.selected_params is not None else FrozenDict(),
            )


@dataclass(frozen=True)
class CandidateResult:
    """Lifecycle and execution result for a single candidate."""
    variant_key: str
    execution_status: str  # 'EVALUABLE' | 'NON_EVALUABLE'
    execution_reason: Optional[str]  # e.g. 'CALIBRATION_FAILED'
    quality_status: str  # 'QUALIFIED' | 'CRITICAL_REGRESSION' | 'NOT_APPLICABLE'
    critical_reasons: Tuple[str, ...]
    calibration: Optional[CalibrationRecord] = None


@dataclass(frozen=True)
class MetricsObject:
    """Canonical metrics for a single candidate across all families."""
    hard_correct_at_1_count: int
    hard_correct_at_3_count: int
    hard_recall_at_1: float
    hard_recall_at_3: float
    hard_mrr_at_3: float
    syn_correct_at_1_count: int
    syn_correct_at_3_count: int
    syn_recall_at_1: float
    syn_recall_at_3: float
    syn_mrr_at_3: float
    para_correct_at_1_count: int
    para_correct_at_3_count: int
    para_recall_at_1: float
    para_recall_at_3: float
    para_mrr_at_3: float
    semantic_quality_score: float
    no_memory_abstention_count: int
    no_memory_false_injection_count: int
    no_memory_abstention_accuracy: float
    near_wrong_correct_at_1_count: int
    near_wrong_precision_at_1: float
    near_wrong_target_mrr_at_3: float
    profile_isolation_pass_count: int
    profile_privacy_leakage_count: int
    profile_private_leakage_count: int
    profile_inactive_leakage_count: int
    profile_cross_profile_leakage_count: int
    stale_correct_count: int
    stale_correct_rate: float
    retention_vs_b: Optional[float] = None
    gain_over_a: Optional[float] = None


@dataclass(frozen=True)
class BenchmarkReceiptV1:
    """Canonical closed benchmark receipt model with frozen candidate/metrics maps."""
    schema_version: str
    run_id: str
    state: str  # 'VALID' | 'GLOBAL_INVALID'
    global_invalid_reason: Optional[str]
    locked_fixture_hash: str
    calibration_fixture_hash: str
    model_id: str
    model_artifact_hash: str
    shared_config_hash: str
    seed: int
    lock_identity: str
    adr_reference: str
    candidates: Optional[Dict[str, Any]]
    metrics: Optional[Dict[str, Any]]
    winner: Optional[str]
    terminal_route: str

    def __post_init__(self):
        if self.candidates is not None and not isinstance(self.candidates, FrozenDict):
            object.__setattr__(self, "candidates", FrozenDict(self.candidates))
        if self.metrics is not None and not isinstance(self.metrics, FrozenDict):
            object.__setattr__(self, "metrics", FrozenDict(self.metrics))
