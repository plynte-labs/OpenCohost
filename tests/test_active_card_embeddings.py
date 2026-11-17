"""Active cards: embeddings on save, recompute on edit, lazy backfill (R2)."""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import opencohost.api.auth as auth
import opencohost.api.main as main_mod
from opencohost.config import settings
from opencohost.core.editorial import card_embeddings
from opencohost.core.editorial.editorial_cards import EditorialCard, EditorialCardStore
from tests.test_api_phase1 import FakeHost


class FakeEmbedder:
    """Deterministic stand-in for the MiniLM worker: vector = [len, 0.5, 0.25]."""

    def __init__(self, model_id: str = "fake/v1", fail: bool = False) -> None:
        self.model_id = model_id
        self.fail = fail
        self.batches: list[list[str]] = []
        self.queries: list[str] = []

    def embed_batch(self, texts, timeout_s):
        self.batches.append(list(texts))
        if self.fail:
            return None
        return [[float(len(t)), 0.5, 0.25] for t in texts]

    def embed_query(self, text, timeout_s):
        self.queries.append(text)
        return None if self.fail else [float(len(text)), 0.5, 0.25]


@pytest.fixture(autouse=True)
def _semantic_on(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_SEMANTIC_ENABLED", True)
    card_embeddings.set_embedder_provider(None)
    yield
    card_embeddings.set_embedder_provider(None)


def _install(embedder):
    card_embeddings.set_embedder_provider(lambda: embedder)
    return embedder


def _store(tmp_path) -> EditorialCardStore:
    return EditorialCardStore(tmp_path / "cards.db")


def _card(topic="Hades II", summary="Costs 30 USD at launch.", **kw) -> EditorialCard:
    return EditorialCard(topic=topic, summary=summary, streamer_take="Great game.", **kw)


def test_source_text_is_topic_summary_and_must_say_only() -> None:
    card = _card(must_say=["price 30 USD", "code KIRA10"], forbidden_claims=["secret"], discount_code="ZZZ")
    text = card_embeddings.embedding_source_text(card)
    assert "Hades II" in text and "Costs 30 USD at launch." in text
    assert "price 30 USD" in text and "code KIRA10" in text
    assert "secret" not in text and "ZZZ" not in text and "Great game." not in text


def test_refresh_stores_vector_with_model_id_and_hash(tmp_path) -> None:
    store = _store(tmp_path)
    embedder = _install(FakeEmbedder("minilm-l12/v1"))
    card = store.upsert(_card(must_say=["30 USD"]))
    assert card_embeddings.refresh_embedding(store, card) is True
    stored = store.get_embedding(card.id)
    assert stored.model_id == "minilm-l12/v1"
    assert stored.text_hash == card_embeddings.source_hash(card_embeddings.embedding_source_text(card))
    assert len(stored.vector) == 3 and stored.vector[1] == 0.5
    assert embedder.batches == [[card_embeddings.embedding_source_text(card)]]


def test_unchanged_card_is_not_embedded_twice_and_edit_recomputes(tmp_path) -> None:
    store = _store(tmp_path)
    embedder = _install(FakeEmbedder())
    card = store.upsert(_card())
    card_embeddings.refresh_embedding(store, card)
    assert card_embeddings.refresh_embedding(store, store.get(card.id)) is True
    assert len(embedder.batches) == 1  # second call found a valid embedding
    first = store.get_embedding(card.id)
    edited = store.upsert(_card(summary="Now costs 45 USD after the patch."))
    card_embeddings.refresh_embedding(store, edited)
    assert len(embedder.batches) == 2
    second = store.get_embedding(card.id)
    assert second.text_hash != first.text_hash and second.vector != first.vector


def test_other_model_id_makes_the_embedding_stale(tmp_path) -> None:
    store = _store(tmp_path)
    card = store.upsert(_card())
    _install(FakeEmbedder("old/v1"))
    card_embeddings.refresh_embedding(store, card)
    newer = _install(FakeEmbedder("new/v2"))
    card_embeddings.refresh_embedding(store, store.get(card.id))
    assert len(newer.batches) == 1
    assert store.get_embedding(card.id).model_id == "new/v2"


def test_semantic_off_computes_nothing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "MEMORY_SEMANTIC_ENABLED", False)
    store = _store(tmp_path)
    embedder = _install(FakeEmbedder())
    card = store.upsert(_card())
    assert card_embeddings.refresh_embedding(store, card) is False
    assert embedder.batches == [] and store.get_embedding(card.id) is None


def test_no_embedder_is_a_quiet_no_op(tmp_path) -> None:
    store = _store(tmp_path)
    card = store.upsert(_card())
    assert card_embeddings.refresh_embedding(store, card) is False
    assert store.get_embedding(card.id) is None


