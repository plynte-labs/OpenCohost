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


# ---------------------------------------------------------------------------
# Slice 2: list_armed, rearm, disable, delete
# ---------------------------------------------------------------------------

def _make_store_card(store: EditorialCardStore, topic: str) -> "EditorialCard":
    from opencohost.core.editorial_cards import EditorialCard
    return store.upsert(EditorialCard(
        topic=topic,
        summary="Summary for test card.",
        streamer_take="My take on this topic.",
    ))


def test_list_armed_returns_only_armed_non_expired_cards(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    from datetime import timedelta

    draft = _make_store_card(store, "Draft Topic Card")
    armed = _make_store_card(store, "Armed Topic Card")
    store.arm(armed.id)

    expired_card = _make_store_card(store, "Expired Topic Card")
    store.arm(expired_card.id)
    # Manually set expires_at to the past via upsert trick
    from opencohost.core.editorial_cards import EditorialCard, EditorialCardStatus
    with store._connect() as conn:
        conn.execute(
            "UPDATE editorial_cards SET expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", expired_card.id),
        )

    result = store.list_armed()
    ids = [c.id for c in result]
    assert armed.id in ids
    assert draft.id not in ids
    assert expired_card.id not in ids


def test_list_armed_empty_when_no_armed_cards(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    _make_store_card(store, "Draft Card Only")
    assert store.list_armed() == []


def test_rearm_moves_used_card_back_to_armed(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    card = _make_store_card(store, "Rearm Test Used")
    store.arm(card.id)
    store.activate_for_topic(card.topic_slug)
    store.mark_used(card.id)

    assert store.get(card.id).status is EditorialCardStatus.USED
    result = store.rearm(card.id)
    assert result is True
    assert store.get(card.id).status is EditorialCardStatus.ARMED


def test_rearm_moves_expired_card_back_to_armed(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    card = _make_store_card(store, "Rearm Test Expired")
    store.arm(card.id)
    # Force expired
    with store._connect() as conn:
        conn.execute(
            "UPDATE editorial_cards SET status = 'expired' WHERE id = ?",
            (card.id,),
        )

    result = store.rearm(card.id)
    assert result is True
    assert store.get(card.id).status is EditorialCardStatus.ARMED


def test_rearm_clear_expiry_nulls_expires_at(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    card = _make_store_card(store, "Rearm Clear Expiry")
    store.arm(card.id)
    with store._connect() as conn:
        conn.execute(
            "UPDATE editorial_cards SET status = 'expired', expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (card.id,),
        )

    result = store.rearm(card.id, clear_expiry=True)
    assert result is True
    refreshed = store.get(card.id)
    assert refreshed.status is EditorialCardStatus.ARMED
    assert refreshed.expires_at is None


def test_rearm_draft_card_returns_false(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    card = _make_store_card(store, "Rearm Draft Test")
    # DRAFT -> rearm should return False (not eligible)
    result = store.rearm(card.id)
    assert result is False


def test_rearm_missing_card_returns_false(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    assert store.rearm("ec_doesnotexist") is False


# Fix 3: rearm on expired-without-clear-expiry returns False, status unchanged

def test_rearm_expired_without_clear_expiry_returns_false(tmp_path) -> None:
    """store.rearm on a card that is EXPIRED and has a past expires_at, without
    clear_expiry=True, must return False and leave status as EXPIRED.

    Rationale: the card would be immediately re-expired on the next list_armed()
    check, so arming it is a no-op that misrepresents the card's state.
    """
    from datetime import timedelta

    store = EditorialCardStore(tmp_path / "cards.db")
    card = _make_store_card(store, "Expired No Clear Expiry")
    store.arm(card.id)
    # Force an already-past expiry date and EXPIRED status
    with store._connect() as conn:
        conn.execute(
            "UPDATE editorial_cards SET status = 'expired', expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (card.id,),
        )

    result = store.rearm(card.id, clear_expiry=False)
    assert result is False, "rearm without clear_expiry on past-expires_at card must return False"
    refreshed = store.get(card.id)
    assert refreshed.status is EditorialCardStatus.EXPIRED, "status must remain EXPIRED"


def test_rearm_expired_with_clear_expiry_arms_and_nulls_expires_at(tmp_path) -> None:
    """store.rearm with clear_expiry=True must set ARMED and expires_at=None."""
    store = EditorialCardStore(tmp_path / "cards.db")
    card = _make_store_card(store, "Expired With Clear Expiry")
    store.arm(card.id)
    with store._connect() as conn:
        conn.execute(
            "UPDATE editorial_cards SET status = 'expired', expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
            (card.id,),
        )

    result = store.rearm(card.id, clear_expiry=True)
    assert result is True, "rearm with clear_expiry=True must succeed"
    refreshed = store.get(card.id)
    assert refreshed.status is EditorialCardStatus.ARMED
    assert refreshed.expires_at is None


def test_disable_moves_armed_card_to_expired(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    card = _make_store_card(store, "Disable Armed Card")
    store.arm(card.id)

    result = store.disable(card.id)
    assert result is True
    assert store.get(card.id).status is EditorialCardStatus.EXPIRED


def test_disable_moves_draft_card_to_expired(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    card = _make_store_card(store, "Disable Draft Card")
    # DRAFT -> disable is valid
    result = store.disable(card.id)
    assert result is True
    assert store.get(card.id).status is EditorialCardStatus.EXPIRED


def test_disable_used_card_returns_false(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    card = _make_store_card(store, "Disable Used Card Test")
    store.arm(card.id)
    store.activate_for_topic(card.topic_slug)
    store.mark_used(card.id)

    result = store.disable(card.id)
    assert result is False
    assert store.get(card.id).status is EditorialCardStatus.USED


def test_disable_already_expired_is_idempotent(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    card = _make_store_card(store, "Disable Idempotent Test")
    store.arm(card.id)
    store.disable(card.id)

    result = store.disable(card.id)
    assert result is True  # idempotent
    assert store.get(card.id).status is EditorialCardStatus.EXPIRED


def test_disable_missing_card_returns_false(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    assert store.disable("ec_doesnotexist") is False


def test_delete_removes_card_from_store(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    card = _make_store_card(store, "Delete Me Card")

    result = store.delete(card.id)
    assert result is True
    assert store.get(card.id) is None


def test_delete_removes_associated_ratings(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    card = _make_store_card(store, "Delete With Ratings")
    from opencohost.core.editorial_cards import EditorialCardRating, EditorialCardRatingValue
    store.record_rating(EditorialCardRating(
        card_id=card.id,
        rating=EditorialCardRatingValue.USEFUL,
    ))
    assert len(store.ratings_for_card(card.id)) == 1

    store.delete(card.id)
    # Card gone, ratings gone
    assert store.get(card.id) is None
    # ratings_for_card on unknown card should return empty
    assert store.ratings_for_card(card.id) == []


def test_delete_missing_card_returns_false(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    assert store.delete("ec_doesnotexist") is False


# ---------------------------------------------------------------------------
# Slice 2: AgendaTopic has editorial_attach_attempted field
# ---------------------------------------------------------------------------

def test_agenda_topic_has_editorial_attach_attempted_field() -> None:
    from opencohost.smart_aggregator.kira_agenda_controller import AgendaTopic
    topic = AgendaTopic(title="Test Topic")
    assert hasattr(topic, "editorial_attach_attempted")
    assert topic.editorial_attach_attempted is False


# ---------------------------------------------------------------------------
# Slice 2: KiraAgendaController has set_auto_attach_provider
# ---------------------------------------------------------------------------

def test_controller_has_set_auto_attach_provider() -> None:
    from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController
    controller = KiraAgendaController()
    assert hasattr(controller, "set_auto_attach_provider")
    assert hasattr(controller, "_auto_attach_provider")
    assert controller._auto_attach_provider is None


def test_set_auto_attach_provider_stores_callback() -> None:
    from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController
    controller = KiraAgendaController()
    sentinel = object()
    controller.set_auto_attach_provider(sentinel)
    assert controller._auto_attach_provider is sentinel


# ---------------------------------------------------------------------------
# Slice 2: bridge has auto_attach and register_provider wires it
# ---------------------------------------------------------------------------

def test_bridge_has_auto_attach_method(tmp_path) -> None:
    from opencohost.core.editorial_agenda_bridge import EditorialAgendaBridge
    from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController
    store = EditorialCardStore(tmp_path / "cards.db")
    controller = KiraAgendaController()
    bridge = EditorialAgendaBridge(store, controller)
    assert hasattr(bridge, "auto_attach")


def test_register_provider_wires_auto_attach(tmp_path) -> None:
    from opencohost.core.editorial_agenda_bridge import EditorialAgendaBridge
    from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController
    store = EditorialCardStore(tmp_path / "cards.db")
    controller = KiraAgendaController()
    bridge = EditorialAgendaBridge(store, controller)
    bridge.register_provider()
    # Verify the provider is the bridge's auto_attach method (compare by __func__ identity)
    assert controller._auto_attach_provider is not None
    assert controller._auto_attach_provider.__func__ is bridge.auto_attach.__func__
    assert controller._auto_attach_provider.__self__ is bridge


def test_auto_attach_returns_false_when_no_armed_cards(tmp_path) -> None:
    from opencohost.core.editorial_agenda_bridge import EditorialAgendaBridge
    from opencohost.smart_aggregator.kira_agenda_controller import AgendaTopic, KiraAgendaController
    store = EditorialCardStore(tmp_path / "cards.db")
    controller = KiraAgendaController()
    bridge = EditorialAgendaBridge(store, controller)
    topic = AgendaTopic(title="GTA retraso confirmado")
    result = bridge.auto_attach(topic)
    assert result is False


def test_auto_attach_attaches_matching_card(tmp_path) -> None:
    from opencohost.core.editorial_agenda_bridge import EditorialAgendaBridge
    from opencohost.smart_aggregator.kira_agenda_controller import AgendaTopic, KiraAgendaController
    store = EditorialCardStore(tmp_path / "cards.db")
    controller = KiraAgendaController()
    bridge = EditorialAgendaBridge(store, controller)
    bridge.register_provider()

    # Create + arm a matching card
    card = store.upsert(EditorialCard(
        topic="GTA retraso",
        summary="El retraso de GTA se confirmo oficialmente.",
        streamer_take="Es una decision acertada para la calidad del juego.",
    ))
    store.arm(card.id)

    # Add topic to controller so link_card_to_topic can find it
    ctrl_topic = controller.add_topic("GTA retraso confirmado noticias", approved=True)
    controller.queue_topic(ctrl_topic.id)

    result = bridge.auto_attach(ctrl_topic)
    assert result is True
    # Card should now be ACTIVE
    assert store.get(card.id).status is EditorialCardStatus.ACTIVE


def test_auto_attach_returns_false_on_corrupt_store(tmp_path) -> None:
    """auto_attach must be fail-open: exception -> False, no crash."""
    from opencohost.core.editorial_agenda_bridge import EditorialAgendaBridge
    from opencohost.smart_aggregator.kira_agenda_controller import AgendaTopic, KiraAgendaController

    # Write garbage to the DB file so sqlite3 fails
    corrupt_db = tmp_path / "corrupt.db"
    corrupt_db.write_bytes(b"not a sqlite database at all")

    store = EditorialCardStore.__new__(EditorialCardStore)
    store.db_path = corrupt_db

    controller = KiraAgendaController()
    bridge = EditorialAgendaBridge(store, controller)
    topic = AgendaTopic(title="GTA retraso confirmado")
    result = bridge.auto_attach(topic)
    assert result is False


# ---------------------------------------------------------------------------
# WU1: reusable-by-default (single_use), last_injected_at, record_injection,
# in_cooldown, migration + upsert semantics (design D1, D4, D5)
# ---------------------------------------------------------------------------

def test_editorial_card_defaults_to_reusable() -> None:
    """D1: a bare card is reusable by default and has never been injected."""
    card = EditorialCard(
        topic="Default reuse", summary="Summary here.", streamer_take="Take here."
    )
    assert card.single_use is False
    assert card.last_injected_at is None


def test_init_db_migrates_legacy_db_adding_new_columns(tmp_path) -> None:
    """D5: a pre-existing DB missing single_use/last_injected_at is migrated
    idempotently; existing rows land reusable (column default) with no history."""
    import sqlite3

    db = tmp_path / "legacy.db"
    # Build a legacy editorial_cards table WITHOUT the two new columns.
    with sqlite3.connect(db) as conn:
        conn.execute(
            """
            CREATE TABLE editorial_cards (
                id TEXT PRIMARY KEY,
                topic_slug TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                topic TEXT NOT NULL,
                summary TEXT NOT NULL,
                streamer_take TEXT NOT NULL,
                counterpoints_json TEXT NOT NULL,
                discussion_hooks_json TEXT NOT NULL,
                triggers_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                expires_at TEXT,
                last_used_at TEXT,
                use_count INTEGER NOT NULL DEFAULT 0,
                origin TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            "INSERT INTO editorial_cards (id, topic_slug, status, topic, summary, "
            "streamer_take, counterpoints_json, discussion_hooks_json, triggers_json, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                "ec_legacy", "legacy-topic", "armed", "Legacy topic", "Summary",
                "Take", "[]", "[]", "[]",
                "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00",
            ),
        )

    # Constructing the store triggers the idempotent migration.
    store = EditorialCardStore(db)
    with store._connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(editorial_cards)")}
    assert "single_use" in columns
    assert "last_injected_at" in columns

    card = store.get("ec_legacy")
    assert card is not None
    assert card.single_use is False  # existing rows land reusable by default
    assert card.last_injected_at is None

    # Idempotent: a second construction over the migrated DB must not error.
    EditorialCardStore(db)


def test_upsert_takes_single_use_from_incoming_and_preserves_last_injected_at(tmp_path) -> None:
    """D5: single_use is content-like (from the incoming card); last_injected_at
    is usage history and is preserved on re-upsert (like use_count/last_used_at)."""
    store = EditorialCardStore(tmp_path / "cards.db")
    first = store.upsert(
        EditorialCard(
            topic="Reuse me",
            summary="Summary here.",
            streamer_take="Take here.",
            single_use=True,
        )
    )
    assert store.get(first.id).single_use is True

    # Record an injection so last_injected_at becomes non-null usage history.
    assert store.record_injection(first.id) is True
    injected_at = store.get(first.id).last_injected_at
    assert injected_at is not None

    updated = store.upsert(
        EditorialCard(
            topic="Reuse me",
            summary="Updated summary.",
            streamer_take="Updated take.",
            single_use=False,
        )
    )
    assert updated.id == first.id
    refreshed = store.get(first.id)
    assert refreshed.single_use is False  # taken from incoming card
    assert refreshed.last_injected_at == injected_at  # preserved history


def test_record_injection_sets_last_injected_at_bumps_use_count_and_keeps_status(tmp_path) -> None:
    """D2: record_injection sets last_injected_at, +1 use_count, never touches status."""
    store = EditorialCardStore(tmp_path / "cards.db")
    card = store.upsert(
        EditorialCard(topic="Injectable", summary="Summary.", streamer_take="Take.")
    )
    store.arm(card.id)
    assert store.get(card.id).status is EditorialCardStatus.ARMED

    before = store.get(card.id)
    assert before.last_injected_at is None
    assert before.use_count == 0

    assert store.record_injection(card.id) is True
    after = store.get(card.id)
    assert after.last_injected_at is not None
    assert after.use_count == 1
    assert after.status is EditorialCardStatus.ARMED  # reusable card stays eligible

    assert store.record_injection(card.id) is True
    assert store.get(card.id).use_count == 2

    assert store.record_injection("ec_missing") is False


def test_in_cooldown_true_within_window_false_outside_and_when_never_injected() -> None:
    """D4: in_cooldown is True only while last_injected_at is inside the window."""
    now = datetime.now(timezone.utc)
    card = EditorialCard(topic="Cool", summary="Summary.", streamer_take="Take.")

    assert card.in_cooldown(now, 300) is False  # never injected

    card.last_injected_at = now - timedelta(seconds=10)
    assert card.in_cooldown(now, 300) is True

    card.last_injected_at = now - timedelta(seconds=600)
    assert card.in_cooldown(now, 300) is False
