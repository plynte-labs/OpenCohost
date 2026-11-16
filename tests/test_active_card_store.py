"""Active cards: store fields, migration and the active flag (R1)."""

from __future__ import annotations

import sqlite3

import pytest

from opencohost.core.editorial.editorial_cards import (
    EditorialCard,
    EditorialCardStatus,
    EditorialCardStore,
    EditorialCardValidationError,
)


def _card(topic: str = "Acme Mic", **kw) -> EditorialCard:
    return EditorialCard(
        topic=topic,
        summary="Acme Mic is a USB microphone.",
        streamer_take="I use it daily.",
        **kw,
    )


def test_card_fields_default_empty_and_inactive() -> None:
    card = _card()
    assert card.must_say == []
    assert card.forbidden_claims == []
    assert card.discount_code == ""
    assert card.link == ""
    assert card.active is False


def test_sponsor_fields_are_cleaned_and_bounded() -> None:
    card = _card(
        must_say=["  costs   49 USD ", ""],
        forbidden_claims=["best mic ever"],
        discount_code=" KIRA10 ",
        link="acme.example/kira",
    )
    assert card.must_say == ["costs 49 USD"]
    assert card.discount_code == "KIRA10"
    with pytest.raises(EditorialCardValidationError):
        _card(link="x" * 241)
    with pytest.raises(EditorialCardValidationError):
        _card(discount_code="x" * 61)
    with pytest.raises(EditorialCardValidationError):
        _card(forbidden_claims=["y" * 241])


def test_sponsor_fields_round_trip_through_store(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    stored = store.upsert(
        _card(
            must_say=["49 USD"],
            forbidden_claims=["cures anxiety"],
            discount_code="KIRA10",
            link="acme.example/kira",
        )
    )
    loaded = store.get(stored.id)
    assert loaded.must_say == ["49 USD"]
    assert loaded.forbidden_claims == ["cures anxiety"]
    assert loaded.discount_code == "KIRA10"
    assert loaded.link == "acme.example/kira"
    assert loaded.active is False


def test_activate_deactivate_and_list_active(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    a = store.upsert(_card("A"))
    assert store.list_active() == []
    assert store.activate(a.id) is True
    assert [c.id for c in store.list_active()] == [a.id]
    assert store.deactivate(a.id) is True
    assert store.list_active() == []
    assert store.deactivate(a.id) is False  # not active any more


def test_several_cards_can_be_active_at_once(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    a = store.upsert(_card("A"))
    b = store.upsert(_card("B"))
    store.activate(a.id)
    store.activate(b.id)
    assert {c.id for c in store.list_active()} == {a.id, b.id}
    assert store.get(a.id).active is True and store.get(b.id).active is True


def test_activate_unknown_or_expired_card_refused(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    a = store.upsert(_card("A"))
    assert store.activate("ec_missing") is False
    store.disable(a.id)
    assert store.activate(a.id) is False
    assert store.list_active() == []


def test_activating_never_consumes_the_card(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    a = store.upsert(_card("A", single_use=True))
    store.arm(a.id)
    before = store.get(a.id)
    store.activate(a.id)
    after = store.get(a.id)
    assert after.status is EditorialCardStatus.ARMED
    assert after.use_count == before.use_count
    assert after.last_used_at is None and after.last_injected_at is None


def test_upsert_preserves_active_state(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    a = store.upsert(_card("A"))
    store.activate(a.id)
    store.upsert(_card("A", discount_code="NEW"))
    reloaded = store.get(a.id)
    assert reloaded.active is True
    assert reloaded.discount_code == "NEW"


def test_disable_and_delete_deactivate(tmp_path) -> None:
    store = EditorialCardStore(tmp_path / "cards.db")
    a = store.upsert(_card("A"))
    store.activate(a.id)
    store.disable(a.id)
    assert store.list_active() == []
    b = store.upsert(_card("B"))
    store.activate(b.id)
    store.delete(b.id)
    assert store.list_active() == []


def test_migration_drops_the_one_pinned_index_idempotently(tmp_path) -> None:
    db = tmp_path / "cards.db"
    store = EditorialCardStore(db)
    a = store.upsert(_card("A"))
    b = store.upsert(_card("B"))
    with sqlite3.connect(db) as conn:
        # A DB written by the earlier pinned build still has the partial unique index.
        conn.execute(
            "CREATE UNIQUE INDEX idx_editorial_cards_one_pinned ON editorial_cards(pinned) WHERE pinned = 1"
        )
        conn.execute("UPDATE editorial_cards SET pinned = 1 WHERE id = ?", (a.id,))
    reopened = EditorialCardStore(db)
    EditorialCardStore(db)  # re-open is a no-op
    with sqlite3.connect(db) as conn:
        names = {r[1] for r in conn.execute("PRAGMA index_list(editorial_cards)")}
    assert "idx_editorial_cards_one_pinned" not in names
    assert reopened.get(a.id).active is True  # the old pin is the new active flag
    assert reopened.activate(b.id) is True
    assert {c.id for c in reopened.list_active()} == {a.id, b.id}


def test_migrates_old_schema_db_idempotently(tmp_path) -> None:
    db = tmp_path / "cards.db"
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
                origin TEXT NOT NULL DEFAULT '',
                single_use INTEGER NOT NULL DEFAULT 0,
                last_injected_at TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO editorial_cards VALUES ('ec_old','old','armed','Old','S','T','[]','[]','[]',"
            "'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00',NULL,NULL,2,'',0,NULL)"
        )
    store = EditorialCardStore(db)
    old = store.get("ec_old")
    assert old.summary == "S" and old.use_count == 2
    assert old.active is False and old.must_say == [] and old.link == ""
    EditorialCardStore(db)  # second open is a no-op
    assert store.activate("ec_old") is True
    assert [c.id for c in EditorialCardStore(db).list_active()] == ["ec_old"]
