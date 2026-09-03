from __future__ import annotations

import hashlib
import math
from pathlib import Path
import pytest
import numpy as np

from opencohost.core.memory_v5_shadow.semantic import (
    ConversationalExchange,
    EmbeddingBackend,
    ExchangeExtractionDiagnostics,
    ExchangeSemanticSignal,
    MiniLMEmbeddingBackend,
    SemanticSignalProvider,
    extract_exchanges_from_events,
)
from opencohost.core.memory_v5_shadow.sessions import SessionFormationReducer


class DeterministicMockEmbeddingBackend:
    def __init__(self, mapping: dict[str, list[float]] | None = None) -> None:
        self.mapping = mapping or {}
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        results = []
        for t in texts:
            self.calls.append(t)
            if t in self.mapping:
                vec = self.mapping[t]
            else:
                # Deterministic SHA-256 pseudo vector (no process hash randomization)
                digest = hashlib.sha256(t.encode("utf-8")).hexdigest()
                h = int(digest[:8], 16) % 1000
                vec = [float(h), 1.0, 0.0]
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            results.append([x / norm for x in vec])
        return results


def test_extract_exchanges_from_events():
    events = [
        {"stream_sequence": 1, "role": "user", "event_id": "u1", "content": "hello", "occurred_at": "10:00Z"},
        {"stream_sequence": 2, "role": "assistant", "event_id": "a1", "content": "hi", "occurred_at": "10:00Z"},
        {"stream_sequence": 3, "role": "user", "event_id": "u2", "content": "topic b", "occurred_at": "10:05Z"},
        {"stream_sequence": 4, "role": "assistant", "event_id": "a2", "content": "answer b", "occurred_at": "10:05Z"},
    ]
    exchanges, diag = extract_exchanges_from_events(events, session_id="sess_1")
    assert len(exchanges) == 2
    assert diag.complete_exchanges == 2
    assert diag.orphan_user_events == 0
    assert diag.orphan_assistant_events == 0

    assert exchanges[0].exchange_ordinal == 1
    assert exchanges[0].user_event_id == "u1"
    assert exchanges[0].assistant_event_id == "a1"
    assert exchanges[0].combined_text == "hello\nhi"

    assert exchanges[1].exchange_ordinal == 2
    assert exchanges[1].user_event_id == "u2"
    assert exchanges[1].assistant_event_id == "a2"
    assert exchanges[1].combined_text == "topic b\nanswer b"


def test_extract_exchanges_orphans_diagnostics():
    events = [
        {"stream_sequence": 1, "role": "user", "event_id": "u1", "content": "orphan user"},
        {"stream_sequence": 2, "role": "user", "event_id": "u2", "content": "second user"},
        {"stream_sequence": 3, "role": "assistant", "event_id": "a2", "content": "paired assistant"},
        {"stream_sequence": 4, "role": "assistant", "event_id": "a3", "content": "orphan assistant"},
    ]
    exchanges, diag = extract_exchanges_from_events(events, session_id="sess_1")
    assert len(exchanges) == 1
    assert diag.complete_exchanges == 1
    assert diag.orphan_user_events == 1
    assert diag.orphan_assistant_events == 1
    assert exchanges[0].user_event_id == "u2"
    assert exchanges[0].assistant_event_id == "a2"


def test_conversational_exchange_payload_safety():
    ex = ConversationalExchange(
        exchange_ordinal=1,
        session_id="s1",
        user_event_id="u1",
        assistant_event_id="a1",
        user_text="secret user text",
        assistant_text="secret assistant text",
        combined_text="secret user text\nsecret assistant text",
        occurred_at="2026-09-02T10:00:00Z",
    )
    # Does not have a raw payload to_dict()
    assert not hasattr(ex, "to_dict")

    # Has safe metadata_dict() without raw payload
    meta = ex.metadata_dict()
    assert "user_text" not in meta
    assert "assistant_text" not in meta
    assert "combined_text" not in meta
    assert meta["exchange_ordinal"] == 1
    assert meta["session_id"] == "s1"
    assert meta["user_event_id"] == "u1"
    assert meta["assistant_event_id"] == "a1"


