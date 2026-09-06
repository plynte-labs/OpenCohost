"""
Shared Evidence Provider, Fixture Parsers, and Deterministic MiniLM ONNX Embedder.

Module Responsibilities:
1. Fixture loading and exact quota validation (Cal=24, Locked=80).
2. Strict disjointness verification between Calibration and Locked evaluation sets.
3. Candidate payload hash computation (normalized bytes without local candidate IDs).
4. Authoritative profile ownership derivation for multi-profile evaluation.
5. Deterministic ONNX CPU MiniLM embedding engine with thread-safe caching.
6. Execution of pure lexical and dense semantic candidate retrieval.
7. SharedEvidenceProvider producing immutable InferenceEvidence and EvaluationTruth pairs.
"""

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, Union

import numpy as np
import onnxruntime as ort
try:
    from tokenizers import Tokenizer
except ImportError:
    Tokenizer = None  # type: ignore

from opencohost.core.memory.memoria_store import (
    _MIN_SHARED_TOKENS,
    _SCORING_STOPWORDS,
    _compute_candidate_idf,
    _significant_tokens,
    select_top_k,
)
from tools.memory_v5_semantic_safety.models import (
    EvaluationTruth,
    InferenceEvidence,
    RankedItem,
)


def load_fixture(fixture_path: Path) -> Dict[str, Any]:
    """Load JSON fixture file with strict UTF-8 decoding."""
    with open(fixture_path, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_fixture_quotas(data: Dict[str, Any], is_calibration: bool, raise_error: bool = False) -> bool:
    """
    Validate that fixture contains exact spec quotas:
    - Calibration: exactly 24 cases (4 each of 6 families).
    - Locked: exactly 80 cases (16 hard, 14 syn, 16 para, 12 no_memory, 12 near_wrong, 5 privacy, 5 stale).
    """
    cases = data.get("cases", [])
    family_counts: Dict[str, int] = {}
    for case in cases:
        fam = case.get("family", "unknown")
        family_counts[fam] = family_counts.get(fam, 0) + 1

    if is_calibration:
        if len(cases) != 24:
            if raise_error:
                raise ValueError(f"Calibration quota failure: expected 24 cases, got {len(cases)}")
            return False
        expected_cal = {
            "hard_lexical": 4,
            "synonym": 4,
            "paraphrase": 4,
            "no_memory": 4,
            "near_but_wrong": 4,
            "profile_privacy": 4,
        }
        for fam, count in expected_cal.items():
            if family_counts.get(fam, 0) != count:
                if raise_error:
                    raise ValueError(f"Calibration family quota mismatch for {fam}: expected {count}, got {family_counts.get(fam, 0)}")
                return False
        return True
    else:
        if len(cases) != 80:
            if raise_error:
                raise ValueError(f"Locked quota failure: expected 80 cases, got {len(cases)}")
            return False
        expected_locked = {
            "hard_lexical": 16,
            "synonym": 14,
            "paraphrase": 16,
            "no_memory": 12,
            "near_but_wrong": 12,
            "profile_privacy": 5,
            "stale_contradiction": 5,
        }
        for fam, count in expected_locked.items():
            if family_counts.get(fam, 0) != count:
                if raise_error:
                    raise ValueError(f"Locked family quota mismatch for {fam}: expected {count}, got {family_counts.get(fam, 0)}")
                return False
        return True


def hash_candidate_payload(cand: Dict[str, Any]) -> str:
    """
    Compute authoritative SHA-256 hash of normalized candidate memory payload.
    Excludes case-local candidate ID to detect identical memory payloads across fixtures.
    """
    title = str(cand.get("title") or "").strip()
    content = str(cand.get("content") or "").strip()
    payload = f"{title}\x00{content}\x00".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def normalize_query_for_disjointness(query: str) -> str:
    """Normalize query text (casefold and whitespace collapse) for disjointness checking."""
    return " ".join(query.casefold().split())


def validate_fixture_disjointness(cal_data: Dict[str, Any], locked_data: Dict[str, Any], raise_error: bool = False) -> bool:
    """
    Validate strict disjointness between Calibration and Locked evaluation sets:
    1. Case IDs are disjoint.
    2. Normalized query texts are disjoint.
    3. Memory candidate payload hashes are disjoint.
    """
    cal_cases = cal_data.get("cases", [])
    locked_cases = locked_data.get("cases", [])

    cal_ids = {str(c.get("case_id") or c.get("id")) for c in cal_cases}
    locked_ids = {str(c.get("case_id") or c.get("id")) for c in locked_cases}
    id_overlap = cal_ids & locked_ids
    if id_overlap:
        if raise_error:
            raise ValueError(f"Disjointness violation: overlapping case IDs: {id_overlap}")
        return False

    cal_queries = {normalize_query_for_disjointness(str(c.get("query", ""))) for c in cal_cases}
    locked_queries = {normalize_query_for_disjointness(str(c.get("query", ""))) for c in locked_cases}
    query_overlap = cal_queries & locked_queries
    if query_overlap:
        if raise_error:
            raise ValueError(f"Disjointness violation: overlapping normalized queries: {query_overlap}")
        return False

    cal_payloads: Set[str] = set()
    for c in cal_cases:
        for cand in c.get("candidates", []):
            cal_payloads.add(hash_candidate_payload(cand))

    locked_payloads: Set[str] = set()
    for c in locked_cases:
        for cand in c.get("candidates", []):
            locked_payloads.add(hash_candidate_payload(cand))

    payload_overlap = cal_payloads & locked_payloads
    if payload_overlap:
        if raise_error:
            raise ValueError(f"Disjointness violation: overlapping candidate payloads ({len(payload_overlap)} hashes)")
        return False

    return True


def resolve_target_user_id(case: Dict[str, Any]) -> str:
    """
    Authoritatively resolve target user profile for a case without fallback ambiguity:
    1. Check top-level 'target_user_id' or 'profile_id' in case.
    2. If missing, look up profile_id of target/expected memory candidates.
    3. If still missing, check candidates[0] profile_id.
    4. Default safely to 'user_1'.
    """
    if case.get("target_user_id"):
        return str(case["target_user_id"])
    if case.get("profile_id"):
        return str(case["profile_id"])

    candidates = case.get("candidates", [])
    expected_ids = set(case.get("target_ids") or case.get("expected_ids") or [])
    if expected_ids:
        for cand in candidates:
            if cand.get("id") in expected_ids and cand.get("profile_id"):
                return str(cand["profile_id"])

    if candidates and candidates[0].get("profile_id"):
        return str(candidates[0]["profile_id"])

    return "user_1"


def execute_lexical_search(
    query: str,
    pool: List[Dict[str, Any]],
    profile_id: Optional[str] = None,
    k: Optional[int] = 3,
) -> Tuple[RankedItem, ...]:
    """
    Execute pure lexical IDF retrieval by reusing opencohost production helpers.
    """
    sorted_pool: List[Dict[str, Any]] = []
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

    idf_map = _compute_candidate_idf(sorted_pool)
    selected = select_top_k(query, sorted_pool, k=len(sorted_pool))
    topic = set(_significant_tokens(query)) - _SCORING_STOPWORDS

    results: List[RankedItem] = []
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

        results.append(
            RankedItem(
                memory_id=str(row.get("id", "")),
                score=float(score),
                rank=rank,
                text=f"{row.get('title', '')} {row.get('content', '')}".strip(),
                user_id=str(row.get("profile_id", "")),
                is_private=bool(row.get("private", 0)),
                is_inactive=bool(row.get("inactive", 0)),
            )
        )
        rank += 1
        if k is not None and len(results) >= k:
            break

    return tuple(results)


class MiniLMEmbedder:
    """
    MiniLM Dense Retriever ONNX Engine (paraphrase-multilingual-MiniLM-L12-v2, 384d).
    Executes CPU inference with masked-mean pooling and L2 normalization.
    """

    def __init__(
        self,
        model_dir: Union[str, Path],
        embed_cache: Optional[Dict[str, List[float]]] = None,
    ):
        self.model_dir = Path(model_dir)
        self.tokenizer: Optional[Tokenizer] = None
        self.session: Optional[ort.InferenceSession] = None
        self._embed_cache = embed_cache if embed_cache is not None else {}
        self.initialized = False

    def initialize(self) -> None:
        """Initialize tokenizer and ONNX runtime session."""
        if self.initialized:
            return

        tokenizer_path = self.model_dir / "tokenizer.json"
        model_path = self.model_dir / "model.onnx"

        if not tokenizer_path.exists() or not model_path.exists():
            raise FileNotFoundError(f"Model artifacts missing in {self.model_dir}")

        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.enable_truncation(max_length=256)
        self.tokenizer.enable_padding(length=256)

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3  # Error only
        self.session = ort.InferenceSession(
            str(model_path),
            sess_options=opts,
            providers=["CPUExecutionProvider"],
        )
        self.initialized = True

    def clear_cache(self) -> None:
        """Explicitly clear the embedding cache."""
        self._embed_cache.clear()

    def encode_text(self, text: str) -> List[float]:
        """Encode text to 384-d normalized vector using ONNX model."""
        if not self.initialized or self.session is None or self.tokenizer is None:
            self.initialize()

        cache_key = hashlib.sha256(text.encode("utf-8")).hexdigest()
        if cache_key in self._embed_cache:
            return self._embed_cache[cache_key]

        encoded = self.tokenizer.encode(text)  # type: ignore
        input_ids = np.array([encoded.ids], dtype=np.int64)
        attention_mask = np.array([encoded.attention_mask], dtype=np.int64)

        session_input_names = {inp.name for inp in self.session.get_inputs()}  # type: ignore
        inputs = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
        }
        if "token_type_ids" in session_input_names:
            inputs["token_type_ids"] = np.array([encoded.type_ids], dtype=np.int64)

        outputs = self.session.run(None, inputs)  # type: ignore
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
        self._embed_cache[cache_key] = vec
        return vec


