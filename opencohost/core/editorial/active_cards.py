"""Prompt seam for active cards.

An active card is verified streamer data for the current stream (a sponsor's
price and code, a game's release facts, a rule of the stream). It is NOT
injected on every turn: ``ActiveCardService`` ranks the active cards against the
turn (MiniLM cosine OR the lexical editorial matcher, see
``active_card_retrieval``) and renders an ``<active_card>`` block only for the
top matches. With no active card, or no match, it returns "" so the prompt stays
byte-identical to a build without the feature. Active cards are reusable: no
cooldown, no use counting, no status change.

The block is code-built, trusted text: the card's structured fields plus a
fixed neutral instruction. The link is rendered without its scheme because the
output guard (``no_suspicious_links``) blocks ``https://`` and ``www.`` in
speech. Logs carry card ids and the turn source only, never turn or card text.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from typing import Any, Callable, Optional, Sequence

from opencohost.config import active_card_guard, settings
from opencohost.core.editorial import card_embeddings
from opencohost.core.editorial.active_card_retrieval import rank_active_cards
from opencohost.core.editorial.editorial_cards import EditorialCard, EditorialCardStore

logger = logging.getLogger("OpenCohost")

_INSTRUCTIONS = {
    "es": (
        "Datos verificados por el streamer para este stream: dalos exactamente "
        "tal cual cuando vengan al caso y nunca los contradigas; no inventes "
        "datos. Si preguntan algo que este bloque no cubre, di que no tienes "
        "ese dato (\"no tengo ese dato\"). Nunca digas nada de never_say."
    ),
    "en": (
        "Verified data from the streamer for this stream: state it exactly as "
        "given when it is relevant and never contradict it; do not invent "
        "data. If asked something this block does not cover, say you do not "
        "have that data. Never say anything listed in never_say."
    ),
}

_SCHEME_RE = re.compile(r"^(?:https?://)?(?:www\.)?", re.IGNORECASE)


def _lang_key(lang: str) -> str:
    return "es" if str(lang or "").lower().startswith("es") else "en"


def link_for_speech(link: str) -> str:
    """The link without scheme or ``www.`` so the output guard lets it through."""
    return _SCHEME_RE.sub("", (link or "").strip())


def render_active_block(card: EditorialCard, lang: str = "es") -> str:
    """Render one active card as an ``<active_card>`` block."""
    payload: dict[str, Any] = {"instruction": _INSTRUCTIONS[_lang_key(lang)]}
    payload["topic"] = card.topic
    payload["summary"] = card.summary
    payload["streamer_take"] = card.streamer_take
    if card.must_say:
        payload["must_say"] = card.must_say
    if card.discount_code:
        payload["code"] = card.discount_code
    if card.link:
        payload["link"] = link_for_speech(card.link)
    if card.forbidden_claims:
        payload["never_say"] = card.forbidden_claims
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"<active_card>\n{body}\n</active_card>"


def active_cards(store: EditorialCardStore) -> list[EditorialCard]:
    """The active cards still in force (an expired one drops out)."""
    return [card for card in store.list_active() if not card.is_expired()]


def sync_guard_from_store(store: EditorialCardStore) -> None:
    """Point the output guard at the currently active cards (or clear it).

    A store failure keeps the last known state: dropping enforcement because
    the database hiccuped would let a forbidden phrase through.
    """
    try:
        cards = active_cards(store)
    except Exception as exc:
        logger.warning("[ACTIVE_CARD] guard sync failed: %s", type(exc).__name__)
        return
    active_card_guard.set_cards((card.id, card.forbidden_claims) for card in cards)


class ActiveCardService:
    """Callable ``(text, source, request_model) -> block`` for the assembler."""

    def __init__(
        self,
        store: EditorialCardStore,
        *,
        lang_reader: Callable[[], str],
    ) -> None:
        self._store = store
        self._lang_reader = lang_reader
        self._local = threading.local()
        sync_guard_from_store(store)  # a card active before startup is enforced at once

    def bind_turn_vector(
        self, text: str, vector: Optional[Sequence[float]], *, attempted: bool
    ) -> None:
        """Hand over the turn's already-computed query vector (never logged).

        The engine calls this right before assembling the prompt with the vector
        the memory recall embedded for the same cleaned *text*. ``attempted``
        says whether an embedding was tried this turn: when it was and failed
        (``vector`` None), the service does not retry on the turn path. The
        binding is single-use and thread-local, and ignored when the text the
        service is asked about differs.
        """
        self._local.bound = (text, tuple(vector) if vector is not None else None, bool(attempted))

    def _take_bound(self, text: str) -> Optional[tuple[Optional[tuple[float, ...]], bool]]:
        bound = getattr(self._local, "bound", None)
        self._local.bound = None
        if bound is None or bound[0] != text:
            return None
        return bound[1], bound[2]

    def _semantic_inputs(self, cards: list[EditorialCard], text: str, bound):
        """(vectors by card id, query vector) for the semantic side; ({}, None) = lexical only."""
        if not card_embeddings.semantic_enabled():
            return {}, None
        embedder = card_embeddings.get_embedder()
        if embedder is None:
            return {}, None
        vectors: dict[str, Sequence[float]] = {}
        stale = False
        for card in cards:
            vec = card_embeddings.valid_vector(self._store, card, embedder.model_id)
            if vec is None:
                stale = True
            else:
                vectors[card.id] = vec
        if stale:
            card_embeddings.schedule_backfill(self._store)  # off the turn path
        if not vectors:
            return {}, None  # nothing to compare against: spend no MiniLM call
        if bound is not None:
            bound_vec, attempted = bound
            if bound_vec is not None:
                return vectors, list(bound_vec)  # reuse the vector the turn already has
            if attempted:
                return {}, None  # the turn's embedding was tried and failed: no retry here
        if not text.strip():
            return {}, None
        try:
            query_vec = embedder.embed_query(text, float(settings.ACTIVE_CARD_QUERY_EMBED_TIMEOUT_S))
        except Exception:
            query_vec = None
        return (vectors, list(query_vec)) if query_vec else ({}, None)

    def _sync_guard(self) -> Optional[list[EditorialCard]]:
        """Arm the output guard from the store; None when the store failed.

        Runs first and on its own so enforcement never depends on retrieval or
        rendering succeeding. A store failure keeps the last known state.
        """
        try:
            cards = active_cards(self._store)
        except Exception as exc:
            logger.warning("[ACTIVE_CARD] guard sync failed: %s", type(exc).__name__)
            return None
        active_card_guard.set_cards((card.id, card.forbidden_claims) for card in cards)
        return cards

    def __call__(self, text: str, source: str, request_model: str = "") -> str:
        """The block to append to the system prompt, or "" (never raises)."""
        # Keep the output guard in step with the store on every turn (an
        # activation made out of band is enforced from the next reply), for
        # EVERY active card, whether or not it is injected this turn.
        cards = self._sync_guard()
        if not cards:
            self._local.bound = None
            return ""
        try:
            from opencohost.core.memory.memoria_store import strip_history_wrapper

            clean = strip_history_wrapper(text)
            bound = self._take_bound(clean)
            vectors, query_vec = self._semantic_inputs(cards, clean, bound)
            matches = rank_active_cards(
                clean,
                cards,
                query_vec=query_vec,
                vectors=vectors,
                min_score=float(settings.ACTIVE_CARD_SEMANTIC_MIN_SCORE),
                top_k=int(settings.ACTIVE_CARD_TOP_K),
            )
            if not matches:
                logger.info("[ACTIVE_CARD] none")
                return ""
            lang = self._lang_reader()
            for match in matches:
                logger.info(
                    "[ACTIVE_CARD] matched card=%s via=%s score=%.2f",
                    match.card.id,
                    match.via,
                    match.score,
                )
            return ("\n\n").join(render_active_block(match.card, lang) for match in matches)
        except Exception as exc:
            logger.warning("[ACTIVE_CARD] build failed: %s", type(exc).__name__)
            return ""
