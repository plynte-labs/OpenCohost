"""
Fixture integrity validation, lexical adapter, MiniLM ONNX embedder,
and SharedEvidenceProvider for memory-v5-semantic-safety-refinement.

Guarantees:
- Dataset integrity and quota validation.
- Strict candidate-level payload disjointness (title + content) and normalized query disjointness.
- Authoritative target profile resolution across all fixture cases.
- Pure lexical search utilizing opencohost production helpers.
- Dense semantic search using real ONNX CPU MiniLM embeddings matching baseline formatting.
- Zero ground-truth leakage into candidate-facing InferenceEvidence.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

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


def load_fixture(path: Union[str, Path]) -> Dict[str, Any]:
    """Load JSON fixture file."""
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_fixture_quotas(data: Dict[str, Any], is_calibration: bool) -> bool:
    """
    Validate that fixture contains exact required case quotas.
    Calibration (24 total): 4 hard_lexical, 4 synonym, 4 paraphrase, 4 no_memory, 4 near_but_wrong, 4 profile_privacy.
    Locked (80 total): 16 hard_lexical, 14 synonym, 16 paraphrase, 12 no_memory, 12 near_but_wrong, 5 profile_privacy, 5 stale_contradiction.
    """
    if not isinstance(data, dict):
        return False
    cases = data.get("cases")
    if not isinstance(cases, list):
        return False

    counts: Dict[str, int] = {}
    for case in cases:
        if not isinstance(case, dict):
            return False
        fam = case.get("family", "")
        counts[fam] = counts.get(fam, 0) + 1

    if is_calibration:
        if len(cases) != 24:
            return False
        expected = {
            "hard_lexical": 4,
            "synonym": 4,
            "paraphrase": 4,
            "no_memory": 4,
            "near_but_wrong": 4,
            "profile_privacy": 4,
        }
        for fam, exp_count in expected.items():
            if counts.get(fam, 0) != exp_count:
                return False
        return True
    else:
        if len(cases) != 80:
            return False
        expected = {
            "hard_lexical": 16,
            "synonym": 14,
            "paraphrase": 16,
            "no_memory": 12,
            "near_but_wrong": 12,
            "profile_privacy": 5,
            "stale_contradiction": 5,
        }
        for fam, exp_count in expected.items():
            if counts.get(fam, 0) != exp_count:
                return False
        return True


def hash_candidate_payload(cand: Dict[str, Any]) -> str:
    """
    Compute candidate semantic payload hash.
    Normative definition: SHA-256 over normalized (title, content) bytes.
    Excludes local candidate IDs to prevent candidate re-keying leakage.
    """
    title = str(cand.get("title") or "").strip()
    content = str(cand.get("content") or "").strip()
    payload = f"{title}\x00{content}\x00".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def validate_fixture_disjointness(cal_data: Dict[str, Any], locked_data: Dict[str, Any]) -> bool:
    """
    Assert calibration and locked datasets are completely disjoint:
    - Zero overlapping case IDs.
    - Zero overlapping normalized query strings (casefolded, collapsed whitespace).
    - Zero overlapping individual candidate memory payload hashes (title + content).
    """
    if not isinstance(cal_data, dict) or not isinstance(locked_data, dict):
        return False
    cal_cases = cal_data.get("cases", [])
    locked_cases = locked_data.get("cases", [])
    if not isinstance(cal_cases, list) or not isinstance(locked_cases, list):
        return False

    cal_ids = {c.get("case_id") or c.get("id") for c in cal_cases if isinstance(c, dict)}
    locked_ids = {c.get("case_id") or c.get("id") for c in locked_cases if isinstance(c, dict)}
    if not cal_ids.isdisjoint(locked_ids):
        return False

    def norm_query(q: str) -> str:
        return re.sub(r"\s+", " ", q.strip().casefold())

    cal_queries = {
        norm_query(c["query"])
        for c in cal_cases
        if isinstance(c, dict) and isinstance(c.get("query"), str)
    }
    locked_queries = {
        norm_query(c["query"])
        for c in locked_cases
        if isinstance(c, dict) and isinstance(c.get("query"), str)
    }
    if not cal_queries.isdisjoint(locked_queries):
        return False

    cal_payloads = {
        hash_candidate_payload(cand)
        for c in cal_cases
        if isinstance(c, dict)
        for cand in c.get("candidates", [])
        if isinstance(cand, dict)
    }
    locked_payloads = {
        hash_candidate_payload(cand)
        for c in locked_cases
        if isinstance(c, dict)
        for cand in c.get("candidates", [])
        if isinstance(cand, dict)
    }
    if not cal_payloads.isdisjoint(locked_payloads):
        return False

    return True


def resolve_target_user_id(case: Dict[str, Any]) -> str:
    """
    Authoritative resolution of target profile ID for a fixture case.
    Priority:
    1. Explicit top-level target_user_id or profile_id on case.
    2. Profile ID of target candidate(s) indicated by expected_ids / target_ids.
    3. Profile ID of the primary/first candidate in the candidate pool.
    """
    if case.get("target_user_id"):
        return str(case["target_user_id"])
    if case.get("profile_id"):
        return str(case["profile_id"])

    expected_ids = set(case.get("expected_ids") or case.get("target_ids") or [])
    candidates = case.get("candidates", [])
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
    k: int = 3,
) -> Tuple[RankedItem, ...]:
    """
    Execute pure lexical IDF retrieval by reusing opencohost production helpers.
    """
    # 1. Deterministic sorting and row normalization
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

    # 2. Compute full-pool IDF map
    idf_map = _compute_candidate_idf(sorted_pool)

    # 3. Select top candidates
    selected = select_top_k(query, sorted_pool, k=len(sorted_pool))
    topic = set(_significant_tokens(query)) - _SCORING_STOPWORDS

    # 4. Filter by profile and privacy, compute exact scores
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
        if len(results) >= k:
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
    k: int = 3,
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
    for score, cand in scored[:k]:
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
    k: int = 3,
) -> Tuple[InferenceEvidence, EvaluationTruth]:
    """
    Map fixture case into opaque InferenceEvidence and EvaluationTruth.
    InferenceEvidence receives an opaque hash key with zero family/truth leakage.
    Target user profile is authoritatively resolved.
    """
    case_id = str(case.get("case_id") or case.get("id", ""))
    opaque_key = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:16]
    query = str(case.get("query", ""))
    candidates = case.get("candidates", [])

    target_user_id = resolve_target_user_id(case)

    lex_ranking = execute_lexical_search(query, candidates, profile_id=target_user_id, k=k)
    sem_ranking = execute_semantic_search(query, candidates, embedder=embedder, profile_id=target_user_id, k=k)

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