def execute_semantic_search(
    query: str,
    pool: List[Dict[str, Any]],
    embedder: MiniLMEmbedder,
    profile_id: Optional[str] = None,
    k: Optional[int] = 3,
) -> Tuple[RankedItem, ...]:
    """
    Execute semantic dense retrieval using MiniLMEmbedder.
    Candidate text is formatted with newline separator matching baseline.
    """
    sorted_pool = sorted(pool, key=lambda c: str(c.get("id", "")))
    query_vec = np.array(embedder.encode_text(query), dtype=np.float32)

    scored: List[Tuple[float, Dict[str, Any]]] = []
    for cand in sorted_pool:
        cand_prof = cand.get("profile_id")
        if profile_id is not None and cand_prof is not None and cand_prof != profile_id:
            continue
        if cand.get("private", 0) == 1 or cand.get("inactive", 0) == 1:
            continue

        cand_text = f"{cand.get('title', '')}\n{cand.get('content', '')}"
        cand_vec = np.array(embedder.encode_text(cand_text), dtype=np.float32)
        sim = float(np.dot(query_vec, cand_vec))
        scored.append((sim, cand))

    # Sort descending by score, tie-break by ID ascending
    scored.sort(key=lambda item: (-item[0], str(item[1].get("id", ""))))

    results: List[RankedItem] = []
    rank = 1
    slice_end = len(scored) if k is None else k
    for score, cand in scored[:slice_end]:
        results.append(
            RankedItem(
                memory_id=str(cand.get("id", "")),
                score=score,
                rank=rank,
                text=f"{cand.get('title', '')} {cand.get('content', '')}".strip(),
                user_id=str(cand.get("profile_id", "")),
                is_private=bool(cand.get("private", 0)),
                is_inactive=bool(cand.get("inactive", 0)),
            )
        )
        rank += 1

    return tuple(results)


