from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, List, Optional, Protocol, Sequence, Tuple

import numpy as np

logger = logging.getLogger("OpenCohost")


@dataclass(frozen=True)
class ExchangeExtractionDiagnostics:
    complete_exchanges: int
    orphan_user_events: int
    orphan_assistant_events: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ConversationalExchange:
    """
    Internal/local data structure representing an atomic turn exchange.
    Contains raw user and assistant text in memory.
    Must NEVER be serialized to persistent journals or public diagnostics.
    """
    exchange_ordinal: int
    session_id: str
    user_event_id: str
    assistant_event_id: str
    user_text: str
    assistant_text: str
    combined_text: str
    occurred_at: str

    def metadata_dict(self) -> dict[str, Any]:
        """Safe metadata-only representation without payload content."""
        return {
            "exchange_ordinal": self.exchange_ordinal,
            "session_id": self.session_id,
            "user_event_id": self.user_event_id,
            "assistant_event_id": self.assistant_event_id,
            "occurred_at": self.occurred_at,
        }


def extract_exchanges_from_events(
    events: Sequence[dict[str, Any]], session_id: str
) -> Tuple[list[ConversationalExchange], ExchangeExtractionDiagnostics]:
    evidence_events = [e for e in events if e.get("stream_type") == "evidence" or "role" in e]
    evidence_events = sorted(evidence_events, key=lambda x: int(x.get("stream_sequence", 0)))

    exchanges: list[ConversationalExchange] = []
    orphan_users = 0
    orphan_assts = 0

    i = 0
    ordinal = 1
    while i < len(evidence_events):
        curr = evidence_events[i]
        role = curr.get("role")
        if role == "user":
            if (i + 1) < len(evidence_events) and evidence_events[i + 1].get("role") == "assistant":
                asst = evidence_events[i + 1]
                u_text = curr.get("content", "")
                a_text = asst.get("content", "")
                combo = f"{u_text}\n{a_text}"
                ex = ConversationalExchange(
                    exchange_ordinal=ordinal,
                    session_id=session_id,
                    user_event_id=curr.get("event_id", ""),
                    assistant_event_id=asst.get("event_id", ""),
                    user_text=u_text,
                    assistant_text=a_text,
                    combined_text=combo,
                    occurred_at=curr.get("occurred_at", ""),
                )
                exchanges.append(ex)
                ordinal += 1
                i += 2
            else:
                orphan_users += 1
                i += 1
        elif role == "assistant":
            orphan_assts += 1
            i += 1
        else:
            i += 1

    diagnostics = ExchangeExtractionDiagnostics(
        complete_exchanges=len(exchanges),
        orphan_user_events=orphan_users,
        orphan_assistant_events=orphan_assts,
    )
    return exchanges, diagnostics


class EmbeddingBackend(Protocol):
    def embed(self, text: str) -> list[float]:
        ...

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        ...


class MiniLMEmbeddingBackend:
    def __init__(self, model_dir: Path | str | None = None) -> None:
        if model_dir is not None:
            self.model_dir = Path(model_dir)
        else:
            from opencohost.config.settings import BASE_DIR
            default_dir = Path(BASE_DIR) / "modelos_f5" / "minilm_l12_onnx"
            if not (default_dir / "model.onnx").is_file():
                import os
                res_dir = os.environ.get("OPENCOHOST_RESOURCES_DIR", "").strip()
                if res_dir:
                    candidate = Path(res_dir) / "modelos_f5" / "minilm_l12_onnx"
                    if (candidate / "model.onnx").is_file():
                        default_dir = candidate
                    else:
                        candidate2 = Path(res_dir) / "minilm_l12_onnx"
                        if (candidate2 / "model.onnx").is_file():
                            default_dir = candidate2
            self.model_dir = default_dir
        self.tokenizer = None
        self.session = None
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        import onnxruntime as ort
        from tokenizers import Tokenizer

        tokenizer_path = self.model_dir / "tokenizer.json"
        model_path = self.model_dir / "model.onnx"
        if not tokenizer_path.exists() or not model_path.exists():
            raise FileNotFoundError(f"MiniLM ONNX artifacts missing in {self.model_dir}")

        self.tokenizer = Tokenizer.from_file(str(tokenizer_path))
        self.tokenizer.enable_truncation(max_length=256)
        self.tokenizer.enable_padding(length=256)

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3  # Error only
        self.session = ort.InferenceSession(
            str(model_path), sess_options=opts, providers=["CPUExecutionProvider"]
        )
        self._initialized = True

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        if not self._initialized:
            self.initialize()

        encoded_batch = self.tokenizer.encode_batch(list(texts))
        input_ids = np.array([e.ids for e in encoded_batch], dtype=np.int64)
        attention_mask = np.array([e.attention_mask for e in encoded_batch], dtype=np.int64)

        inputs = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
        }
        session_inputs = {inp.name for inp in self.session.get_inputs()}
        if "token_type_ids" in session_inputs:
            inputs["token_type_ids"] = np.array([e.type_ids for e in encoded_batch], dtype=np.int64)

        # Single batched ONNX execution: [N, seq_len, 384]
        outputs = self.session.run(None, inputs)
        token_embeddings = outputs[0]

        # Vectorized masked mean pooling: [N, 384]
        mask_f = attention_mask[:, :, np.newaxis].astype(np.float32)
        sum_embeddings = np.sum(token_embeddings * mask_f, axis=1)
        sum_mask = np.clip(np.sum(mask_f, axis=1), a_min=1e-9, a_max=None)
        mean_pooled = sum_embeddings / sum_mask

        # Vectorized L2 normalization
        norm = np.clip(np.linalg.norm(mean_pooled, axis=1, keepdims=True), a_min=1e-9, a_max=None)
        normalized = mean_pooled / norm
        return normalized.tolist()


