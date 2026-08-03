"""Integration tests for editorial auto-attach: real store + real controller + real bridge.

Uses actual SQLite DBs under tmp_path.
Topic titles must NOT contain emojis or code-like symbols — KiraAgendaController.sanitize_topic_text
rejects them. All titles here use plain Spanish or English text.
"""

from __future__ import annotations

import pytest

from opencohost.core.editorial.editorial_agenda_bridge import EditorialAgendaBridge
from opencohost.core.editorial.editorial_cards import EditorialCard, EditorialCardStatus, EditorialCardStore
from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_card(
    store: EditorialCardStore,
    topic: str,
    triggers: list[str] | None = None,
    *,
    single_use: bool = False,
) -> EditorialCard:
    card = store.upsert(EditorialCard(
        topic=topic,
        summary=f"Summary about {topic}.",
        streamer_take=f"My take on {topic}.",
        triggers=triggers or [],
        single_use=single_use,
    ))
    return card


def _setup(tmp_path) -> tuple[EditorialCardStore, KiraAgendaController, EditorialAgendaBridge]:
    store = EditorialCardStore(tmp_path / "cards.db")
    controller = KiraAgendaController()
    bridge = EditorialAgendaBridge(store, controller)
    bridge.register_provider()
    return store, controller, bridge


# ---------------------------------------------------------------------------
# Test 1: Auto-attach injects editorial context when topic matches an armed card
# ---------------------------------------------------------------------------

def test_auto_attach_matching_topic_injects_editorial_context(tmp_path) -> None:
    store, controller, bridge = _setup(tmp_path)

    # Create and arm a card with strong token overlap to the topic
    card = _make_card(store, "GTA retraso confirmado")
    store.arm(card.id)

    # Add and queue a matching topic (Spanish title, no emojis)
    topic = controller.add_topic("El retraso de GTA confirmado noticias", approved=True)
    controller.queue_topic(topic.id)

    controller.enable()
    action = controller.next_action()

    assert action.kind == "enqueue"
    assert "<editorial_context>" in action.prompt
    # Card should now be ACTIVE (after auto-attach -> link_card_to_topic)
    assert store.get(card.id).status is EditorialCardStatus.ACTIVE


# ---------------------------------------------------------------------------
# Test 2: mark_used_after_successful_generation transitions card to USED
# ---------------------------------------------------------------------------

def test_mark_used_after_generation(tmp_path) -> None:
    store, controller, bridge = _setup(tmp_path)

    # D3/D7 pin: only a single_use card retires to USED via the recorder path.
    card = _make_card(store, "GTA retraso confirmado", single_use=True)
    store.arm(card.id)

    topic = controller.add_topic("El retraso de GTA confirmado noticias", approved=True)
    controller.queue_topic(topic.id)

    controller.enable()
    controller.next_action()

    # At this point editorial_card_consumed should be True
    # Simulate post-generation mark-used
    result = bridge.mark_used_after_successful_generation()
    assert result is True
    assert store.get(card.id).status is EditorialCardStatus.USED


# ---------------------------------------------------------------------------
# Test 3: Non-matching topic produces no editorial context
# ---------------------------------------------------------------------------

def test_non_matching_topic_no_injection(tmp_path) -> None:
    store, controller, bridge = _setup(tmp_path)

    # Card about GTA
    card = _make_card(store, "GTA retraso")
    store.arm(card.id)

    # Completely unrelated topic
    topic = controller.add_topic("Minecraft construccion supervivencia", approved=True)
    controller.queue_topic(topic.id)

    controller.enable()
    action = controller.next_action()

    assert action.kind == "enqueue"
    assert "<editorial_context>" not in action.prompt


# ---------------------------------------------------------------------------
# Test 4: auto-attach attempted only once per topic
# ---------------------------------------------------------------------------

def test_attach_attempted_only_once(tmp_path) -> None:
    store, controller, bridge = _setup(tmp_path)

    # Non-matching card so auto_attach returns False
    card = _make_card(store, "Minecraft construccion")
    store.arm(card.id)

    topic = controller.add_topic("GTA retraso noticias confirmadas", approved=True)
    controller.queue_topic(topic.id)

    controller.enable()
    action = controller.next_action()

    # After first next_action(), editorial_attach_attempted must be True
    assert topic.editorial_attach_attempted is True

    # Track auto_attach call count using a wrapper
    call_count = [0]
    original_auto_attach = bridge.auto_attach

    def counting_auto_attach(t):
        call_count[0] += 1
        return original_auto_attach(t)

    controller.set_auto_attach_provider(counting_auto_attach)

    # Call next_action again on the same topic (not yet exhausted)
    # editorial_attach_attempted is True -> should NOT call auto_attach again
    controller.next_action()
    assert call_count[0] == 0, "auto_attach should not be called again once attempted"


# ---------------------------------------------------------------------------
# Test 5: Ambiguous catalog -> no editorial context injected
# ---------------------------------------------------------------------------