@pytest.mark.parametrize("mode", ["none", "raises", "mismatch"])
def test_worker_failure_never_raises_and_stores_nothing(tmp_path, mode) -> None:
    store = _store(tmp_path)

    class Broken(FakeEmbedder):
        def embed_batch(self, texts, timeout_s):
            if mode == "raises":
                raise RuntimeError("worker died")
            if mode == "mismatch":
                return [[1.0]] * (len(texts) + 1)
            return None

    _install(Broken())
    card = store.upsert(_card())
    assert card_embeddings.refresh_embedding(store, card) is False
    assert store.get_embedding(card.id) is None


def test_provider_that_raises_is_swallowed(tmp_path) -> None:
    store = _store(tmp_path)

    def boom():
        raise RuntimeError("no memory subsystem")

    card_embeddings.set_embedder_provider(boom)
    card = store.upsert(_card())
    assert card_embeddings.refresh_embedding(store, card) is False


def test_backfill_embeds_only_active_cards_that_lack_a_valid_vector(tmp_path) -> None:
    store = _store(tmp_path)
    embedder = _install(FakeEmbedder())
    a = store.upsert(_card("A card", "About a"))
    b = store.upsert(_card("B card", "About b"))
    c = store.upsert(_card("C card", "About c"))
    store.activate(a.id)
    store.activate(b.id)
    card_embeddings.refresh_embedding(store, store.get(a.id))  # a already valid
    embedder.batches.clear()
    done = card_embeddings.backfill(store, store.list_active())
    assert done == 1
    assert len(embedder.batches) == 1 and len(embedder.batches[0]) == 1
    assert store.get_embedding(b.id) is not None
    assert store.get_embedding(c.id) is None  # inactive: not embedded by the backfill
    assert card_embeddings.backfill(store, store.list_active()) == 0


def test_schedule_backfill_runs_off_thread_once_and_never_raises(tmp_path) -> None:
    store = _store(tmp_path)
    embedder = _install(FakeEmbedder())
    a = store.upsert(_card())
    store.activate(a.id)
    jobs = []
    assert card_embeddings.schedule_backfill(store, scheduler=jobs.append) is True
    assert card_embeddings.schedule_backfill(store, scheduler=jobs.append) is False  # in flight
    assert embedder.batches == []  # nothing ran on the caller's thread
    jobs[0]()
    assert store.get_embedding(a.id) is not None
    assert card_embeddings.schedule_backfill(store, scheduler=jobs.append) is False  # nothing left to do


