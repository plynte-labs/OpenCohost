"""MiniLM embeddings for editorial cards (active-card retrieval).

A card is embedded from ``topic + summary + must_say`` (never the streamer's
take, forbidden phrases, code or link) so a viewer's paraphrase of a fact lands
near the card. The vector is stored with the model id and a hash of the embedded
text: an edit or a model change makes it stale and it is recomputed.

Everything here is best effort and fail-open. The embedder is the MiniLM worker
process the memory subsystem already owns; when it is missing, down or slow the
card simply has no vector and retrieval stays lexical. Nothing here raises into
the turn path, and no card or query text is ever logged.

Embeddings are only computed while the Memoria "semantic" toggle is on
(``MEMORY_SEMANTIC_ENABLED``): off means no MiniLM use at all.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from typing import Any, Callable, Iterable, Optional, Protocol, Sequence

from opencohost.config import settings
from opencohost.core.editorial.editorial_cards import EditorialCard, EditorialCardStore

logger = logging.getLogger("OpenCohost")


class CardEmbedder(Protocol):
    """What card retrieval needs from the MiniLM worker."""

    model_id: str

    def embed_batch(self, texts: Sequence[str], timeout_s: float) -> Optional[list[list[float]]]: ...

    def embed_query(self, text: str, timeout_s: float) -> Optional[list[float]]: ...


class MemoryWorkerEmbedder:
    """Adapter over the memory subsystem's fact index worker (MiniLM process)."""

    def __init__(self, fact_index: Any) -> None:
        self._index = fact_index
        self.model_id = f"{fact_index.model_id}/{fact_index.model_version}"

    @classmethod
    def from_memory(cls, memory: Any) -> Optional["MemoryWorkerEmbedder"]:
        """Build the adapter from ``motor._memory``; None when there is no worker."""
        index = getattr(memory, "fact_index", None) if memory is not None else None
        if index is None or getattr(index, "worker", None) is None:
            return None
        return cls(index)

    def embed_batch(self, texts: Sequence[str], timeout_s: float) -> Optional[list[list[float]]]:
        return self._index.worker.embed_batch(list(texts), timeout_s=timeout_s)

    def embed_query(self, text: str, timeout_s: float) -> Optional[list[float]]:
        return self._index.worker.embed_query(text, timeout_s=timeout_s)


_provider: Optional[Callable[[], Optional[CardEmbedder]]] = None
_inflight: set[str] = set()
_inflight_lock = threading.Lock()


def set_embedder_provider(provider: Optional[Callable[[], Optional[CardEmbedder]]]) -> None:
    """Install (or clear) the callable that yields the current embedder.

    A provider rather than an embedder because the memory subsystem starts
    after the editorial bridge is wired and may be absent altogether.
    """
    global _provider
    _provider = provider


def get_embedder() -> Optional[CardEmbedder]:
    """The current embedder, or None. Never raises."""
    provider = _provider
    if provider is None:
        return None
    try:
        return provider()
    except Exception as exc:
        logger.debug("[ACTIVE_CARD] embedder unavailable: %s", type(exc).__name__)
        return None


def semantic_enabled() -> bool:
    """The Memoria semantic toggle, read live."""
    return bool(settings.MEMORY_SEMANTIC_ENABLED)


def embedding_source_text(card: EditorialCard) -> str:
    """The text a card is embedded from: topic, summary, must-say facts."""
    return "\n".join([card.topic, card.summary, *card.must_say])


def source_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def valid_vector(
    store: EditorialCardStore, card: EditorialCard, model_id: str
) -> Optional[tuple[float, ...]]:
    """The card's stored vector when it is current for *model_id* and the card text."""
    stored = store.get_embedding(card.id)
    if (
        stored is not None
        and stored.model_id == model_id
        and stored.text_hash == source_hash(embedding_source_text(card))
    ):
        return stored.vector
    return None


