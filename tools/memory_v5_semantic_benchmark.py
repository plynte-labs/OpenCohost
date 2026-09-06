"""
Memory v5 Semantic Retrieval Quality Benchmark Harness (REDUCED).

Evaluates:
- Variant A: Control - v4 IDF lexical retrieval (via _LexicalAdapter reusing opencohost.core.memory.memoria_store)
- Variant B: MiniLM dense (paraphrase-multilingual-MiniLM-L12-v2, 384d, ONNX CPU)
- Variant C: Hybrid IDF + MiniLM via Reciprocal Rank Fusion (RRF k=60, w=1:1)

Strict compliance with OpenSpec specification:
00BA82DEE1C2C7B6630449EBB6F90A6B94EC874663239DC80764436D1824B63F
"""

import hashlib
import json
import math
import os
import platform
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from opencohost.core.memory.memoria_store import (
    _compute_candidate_idf,
    _significant_tokens,
    is_meta_recall_query,
    select_top_k,
)

# ============================================================================
# Canonical Keys and Schema Constants
# ============================================================================

TOP_LEVEL_KEYS: Set[str] = {
    "schema_version",
    "run_id",
    "state",
    "global_invalid_reason",
    "locked_fixture_hash",
    "calibration_fixture_hash",
    "model_id",
    "model_artifact_hash",
    "shared_config_hash",
    "seed",
    "lock_identity",
    "adr_reference",
    "candidates",
    "metrics",
    "winner",
    "terminal_route",
}

CANDIDATE_RECEIPT_KEYS: Set[str] = {
    "variant_key",
    "execution_status",
    "execution_reason",
    "quality_status",
    "critical_reasons",
    "calibration",
}

CALIBRATION_RECEIPT_KEYS: Set[str] = {
    "variant_key",
    "threshold",
    "calibration_fixture_hash",
    "model_artifact_hash",
    "shared_config_hash",
    "frozen_at",
    "frozen_sequence",
    "cal_no_memory_abstention_count",
    "cal_no_memory_false_injection_count",
    "cal_no_memory_abstention_accuracy",
    "cal_profile_isolation_pass_count",
    "cal_profile_privacy_leakage_count",
    "cal_profile_private_leakage_count",
    "cal_profile_inactive_leakage_count",
    "cal_profile_cross_profile_leakage_count",
    "cal_near_wrong_precision_at_1",
    "cal_near_wrong_a_precision_at_1",
    "cal_syn_mrr_at_3",
    "cal_para_mrr_at_3",
    "cal_syn_para_mrr3_mean",
    "cal_hard_correct_at_1_count",
    "cal_hard_recall_at_1",
}

METRICS_OBJECT_KEYS: Set[str] = {
    "hard_correct_at_1_count",
    "hard_correct_at_3_count",
    "hard_recall_at_1",
    "hard_recall_at_3",
    "hard_mrr_at_3",
    "syn_correct_at_1_count",
    "syn_correct_at_3_count",
    "syn_recall_at_1",
    "syn_recall_at_3",
    "syn_mrr_at_3",
    "para_correct_at_1_count",
    "para_correct_at_3_count",
    "para_recall_at_1",
    "para_recall_at_3",
    "para_mrr_at_3",
    "semantic_quality_score",
    "no_memory_abstention_count",
    "no_memory_false_injection_count",
    "no_memory_abstention_accuracy",
    "near_wrong_correct_at_1_count",
    "near_wrong_precision_at_1",
    "near_wrong_target_mrr_at_3",
    "profile_isolation_pass_count",
    "profile_privacy_leakage_count",
    "profile_private_leakage_count",
    "profile_inactive_leakage_count",
    "profile_cross_profile_leakage_count",
    "stale_correct_count",
    "stale_correct_rate",
}

CRITICAL_REASONS_CANONICAL_ORDER: List[str] = [
    "HARD_RECALL_REGRESSION",
    "NO_MEMORY_INJECTION",
    "NEAR_WRONG_REGRESSION",
    "PROFILE_PRIVACY_LEAKAGE",
]

GLOBAL_INVALID_REASONS_CANONICAL_ORDER: List[str] = [
    "PRIVACY_INVARIANT_VIOLATION",
    "BASELINE_INVALID",
    "LOCKED_FIXTURE_INVALID",
    "CALIBRATION_FIXTURE_INVALID",
    "FIXTURE_OVERLAP",
    "SHARED_CONFIG_INVALID",
    "HARNESS_INVALID",
]


# ============================================================================
# Canonical JSON and Hashing Helpers
# ============================================================================