def test_semantic_signal_provider_cosine_math():
    v1 = [1.0, 0.0, 0.0]
    v2 = [1.0, 0.0, 0.0]  # identical to v1 -> sim = 1.0
    v3 = [0.0, 1.0, 0.0]  # orthogonal to v1 -> sim = 0.0

    mock = DeterministicMockEmbeddingBackend(
        {
            "u1\na1": v1,
            "u2\na2": v2,
            "u3\na3": v3,
        }
    )
    exchanges = [
        ConversationalExchange(1, "s1", "u1", "a1", "u1", "a1", "u1\na1", "t1"),
        ConversationalExchange(2, "s1", "u2", "a2", "u2", "a2", "u2\na2", "t2"),
        ConversationalExchange(3, "s1", "u3", "a3", "u3", "a3", "u3\na3", "t3"),
    ]

    provider = SemanticSignalProvider(backend=mock)
    signals = provider.analyze_session(exchanges, context_window_size=2)

    assert len(signals) == 3
    # Ex #1 is baseline: must be None, not artificial 1.0
    assert signals[0].similarity_to_previous is None
    assert signals[0].similarity_to_recent_context is None

    # Ex #2 is identical to Ex #1
    assert signals[1].similarity_to_previous is not None
    assert math.isclose(signals[1].similarity_to_previous, 1.0, abs_tol=1e-3)
    assert signals[1].similarity_to_recent_context is not None
    assert math.isclose(signals[1].similarity_to_recent_context, 1.0, abs_tol=1e-3)

    # Ex #3 is orthogonal to Ex #2
    assert signals[2].similarity_to_previous is not None
    assert math.isclose(signals[2].similarity_to_previous, 0.0, abs_tol=1e-3)
    assert signals[2].similarity_to_recent_context is not None
    assert math.isclose(signals[2].similarity_to_recent_context, 0.0, abs_tol=1e-3)


def test_session_scoping_isolation_regression():
    """
    BLOCKER REGRESSION TEST:
    One run with Profile A (2 exchanges), PROFILE_SWITCH, Profile B (3 exchanges).
    Semantic extraction using authoritative reducer session_events must return
    EXACTLY 2 exchanges for Session A and 3 for Session B with ZERO cross-contamination.
    """
    run_id = "test_run_isolation_01"
    pid_a = "prof_a"
    pid_b = "prof_b"

    stream = [
        # Session A: 2 exchanges = 4 events
        {"stream_type": "evidence", "run_id": run_id, "profile_id": pid_a, "stream_sequence": 1, "occurred_at": "2026-09-02T10:00:00Z", "role": "user", "event_id": "ua1", "content": "u text a1"},
        {"stream_type": "evidence", "run_id": run_id, "profile_id": pid_a, "stream_sequence": 2, "occurred_at": "2026-09-02T10:00:10Z", "role": "assistant", "event_id": "aa1", "content": "a text a1"},
        {"stream_type": "evidence", "run_id": run_id, "profile_id": pid_a, "stream_sequence": 3, "occurred_at": "2026-09-02T10:01:00Z", "role": "user", "event_id": "ua2", "content": "u text a2"},
        {"stream_type": "evidence", "run_id": run_id, "profile_id": pid_a, "stream_sequence": 4, "occurred_at": "2026-09-02T10:01:10Z", "role": "assistant", "event_id": "aa2", "content": "a text a2"},

        # Switch Profile A -> Profile B
        {"stream_type": "lifecycle", "run_id": run_id, "owner_profile_id": pid_a, "stream_sequence": 5, "occurred_at": "2026-09-02T10:05:00Z", "kind": "PROFILE_SWITCH_OUT"},
        {"stream_type": "lifecycle", "run_id": run_id, "owner_profile_id": pid_b, "stream_sequence": 6, "occurred_at": "2026-09-02T10:05:01Z", "kind": "PROFILE_SWITCH_IN"},

        # Session B: 3 exchanges = 6 events
        {"stream_type": "evidence", "run_id": run_id, "profile_id": pid_b, "stream_sequence": 7, "occurred_at": "2026-09-02T10:06:00Z", "role": "user", "event_id": "ub1", "content": "u text b1"},
        {"stream_type": "evidence", "run_id": run_id, "profile_id": pid_b, "stream_sequence": 8, "occurred_at": "2026-09-02T10:06:10Z", "role": "assistant", "event_id": "ab1", "content": "a text b1"},
        {"stream_type": "evidence", "run_id": run_id, "profile_id": pid_b, "stream_sequence": 9, "occurred_at": "2026-09-02T10:07:00Z", "role": "user", "event_id": "ub2", "content": "u text b2"},
        {"stream_type": "evidence", "run_id": run_id, "profile_id": pid_b, "stream_sequence": 10, "occurred_at": "2026-09-02T10:07:10Z", "role": "assistant", "event_id": "ab2", "content": "a text b2"},
        {"stream_type": "evidence", "run_id": run_id, "profile_id": pid_b, "stream_sequence": 11, "occurred_at": "2026-09-02T10:08:00Z", "role": "user", "event_id": "ub3", "content": "u text b3"},
        {"stream_type": "evidence", "run_id": run_id, "profile_id": pid_b, "stream_sequence": 12, "occurred_at": "2026-09-02T10:08:10Z", "role": "assistant", "event_id": "ab3", "content": "a text b3"},
    ]

    reducer = SessionFormationReducer()
    sessions = reducer.rebuild_from_stream(stream)
    assert len(sessions) == 2

    sess_a = [s for s in sessions if s.profile_id == pid_a][0]
    sess_b = [s for s in sessions if s.profile_id == pid_b][0]

    # Reducer-authoritative event lists
    evs_a = reducer.session_events.get(sess_a.session_id, [])
    evs_b = reducer.session_events.get(sess_b.session_id, [])

    exchanges_a, diag_a = extract_exchanges_from_events(evs_a, session_id=sess_a.session_id)
    exchanges_b, diag_b = extract_exchanges_from_events(evs_b, session_id=sess_b.session_id)

    # Session A exact verification
    assert diag_a.complete_exchanges == 2
    assert diag_a.orphan_user_events == 0
    assert len(exchanges_a) == 2
    assert [e.user_event_id for e in exchanges_a] == ["ua1", "ua2"]
    assert [e.assistant_event_id for e in exchanges_a] == ["aa1", "aa2"]

    # Session B exact verification
    assert diag_b.complete_exchanges == 3
    assert diag_b.orphan_user_events == 0
    assert len(exchanges_b) == 3
    assert [e.user_event_id for e in exchanges_b] == ["ub1", "ub2", "ub3"]
    assert [e.assistant_event_id for e in exchanges_b] == ["ab1", "ab2", "ab3"]

    # Zero cross-contamination
    event_ids_a = {e.user_event_id for e in exchanges_a} | {e.assistant_event_id for e in exchanges_a}
    event_ids_b = {e.user_event_id for e in exchanges_b} | {e.assistant_event_id for e in exchanges_b}
    assert event_ids_a.isdisjoint(event_ids_b)


