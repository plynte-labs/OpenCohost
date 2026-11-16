"""Active cards: retrieval by MiniLM cosine OR lexical match (R3)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from opencohost.config import settings
from opencohost.core.editorial import card_embeddings
from opencohost.core.editorial.active_card_retrieval import CardMatch, cosine, rank_active_cards
from opencohost.core.editorial.active_cards import ActiveCardService
from opencohost.core.editorial.editorial_cards import EditorialCard, EditorialCardStore
from opencohost.core.editorial.editorial_matching import select_card

from tests.test_active_card_embeddings import FakeEmbedder

# The shipped threshold, captured before the autouse fixture overrides it.
SHIPPED_MIN_SCORE = settings.ACTIVE_CARD_SEMANTIC_MIN_SCORE


def _card(topic="Hades II", summary="Costs 30 USD.", **kw) -> EditorialCard:
    return EditorialCard(topic=topic, summary=summary, streamer_take="Great game.", **kw)


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_SEMANTIC_ENABLED", True)
    monkeypatch.setattr(settings, "ACTIVE_CARD_TOP_K", 1)
    monkeypatch.setattr(settings, "ACTIVE_CARD_SEMANTIC_MIN_SCORE", 0.5)
    card_embeddings.set_embedder_provider(None)
    from opencohost.config import active_card_guard

    active_card_guard.clear()
    yield
    card_embeddings.set_embedder_provider(None)
    active_card_guard.clear()


# --- pure ranking with fake vectors ------------------------------------------


def _ids(matches):
    return [m.card.id for m in matches]


def test_cosine_basics_and_degenerate_inputs() -> None:
    assert cosine([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine([2.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)  # not assuming unit vectors
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
    assert cosine([1.0], [1.0, 0.0]) == 0.0  # dimension mismatch is a miss, not an error


def test_semantic_hit_above_threshold_and_miss_below() -> None:
    a, b = _card("A card"), _card("B card")
    vectors = {a.id: [1.0, 0.0], b.id: [0.0, 1.0]}
    hits = rank_active_cards(
        "unrelated words", [a, b], query_vec=[0.9, 0.1], vectors=vectors, min_score=0.5, top_k=2
    )
    assert _ids(hits) == [a.id]
    assert hits[0].via == "semantic" and hits[0].score == pytest.approx(0.9 / (0.82**0.5), abs=1e-3)
    nothing = rank_active_cards(
        "unrelated words", [a, b], query_vec=[0.9, 0.1], vectors=vectors, min_score=0.999, top_k=2
    )
    assert nothing == []


def test_top_k_limits_and_orders_by_score() -> None:
    cards = [_card(f"Card {i}") for i in range(3)]
    vectors = {cards[0].id: [0.6, 0.8], cards[1].id: [1.0, 0.0], cards[2].id: [0.9, 0.436]}
    one = rank_active_cards("x", cards, query_vec=[1.0, 0.0], vectors=vectors, min_score=0.5, top_k=1)
    assert _ids(one) == [cards[1].id]
    two = rank_active_cards("x", cards, query_vec=[1.0, 0.0], vectors=vectors, min_score=0.5, top_k=2)
    assert _ids(two) == [cards[1].id, cards[2].id]


def test_lexical_hit_without_any_vector() -> None:
    card = _card(triggers=["cuesta"])
    hits = rank_active_cards("cuanto cuesta", [card], query_vec=None, vectors={}, min_score=0.5, top_k=1)
    assert [(m.card.id, m.via) for m in hits] == [(card.id, "lexical")]
    assert hits[0].score == pytest.approx(0.9)


def test_semantic_and_lexical_are_ored_and_lexical_leads() -> None:
    lex = _card("Lexical card", triggers=["cuesta"])
    sem = _card("Semantic card")
    vectors = {sem.id: [1.0, 0.0]}
    both = rank_active_cards(
        "cuanto cuesta", [sem, lex], query_vec=[1.0, 0.0], vectors=vectors, min_score=0.5, top_k=2
    )
    assert _ids(both) == [lex.id, sem.id]
    assert [m.via for m in both] == ["lexical", "semantic"]
    top1 = rank_active_cards(
        "cuanto cuesta", [sem, lex], query_vec=[1.0, 0.0], vectors=vectors, min_score=0.5, top_k=1
    )
    assert _ids(top1) == [lex.id]


def test_card_matching_both_ways_reports_semantic_when_the_cosine_qualifies() -> None:
    card = _card(triggers=["cuesta"])
    hit = rank_active_cards(
        "cuanto cuesta", [card], query_vec=[1.0, 0.0], vectors={card.id: [1.0, 0.0]}, min_score=0.5, top_k=1
    )[0]
    assert hit.via == "semantic" and hit.score == pytest.approx(1.0)


def test_cards_without_a_vector_still_match_lexically_when_others_have_one() -> None:
    with_vec = _card("Has vec")
    no_vec = _card("No vec", triggers=["cuesta"])
    hits = rank_active_cards(
        "cuanto cuesta", [with_vec, no_vec], query_vec=[0.0, 1.0], vectors={with_vec.id: [1.0, 0.0]},
        min_score=0.5, top_k=2,
    )
    assert _ids(hits) == [no_vec.id]


def test_lexical_only_result_equals_the_existing_matcher() -> None:
    cards = [_card("Hades II", triggers=["precio"]), _card("Acme Mic", triggers=["microfono"])]
    for text in ("cual es el precio", "que microfono usas", "hola que tal", "precio microfono"):
        hits = rank_active_cards(text, cards, query_vec=None, vectors={}, min_score=0.5, top_k=5)
        expected = [c for c in cards if select_card(text, [c]) is not None]
        assert sorted(_ids(hits)) == sorted(c.id for c in expected), text


# --- the service ----------------------------------------------------------------


class _Embedder(FakeEmbedder):
    """Query vector [1, 0, 0]; cards get [1, 0, 0] when their text mentions price."""

    def embed_batch(self, texts, timeout_s):
        self.batches.append(list(texts))
        return [[1.0, 0.0, 0.0] if "price" in t.lower() else [0.0, 1.0, 0.0] for t in texts]

    def embed_query(self, text, timeout_s):
        self.queries.append((text, timeout_s))
        return [1.0, 0.0, 0.0]


def _store(tmp_path) -> EditorialCardStore:
    return EditorialCardStore(tmp_path / "cards.db")


def _active(store, topic="Hades II", summary="The price is 30 USD.", **kw):
    card = store.upsert(_card(topic, summary, **kw))
    assert store.activate(card.id)
    return store.get(card.id)


def _service(store):
    return ActiveCardService(store, lang_reader=lambda: "en")


def _install(embedder):
    card_embeddings.set_embedder_provider(lambda: embedder)
    return embedder


def _lines(caplog):
    return [r.getMessage() for r in caplog.records if "[ACTIVE_CARD]" in r.getMessage()]


def test_no_active_card_returns_empty_and_logs_nothing(tmp_path, caplog) -> None:
    store = _store(tmp_path)
    store.upsert(_card())
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        assert _service(store)("what's the price", "chat", "m") == ""
    assert _lines(caplog) == []


def test_active_but_unrelated_turn_gets_nothing_and_one_none_line(tmp_path, caplog) -> None:
    store = _store(tmp_path)
    _active(store)
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        assert _service(store)("háblame de animales", "chat", "m") == ""
    assert _lines(caplog) == ["[ACTIVE_CARD] none"]


def test_semantic_match_injects_only_that_card_and_logs_without_text(tmp_path, caplog) -> None:
    store = _store(tmp_path)
    _install(_Embedder("fake/v1"))
    hit = _active(store, "Hades II", "The price is 30 USD.")
    _active(store, "Zebra facts", "Stripes are unique.")
    svc = _service(store)
    card_embeddings.backfill(store, store.list_active())
    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        block = svc("what is it worth, secret viewer text", "chat", "m")
    assert block.count("<active_card>") == 1 and "Hades II" in block and "Zebra" not in block
    lines = _lines(caplog)
    assert lines == [f"[ACTIVE_CARD] matched card={hit.id} via=semantic score=1.00"]
    assert "secret" not in caplog.text and "30 USD" not in caplog.text


def test_matched_card_is_reusable_every_turn_with_no_consumption(tmp_path) -> None:
    store = _store(tmp_path)
    card = _active(store, single_use=True, triggers=["cuesta"])
    store.arm(card.id)
    svc = _service(store)
    for _ in range(5):
        assert "<active_card>" in svc("cuanto cuesta", "direct", "m")  # no cooldown
    after = store.get(card.id)
    assert after.use_count == 0 and after.status.value == "armed"
    assert after.last_injected_at is None and after.last_used_at is None


def test_top_k_setting_controls_how_many_cards(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    _active(store, "Card one", triggers=["cuesta"])
    _active(store, "Card two", triggers=["cuesta"])
    svc = _service(store)
    assert svc("cuanto cuesta", "direct", "m").count("<active_card>") == 1
    monkeypatch.setattr(settings, "ACTIVE_CARD_TOP_K", 2)
    assert svc("cuanto cuesta", "direct", "m").count("<active_card>") == 2


def test_threshold_setting_is_honored(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    _install(_Embedder())
    _active(store)
    card_embeddings.backfill(store, store.list_active())
    svc = _service(store)
    assert svc("anything", "chat", "m") != ""
    monkeypatch.setattr(settings, "ACTIVE_CARD_SEMANTIC_MIN_SCORE", 1.01)
    assert svc("anything", "chat", "m") == ""


def test_bound_turn_vector_is_reused_without_a_second_embed(tmp_path) -> None:
    store = _store(tmp_path)
    embedder = _install(_Embedder())
    _active(store)
    card_embeddings.backfill(store, store.list_active())
    svc = _service(store)
    svc.bind_turn_vector("what is it worth", [1.0, 0.0, 0.0], attempted=True)
    assert svc("what is it worth", "direct", "m") != ""
    assert embedder.queries == []  # the turn's existing vector served the ranking


def test_bound_vector_is_single_use_and_text_checked(tmp_path) -> None:
    store = _store(tmp_path)
    embedder = _install(_Embedder())
    _active(store)
    card_embeddings.backfill(store, store.list_active())
    svc = _service(store)
    svc.bind_turn_vector("old turn text", [0.0, 1.0, 0.0], attempted=True)
    svc("a different turn", "chat", "m")  # text differs: the stale binding is dropped
    assert len(embedder.queries) == 1
    svc.bind_turn_vector("same", [1.0, 0.0, 0.0], attempted=True)
    svc("same", "direct", "m")
    svc("same", "direct", "m")  # consumed by the first call
    assert len(embedder.queries) == 2


def test_failed_recall_embedding_is_not_retried_on_the_turn_path(tmp_path) -> None:
    store = _store(tmp_path)
    embedder = _install(_Embedder())
    _active(store, triggers=["cuesta"])
    card_embeddings.backfill(store, store.list_active())
    svc = _service(store)
    svc.bind_turn_vector("cuanto cuesta", None, attempted=True)  # recall timed out
    block = svc("cuanto cuesta", "direct", "m")
    assert embedder.queries == []
    assert "<active_card>" in block  # lexical still works


def test_source_without_a_turn_vector_embeds_the_query_once_bounded(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    monkeypatch.setattr(settings, "ACTIVE_CARD_QUERY_EMBED_TIMEOUT_S", 0.25)
    embedder = _install(_Embedder())
    _active(store)
    card_embeddings.backfill(store, store.list_active())
    assert _service(store)("what is it worth", "chat", "m") != ""
    assert embedder.queries == [("what is it worth", 0.25)]


def test_no_query_embed_when_no_card_has_a_vector_yet_and_backfill_is_scheduled(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    embedder = _install(_Embedder())
    _active(store)
    scheduled = []
    monkeypatch.setattr(
        card_embeddings, "_default_scheduler", lambda job: scheduled.append(job)
    )
    assert _service(store)("what is it worth", "chat", "m") == ""
    assert embedder.queries == []  # nothing to compare against: no wasted MiniLM call
    assert len(scheduled) == 1  # off the turn path
    scheduled[0]()
    assert store.get_embedding(store.list_active()[0].id) is not None


@pytest.mark.parametrize("mode", ["none", "raises"])
def test_worker_failure_on_the_turn_path_degrades_to_lexical(tmp_path, mode) -> None:
    store = _store(tmp_path)

    class Flaky(_Embedder):
        def embed_query(self, text, timeout_s):
            if mode == "raises":
                raise RuntimeError("worker died")
            return None

    embedder = _install(Flaky())
    _active(store, triggers=["cuesta"])
    card_embeddings.backfill(store, store.list_active())
    svc = _service(store)
    assert "<active_card>" in svc("cuanto cuesta", "chat", "m")
    assert svc("what is it worth", "chat", "m") == ""
    assert embedder.model_id == "fake/v1"


def test_semantic_off_is_byte_identical_to_the_lexical_path(tmp_path, monkeypatch) -> None:
    store = _store(tmp_path)
    embedder = _install(_Embedder())
    _active(store, "Hades II", triggers=["cuesta"])
    _active(store, "Acme Mic", "A USB microphone.", triggers=["microfono"])
    card_embeddings.backfill(store, store.list_active())
    queries = ["cuanto cuesta", "what is it worth", "que microfono usas", "hola", "cuesta microfono"]

    monkeypatch.setattr(settings, "MEMORY_SEMANTIC_ENABLED", False)
    embedder.queries.clear()
    svc = _service(store)
    off_outputs = []
    for q in queries:
        svc.bind_turn_vector(q, [1.0, 0.0, 0.0], attempted=True)  # even a bound vector is ignored
        off_outputs.append(svc(q, "direct", "m"))
    assert embedder.queries == []

    # Reference: no embedder and no vectors at all, i.e. the plain lexical path.
    card_embeddings.set_embedder_provider(None)
    monkeypatch.setattr(settings, "MEMORY_SEMANTIC_ENABLED", True)
    reference = _service(store)
    assert off_outputs == [reference(q, "direct", "m") for q in queries]


def test_wrapped_owner_text_is_cleaned_before_matching(tmp_path) -> None:
    from opencohost.i18n import active as i18n_active
    from opencohost.i18n.startup import resolve_active_bundle

    i18n_active.set_active_bundle(resolve_active_bundle(locale="es"))
    try:
        store = _store(tmp_path)
        _active(store, triggers=["cuesta"])
        wrapped = "El streamer dijo (PTT): cuanto cuesta"
        assert "<active_card>" in _service(store)(wrapped, "ptt", "m")
    finally:
        i18n_active.reset_active_bundle()


def test_store_failure_is_fail_open_and_does_not_log_text(tmp_path, caplog) -> None:
    class Boom:
        def list_active(self):
            raise RuntimeError("db gone")

    with caplog.at_level(logging.INFO, logger="OpenCohost"):
        assert ActiveCardService(Boom(), lang_reader=lambda: "es")("secret", "direct", "m") == ""
    assert "secret" not in caplog.text


# --- engine wiring ----------------------------------------------------------------


def test_engine_binds_the_recall_query_vector_to_the_service(monkeypatch, tmp_path) -> None:
    """The motor hands the turn's already-computed query vector to the service."""
    from tests.test_memory_fact_semantic_rank import (
        FACT_PET,
        QUERY_PET,
        _prompt,
        _semantic_motor,
    )

    rows = [{"id": "pet", "content": FACT_PET}]
    motor, _db, _index, _cache, worker = _semantic_motor(monkeypatch, tmp_path / "m", rows)
    store = _store(tmp_path)
    card_embeddings.set_embedder_provider(
        lambda: card_embeddings.MemoryWorkerEmbedder.from_memory(motor._memory)
    )
    _active(store, "Mascota", "zqpetfact el streamer tiene un perro")
    card_embeddings.backfill(store, store.list_active())
    motor.active_card_service = ActiveCardService(store, lang_reader=lambda: "es")
    worker.queries.clear()

    prompt = _prompt(motor, QUERY_PET)

    assert "<active_card>" in prompt and "Mascota" in prompt
    assert len(worker.queries) == 1  # the recall's embed; the card ranking added none