def canonical_json_dumps(obj: Any) -> str:
    """Canonical JSON representation: sorted keys, compact separators, UTF-8, no NaN."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def canonical_json_sha256(obj: Any) -> str:
    """SHA-256 over canonical UTF-8 JSON bytes."""
    return hashlib.sha256(canonical_json_dumps(obj).encode("utf-8")).hexdigest()


def compute_file_sha256(file_path: Union[str, Path]) -> str:
    """Compute SHA-256 over file bytes."""
    h = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def compute_model_artifact_sha256(manifest: List[Dict[str, Any]]) -> str:
    """Compute SHA-256 over sorted manifest concatenation."""
    sorted_manifest = sorted(manifest, key=lambda x: x.get("path", ""))
    concat_hashes = "".join(item.get("sha256", "") for item in sorted_manifest)
    return hashlib.sha256(concat_hashes.encode("utf-8")).hexdigest()


def compute_shared_config_sha256(config: Dict[str, Any]) -> str:
    """Compute canonical SHA-256 for shared configuration."""
    canonical_cfg = {
        "rrf_k": int(config.get("rrf_k", 60)),
        "rrf_weight_lexical": float(config.get("rrf_weight_lexical", 1.0)),
        "rrf_weight_semantic": float(config.get("rrf_weight_semantic", 1.0)),
        "seed": int(config.get("seed", 42)),
    }
    return canonical_json_sha256(canonical_cfg)


def sort_critical_reasons(reasons: List[str]) -> List[str]:
    """Sort critical reasons in canonical order 1..4 without duplicates."""
    seen = set(reasons)
    return [r for r in CRITICAL_REASONS_CANONICAL_ORDER if r in seen]


def validate_2d_status(
    execution_status: str,
    quality_status: str,
    critical_reasons: List[str],
) -> bool:
    """Check whether a 2D status triplet is valid under the specification."""
    if execution_status == "EVALUABLE" and quality_status == "QUALIFIED":
        return len(critical_reasons) == 0
    if execution_status == "EVALUABLE" and quality_status == "CRITICAL_REGRESSION":
        if len(critical_reasons) == 0 or len(critical_reasons) > 4:
            return False
        return critical_reasons == sort_critical_reasons(critical_reasons)
    if execution_status == "NON_EVALUABLE" and quality_status == "NOT_APPLICABLE":
        return len(critical_reasons) == 0
    return False


# ============================================================================
# Runtime & Model Artifact Verification (PREPARE)
# ============================================================================

def verify_runtime_lock(lock_data: Dict[str, Any]) -> Tuple[bool, Optional[str], str]:
    """
    Actively verify running Python environment against lockfile.
    Returns (valid, invalid_reason, lock_identity_str).
    """
    interp = lock_data.get("interpreter", {})
    expected_impl = interp.get("implementation", "")
    expected_ver = interp.get("version", "")
    expected_os = interp.get("os", "")
    expected_arch = interp.get("arch", "")

    actual_impl = sys.implementation.name
    actual_ver = platform.python_version()
    actual_os = platform.system()
    actual_arch = platform.machine()

    lock_identity_str = f"{expected_impl}-{expected_ver}-{expected_os}-{expected_arch}"

    if (
        actual_impl != expected_impl
        or actual_ver != expected_ver
        or actual_os != expected_os
        or actual_arch != expected_arch
    ):
        return False, "SHARED_CONFIG_INVALID", lock_identity_str

    return True, None, lock_identity_str


def verify_model_artifacts(
    model_dir: Path,
    manifest: List[Dict[str, Any]],
) -> Tuple[bool, Optional[str], Optional[str]]:
    """
    Verify presence and SHA-256 hashes of model files against lock manifest.
    Returns (valid, failure_reason, model_artifact_hash).
    """
    if not model_dir.exists() or not model_dir.is_dir():
        return False, "MODEL_UNAVAILABLE", None

    for item in manifest:
        rel_path = item.get("path", "")
        expected_size = item.get("size", 0)
        expected_sha256 = item.get("sha256", "")

        file_path = model_dir / rel_path
        if not file_path.exists() or not file_path.is_file():
            return False, "MODEL_UNAVAILABLE", None

        if file_path.stat().st_size != expected_size:
            return False, "MODEL_HASH_MISMATCH", None

        actual_sha256 = compute_file_sha256(file_path)
        if actual_sha256.lower() != expected_sha256.lower():
            return False, "MODEL_HASH_MISMATCH", None

    model_artifact_hash = compute_model_artifact_sha256(manifest)
    return True, None, model_artifact_hash


# ============================================================================
# Variant A: Lexical Adapter (Pure v4 Reuser)
# ============================================================================

class _LexicalAdapter:
    """
    Variant A Lexical Adapter.
    Strictly reuses select_top_k and _compute_candidate_idf from opencohost.core.memory.memoria_store.
    """

    def __init__(self):
        self.name = "Variant A: v4 Lexical IDF Control"
        self.variant_key = "a"

    def retrieve(
        self,
        query: str,
        pool: List[Dict[str, Any]],
        profile_id: Optional[str] = None,
        k: int = 3,
    ) -> List[Dict[str, Any]]:
        # 1. Deterministic ID-sort over full pool and row adapter
        sorted_pool = []
        for cand in sorted(pool, key=lambda c: str(c.get("id", ""))):
            c = dict(cand)
            if "signature" not in c or not c["signature"]:
                tokens = _significant_tokens(f"{c.get('title', '')} {c.get('content', '')}")
                c["signature"] = " ".join(tokens) if tokens else c.get("title", "")
            if "confidence" not in c:
                c["confidence"] = 1.0
            if "access_count" not in c:
                c["access_count"] = 1
            sorted_pool.append(c)

        # 2. Compute full-pool IDF map before any filtering
        idf_map = _compute_candidate_idf(sorted_pool)

        # 3. Call select_top_k directly on sorted_pool
        selected = select_top_k(query, sorted_pool, k=len(sorted_pool))

        from opencohost.core.memory.memoria_store import _MIN_SHARED_TOKENS, _SCORING_STOPWORDS
        topic = set(_significant_tokens(query)) - _SCORING_STOPWORDS

        # 4. Filter by profile and privacy and calculate score
        results = []
        rank = 1
        for row in selected:
            row_prof = row.get("profile_id")
            if profile_id is not None and row_prof is not None and row_prof != profile_id:
                continue
            if row.get("private", 0) == 1 or row.get("inactive", 0) == 1:
                continue

            sig = set((row.get("signature") or row.get("title", "")).split()) - _SCORING_STOPWORDS
            shared = topic & sig
            score = sum(idf_map[token] for token in shared) if len(shared) >= _MIN_SHARED_TOKENS else 0.0

            results.append({
                "id": str(row.get("id", "")),
                "score": float(score),
                "rank": rank,
            })
            rank += 1
            if len(results) >= k:
                break

        return results


# ============================================================================
# Variant B: MiniLM Dense Retriever (Real ONNX CPU, Zero Fallback)
# ============================================================================

class MiniLMRetriever:
    """
    Variant B: MiniLM Dense Retriever (paraphrase-multilingual-MiniLM-L12-v2, 384d).
    Uses tokenizers + onnxruntime CPU inference with masked-mean pooling and L2 normalization.
    Zero fallback allowed.
    """

    def __init__(
        self,
        model_dir: Optional[Union[str, Path]] = None,
        threshold: Optional[Union[float, str]] = None,
        session: Optional[Any] = None,
        tokenizer: Optional[Any] = None,
        embed_cache: Optional[Dict[str, List[float]]] = None,
    ):
        self.name = "Variant B: MiniLM Dense"
        self.variant_key = "b"
        self.model_dir = Path(model_dir) if model_dir else None
        self.threshold = threshold
        self.session = session
        self.tokenizer = tokenizer
        self.initialized = session is not None and tokenizer is not None
        self._embed_cache = embed_cache if embed_cache is not None else {}

    def initialize(self) -> None:
        """Initialize tokenizer and ONNX runtime session. Fails closed if missing."""
        if not self.model_dir:
            raise RuntimeError("MiniLMRetriever requires a valid model_dir containing model.onnx and tokenizer.json")

        tokenizer_path = self.model_dir / "tokenizer.json"
        model_path = self.model_dir / "model.onnx"

        if not tokenizer_path.exists() or not model_path.exists():
            raise FileNotFoundError(f"Model artifacts missing in {self.model_dir}")

        import numpy as np
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.enable_truncation(max_length=256)
        self.tokenizer.enable_padding(length=256)

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3  # Error only
        self.session = ort.InferenceSession(str(model_path), sess_options=opts, providers=["CPUExecutionProvider"])
        self.initialized = True

    def _encode_text(self, text: str) -> List[float]:
        """Encode text to 384-d normalized vector using ONNX model."""
        if not self.initialized or self.session is None or self.tokenizer is None:
            raise RuntimeError("MiniLMRetriever is not initialized. Fallbacks are strictly forbidden.")

        if text in self._embed_cache:
            return self._embed_cache[text]

        import numpy as np

        encoded = self.tokenizer.encode(text)
        input_ids = np.array([encoded.ids], dtype=np.int64)
        attention_mask = np.array([encoded.attention_mask], dtype=np.int64)

        session_input_names = {inp.name for inp in self.session.get_inputs()}
        inputs = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
        }
        if "token_type_ids" in session_input_names:
            inputs["token_type_ids"] = np.array([encoded.type_ids], dtype=np.int64)

        outputs = self.session.run(None, inputs)
        token_embeddings = outputs[0]  # shape: (1, seq_len, 384)

        # Masked MEAN pooling
        mask = attention_mask[:, :, np.newaxis].astype(np.float32)
        sum_embeddings = np.sum(token_embeddings * mask, axis=1)
        sum_mask = np.clip(np.sum(mask, axis=1), a_min=1e-9, a_max=None)
        mean_pooled = sum_embeddings / sum_mask

        # L2 normalization
        norm = np.linalg.norm(mean_pooled, axis=1, keepdims=True)
        norm = np.clip(norm, a_min=1e-9, a_max=None)
        normalized = mean_pooled / norm
        vec = normalized[0].tolist()
        self._embed_cache[text] = vec
        return vec

    def retrieve(
        self,
        query: str,
        pool: List[Dict[str, Any]],
        profile_id: Optional[str] = None,
        k: int = 3,
    ) -> List[Dict[str, Any]]:
        if not self.initialized:
            raise RuntimeError("Cannot retrieve with uninitialized MiniLMRetriever")

        sorted_pool = sorted(pool, key=lambda c: str(c.get("id", "")))
        query_vec = self._encode_text(query)

        scored = []
        for cand in sorted_pool:
            cand_prof = cand.get("profile_id")
            if profile_id is not None and cand_prof is not None and cand_prof != profile_id:
                continue
            if cand.get("private", 0) == 1 or cand.get("inactive", 0) == 1:
                continue

            cand_text = f"{cand.get('title', '')}\n{cand.get('content', '')}"
            cand_vec = self._encode_text(cand_text)
            cos_sim = sum(a * b for a, b in zip(query_vec, cand_vec))
            scored.append({
                "id": str(cand.get("id", "")),
                "score": float(cos_sim),
            })

        if not scored:
            return []

        # Sort by score descending, tie-break by ID ascending
        scored.sort(key=lambda x: (-x["score"], x["id"]))

        # Apply threshold
        top_score = scored[0]["score"]
        if self.threshold == "ABSTAIN_ALL":
            return []
        elif isinstance(self.threshold, (int, float)):
            if top_score < self.threshold:
                return []
            scored = [x for x in scored if x["score"] >= self.threshold]

        results = []
        for rank, item in enumerate(scored[:k], 1):
            results.append({
                "id": item["id"],
                "score": item["score"],
                "rank": rank,
            })
        return results


# ============================================================================
# Variant C: Hybrid RRF Fusion
# ============================================================================

def rrf_fuse(
    lexical_results: List[Dict[str, Any]],
    semantic_results: List[Dict[str, Any]],
    k: int = 60,
    w_lex: float = 1.0,
    w_sem: float = 1.0,
    threshold: Optional[Union[float, str]] = None,
) -> List[Dict[str, Any]]:
    """
    Reciprocal Rank Fusion over Variant A (Lexical) and Variant B (Dense).
    RRF_Score(d) = sum(w_m / (k + rank_m(d))).
    """
    scores: Dict[str, float] = {}

    for item in lexical_results:
        cid = item["id"]
        rank = item["rank"]
        scores[cid] = scores.get(cid, 0.0) + (w_lex / (k + rank))

    for item in semantic_results:
        cid = item["id"]
        rank = item["rank"]
        scores[cid] = scores.get(cid, 0.0) + (w_sem / (k + rank))

    if not scores:
        return []

    sorted_items = sorted(scores.items(), key=lambda x: (-x[1], x[0]))

    top_score = sorted_items[0][1]
    if threshold == "ABSTAIN_ALL":
        return []
    elif isinstance(threshold, (int, float)):
        if top_score < threshold:
            return []
        sorted_items = [x for x in sorted_items if x[1] >= threshold]

    results = []
    for rank, (cid, score) in enumerate(sorted_items, 1):
        results.append({
            "id": cid,
            "score": float(score),
            "rank": rank,
        })
    return results


class HybridRRFRetriever:
    """
    Variant C: Hybrid RRF Retriever combining Lexical (A) + MiniLM (B).
    """

    def __init__(
        self,
        lexical_retriever: _LexicalAdapter,
        semantic_retriever: MiniLMRetriever,
        threshold: Optional[Union[float, str]] = None,
        k_rrf: int = 60,
        w_lex: float = 1.0,
        w_sem: float = 1.0,
    ):
        self.name = "Variant C: Hybrid RRF"
        self.variant_key = "c"
        self.lexical = lexical_retriever
        self.semantic = semantic_retriever
        self.threshold = threshold
        self.k_rrf = k_rrf
        self.w_lex = w_lex
        self.w_sem = w_sem

    def retrieve(
        self,
        query: str,
        pool: List[Dict[str, Any]],
        profile_id: Optional[str] = None,
        k: int = 3,
    ) -> List[Dict[str, Any]]:
        lex_results = self.lexical.retrieve(query, pool, profile_id=profile_id, k=len(pool))
        sem_results = self.semantic.retrieve(query, pool, profile_id=profile_id, k=len(pool))
        fused = rrf_fuse(
            lex_results,
            sem_results,
            k=self.k_rrf,
            w_lex=self.w_lex,
            w_sem=self.w_sem,
            threshold=self.threshold,
        )
        return fused[:k]


# ============================================================================
# Calibration Logic & Threshold Derivation
# ============================================================================

def generate_threshold_candidates(scores: List[float]) -> List[Union[float, str]]:
    """Generate exhaustive threshold candidates: [SELECT_ALL, midpoints..., ABSTAIN_ALL]."""
    uniques = sorted(set(scores))
    if not uniques:
        return ["SELECT_ALL", "ABSTAIN_ALL"]
    if len(uniques) == 1:
        return ["SELECT_ALL", "ABSTAIN_ALL"]

    midpoints = []
    for i in range(len(uniques) - 1):
        midpoints.append((uniques[i] + uniques[i + 1]) / 2.0)

    return ["SELECT_ALL"] + midpoints + ["ABSTAIN_ALL"]


def evaluate_calibration_safety_and_objectives(
    retriever_factory: Callable[..., Any],
    cal_cases: List[Dict[str, Any]],
    threshold: Union[float, str],
    a_precision_at_1: float,
) -> Dict[str, Any]:
    """Evaluate candidate threshold on the 24 calibration cases."""
    retriever = retriever_factory(threshold=threshold)

    no_mem_abstentions = 0
    no_mem_false_injections = 0
    privacy_leaks = 0
    private_leaks = 0
    inactive_leaks = 0
    cross_prof_leaks = 0
    near_wrong_correct = 0
    syn_recip_ranks = []
    para_recip_ranks = []
    hard_correct_at_1 = 0

    for case in cal_cases:
        fam = case["family"]
        query = case["query"]
        expected_ids = set(case.get("expected_ids", []))
        expected_abstain = case.get("expected_abstain", False)
        candidates = case["candidates"]
        prof_id = candidates[0].get("profile_id") if candidates else None

        results = retriever.retrieve(query, candidates, profile_id=prof_id, k=3)
        top_id = results[0]["id"] if results else None
        top_ids_at_3 = [r["id"] for r in results[:3]]

        if fam == "no_memory":
            if len(results) == 0:
                no_mem_abstentions += 1
            else:
                no_mem_false_injections += 1
        elif fam == "profile_privacy":
            cand_map = {c["id"]: c for c in candidates}
            for res_item in results:
                c_data = cand_map.get(res_item["id"], {})
                if c_data.get("private", 0) == 1:
                    private_leaks += 1
                    privacy_leaks += 1
                elif c_data.get("inactive", 0) == 1:
                    inactive_leaks += 1
                    privacy_leaks += 1
                elif prof_id is not None and c_data.get("profile_id") != prof_id:
                    cross_prof_leaks += 1
                    privacy_leaks += 1
        elif fam == "near_but_wrong":
            if top_id in expected_ids:
                near_wrong_correct += 1
        elif fam == "synonym":
            rr = 0.0
            for rank_idx, cand_id in enumerate(top_ids_at_3, 1):
                if cand_id in expected_ids:
                    rr = 1.0 / rank_idx
                    break
            syn_recip_ranks.append(rr)
        elif fam == "paraphrase":
            rr = 0.0
            for rank_idx, cand_id in enumerate(top_ids_at_3, 1):
                if cand_id in expected_ids:
                    rr = 1.0 / rank_idx
                    break
            para_recip_ranks.append(rr)
        elif fam == "hard_lexical":
            if top_id in expected_ids:
                hard_correct_at_1 += 1

    near_wrong_precision = near_wrong_correct / 4.0
    syn_mrr = sum(syn_recip_ranks) / len(syn_recip_ranks) if syn_recip_ranks else 0.0
    para_mrr = sum(para_recip_ranks) / len(para_recip_ranks) if para_recip_ranks else 0.0
    syn_para_mean = (syn_mrr + para_mrr) / 2.0
    hard_recall = hard_correct_at_1 / 4.0

    is_safe = (
        no_mem_false_injections == 0
        and privacy_leaks == 0
        and near_wrong_precision >= a_precision_at_1
    )

    return {
        "threshold": threshold,
        "safe": is_safe,
        "cal_no_memory_abstention_count": no_mem_abstentions,
        "cal_no_memory_false_injection_count": no_mem_false_injections,
        "cal_no_memory_abstention_accuracy": no_mem_abstentions / 4.0,
        "cal_profile_isolation_pass_count": 4 - min(4, privacy_leaks),
        "cal_profile_privacy_leakage_count": privacy_leaks,
        "cal_profile_private_leakage_count": private_leaks,
        "cal_profile_inactive_leakage_count": inactive_leaks,
        "cal_profile_cross_profile_leakage_count": cross_prof_leaks,
        "cal_near_wrong_precision_at_1": near_wrong_precision,
        "cal_near_wrong_a_precision_at_1": a_precision_at_1,
        "cal_syn_mrr_at_3": syn_mrr,
        "cal_para_mrr_at_3": para_mrr,
        "cal_syn_para_mrr3_mean": syn_para_mean,
        "cal_hard_correct_at_1_count": hard_correct_at_1,
        "cal_hard_recall_at_1": hard_recall,
    }


def select_best_calibration_threshold(eval_candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Strict lexicographic selection: 1. MRR mean -> 2. Hard recall -> 3. Conservative."""
    safe_candidates = [c for c in eval_candidates if c.get("safe", False)]
    if not safe_candidates:
        return None

    def sort_key(c: Dict[str, Any]) -> Tuple[float, float, int, float]:
        mrr_mean = c.get("cal_syn_para_mrr3_mean", 0.0)
        hard_rec = c.get("cal_hard_recall_at_1", 0.0)
        t = c.get("threshold")
        if t == "ABSTAIN_ALL":
            conservatism_tier = 2
            val = 0.0
        elif isinstance(t, (int, float)):
            conservatism_tier = 1
            val = float(t)
        else:  # SELECT_ALL
            conservatism_tier = 0
            val = 0.0
        return (mrr_mean, hard_rec, conservatism_tier, val)

    safe_candidates.sort(key=sort_key, reverse=True)
    return safe_candidates[0]