def has_valid_embedding(store: EditorialCardStore, card: EditorialCard, model_id: str) -> bool:
    return valid_vector(store, card, model_id) is not None


def _embed_cards(
    store: EditorialCardStore, embedder: CardEmbedder, cards: list[EditorialCard]
) -> int:
    """Embed *cards* in one worker call and store the vectors; returns how many."""
    texts = [embedding_source_text(card) for card in cards]
    vectors = embedder.embed_batch(texts, float(settings.ACTIVE_CARD_EMBED_TIMEOUT_S))
    if not vectors or len(vectors) != len(cards):
        return 0
    for card, text, vector in zip(cards, texts, vectors):
        store.set_embedding(card.id, vector, embedder.model_id, source_hash(text))
    return len(cards)


def refresh_embedding(store: EditorialCardStore, card: EditorialCard) -> bool:
    """Make sure *card* has a fresh embedding; True when it has one afterwards.

    Skips the worker when the stored vector is still valid. Returns False (and
    stores nothing) with semantic off, no embedder, or any worker failure.
    Never raises.
    """
    try:
        if not semantic_enabled():
            return False
        embedder = get_embedder()
        if embedder is None:
            return False
        if has_valid_embedding(store, card, embedder.model_id):
            return True
        return _embed_cards(store, embedder, [card]) == 1
    except Exception as exc:
        logger.debug("[ACTIVE_CARD] embedding failed: %s", type(exc).__name__)
        return False


def _stale(store: EditorialCardStore, cards: Iterable[EditorialCard], model_id: str) -> list[EditorialCard]:
    return [card for card in cards if not has_valid_embedding(store, card, model_id)]


def needs_backfill(store: EditorialCardStore, cards: Iterable[EditorialCard]) -> bool:
    """True when some of *cards* lack a valid vector and an embedder exists."""
    try:
        if not semantic_enabled():
            return False
        embedder = get_embedder()
        return embedder is not None and bool(_stale(store, cards, embedder.model_id))
    except Exception:
        return False


def backfill(store: EditorialCardStore, cards: Iterable[EditorialCard]) -> int:
    """Embed those of *cards* without a valid vector in one batch; returns how many.

    Meant for a background thread, never the turn path. Never raises.
    """
    try:
        if not semantic_enabled():
            return 0
        embedder = get_embedder()
        if embedder is None:
            return 0
        stale = _stale(store, cards, embedder.model_id)
        if not stale:
            return 0
        return _embed_cards(store, embedder, stale)
    except Exception as exc:
        logger.debug("[ACTIVE_CARD] backfill failed: %s", type(exc).__name__)
        return 0


def _default_scheduler(job: Callable[[], Any]) -> threading.Thread:
    thread = threading.Thread(target=job, name="active-card-backfill", daemon=True)
    thread.start()
    return thread


def schedule_backfill(
    store: EditorialCardStore,
    *,
    scheduler: Optional[Callable[[Callable[[], Any]], Any]] = None,
) -> bool:
    """Start one background backfill of the active cards lacking a vector.

    Returns True when a job was scheduled; False when semantic is off, there is
    nothing to embed, or a job for this store is already in flight. Cheap enough
    for the turn path: it never calls the worker on the caller's thread.
    """
    try:
        if not semantic_enabled():
            return False
        key = str(store.db_path)
        active = store.list_active()
        if not needs_backfill(store, active):
            return False
        with _inflight_lock:
            if key in _inflight:
                return False
            _inflight.add(key)
    except Exception:
        return False

    def _job() -> None:
        try:
            backfill(store, store.list_active())
        except Exception as exc:
            logger.debug("[ACTIVE_CARD] backfill job failed: %s", type(exc).__name__)
        finally:
            with _inflight_lock:
                _inflight.discard(key)

    try:
        (scheduler or _default_scheduler)(_job)
    except Exception:
        with _inflight_lock:
            _inflight.discard(key)
        return False
    return True