def generate_case_evidence(
    case: Dict[str, Any],
    embedder: MiniLMEmbedder,
    k_semantic: int = 3,
) -> Tuple[InferenceEvidence, EvaluationTruth]:
    """
    Map fixture case into opaque InferenceEvidence and EvaluationTruth.
    lexical_ranking provides full case pool ranking/scores so mechanisms like H2
    can inspect the semantic candidate's actual IDF score even if ranked > 3.
    """
    case_id = str(case.get("case_id") or case.get("id", ""))
    opaque_key = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:16]
    query = str(case.get("query", ""))
    candidates = case.get("candidates", [])

    target_user_id = resolve_target_user_id(case)

    # Full case pool lexical ranking for complete signal visibility without ground truth leakage
    lex_ranking = execute_lexical_search(query, candidates, profile_id=target_user_id, k=None)
    sem_ranking = execute_semantic_search(query, candidates, embedder=embedder, profile_id=target_user_id, k=k_semantic)

    inf_evidence = InferenceEvidence(
        evidence_key=opaque_key,
        query=query,
        lexical_ranking=lex_ranking,
        semantic_ranking=sem_ranking,
    )

    targets = case.get("target_ids") or case.get("expected_ids") or []
    eval_truth = EvaluationTruth(
        case_id=case_id,
        family=str(case.get("family", "")),
        target_ids=tuple(str(tid) for tid in targets),
        expected_abstain=bool(case.get("expected_abstain", False)),
        target_user_id=target_user_id,
        diagnostic_subtype=case.get("diagnostic_subtype"),
    )

    return inf_evidence, eval_truth