# ============================================================================
# Metrics Calculation over Locked Cases
# ============================================================================

def compute_metrics_object(
    retriever: Any,
    locked_cases: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Compute 29-field MetricsObject over 80 locked cases."""
    hard_c1 = 0
    hard_c3 = 0
    hard_recip_ranks = []

    syn_c1 = 0
    syn_c3 = 0
    syn_recip_ranks = []

    para_c1 = 0
    para_c3 = 0
    para_recip_ranks = []

    no_mem_abstentions = 0
    no_mem_false_injections = 0

    near_wrong_c1 = 0
    near_wrong_target_recip_ranks = []

    privacy_passes = 0
    privacy_leaks = 0
    private_leaks = 0
    inactive_leaks = 0
    cross_prof_leaks = 0

    stale_correct = 0

    for case in locked_cases:
        fam = case["family"]
        query = case["query"]
        expected_ids = set(case.get("expected_ids", []))
        expected_abstain = case.get("expected_abstain", False)
        candidates = case["candidates"]
        prof_id = candidates[0].get("profile_id") if candidates else None

        results = retriever.retrieve(query, candidates, profile_id=prof_id, k=3)
        top_id = results[0]["id"] if results else None
        top_ids_at_3 = [r["id"] for r in results[:3]]

        if fam == "hard_lexical":
            if top_id in expected_ids:
                hard_c1 += 1
            if any(cid in expected_ids for cid in top_ids_at_3):
                hard_c3 += 1
            rr = 0.0
            for rank_idx, cid in enumerate(top_ids_at_3, 1):
                if cid in expected_ids:
                    rr = 1.0 / rank_idx
                    break
            hard_recip_ranks.append(rr)

        elif fam == "synonym":
            if top_id in expected_ids:
                syn_c1 += 1
            if any(cid in expected_ids for cid in top_ids_at_3):
                syn_c3 += 1
            rr = 0.0
            for rank_idx, cid in enumerate(top_ids_at_3, 1):
                if cid in expected_ids:
                    rr = 1.0 / rank_idx
                    break
            syn_recip_ranks.append(rr)

        elif fam == "paraphrase":
            if top_id in expected_ids:
                para_c1 += 1
            if any(cid in expected_ids for cid in top_ids_at_3):
                para_c3 += 1
            rr = 0.0
            for rank_idx, cid in enumerate(top_ids_at_3, 1):
                if cid in expected_ids:
                    rr = 1.0 / rank_idx
                    break
            para_recip_ranks.append(rr)

        elif fam == "no_memory":
            if len(results) == 0:
                no_mem_abstentions += 1
            else:
                no_mem_false_injections += 1

        elif fam == "near_but_wrong":
            if top_id in expected_ids:
                near_wrong_c1 += 1
            rr = 0.0
            for rank_idx, cid in enumerate(top_ids_at_3, 1):
                if cid in expected_ids:
                    rr = 1.0 / rank_idx
                    break
            near_wrong_target_recip_ranks.append(rr)

        elif fam == "profile_privacy":
            cand_map = {c["id"]: c for c in candidates}
            case_has_leak = False
            for res_item in results:
                c_data = cand_map.get(res_item["id"], {})
                if c_data.get("private", 0) == 1:
                    private_leaks += 1
                    privacy_leaks += 1
                    case_has_leak = True
                elif c_data.get("inactive", 0) == 1:
                    inactive_leaks += 1
                    privacy_leaks += 1
                    case_has_leak = True
                elif prof_id is not None and c_data.get("profile_id") != prof_id:
                    cross_prof_leaks += 1
                    privacy_leaks += 1
                    case_has_leak = True

            if not case_has_leak and top_id in expected_ids:
                privacy_passes += 1

        elif fam == "stale_contradiction":
            if top_id in expected_ids:
                stale_correct += 1

    syn_mrr = sum(syn_recip_ranks) / 14.0 if syn_recip_ranks else 0.0
    para_mrr = sum(para_recip_ranks) / 16.0 if para_recip_ranks else 0.0
    semantic_quality_score = (syn_mrr + para_mrr) / 2.0

    return {
        "hard_correct_at_1_count": hard_c1,
        "hard_correct_at_3_count": hard_c3,
        "hard_recall_at_1": hard_c1 / 16.0,
        "hard_recall_at_3": hard_c3 / 16.0,
        "hard_mrr_at_3": sum(hard_recip_ranks) / 16.0 if hard_recip_ranks else 0.0,
        "syn_correct_at_1_count": syn_c1,
        "syn_correct_at_3_count": syn_c3,
        "syn_recall_at_1": syn_c1 / 14.0,
        "syn_recall_at_3": syn_c3 / 14.0,
        "syn_mrr_at_3": syn_mrr,
        "para_correct_at_1_count": para_c1,
        "para_correct_at_3_count": para_c3,
        "para_recall_at_1": para_c1 / 16.0,
        "para_recall_at_3": para_c3 / 16.0,
        "para_mrr_at_3": para_mrr,
        "semantic_quality_score": semantic_quality_score,
        "no_memory_abstention_count": no_mem_abstentions,
        "no_memory_false_injection_count": no_mem_false_injections,
        "no_memory_abstention_accuracy": no_mem_abstentions / 12.0,
        "near_wrong_correct_at_1_count": near_wrong_c1,
        "near_wrong_precision_at_1": near_wrong_c1 / 12.0,
        "near_wrong_target_mrr_at_3": sum(near_wrong_target_recip_ranks) / 12.0 if near_wrong_target_recip_ranks else 0.0,
        "profile_isolation_pass_count": privacy_passes,
        "profile_privacy_leakage_count": privacy_leaks,
        "profile_private_leakage_count": private_leaks,
        "profile_inactive_leakage_count": inactive_leaks,
        "profile_cross_profile_leakage_count": cross_prof_leaks,
        "stale_correct_count": stale_correct,
        "stale_correct_rate": stale_correct / 5.0,
    }


# ============================================================================
# Quality Gates and Winner Selection
# ============================================================================

def evaluate_quality_gates(
    candidate_metrics: Dict[str, Any],
    a_metrics: Dict[str, Any],
) -> Tuple[str, List[str]]:
    """Evaluate 4 quality gates for candidate B or C against baseline A."""
    failed_reasons = []

    # 1. HARD_RECALL_REGRESSION: hard_correct_at_1_count ==16 literal frozen requirement
    if candidate_metrics.get("hard_correct_at_1_count", 0) != 16:
        failed_reasons.append("HARD_RECALL_REGRESSION")

    # 2. NO_MEMORY_INJECTION: no_memory_false_injection_count > 0
    if candidate_metrics.get("no_memory_false_injection_count", 0) > 0:
        failed_reasons.append("NO_MEMORY_INJECTION")

    # 3. NEAR_WRONG_REGRESSION: near_wrong_precision_at_1 < a_metrics.near_wrong_precision_at_1
    if candidate_metrics.get("near_wrong_precision_at_1", 0.0) < a_metrics.get("near_wrong_precision_at_1", 0.0):
        failed_reasons.append("NEAR_WRONG_REGRESSION")

    # 4. PROFILE_PRIVACY_LEAKAGE: profile_privacy_leakage_count > 0
    if candidate_metrics.get("profile_privacy_leakage_count", 0) > 0:
        failed_reasons.append("PROFILE_PRIVACY_LEAKAGE")

    ordered_reasons = sort_critical_reasons(failed_reasons)
    if ordered_reasons:
        return "CRITICAL_REGRESSION", ordered_reasons
    else:
        return "QUALIFIED", []


def evaluate_terminal_routing(
    global_invalid_reason: Optional[str],
    candidate_b_status: Tuple[str, str, List[str]],
    candidate_c_status: Tuple[str, str, List[str]],
    a_metrics: Optional[Dict[str, Any]],
    b_metrics: Optional[Dict[str, Any]],
    c_metrics: Optional[Dict[str, Any]],
) -> Tuple[str, Optional[str]]:
    """Deterministic terminal routing per Specification steps 1-7 frozen contract."""
    if global_invalid_reason is not None:
        return "INCONCLUSIVE", None
    b_exec, b_qual, _ = candidate_b_status
    c_exec, c_qual, _ = candidate_c_status
    if b_exec != "EVALUABLE" and c_exec != "EVALUABLE":
        return "INCONCLUSIVE", None
    has_qualified = (b_exec == "EVALUABLE" and b_qual == "QUALIFIED") or (c_exec == "EVALUABLE" and c_qual == "QUALIFIED")
    if not has_qualified:
        return "KEEP_LEXICAL", None
    if a_metrics is None:
        return "INCONCLUSIVE", None
    a_score = a_metrics.get("semantic_quality_score", 0.0)
    qualified = []
    for key, exec_st, qual_st, m in [("b", b_exec, b_qual, b_metrics), ("c", c_exec, c_qual, c_metrics)]:
        if exec_st == "EVALUABLE" and qual_st == "QUALIFIED" and m is not None:
            cand_score = m.get("semantic_quality_score", 0.0)
            mean_recall_3 = (m.get("syn_recall_at_3", 0.0) + m.get("para_recall_at_3", 0.0)) / 2.0
            para_mrr_3 = m.get("para_mrr_at_3", 0.0)
            qualified.append((cand_score, mean_recall_3, para_mrr_3, 1 if key == "b" else 0, key, m))
    if not qualified:
        return "KEEP_LEXICAL", None
    qualified.sort(key=lambda x: (x[0], x[1], x[2], x[3]), reverse=True)
    best_key = qualified[0][4]
    best_score = qualified[0][0]
    rel_gain = (best_score - a_score) / max(a_score, 1e-9)
    abs_gain = best_score - a_score
    if rel_gain >= 0.25 and abs_gain >= 0.10:
        return "SEMANTIC_PROMISING", best_key
    elif rel_gain < 0.10:
        return "KEEP_LEXICAL", best_key
    else:
        return "INCONCLUSIVE", best_key


# ============================================================================
# Benchmark Receipt Validator
# ============================================================================

class BenchmarkReceiptValidator:
    """Validates BenchmarkReceiptV1 strictly against closed schema tables."""

    @classmethod
    def validate(cls, receipt: Dict[str, Any]) -> bool:
        if not isinstance(receipt, dict):
            return False
        if set(receipt.keys()) != TOP_LEVEL_KEYS:
            return False
        if receipt.get("schema_version") != "memory-semantic-benchmark-receipt-v1":
            return False
        if receipt.get("model_id") != "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2":
            return False
        if receipt.get("adr_reference") != "ADR-053":
            return False
        if not isinstance(receipt.get("run_id"), str) or len(receipt.get("run_id","")) < 16 or not re.match(r"^[0-9A-Za-z-]+$", receipt.get("run_id","")):
            return False
        state = receipt.get("state")
        if state not in ("VALID", "GLOBAL_INVALID"):
            return False
        gir = receipt.get("global_invalid_reason")
        if state == "VALID" and gir is not None:
            return False
        if state == "GLOBAL_INVALID" and gir not in GLOBAL_INVALID_REASONS_CANONICAL_ORDER:
            return False
        if state == "GLOBAL_INVALID":
            # frozen phase rule: GLOBAL_INVALID must be pre-evaluation null candidates/metrics, winner null, INCONCLUSIVE
            cands = receipt.get("candidates", {})
            mets = receipt.get("metrics", {})
            if cands.get("b") is not None or cands.get("c") is not None:
                return False
            if mets.get("a") is not None or mets.get("b") is not None or mets.get("c") is not None:
                return False
            if receipt.get("winner") is not None:
                return False
            if receipt.get("terminal_route") != "INCONCLUSIVE":
                return False
        # hashes hex64 or null (availability-based for GLOBAL_INVALID, strict for VALID)
        for hk in ("locked_fixture_hash","calibration_fixture_hash","model_artifact_hash","shared_config_hash"):
            v = receipt.get(hk)
            if v is not None and not (isinstance(v,str) and re.match(r"^[0-9a-f]{64}$", v)):
                return False
        seed = receipt.get("seed")
        if seed is not None and (not isinstance(seed,int) or isinstance(seed,bool)):
            return False
        li = receipt.get("lock_identity")
        if li is not None and not isinstance(li,str):
            return False
        # R13: VALID strict nullability with MODEL_UNAVAILABLE exception
        if state == "VALID":
            if receipt.get("locked_fixture_hash") is None:
                return False
            if receipt.get("calibration_fixture_hash") is None:
                return False
            if receipt.get("shared_config_hash") is None:
                return False
            if receipt.get("seed") is None or isinstance(receipt.get("seed"), bool):
                return False
            if receipt.get("lock_identity") is None:
                return False
            mah = receipt.get("model_artifact_hash")
            if mah is None:
                cands_tmp = receipt.get("candidates", {})
                b_tmp = cands_tmp.get("b")
                c_tmp = cands_tmp.get("c")
                if not (b_tmp and c_tmp and b_tmp.get("execution_status") == "NON_EVALUABLE" and b_tmp.get("execution_reason") == "MODEL_UNAVAILABLE" and c_tmp.get("execution_status") == "NON_EVALUABLE" and c_tmp.get("execution_reason") == "MODEL_UNAVAILABLE" and receipt.get("winner") is None and receipt.get("terminal_route") == "INCONCLUSIVE"):
                    return False
            elif not re.match(r"^[0-9a-f]{64}$", mah):
                return False
        route = receipt.get("terminal_route")
        if route not in ("SEMANTIC_PROMISING", "KEEP_LEXICAL", "INCONCLUSIVE"):
            return False
        winner = receipt.get("winner")
        if winner not in (None, "b", "c"):
            return False
        if route == "SEMANTIC_PROMISING" and winner not in ("b", "c"):
            return False
        if route in ("KEEP_LEXICAL","INCONCLUSIVE") and winner not in (None,"b","c"):
            return False
        candidates = receipt.get("candidates")
        if not isinstance(candidates, dict) or set(candidates.keys()) != {"b", "c"}:
            return False
        for vkey in ("b", "c"):
            cand = candidates[vkey]
            if cand is not None:
                if not isinstance(cand, dict) or set(cand.keys()) != CANDIDATE_RECEIPT_KEYS:
                    return False
                if cand.get("variant_key") != vkey:
                    return False
                if not validate_2d_status(cand.get("execution_status",""), cand.get("quality_status",""), cand.get("critical_reasons",[])):
                    return False
                er = cand.get("execution_reason")
                es = cand.get("execution_status")
                if es == "EVALUABLE" and er is not None:
                    return False
                if es == "NON_EVALUABLE" and er not in ("DEPENDENCY_UNAVAILABLE","MODEL_UNAVAILABLE","MODEL_HASH_MISMATCH","CONFIG_INVALID","CALIBRATION_LEAK","CALIBRATION_FAILED","EXECUTION_FAILED","METRICS_INCOMPLETE"):
                    return False
                calib = cand.get("calibration")
                if es == "NON_EVALUABLE" and calib is not None:
                    return False
                if es == "EVALUABLE" and calib is None:
                    return False
                if es == "EVALUABLE" and calib is not None:
                    if not isinstance(calib, dict) or set(calib.keys()) != CALIBRATION_RECEIPT_KEYS:
                        return False
                    if calib.get("variant_key") != vkey:
                        return False
                    th = calib.get("threshold")
                    if isinstance(th,bool) or not (isinstance(th,float) or isinstance(th,int) or th in ("SELECT_ALL","ABSTAIN_ALL")):
                        return False
                    for hkey in ("calibration_fixture_hash","model_artifact_hash","shared_config_hash"):
                        hv = calib.get(hkey)
                        if not isinstance(hv,str) or not re.match(r"^[0-9a-f]{64}$", hv):
                            return False
                    # R13: bind nested calibration hashes to top-level hashes
                    if calib.get("calibration_fixture_hash") != receipt.get("calibration_fixture_hash"):
                        return False
                    if calib.get("shared_config_hash") != receipt.get("shared_config_hash"):
                        return False
                    if receipt.get("model_artifact_hash") is not None and calib.get("model_artifact_hash") != receipt.get("model_artifact_hash"):
                        return False
                    if receipt.get("model_artifact_hash") is None and calib.get("model_artifact_hash") is not None:
                        return False
                    if not isinstance(calib.get("frozen_at"), str) or not re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", calib.get("frozen_at")):
                        return False
                    if not isinstance(calib.get("frozen_sequence"), int) or isinstance(calib.get("frozen_sequence"), bool) or calib.get("frozen_sequence") < 0:
                        return False
                    for ik in ("frozen_sequence","cal_no_memory_abstention_count","cal_no_memory_false_injection_count","cal_profile_isolation_pass_count","cal_profile_privacy_leakage_count","cal_profile_private_leakage_count","cal_profile_inactive_leakage_count","cal_profile_cross_profile_leakage_count","cal_hard_correct_at_1_count"):
                        if not isinstance(calib.get(ik), int) or isinstance(calib.get(ik), bool):
                            return False
                    if not 0 <= calib.get("cal_no_memory_abstention_count") <= 4: return False
                    if not 0 <= calib.get("cal_no_memory_false_injection_count") <= 4: return False
                    if not 0 <= calib.get("cal_profile_isolation_pass_count") <= 4: return False
                    if not 0 <= calib.get("cal_profile_privacy_leakage_count") <= 4: return False
                    if not 0 <= calib.get("cal_profile_private_leakage_count") <= 4: return False
                    if not 0 <= calib.get("cal_profile_inactive_leakage_count") <= 4: return False
                    if not 0 <= calib.get("cal_profile_cross_profile_leakage_count") <= 4: return False
                    if not 0 <= calib.get("cal_hard_correct_at_1_count") <= 4: return False
                    for rk in ("cal_no_memory_abstention_accuracy","cal_near_wrong_precision_at_1","cal_near_wrong_a_precision_at_1","cal_syn_mrr_at_3","cal_para_mrr_at_3","cal_syn_para_mrr3_mean","cal_hard_recall_at_1"):
                        rv = calib.get(rk)
                        if isinstance(rv,bool) or not isinstance(rv,(int,float)) or not 0 <= rv <= 1:
                            return False
                    # count-ratio consistency for calibration (normatively derivable)
                    if abs(calib.get("cal_no_memory_abstention_accuracy",-1) - calib.get("cal_no_memory_abstention_count",0)/4.0) > 1e-9: return False
                    if abs(calib.get("cal_hard_recall_at_1",-1) - calib.get("cal_hard_correct_at_1_count",0)/4.0) > 1e-9: return False
                    if abs(calib.get("cal_syn_para_mrr3_mean",-1) - (calib.get("cal_syn_mrr_at_3",0)+calib.get("cal_para_mrr_at_3",0))/2.0) > 1e-9: return False
        metrics = receipt.get("metrics")
        if not isinstance(metrics, dict) or set(metrics.keys()) != {"a", "b", "c"}:
            return False
        # VALID requires non-null baseline and candidates; NON_EVALUABLE must have null calibration/metrics
        if state == "VALID":
            if candidates.get("b") is None or candidates.get("c") is None:
                return False
            if metrics.get("a") is None:
                return False
        cands = receipt.get("candidates", {})
        for vkey in ("b", "c"):
            cand = cands.get(vkey)
            m = metrics.get(vkey)
            if cand is not None:
                es = cand.get("execution_status")
                if es == "EVALUABLE" and m is None:
                    return False
                if es == "NON_EVALUABLE" and m is not None:
                    return False
        for mkey in ("a", "b", "c"):
            m = metrics[mkey]
            if m is not None:
                if not isinstance(m, dict) or set(m.keys()) != METRICS_OBJECT_KEYS:
                    return False
                # int bounds
                if not 0 <= m.get("hard_correct_at_1_count", -1) <= 16: return False
                if not 0 <= m.get("hard_correct_at_3_count", -1) <= 16: return False
                if not 0 <= m.get("syn_correct_at_1_count", -1) <= 14: return False
                if not 0 <= m.get("syn_correct_at_3_count", -1) <= 14: return False
                if not 0 <= m.get("para_correct_at_1_count", -1) <= 16: return False
                if not 0 <= m.get("para_correct_at_3_count", -1) <= 16: return False
                if not 0 <= m.get("no_memory_abstention_count", -1) <= 12: return False
                if not 0 <= m.get("no_memory_false_injection_count", -1) <= 12: return False
                if not 0 <= m.get("near_wrong_correct_at_1_count", -1) <= 12: return False
                if not 0 <= m.get("profile_isolation_pass_count", -1) <= 5: return False
                if not 0 <= m.get("profile_privacy_leakage_count", -1) <= 5: return False
                if not 0 <= m.get("profile_private_leakage_count", -1) <= 5: return False
                if not 0 <= m.get("profile_inactive_leakage_count", -1) <= 5: return False
                if not 0 <= m.get("profile_cross_profile_leakage_count", -1) <= 5: return False
                if not 0 <= m.get("stale_correct_count", -1) <= 5: return False
                for int_key in ("hard_correct_at_1_count","hard_correct_at_3_count","syn_correct_at_1_count","syn_correct_at_3_count","para_correct_at_1_count","para_correct_at_3_count","no_memory_abstention_count","no_memory_false_injection_count","near_wrong_correct_at_1_count","profile_isolation_pass_count","profile_privacy_leakage_count","profile_private_leakage_count","profile_inactive_leakage_count","profile_cross_profile_leakage_count","stale_correct_count"):
                    if not isinstance(m.get(int_key), int) or isinstance(m.get(int_key), bool):
                        return False
                for num_key in ("hard_recall_at_1","hard_recall_at_3","hard_mrr_at_3","syn_recall_at_1","syn_recall_at_3","syn_mrr_at_3","para_recall_at_1","para_recall_at_3","para_mrr_at_3","semantic_quality_score","no_memory_abstention_accuracy","near_wrong_precision_at_1","near_wrong_target_mrr_at_3","stale_correct_rate"):
                    v = m.get(num_key)
                    if isinstance(v,bool) or not isinstance(v, (int,float)) or isinstance(v,str) or not 0 <= v <= 1:
                        return False
                # count-ratio consistency for metrics (normatively derivable)
                if abs(m.get("hard_recall_at_1",-1) - m.get("hard_correct_at_1_count",0)/16.0) > 1e-9: return False
                if abs(m.get("hard_recall_at_3",-1) - m.get("hard_correct_at_3_count",0)/16.0) > 1e-9: return False
                if abs(m.get("syn_recall_at_1",-1) - m.get("syn_correct_at_1_count",0)/14.0) > 1e-9: return False
                if abs(m.get("syn_recall_at_3",-1) - m.get("syn_correct_at_3_count",0)/14.0) > 1e-9: return False
                if abs(m.get("para_recall_at_1",-1) - m.get("para_correct_at_1_count",0)/16.0) > 1e-9: return False
                if abs(m.get("para_recall_at_3",-1) - m.get("para_correct_at_3_count",0)/16.0) > 1e-9: return False
                if abs(m.get("no_memory_abstention_accuracy",-1) - m.get("no_memory_abstention_count",0)/12.0) > 1e-9: return False
                if abs(m.get("near_wrong_precision_at_1",-1) - m.get("near_wrong_correct_at_1_count",0)/12.0) > 1e-9: return False
                if abs(m.get("stale_correct_rate",-1) - m.get("stale_correct_count",0)/5.0) > 1e-9: return False
                if abs(m.get("semantic_quality_score",-1) - (m.get("syn_mrr_at_3",0)+m.get("para_mrr_at_3",0))/2.0) > 1e-9: return False
        # winner/terminal_route consistency with validated statuses/metrics/routing
        # compute expected routing only when VALID and baseline present
        if state == "VALID":
            try:
                b_cand = candidates.get("b")
                c_cand = candidates.get("c")
                b_status = (b_cand.get("execution_status"), b_cand.get("quality_status"), b_cand.get("critical_reasons")) if b_cand else ("NON_EVALUABLE","NOT_APPLICABLE",[])
                c_status = (c_cand.get("execution_status"), c_cand.get("quality_status"), c_cand.get("critical_reasons")) if c_cand else ("NON_EVALUABLE","NOT_APPLICABLE",[])
                exp_route, exp_winner = evaluate_terminal_routing(None, b_status, c_status, metrics.get("a"), metrics.get("b"), metrics.get("c"))
                if exp_route != route or exp_winner != winner:
                    return False
            except Exception:
                return False
        return True


# ============================================================================
# Canonical Receipt Projection (single source for JSON/Markdown)
# ============================================================================

def _receipt_projection_lines(receipt: Dict[str, Any]) -> list:
    """Deterministic canonical projection of all normative receipt leaves for Markdown."""
    lines = []
    # Top-level header leaves
    lines.append(f"- **Run ID**: `{receipt.get('run_id')}`")
    lines.append(f"- **State**: `{receipt.get('state')}`")
    lines.append(f"- **Terminal Route**: `{receipt.get('terminal_route')}`")
    lines.append(f"- **Winner**: `{receipt.get('winner')}`")
    lines.append(f"- **Schema Version**: `{receipt.get('schema_version')}`")
    lines.append(f"- **Model ID**: `{receipt.get('model_id')}`")
    lines.append(f"- **ADR Reference**: `{receipt.get('adr_reference')}`")
    # Complete top-level identity/nullability
    for hk in ("locked_fixture_hash","calibration_fixture_hash","model_artifact_hash","shared_config_hash","lock_identity","seed","global_invalid_reason"):
        lines.append(f"- `{hk}`: `{receipt.get(hk)}`")
    # Candidates with full calibration
    cands = receipt.get("candidates", {})
    for ck in ("b","c"):
        c = cands.get(ck)
        if c is None:
            lines.append(f"- `candidates.{ck}`: `null`")
        else:
            lines.append(f"- `candidates.{ck}.variant_key`: `{c.get('variant_key')}`")
            lines.append(f"- `candidates.{ck}.execution_status`: `{c.get('execution_status')}`")
            lines.append(f"- `candidates.{ck}.execution_reason`: `{c.get('execution_reason')}`")
            lines.append(f"- `candidates.{ck}.quality_status`: `{c.get('quality_status')}`")
            lines.append(f"- `candidates.{ck}.critical_reasons`: `{','.join(c.get('critical_reasons',[]))}`")
            cal = c.get("calibration")
            if cal is None:
                lines.append(f"- `candidates.{ck}.calibration`: `null`")
            else:
                # All 22 calibration leaves in sorted order for determinism
                for k in sorted(CALIBRATION_RECEIPT_KEYS):
                    lines.append(f"- `candidates.{ck}.calibration.{k}`: `{cal.get(k)}`")
    # Metrics with all 29 leaves
    mets = receipt.get("metrics", {})
    for mk in ("a","b","c"):
        m = mets.get(mk)
        if m is None:
            lines.append(f"- `metrics.{mk}`: `null`")
        else:
            for k in sorted(METRICS_OBJECT_KEYS):
                lines.append(f"- `metrics.{mk}.{k}`: `{m.get(k)}`")
    return lines

# ============================================================================
# Atomic Publication & Markdown Report
# ============================================================================

def render_markdown_report(receipt: Dict[str, Any]) -> str:
    """Render metadata-only markdown summary — single canonical projection."""
    lines = [
        f"# Memory v5 Semantic Retrieval Quality Benchmark Report",
        f"",
    ]
    lines.extend(_receipt_projection_lines(receipt))
    lines.append("")
    return "\n".join(lines)


def validate_json_markdown_consistency(receipt: Dict[str, Any], markdown: str) -> bool:
    """Fail-closed: every normative leaf projection line must appear verbatim in Markdown."""
    for line in _receipt_projection_lines(receipt):
        if line not in markdown:
            return False
    return True


def publish_generation_atomic(
    run_id: str,
    receipt: Dict[str, Any],
    report_md: str,
    docs_base_dir: Optional[Union[str, Path]] = None,
) -> Dict[str, Path]:
    """
    Publish immutable generation and atomically update pointer.
    Raises FileExistsError if generation run_id already exists.
    """
    base_dir = Path(docs_base_dir) if docs_base_dir else REPO_ROOT / "docs" / "memory_v5"
    generations_dir = base_dir / "generations" / run_id

    if generations_dir.exists():
        raise FileExistsError(f"Generation directory {generations_dir} already exists and is strictly immutable.")

    generations_dir.mkdir(parents=True, exist_ok=False)

    receipt_path = generations_dir / "receipt.json"
    report_path = generations_dir / "report.md"

    with open(receipt_path, "w", encoding="utf-8") as f:
        f.write(canonical_json_dumps(receipt))
        f.flush()
        os.fsync(f.fileno())

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report_md)
        f.flush()
        os.fsync(f.fileno())

    pointer_path = base_dir / "current-generation.json"
    temp_pointer_path = base_dir / f"current-generation.tmp.{run_id}"

    pointer_data = {
        "schema": "memory-v5-current-generation/v1",
        "current_run_id": run_id,
        "receipt_path": f"docs/memory_v5/generations/{run_id}/receipt.json",
        "report_path": f"docs/memory_v5/generations/{run_id}/report.md",
        "published_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    with open(temp_pointer_path, "w", encoding="utf-8") as f:
        json.dump(pointer_data, f, indent=2, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())

    temp_pointer_path.replace(pointer_path)

    return {
        "receipt_path": receipt_path,
        "report_path": report_path,
        "pointer_path": pointer_path,
    }


# ============================================================================
# Full Benchmark Coordinator and Runner
# ============================================================================

def run_benchmark(
    variants: Optional[List[str]] = None,
    docs_base_dir: Optional[Union[str, Path]] = None,
    model_dir: Optional[Union[str, Path]] = None,
) -> Dict[str, Any]:
    """
    Execute full Memory v5 Semantic Retrieval Quality Benchmark.
    Runs PREPARE -> CALIBRATE -> LOCKED_EVALUATION -> METRICS -> QUALITY.
    Constructs, validates, and publishes immutable generation + atomic pointer.
    """
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_id = f"run-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    base_dir = Path(docs_base_dir) if docs_base_dir else REPO_ROOT / "docs" / "memory_v5"

    # Default model directory
    resolved_model_dir = Path(model_dir) if model_dir else REPO_ROOT / "modelos_f5" / "minilm_l12_onnx"

    # Load lockfile
    lock_path = REPO_ROOT / "tools" / "memory_v5_semantic_benchmark.lock"
    with open(lock_path, "r", encoding="utf-8") as f:
        lock_data = json.load(f)

    # 1. PREPARE: Runtime Lock Verification
    lock_valid, lock_fail_reason, lock_identity = verify_runtime_lock(lock_data)
    global_invalid_reason = lock_fail_reason if not lock_valid else None

    shared_cfg = lock_data.get("shared_config", {})
    shared_config_hash = compute_shared_config_sha256(shared_cfg)
    seed = int(shared_cfg.get("seed", 42))

    # Load fixtures
    cal_path = REPO_ROOT / "tests" / "fixtures" / "memory_v5_semantic_calibration.json"
    locked_path = REPO_ROOT / "tests" / "fixtures" / "memory_v5_semantic_locked.json"

    calibration_fixture_hash = compute_file_sha256(cal_path) if cal_path.exists() else None
    locked_fixture_hash = compute_file_sha256(locked_path) if locked_path.exists() else None

    with open(cal_path, "r", encoding="utf-8") as f:
        cal_data = json.load(f)
    with open(locked_path, "r", encoding="utf-8") as f:
        locked_data = json.load(f)

    cal_cases = cal_data.get("cases", [])
    locked_cases = locked_data.get("cases", [])

    # Pre-evaluation GLOBAL_INVALID fast path (at least SHARED_CONFIG_INVALID)
    if global_invalid_reason is not None:
        manifest_tmp = lock_data.get("model", {}).get("files", [])
        _, _, model_hash_tmp = verify_model_artifacts(resolved_model_dir, manifest_tmp)
        # For GLOBAL_INVALID, model_artifact_hash is hex if computable else None (availability-based)
        _mah = model_hash_tmp
        receipt_gi = {
            "schema_version": "memory-semantic-benchmark-receipt-v1",
            "run_id": run_id,
            "state": "GLOBAL_INVALID",
            "global_invalid_reason": global_invalid_reason,
            "locked_fixture_hash": locked_fixture_hash,
            "calibration_fixture_hash": calibration_fixture_hash,
            "model_id": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
            "model_artifact_hash": _mah,
            "shared_config_hash": shared_config_hash,
            "seed": seed,
            "lock_identity": lock_identity,
            "adr_reference": "ADR-053",
            "candidates": {"b": None, "c": None},
            "metrics": {"a": None, "b": None, "c": None},
            "winner": None,
            "terminal_route": "INCONCLUSIVE",
        }
        assert BenchmarkReceiptValidator.validate(receipt_gi), "Constructed BenchmarkReceiptV1 is schema-invalid!"
        report_gi = render_markdown_report(receipt_gi)
        assert validate_json_markdown_consistency(receipt_gi, report_gi), "JSON/Markdown mismatch - no valid result"
        publish_generation_atomic(run_id, receipt_gi, report_gi, docs_base_dir=base_dir)
        diag_path_gi = base_dir / "semantic-retrieval-benchmark-diagnostics.json"
        diag_data_gi = {
            "schema": "memory-v5-semantic-benchmark-diagnostics/v1",
            "run_id": run_id,
            "evaluation_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "candidates_evaluated": ["a", "b", "c"],
            "counts": {"calibration_cases": len(cal_cases), "locked_cases": len(locked_cases)},
        }
        with open(diag_path_gi, "w", encoding="utf-8") as f:
            json.dump(diag_data_gi, f, indent=2, ensure_ascii=False)
        return receipt_gi

    # 2. Baseline A
    adapter_a = _LexicalAdapter()
    a_cal_metrics = evaluate_calibration_safety_and_objectives(
        lambda threshold=None: adapter_a,
        cal_cases,
        threshold=0.0,
        a_precision_at_1=0.0,
    )
    a_precision_at_1 = a_cal_metrics["cal_near_wrong_precision_at_1"]
    a_metrics = compute_metrics_object(adapter_a, locked_cases)

    # 3. MiniLM (Variant B) PREPARE: Model Artifact Verification
    manifest = lock_data.get("model", {}).get("files", [])
    model_valid, model_fail_reason, model_artifact_hash = verify_model_artifacts(resolved_model_dir, manifest)

    retriever_b: Optional[MiniLMRetriever] = None
    b_exec_status = "EVALUABLE"
    b_exec_reason = None
    b_calibration_receipt: Optional[Dict[str, Any]] = None
    b_metrics: Optional[Dict[str, Any]] = None
    b_qual_status = "NOT_APPLICABLE"
    b_critical_reasons: List[str] = []

    if not model_valid:
        b_exec_status = "NON_EVALUABLE"
        b_exec_reason = model_fail_reason
    else:
        try:
            retriever_b = MiniLMRetriever(model_dir=resolved_model_dir)
            retriever_b.initialize()
        except Exception:
            b_exec_status = "NON_EVALUABLE"
            b_exec_reason = "MODEL_UNAVAILABLE"

    # Calibrate B if evaluable
    if b_exec_status == "EVALUABLE" and retriever_b is not None:
        cal_b_scores = []
        for case in cal_cases:
            res = retriever_b.retrieve(case["query"], case["candidates"], profile_id=case["candidates"][0].get("profile_id"), k=3)
            if res:
                cal_b_scores.append(res[0]["score"])

        b_cand_thresholds = generate_threshold_candidates(cal_b_scores)
        b_eval_thresholds = [
            evaluate_calibration_safety_and_objectives(
                lambda threshold=t: MiniLMRetriever(
                    model_dir=resolved_model_dir,
                    threshold=threshold,
                    session=retriever_b.session,
                    tokenizer=retriever_b.tokenizer,
                    embed_cache=retriever_b._embed_cache,
                ),
                cal_cases,
                threshold=t,
                a_precision_at_1=a_precision_at_1,
            )
            for t in b_cand_thresholds
        ]
        best_b_cal = select_best_calibration_threshold(b_eval_thresholds)

        if best_b_cal is None:
            b_exec_status = "NON_EVALUABLE"
            b_exec_reason = "CALIBRATION_FAILED"
            b_qual_status = "NOT_APPLICABLE"
        else:
            b_frozen_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            b_calibration_receipt = {
                "variant_key": "b",
                "threshold": best_b_cal["threshold"],
                "calibration_fixture_hash": calibration_fixture_hash or ("0" * 64),
                "model_artifact_hash": model_artifact_hash or ("0" * 64),
                "shared_config_hash": shared_config_hash,
                "frozen_at": b_frozen_at,
                "frozen_sequence": 1,
                "cal_no_memory_abstention_count": best_b_cal["cal_no_memory_abstention_count"],
                "cal_no_memory_false_injection_count": best_b_cal["cal_no_memory_false_injection_count"],
                "cal_no_memory_abstention_accuracy": best_b_cal["cal_no_memory_abstention_accuracy"],
                "cal_profile_isolation_pass_count": best_b_cal["cal_profile_isolation_pass_count"],
                "cal_profile_privacy_leakage_count": best_b_cal["cal_profile_privacy_leakage_count"],
                "cal_profile_private_leakage_count": best_b_cal["cal_profile_private_leakage_count"],
                "cal_profile_inactive_leakage_count": best_b_cal["cal_profile_inactive_leakage_count"],
                "cal_profile_cross_profile_leakage_count": best_b_cal["cal_profile_cross_profile_leakage_count"],
                "cal_near_wrong_precision_at_1": best_b_cal["cal_near_wrong_precision_at_1"],
                "cal_near_wrong_a_precision_at_1": best_b_cal["cal_near_wrong_a_precision_at_1"],
                "cal_syn_mrr_at_3": best_b_cal["cal_syn_mrr_at_3"],
                "cal_para_mrr_at_3": best_b_cal["cal_para_mrr_at_3"],
                "cal_syn_para_mrr3_mean": best_b_cal["cal_syn_para_mrr3_mean"],
                "cal_hard_correct_at_1_count": best_b_cal["cal_hard_correct_at_1_count"],
                "cal_hard_recall_at_1": best_b_cal["cal_hard_recall_at_1"],
            }
            retriever_b.threshold = best_b_cal["threshold"]
            b_metrics = compute_metrics_object(retriever_b, locked_cases)
            b_qual_status, b_critical_reasons = evaluate_quality_gates(b_metrics, a_metrics)

    # 4. Hybrid RRF (Variant C) — C independent of B threshold, candidate-local lifecycle
    c_exec_status = "EVALUABLE"
    c_exec_reason = None
    c_calibration_receipt: Optional[Dict[str, Any]] = None
    c_metrics: Optional[Dict[str, Any]] = None
    c_qual_status = "NOT_APPLICABLE"
    c_critical_reasons: List[str] = []
    # Reuse verified session/tokenizer/cache but with threshold=None for C (C consumes unthresholded MiniLM ranking)
    retriever_b_for_c: Optional[MiniLMRetriever] = None
    if retriever_b is not None and getattr(retriever_b, "session", None) is not None and getattr(retriever_b, "tokenizer", None) is not None:
        retriever_b_for_c = MiniLMRetriever(model_dir=resolved_model_dir, threshold=None, session=retriever_b.session, tokenizer=retriever_b.tokenizer, embed_cache=retriever_b._embed_cache)
    # C suppressed only by genuinely shared PREPARE failures, not B-local CALIBRATION_FAILED etc.
    _shared_reasons = {"MODEL_UNAVAILABLE", "MODEL_HASH_MISMATCH", "DEPENDENCY_UNAVAILABLE"}
    if retriever_b is None or (b_exec_status != "EVALUABLE" and b_exec_reason in _shared_reasons):
        c_exec_status = "NON_EVALUABLE"
        c_exec_reason = b_exec_reason
    elif retriever_b_for_c is None and b_exec_status != "EVALUABLE":
        c_exec_status = "NON_EVALUABLE"
        c_exec_reason = b_exec_reason
    else:
        # Calibrate C using unthresholded semantic view
        hybrid_unfiltered = HybridRRFRetriever(adapter_a, retriever_b_for_c, threshold=None)
        cal_c_scores = []
        for case in cal_cases:
            res = hybrid_unfiltered.retrieve(case["query"], case["candidates"], profile_id=case["candidates"][0].get("profile_id"), k=3)
            if res:
                cal_c_scores.append(res[0]["score"])

        c_cand_thresholds = generate_threshold_candidates(cal_c_scores)
        c_eval_thresholds = [
            evaluate_calibration_safety_and_objectives(
                lambda threshold=t: HybridRRFRetriever(adapter_a, retriever_b_for_c, threshold=threshold),
                cal_cases,
                threshold=t,
                a_precision_at_1=a_precision_at_1,
            )
            for t in c_cand_thresholds
        ]
        best_c_cal = select_best_calibration_threshold(c_eval_thresholds)

        if best_c_cal is None:
            c_exec_status = "NON_EVALUABLE"
            c_exec_reason = "CALIBRATION_FAILED"
            c_qual_status = "NOT_APPLICABLE"
        else:
            c_frozen_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            c_calibration_receipt = {
                "variant_key": "c",
                "threshold": best_c_cal["threshold"],
                "calibration_fixture_hash": calibration_fixture_hash or ("0" * 64),
                "model_artifact_hash": model_artifact_hash or ("0" * 64),
                "shared_config_hash": shared_config_hash,
                "frozen_at": c_frozen_at,
                "frozen_sequence": 1,
                "cal_no_memory_abstention_count": best_c_cal["cal_no_memory_abstention_count"],
                "cal_no_memory_false_injection_count": best_c_cal["cal_no_memory_false_injection_count"],
                "cal_no_memory_abstention_accuracy": best_c_cal["cal_no_memory_abstention_accuracy"],
                "cal_profile_isolation_pass_count": best_c_cal["cal_profile_isolation_pass_count"],
                "cal_profile_privacy_leakage_count": best_c_cal["cal_profile_privacy_leakage_count"],
                "cal_profile_private_leakage_count": best_c_cal["cal_profile_private_leakage_count"],
                "cal_profile_inactive_leakage_count": best_c_cal["cal_profile_inactive_leakage_count"],
                "cal_profile_cross_profile_leakage_count": best_c_cal["cal_profile_cross_profile_leakage_count"],
                "cal_near_wrong_precision_at_1": best_c_cal["cal_near_wrong_precision_at_1"],
                "cal_near_wrong_a_precision_at_1": best_c_cal["cal_near_wrong_a_precision_at_1"],
                "cal_syn_mrr_at_3": best_c_cal["cal_syn_mrr_at_3"],
                "cal_para_mrr_at_3": best_c_cal["cal_para_mrr_at_3"],
                "cal_syn_para_mrr3_mean": best_c_cal["cal_syn_para_mrr3_mean"],
                "cal_hard_correct_at_1_count": best_c_cal["cal_hard_correct_at_1_count"],
                "cal_hard_recall_at_1": best_c_cal["cal_hard_recall_at_1"],
            }
            retriever_c = HybridRRFRetriever(adapter_a, retriever_b_for_c, threshold=best_c_cal["threshold"])
            c_metrics = compute_metrics_object(retriever_c, locked_cases)
            c_qual_status, c_critical_reasons = evaluate_quality_gates(c_metrics, a_metrics)

    # 5. Terminal Routing
    route, winner = evaluate_terminal_routing(
        global_invalid_reason=global_invalid_reason,
        candidate_b_status=(b_exec_status, b_qual_status, b_critical_reasons),
        candidate_c_status=(c_exec_status, c_qual_status, c_critical_reasons),
        a_metrics=a_metrics,
        b_metrics=b_metrics,
        c_metrics=c_metrics,
    )

    completed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # 6. Build Receipt - exact BenchmarkReceiptV1 closed schema
    receipt = {
        "schema_version": "memory-semantic-benchmark-receipt-v1",
        "run_id": run_id,
        "state": "GLOBAL_INVALID" if global_invalid_reason else "VALID",
        "global_invalid_reason": global_invalid_reason,
        "locked_fixture_hash": locked_fixture_hash,
        "calibration_fixture_hash": calibration_fixture_hash,
        "model_id": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "model_artifact_hash": model_artifact_hash,
        "shared_config_hash": shared_config_hash,
        "seed": seed,
        "lock_identity": lock_identity,
        "adr_reference": "ADR-053",
        "candidates": {
            "b": {
                "variant_key": "b",
                "execution_status": b_exec_status,
                "execution_reason": b_exec_reason,
                "quality_status": b_qual_status,
                "critical_reasons": b_critical_reasons,
                "calibration": b_calibration_receipt,
            },
            "c": {
                "variant_key": "c",
                "execution_status": c_exec_status,
                "execution_reason": c_exec_reason,
                "quality_status": c_qual_status,
                "critical_reasons": c_critical_reasons,
                "calibration": c_calibration_receipt,
            },
        },
        "metrics": {
            "a": a_metrics,
            "b": b_metrics,
            "c": c_metrics,
        },
        "winner": winner,
        "terminal_route": route,
    }

    assert BenchmarkReceiptValidator.validate(receipt), "Constructed BenchmarkReceiptV1 is schema-invalid!"

    report_md = render_markdown_report(receipt)
    assert validate_json_markdown_consistency(receipt, report_md), "JSON/Markdown mismatch - no valid result"
    publish_generation_atomic(run_id, receipt, report_md, docs_base_dir=base_dir)

    diag_path = base_dir / "semantic-retrieval-benchmark-diagnostics.json"
    diag_data = {
        "schema": "memory-v5-semantic-benchmark-diagnostics/v1",
        "run_id": run_id,
        "evaluation_timestamp": completed_at,
        "candidates_evaluated": ["a", "b", "c"],
        "counts": {
            "calibration_cases": len(cal_cases),
            "locked_cases": len(locked_cases),
        },
    }
    with open(diag_path, "w", encoding="utf-8") as f:
        json.dump(diag_data, f, indent=2, ensure_ascii=False)

    return receipt


# Helper for MiniLMRetriever lambda in evaluation
def _init_retriever_helper(model_dir: Union[str, Path], threshold: Any) -> MiniLMRetriever:
    r = MiniLMRetriever(model_dir=model_dir, threshold=threshold)
    r.initialize()
    return r

MiniLMRetriever._init_for_eval = lambda self: (self.initialize(), self)[1]


if __name__ == "__main__":
    print("Running Memory v5 Semantic Retrieval Quality Benchmark (Real ONNX CPU)...")
    res = run_benchmark()
    print(f"Benchmark completed! Run ID: {res['run_id']}, Route: {res['terminal_route']}, Winner: {res['winner']}")
