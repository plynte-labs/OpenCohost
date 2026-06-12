"""Tests for the Editorial Cue Cards MVP store and lifecycle."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from opencohost.core.editorial_cards import (
    EditorialCard,
    EditorialCardRating,
    EditorialCardRatingValue,
    EditorialCardStatus,
    EditorialCardStore,
    EditorialCardValidationError,
)


def test_card_schema_requires_editorial_fields_and_rejects_raw_inputs() -> None:
    with pytest.raises(EditorialCardValidationError):
        EditorialCard(topic="", summary="Resumen", streamer_take="Mi ángulo")

    with pytest.raises(EditorialCardValidationError):
        EditorialCard(
            topic="Parche nuevo",
            summary="Cambios relevantes del parche.",
            streamer_take="Quiero discutir el impacto.",
            raw_chat="usuario: texto crudo",
        )

    card = EditorialCard(
        topic="Parche nuevo",
        summary="Cambios relevantes del parche.",
        streamer_take="Quiero discutir el impacto.",
        counterpoints=["Puede ayudar a jugadores nuevos."],
        discussion_hooks=["¿Esto mejora o simplifica demasiado?"],
        triggers=["parche", "balance"],
    )

    assert card.topic_slug == "parche-nuevo"
    assert card.status is EditorialCardStatus.DRAFT


def test_card_lifecycle_allows_one_active_card_and_prevents_expired_reactivation(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    first = store.upsert(
        EditorialCard(
            topic="Monetización Game X",
            summary="La comunidad critica nuevos precios.",
            streamer_take="Quiero debatir si cruza a pay-to-win.",
        )
    )
    second = store.upsert(
        EditorialCard(
            topic="Notas del parche",
            summary="El parche ajusta balance y progresión.",
            streamer_take="Quiero compararlo con lo que pedía la comunidad.",
        )
    )

    store.arm(first.id)
    store.arm(second.id)
    active = store.activate_for_topic(first.topic_slug)

    assert active.id == first.id
    assert store.get(first.id).status is EditorialCardStatus.ACTIVE
    assert store.activate_for_topic(second.topic_slug) is None

    store.mark_used(first.id)
    assert store.get(first.id).status is EditorialCardStatus.USED
    assert store.get(first.id).use_count == 1
    assert store.activate_for_topic(first.topic_slug) is None

    expired = store.upsert(
        EditorialCard(
            topic="Evento viejo",
            summary="Contexto que ya no sirve.",
            streamer_take="No debería activarse.",
            expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
    )
    assert store.arm(expired.id) is False


def test_duplicate_topic_slug_upserts_and_armed_lookup_is_deterministic(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    original = store.upsert(
        EditorialCard(
            topic="Balance del parche",
            summary="Resumen inicial.",
            streamer_take="Ángulo inicial.",
        )
    )
    updated = store.upsert(
        EditorialCard(
            topic="Balance del parche",
            summary="Resumen actualizado.",
            streamer_take="Ángulo más claro.",
        )
    )

    assert updated.id == original.id
    assert store.get(original.id).summary == "Resumen actualizado."

    store.arm(original.id)
    armed = store.find_armed_by_topic_slug("balance-del-parche")

    assert armed is not None
    assert armed.id == original.id


def test_prompt_block_is_bounded_structured_and_has_no_raw_fields() -> None:
    card = EditorialCard(
        topic="Polémica de monetización",
        summary="La comunidad critica precios nuevos.",
        streamer_take="Quiero debatir si esto cruza la línea de pay-to-win.",
        counterpoints=["El estudio dice que son cosméticos."],
        discussion_hooks=["¿Dónde está la línea justa?"],
    )

    block = card.to_prompt_block(max_chars=900)

    assert block.startswith("<editorial_context>")
    assert block.endswith("</editorial_context>")
    assert "Polémica de monetización" in block
    assert "priorizá claridad" in block
    assert "raw_chat" not in block
    assert "raw_page" not in block
    assert len(block) <= 900


def test_rating_records_store_utility_without_raw_chat(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    card = store.upsert(
        EditorialCard(
            topic="Debate de dificultad",
            summary="La comunidad discute si el juego se volvió más fácil.",
            streamer_take="Quiero llevarlo a diseño y accesibilidad.",
        )
    )

    rating = store.record_rating(
        EditorialCardRating(
            card_id=card.id,
            rating=EditorialCardRatingValue.USEFUL,
            reason_code="opened_discussion",
        )
    )

    assert rating.card_id == card.id
    assert rating.rating is EditorialCardRatingValue.USEFUL
    assert store.ratings_for_card(card.id)[0].reason_code == "opened_discussion"

    with pytest.raises(EditorialCardValidationError):
        EditorialCardRating(
            card_id=card.id,
            rating=EditorialCardRatingValue.NOT_USEFUL,
            raw_chat="usuario: texto crudo",
        )


def test_list_all_returns_empty_for_fresh_db(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    assert store.list_all() == []


def test_list_all_orders_by_updated_at_desc(tmp_path) -> None:
    import time

    store = EditorialCardStore(tmp_path / "cards.db")
    first = store.upsert(
        EditorialCard(
            topic="Topic Alpha",
            summary="First card summary here.",
            streamer_take="My angle on alpha.",
        )
    )
    # Small sleep so updated_at timestamps differ on Windows (1-second resolution)
    time.sleep(1.1)
    second = store.upsert(
        EditorialCard(
            topic="Topic Beta",
            summary="Second card summary here.",
            streamer_take="My angle on beta.",
        )
    )

    cards = store.list_all()
    assert len(cards) == 2
    # Most recently updated first
    assert cards[0].id == second.id
    assert cards[1].id == first.id