def test_schedule_backfill_is_inert_when_semantic_is_off(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(settings, "MEMORY_SEMANTIC_ENABLED", False)
    store = _store(tmp_path)
    _install(FakeEmbedder())
    a = store.upsert(_card())
    store.activate(a.id)
    jobs = []
    assert card_embeddings.schedule_backfill(store, scheduler=jobs.append) is False
    assert jobs == []


def test_store_round_trips_the_vector_and_clears_it(tmp_path) -> None:
    store = _store(tmp_path)
    card = store.upsert(_card())
    store.set_embedding(card.id, [0.5, -0.25, 1.0], "m/v1", "abc")
    got = store.get_embedding(card.id)
    assert got.vector == (0.5, -0.25, 1.0) and got.model_id == "m/v1" and got.text_hash == "abc"
    store.clear_embedding(card.id)
    assert store.get_embedding(card.id) is None
    assert store.get_embedding("ec_missing") is None


def test_old_db_gains_the_embedding_columns(tmp_path) -> None:
    db = tmp_path / "cards.db"
    store = EditorialCardStore(db)
    card = store.upsert(_card())
    with sqlite3.connect(db) as conn:
        for col in ("embedding", "embedding_model", "embedding_hash"):
            conn.execute(f"ALTER TABLE editorial_cards DROP COLUMN {col}")
    reopened = EditorialCardStore(db)
    assert reopened.get_embedding(card.id) is None
    reopened.set_embedding(card.id, [1.0], "m", "h")
    assert reopened.get_embedding(card.id).vector == (1.0,)
    EditorialCardStore(db)  # idempotent


def test_embedding_text_is_never_logged(tmp_path, caplog) -> None:
    store = _store(tmp_path)
    _install(FakeEmbedder(fail=True))
    card = store.upsert(_card(summary="SECRET-SPONSOR-DETAIL"))
    with caplog.at_level("DEBUG"):
        card_embeddings.refresh_embedding(store, card)
    assert "SECRET-SPONSOR-DETAIL" not in caplog.text


# --- worker adapter ---------------------------------------------------------


def _fact_index(worker, model_id="minilm-l12", model_version="v1"):
    from opencohost.core.memory_v5_shadow.fact_index import FactSemanticIndex

    index = FactSemanticIndex.__new__(FactSemanticIndex)
    index.worker = worker
    index.model_id = model_id
    index.model_version = model_version
    return index


def test_memory_worker_embedder_uses_the_fact_index_worker_and_model() -> None:
    calls = {}

    class Worker:
        def embed_batch(self, texts, timeout_s=10.0):
            calls["batch"] = (list(texts), timeout_s)
            return [[1.0, 0.0] for _ in texts]

        def embed_query(self, text, timeout_s=0.5):
            calls["query"] = (text, timeout_s)
            return [0.0, 1.0]

    memory = SimpleNamespace(fact_index=_fact_index(Worker()))
    embedder = card_embeddings.MemoryWorkerEmbedder.from_memory(memory)
    assert embedder.model_id == "minilm-l12/v1"
    assert embedder.embed_batch(["a", "b"], 2.0) == [[1.0, 0.0], [1.0, 0.0]]
    assert calls["batch"] == (["a", "b"], 2.0)
    assert embedder.embed_query("q", 0.25) == [0.0, 1.0]
    assert calls["query"] == ("q", 0.25)


def test_memory_worker_embedder_is_unavailable_without_a_memory_subsystem() -> None:
    assert card_embeddings.MemoryWorkerEmbedder.from_memory(None) is None
    assert card_embeddings.MemoryWorkerEmbedder.from_memory(SimpleNamespace(fact_index=None)) is None


def test_engine_host_installs_the_embedder_provider(tmp_path, monkeypatch) -> None:
    import opencohost.api.engine_host as engine_host_mod
    from opencohost.api.engine_host import EngineHost

    monkeypatch.setattr(engine_host_mod, "EDITORIAL_CARDS_DB", str(tmp_path / "cards.db"))
    host = EngineHost.__new__(EngineHost)
    host.motor = SimpleNamespace(_memory=None)
    host.agenda = None
    host.agenda_lock = None
    host._wire_editorial_bridge()
    assert card_embeddings.get_embedder() is None  # no memory subsystem yet: quiet None
    worker = SimpleNamespace(embed_batch=lambda t, timeout_s=1: [[1.0]] * len(t))
    host.motor._memory = SimpleNamespace(fact_index=_fact_index(worker))
    embedder = card_embeddings.get_embedder()
    assert embedder is not None and embedder.model_id == "minilm-l12/v1"


# --- routes -----------------------------------------------------------------

BASE = "/api/editorial/cards"
_ORIGINS = ["http://localhost:5173"]


@pytest.fixture
def api(tmp_path, monkeypatch):
    main_mod._host_active = False
    monkeypatch.setattr(main_mod, "EDITORIAL_CARDS_DB", str(tmp_path / "cards.db"))
    monkeypatch.setattr(auth, "_rate_limiter", auth.RateLimiter())
    auth.ensure_tokens()
    headers = {"Authorization": f"Bearer {auth.load_tokens()['operator']}"}
    agent = {"Authorization": f"Bearer {auth.load_tokens()['agent']}"}
    store = EditorialCardStore(main_mod.EDITORIAL_CARDS_DB)
    with TestClient(main_mod.create_app(host_factory=FakeHost, cors_origins=_ORIGINS)) as client:
        yield client, headers, agent, store
    main_mod._host_active = False


BODY = {"topic": "Hades II", "summary": "Costs 30 USD.", "streamer_take": "Great.", "must_say": ["30 USD"]}


def test_create_route_embeds_on_save_and_field_edit_recomputes(api) -> None:
    client, headers, _, store = api
    embedder = _install(FakeEmbedder())
    created = client.post(BASE, json=BODY, headers=headers).json()
    before = store.get_embedding(created["id"])
    assert before is not None
    client.put(f"{BASE}/{created['id']}/fields", json={"must_say": ["45 USD now"]}, headers=headers)
    after = store.get_embedding(created["id"])
    assert after.text_hash != before.text_hash
    assert len(embedder.batches) == 2


def test_agent_draft_is_embedded_on_save_too(api) -> None:
    client, _, agent, store = api
    _install(FakeEmbedder())
    resp = client.post("/api/agent/cards", json={"agent": "claude-code", **BODY}, headers=agent)
    assert store.get_embedding(resp.json()["id"]) is not None


def test_route_succeeds_when_the_worker_is_down(api) -> None:
    client, headers, _, store = api
    _install(FakeEmbedder(fail=True))
    resp = client.post(BASE, json=BODY, headers=headers)
    assert resp.status_code == 200
    assert store.get_embedding(resp.json()["id"]) is None