def test_ambiguous_catalog_no_injection(tmp_path) -> None:
    store, controller, bridge = _setup(tmp_path)

    # Two cards with nearly identical tokens relative to topic
    card1 = _make_card(store, "GTA retraso anuncio", triggers=["gta retraso"])
    card2 = _make_card(store, "GTA retraso confirmado", triggers=["gta retraso"])
    store.arm(card1.id)
    store.arm(card2.id)

    topic = controller.add_topic("GTA retraso noticias importantes", approved=True)
    controller.queue_topic(topic.id)

    controller.enable()
    action = controller.next_action()

    assert action.kind == "enqueue"
    assert "<editorial_context>" not in action.prompt


# ---------------------------------------------------------------------------
# Test 6: Corrupt DB -> auto_attach fails gracefully, next_action still works
# ---------------------------------------------------------------------------

def test_corrupt_db_agenda_still_works(tmp_path) -> None:
    corrupt_db = tmp_path / "corrupt.db"
    corrupt_db.write_bytes(b"not a sqlite database at all")

    # Real controller + corrupt store
    controller = KiraAgendaController()
    bad_store = EditorialCardStore.__new__(EditorialCardStore)
    bad_store.db_path = corrupt_db

    bridge = EditorialAgendaBridge(bad_store, controller)
    bridge.register_provider()

    topic = controller.add_topic("GTA retraso confirmado noticias", approved=True)
    controller.queue_topic(topic.id)

    controller.enable()
    # Should not raise; auto_attach catches the exception and returns False
    action = controller.next_action()
    assert action.kind == "enqueue"
    assert "<editorial_context>" not in action.prompt


# ---------------------------------------------------------------------------
# WU2/D4: cooldown filter excludes a recently-injected card from auto-attach
# ---------------------------------------------------------------------------

def test_auto_attach_skips_card_in_cooldown(tmp_path) -> None:
    """D4: a reusable card injected within EDITORIAL_REUSE_COOLDOWN_SECONDS is
    filtered from the auto-attach eligible set, so no editorial context is
    injected for a matching topic and the card stays ARMED (never activated)."""
    store, controller, bridge = _setup(tmp_path)

    card = _make_card(store, "GTA retraso confirmado")
    store.arm(card.id)
    store.record_injection(card.id)  # last_injected_at = now -> in cooldown

    topic = controller.add_topic("El retraso de GTA confirmado noticias", approved=True)
    controller.queue_topic(topic.id)

    controller.enable()
    action = controller.next_action()

    assert action.kind == "enqueue"
    assert "<editorial_context>" not in action.prompt
    assert store.get(card.id).status is EditorialCardStatus.ARMED


# ---------------------------------------------------------------------------
# Finding 4: cooldown-expiry re-admission — the sibling of the exclusion test
# above. Seeds a deterministic OLD last_injected_at (no sleeping) at exactly
# EDITORIAL_REUSE_COOLDOWN_SECONDS ago; in_cooldown uses strict '<', so a card
# injected exactly (or more than) cooldown_s ago must be re-admitted by BOTH
# auto_attach and resolve_direct_context.
# ---------------------------------------------------------------------------

def _seed_old_injection(store: EditorialCardStore, card_id: str, seconds_ago: float) -> None:
    """Directly set last_injected_at to *seconds_ago* in the past — deterministic,
    no sleep. Mirrors the raw-UPDATE pattern used elsewhere in the test suite
    (e.g. test_editorial_cards.py expiry tests)."""
    from datetime import datetime, timedelta, timezone

    old_ts = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    with store._connect() as conn:
        conn.execute(
            "UPDATE editorial_cards SET last_injected_at = ? WHERE id = ?",
            (old_ts.isoformat(), card_id),
        )


def test_auto_attach_readmits_card_at_cooldown_boundary(tmp_path) -> None:
    """D4 boundary: a reusable card last injected exactly
    EDITORIAL_REUSE_COOLDOWN_SECONDS ago is no longer in_cooldown (strict '<'),
    so auto_attach re-admits it and links it to a matching topic."""
    import opencohost.config.settings as settings

    store, controller, bridge = _setup(tmp_path)

    card = _make_card(store, "GTA retraso confirmado")
    store.arm(card.id)
    _seed_old_injection(store, card.id, settings.EDITORIAL_REUSE_COOLDOWN_SECONDS)

    topic = controller.add_topic("El retraso de GTA confirmado noticias", approved=True)
    controller.queue_topic(topic.id)

    controller.enable()
    action = controller.next_action()

    assert action.kind == "enqueue"
    assert "<editorial_context>" in action.prompt
    assert store.get(card.id).status is EditorialCardStatus.ACTIVE


def test_resolve_direct_context_readmits_card_at_cooldown_boundary(tmp_path) -> None:
    """D4 boundary: resolve_direct_context follows the same in_cooldown
    semantics as auto_attach — a card injected exactly cooldown_s ago is
    eligible again for a direct-turn match."""
    import opencohost.config.settings as settings

    store, controller, bridge = _setup(tmp_path)

    card = _make_card(store, "GTA retraso confirmado")
    store.arm(card.id)
    _seed_old_injection(store, card.id, settings.EDITORIAL_REUSE_COOLDOWN_SECONDS)

    result = bridge.resolve_direct_context("El retraso de GTA confirmado")

    assert result is not None
    assert "<editorial_context>" in result
    assert bridge._pending_direct_card_id == card.id