@dataclass(frozen=True)
class ExchangeSemanticSignal:
    exchange_ordinal: int
    occurred_at: str
    similarity_to_previous: Optional[float]
    similarity_to_recent_context: Optional[float]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class SemanticSignalProvider:
    def __init__(self, backend: Optional[EmbeddingBackend] = None) -> None:
        self.backend = backend

    def analyze_session(
        self, exchanges: Sequence[ConversationalExchange], context_window_size: int = 3
    ) -> list[ExchangeSemanticSignal]:
        if not exchanges or self.backend is None:
            return []

        texts = [ex.combined_text for ex in exchanges]
        vectors = [np.array(v, dtype=np.float32) for v in self.backend.embed_batch(texts)]

        signals: list[ExchangeSemanticSignal] = []
        for idx in range(len(exchanges)):
            curr_vec = vectors[idx]
            occ = exchanges[idx].occurred_at

            if idx == 0:
                signals.append(
                    ExchangeSemanticSignal(
                        exchange_ordinal=exchanges[idx].exchange_ordinal,
                        occurred_at=occ,
                        similarity_to_previous=None,
                        similarity_to_recent_context=None,
                    )
                )
                continue

            prev_vec = vectors[idx - 1]
            sim_prev = float(np.dot(prev_vec, curr_vec))

            # Context centroid from preceding window
            win_start = max(0, idx - context_window_size)
            preceding_win = vectors[win_start:idx]
            centroid = np.mean(preceding_win, axis=0)
            c_norm = np.linalg.norm(centroid)
            if c_norm > 1e-9:
                centroid = centroid / c_norm
            sim_ctx = float(np.dot(centroid, curr_vec))

            signals.append(
                ExchangeSemanticSignal(
                    exchange_ordinal=exchanges[idx].exchange_ordinal,
                    occurred_at=occ,
                    similarity_to_previous=round(sim_prev, 4),
                    similarity_to_recent_context=round(sim_ctx, 4),
                )
            )

        return signals


@dataclass(frozen=True)
class EpisodeCohesionDiagnostics:
    """Descriptive metrics on within-episode exchange semantic cohesion."""
    mean_exchange_to_centroid: float
    min_exchange_to_centroid: float
    exchange_count: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EpisodeSemanticRepresentation:
    """
    Aggregate semantic representation of an Episode.
    Defined as the L2-normalized mean of L2-normalized ConversationalExchange vectors.
    """
    episode_id: str
    session_id: str
    profile_id: str
    vector: list[float]
    cohesion: EpisodeCohesionDiagnostics
    exchange_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "session_id": self.session_id,
            "profile_id": self.profile_id,
            "cohesion": self.cohesion.to_dict(),
            "exchange_count": self.exchange_count,
            "vector_dim": len(self.vector),
        }


def build_episode_semantic_representation(
    episode_id: str,
    session_id: str,
    profile_id: str,
    exchange_vectors: Sequence[Sequence[float]],
) -> Optional[EpisodeSemanticRepresentation]:
    if not exchange_vectors:
        return None

    # Vectorized centroid
    vecs = np.array(exchange_vectors, dtype=np.float32)
    centroid = np.mean(vecs, axis=0)
    c_norm = float(np.linalg.norm(centroid))
    if c_norm > 1e-9:
        centroid = centroid / c_norm
    else:
        return None

    # Within-episode cohesion: dot product of each exchange vector with the normalized centroid
    cosines = np.dot(vecs, centroid)
    mean_cohesion = float(np.mean(cosines))
    min_cohesion = float(np.min(cosines))

    cohesion = EpisodeCohesionDiagnostics(
        mean_exchange_to_centroid=round(mean_cohesion, 4),
        min_exchange_to_centroid=round(min_cohesion, 4),
        exchange_count=len(exchange_vectors),
    )

    return EpisodeSemanticRepresentation(
        episode_id=episode_id,
        session_id=session_id,
        profile_id=profile_id,
        vector=centroid.tolist(),
        cohesion=cohesion,
        exchange_count=len(exchange_vectors),
    )