def test_engine_without_active_cards_leaves_the_prompt_unchanged(monkeypatch, tmp_path) -> None:
    from tests.test_memory_fact_semantic_rank import FACT_PET, QUERY_PET, _prompt, _semantic_motor

    rows = [{"id": "pet", "content": FACT_PET}]
    motor, *_ = _semantic_motor(monkeypatch, tmp_path / "a", rows)
    base = _prompt(motor, QUERY_PET)
    motor2, *_ = _semantic_motor(monkeypatch, tmp_path / "b", rows)
    motor2.active_card_service = ActiveCardService(_store(tmp_path), lang_reader=lambda: "es")
    assert _prompt(motor2, QUERY_PET) == base


# --- calibration against the real MiniLM -------------------------------------------

_MODEL_DIR_CANDIDATES = [
    os.environ.get("OPENCOHOST_MINILM_DIR", ""),
    str(Path(__file__).resolve().parents[1] / "modelos_f5" / "minilm_l12_onnx"),
]
_MODEL_DIR = next(
    (Path(d) for d in _MODEL_DIR_CANDIDATES if d and (Path(d) / "model.onnx").is_file()), None
)

CALIBRATION_CARDS = {
    "game_en": [
        "Hades II",
        "Hades II costs 30 USD on Steam at launch.",
        "price 30 USD",
        "released on Steam",
    ],
    "game_es": [
        "Hades II",
        "Hades II cuesta 30 dolares en Steam al lanzamiento.",
        "precio 30 dolares",
        "salio en Steam",
    ],
    "mic_es": [
        "Acme Mic",
        "Acme Mic es un microfono USB patrocinador del stream.",
        "cuesta 49 dolares",
        "codigo KIRA10 da 10 por ciento de descuento",
    ],
    "mic_en": [
        "Acme Mic",
        "Acme Mic is a USB microphone and sponsor of the stream.",
        "costs 49 USD",
        "code KIRA10 gives 10 percent off",
    ],
}
ON_TOPIC = {
    "game_en": ["what's the price", "how much does it cost", "is Hades II expensive", "when can I buy it on Steam"],
    "game_es": ["¿cuánto cuesta?", "cuanto sale el juego", "¿cuál es el precio?", "donde lo compro en Steam"],
    "mic_es": [
        "¿cuánto cuesta el micrófono?",
        "¿qué microfono usas?",
        "tiene descuento?",
        "dame el codigo de descuento",
    ],
    "mic_en": [
        "how much is the mic",
        "what microphone do you use",
        "is there a discount code",
        "what's the price of the microphone",
    ],
}
OFF_TOPIC = [
    "háblame de animales",
    "what's the weather today",
    "¿qué tal tu día?",
    "cuéntame un chiste",
    "me gusta mucho el fútbol",
    "what is your favorite color",
    "hola Kira",
    "¿cómo se hace una paella?",
    "tell me about dogs",
    "buenas noches a todos",
    "¿quién ganó el partido ayer?",
    "recomiéndame una película",
    "what's up",
    "me duele la cabeza",
    "¿cuánto cuesta una casa en Madrid?",
    "me encanta cocinar",
    "¿qué juego estás jugando?",
    "how are you feeling today",
    "¿cuántos años tienes?",
    "do you like pizza",
]


