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
        from opencohost.config.settings import BASE_DIR
        default_dir = Path(BASE_DIR) / "modelos_f5" / "minilm_l12_onnx"
        self.model_dir = Path(model_dir) if model_dir is not None else default_dir
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