def batch_embed_unique_exchanges(
    exchanges: Sequence[ConversationalExchange], backend: EmbeddingBackend
) -> dict[str, list[float]]:
    """
    Embed all unique exchanges once per invocation and return a mapping from
    exchange_key (user_event_id:assistant_event_id) to the embedded vector.
    """
    if not exchanges:
        return {}

    unique_texts: list[str] = []
    text_to_key: dict[str, list[str]] = {}

    for ex in exchanges:
        key = f"{ex.user_event_id}:{ex.assistant_event_id}"
        t = ex.combined_text
        if t not in text_to_key:
            text_to_key[t] = []
            unique_texts.append(t)
        text_to_key[t].append(key)

    vectors = backend.embed_batch(unique_texts)

    key_to_vector: dict[str, list[float]] = {}
    for text, vec in zip(unique_texts, vectors):
        for k in text_to_key[text]:
            key_to_vector[k] = vec

    return key_to_vector


@dataclass(frozen=True)
class EpisodeSimilarityNeighbor:
    neighbor_episode_id: str
    neighbor_session_id: str
    cosine_similarity: float
    time_delta_seconds: float
    is_same_session: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class EpisodeSimilarityReport:
    episode_id: str
    session_id: str
    profile_id: str
    cohesion: EpisodeCohesionDiagnostics
    nearest_overall: list[EpisodeSimilarityNeighbor]
    nearest_cross_session: list[EpisodeSimilarityNeighbor]

    def to_dict(self) -> dict[str, Any]:
        return {
            "episode_id": self.episode_id,
            "session_id": self.session_id,
            "profile_id": self.profile_id,
            "cohesion": self.cohesion.to_dict(),
            "nearest_overall": [n.to_dict() for n in self.nearest_overall],
            "nearest_cross_session": [n.to_dict() for n in self.nearest_cross_session],
        }


def _parse_iso_ts(ts_str: str) -> float:
    try:
        from datetime import datetime
        clean = ts_str.replace("Z", "+00:00")
        return datetime.fromisoformat(clean).timestamp()
    except Exception:
        return 0.0


def compute_episode_similarities(
    episodes: Sequence[EpisodeSemanticRepresentation],
    episode_timestamps: Optional[dict[str, str]] = None,
) -> list[EpisodeSimilarityReport]:
    if not episodes:
        return []

    ts_map = episode_timestamps or {}
    vectors = np.array([ep.vector for ep in episodes], dtype=np.float32)
    # Pairwise cosine similarity matrix: [M, M]
    sim_matrix = np.dot(vectors, vectors.T)

    reports: list[EpisodeSimilarityReport] = []
    n = len(episodes)

    for i in range(n):
        curr_ep = episodes[i]
        curr_ts = _parse_iso_ts(ts_map.get(curr_ep.episode_id, ""))

        overall: list[EpisodeSimilarityNeighbor] = []
        cross_session: list[EpisodeSimilarityNeighbor] = []

        for j in range(n):
            if i == j:
                continue
            other_ep = episodes[j]
            cos_sim = float(sim_matrix[i, j])
            other_ts = _parse_iso_ts(ts_map.get(other_ep.episode_id, ""))
            dt = round(other_ts - curr_ts, 1)
            is_same = curr_ep.session_id == other_ep.session_id

            neighbor = EpisodeSimilarityNeighbor(
                neighbor_episode_id=other_ep.episode_id,
                neighbor_session_id=other_ep.session_id,
                cosine_similarity=round(cos_sim, 4),
                time_delta_seconds=dt,
                is_same_session=is_same,
            )
            overall.append(neighbor)
            if not is_same:
                cross_session.append(neighbor)

        # Sort descending by cosine similarity
        overall.sort(key=lambda x: x.cosine_similarity, reverse=True)
        cross_session.sort(key=lambda x: x.cosine_similarity, reverse=True)

        reports.append(
            EpisodeSimilarityReport(
                episode_id=curr_ep.episode_id,
                session_id=curr_ep.session_id,
                profile_id=curr_ep.profile_id,
                cohesion=curr_ep.cohesion,
                nearest_overall=overall,
                nearest_cross_session=cross_session,
            )
        )

    return reports


def load_episode_evidence_events(
    conn: Any, episode_id: str
) -> list[dict[str, Any]]:
    """
    Resolve Episode Evidence exclusively through episode_membership.event_id
    ordered strictly by sequence_index. Never infer membership from timestamps.
    """
    sql = """
    SELECT e.event_id, e.profile_id, e.run_id, e.stream_sequence, e.occurred_at, e.role, e.content
    FROM episode_membership m
    JOIN evidence_journal e ON m.event_id = e.event_id
    WHERE m.episode_id = ?
    ORDER BY m.sequence_index ASC
    """
    cur = conn.execute(sql, (episode_id,))
    cols = [d[0] for d in cur.description] if cur.description else []
    results = []
    for r in cur.fetchall():
        if hasattr(r, "keys"):
            results.append(dict(r))
        else:
            results.append(dict(zip(cols, r)))
    return results