@pytest.mark.skipif(_MODEL_DIR is None, reason="MiniLM ONNX model artifacts not present on disk")
def test_calibration_real_minilm_separates_on_topic_from_off_topic() -> None:
    if not os.environ.get("OPENCOHOST_ACTIVE_CARD_MIN_SCORE"):
        assert SHIPPED_MIN_SCORE == 0.24
    """The shipped threshold admits every on-topic paraphrase and no off-topic phrase.

    Measured 2026-10-04 with the shipped MiniLM-L12 ONNX (the model the worker
    hosts): on-topic cosines min 0.255 / median 0.521, off-topic best-card
    cosines max 0.213 / median 0.072. Known limit: a phrase sharing the card's
    topic word ("what's the price of gas" 0.47, "hablemos de música" vs a
    microphone card 0.38) is semantically close and will pass; the prompt
    instruction ("state it only when relevant, otherwise say you do not have
    that data") is the second line of defence.
    """
    from opencohost.core.memory_v5_shadow.semantic import MiniLMEmbeddingBackend

    backend = MiniLMEmbeddingBackend(_MODEL_DIR)
    keys = list(CALIBRATION_CARDS)
    card_vecs = dict(zip(keys, backend.embed_batch(["\n".join(CALIBRATION_CARDS[k]) for k in keys])))
    on = [(k, q) for k in keys for q in ON_TOPIC[k]]
    queries = [q for _, q in on] + OFF_TOPIC
    qvec = dict(zip(queries, backend.embed_batch(queries)))
    threshold = SHIPPED_MIN_SCORE

    misses = [(k, q, round(cosine(qvec[q], card_vecs[k]), 3)) for k, q in on if cosine(qvec[q], card_vecs[k]) < threshold]
    assert misses == [], f"on-topic phrases below {threshold}: {misses}"
    false_hits = [
        (q, round(max(cosine(qvec[q], v) for v in card_vecs.values()), 3))
        for q in OFF_TOPIC
        if max(cosine(qvec[q], v) for v in card_vecs.values()) >= threshold
    ]
    assert false_hits == [], f"off-topic phrases at or above {threshold}: {false_hits}"