def test_minilm_parity_and_semantic_discrimination():
    from opencohost.config.settings import BASE_DIR
    model_dir = Path(BASE_DIR) / "modelos_f5" / "minilm_l12_onnx"
    if not (model_dir / "model.onnx").exists():
        pytest.skip("MiniLM ONNX model artifact not found locally")

    backend = MiniLMEmbeddingBackend(model_dir=model_dir)
    backend.initialize()

    # 1. Test Semantic Discrimination
    a = "Hoy vamos a hablar sobre la arquitectura del software y microservicios"
    b_paraphrase = "En este video explicamos el diseño de sistemas y arquitectura de microservicios"
    c_unrelated = "Receta para preparar pastel de chocolate con fresas y vainilla"

    vecs = backend.embed_batch([a, b_paraphrase, c_unrelated])
    v_a = np.array(vecs[0])
    v_b = np.array(vecs[1])
    v_c = np.array(vecs[2])

    sim_related = float(np.dot(v_a, v_b))
    sim_unrelated = float(np.dot(v_a, v_c))

    assert sim_related > sim_unrelated
    assert sim_related > 0.60
    assert sim_unrelated < 0.20

    # 2. Test Parity with Reference MiniLMEmbedder from tools
    try:
        from tools.memory_v5_semantic_safety.evidence import MiniLMEmbedder
        ref_embedder = MiniLMEmbedder(model_dir)
        ref_embedder.initialize()

        for text in [a, b_paraphrase, c_unrelated]:
            ref_vec = np.array(ref_embedder.encode_text(text))
            batched_vec = np.array(backend.embed(text))
            cosine_parity = float(np.dot(ref_vec, batched_vec))
            # Must match reference down to floating point precision
            assert cosine_parity >= 0.99999
    except ImportError:
        pass
