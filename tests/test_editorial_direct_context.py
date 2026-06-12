"""Tests for the editorial direct-host-mode context injection (Part 2).

STRICT TDD: these tests were written BEFORE the implementation.

Coverage:
  1. trigger_match_returns_block_card_stays_armed
  2. no_match_returns_none
  3. chat_origin_no_injection  (engine-level gate)
  4. direct_source_injects_block  (engine-level)
  5. corrupt_store_returns_none  (fail-open)
  6. ambiguous_cards_returns_none
  7. integration_direct_flow_has_block  (end-to-end with mocked ollama)
  8. integration_chat_flow_no_block
"""

from __future__ import annotations

import json
import os
import queue
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from opencohost.core.editorial_cards import EditorialCard, EditorialCardStatus, EditorialCardStore
from opencohost.core.editorial_agenda_bridge import EditorialAgendaBridge
from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_store(tmp_path: Path) -> EditorialCardStore:
    return EditorialCardStore(tmp_path / "cards.db")


def _arm_card(store: EditorialCardStore, topic: str, triggers: list[str] | None = None) -> EditorialCard:
    card = store.upsert(EditorialCard(
        topic=topic,
        summary=f"Summary for {topic}.",
        streamer_take=f"Streamer take on {topic}.",
        triggers=triggers or [],
    ))
    store.arm(card.id)
    return store.get(card.id)  # return refreshed card with ARMED status


def _make_bridge(tmp_path: Path) -> tuple[EditorialCardStore, EditorialAgendaBridge]:
    store = _make_store(tmp_path)
    controller = KiraAgendaController()
    bridge = EditorialAgendaBridge(store, controller)
    bridge.register_provider()
    return store, bridge


def _make_motor(*, tmp_dir: str | None = None):
    """Create a MotorVocalIA with mocked dependencies.

    Mirrors the pattern from tests/test_heavy_model_inference_recovery.py.
    """
    import opencohost.config.settings as settings

    original_last_model_file = settings.LAST_MODEL_FILE
    if tmp_dir is not None:
        settings.LAST_MODEL_FILE = os.path.join(tmp_dir, "last_model.json")

    log_q: queue.Queue = queue.Queue()
    ui_events: list = []

    def ui_callback(event):
        ui_events.append(event)

    try:
        from opencohost.core.llm_engine import MotorVocalIA
        motor = MotorVocalIA(log_q, ui_callback)
    finally:
        if tmp_dir is not None:
            settings.LAST_MODEL_FILE = original_last_model_file

    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    return motor, log_q, ui_events


# ---------------------------------------------------------------------------
# Test 1: trigger match returns block; card stays ARMED
# ---------------------------------------------------------------------------

def test_trigger_match_returns_block_card_stays_armed(tmp_path: Path) -> None:
    """resolve_direct_context returns an <editorial_context> block and leaves the card ARMED."""
    store, bridge = _make_bridge(tmp_path)
    _arm_card(store, "GTA delay confirmed", triggers=["gta 6", "delay"])

    result = bridge.resolve_direct_context("hey kira what do you think about the gta delay")

    assert result is not None
    assert "<editorial_context>" in result

    # Card must still be ARMED — non-consuming
    armed = store.list_armed()
    assert len(armed) == 1
    assert armed[0].status is EditorialCardStatus.ARMED


# ---------------------------------------------------------------------------
# Test 2: no match returns None
# ---------------------------------------------------------------------------

def test_no_match_returns_none(tmp_path: Path) -> None:
    """When no card matches the query, resolve_direct_context returns None."""
    store, bridge = _make_bridge(tmp_path)
    _arm_card(store, "Minecraft speedrun", triggers=["speedrun"])

    result = bridge.resolve_direct_context("kira what do you think about the weather today")

    assert result is None


# ---------------------------------------------------------------------------
# Test 3: chat origin does NOT inject (engine-level gate)
# ---------------------------------------------------------------------------

def test_chat_origin_no_injection(tmp_path: Path) -> None:
    """_generar_dialogo with source='chat' must NOT inject <editorial_context>."""
    motor, _, _ = _make_motor(tmp_dir=str(tmp_path))

    # Wire a provider that always returns a block
    provider = MagicMock(return_value="<editorial_context>\n{}\n</editorial_context>")
    motor.direct_editorial_context_provider = provider

    # Capture messages sent to ollama
    captured_messages: list = []

    def fake_ollama_chat_fn(**kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        return {"message": {"content": "ok"}}

    motor._ollama_chat = MagicMock(side_effect=fake_ollama_chat_fn)

    motor._generar_dialogo("some chat context", source="chat")

    # Provider must NOT have been called
    provider.assert_not_called()
    # No editorial_context in any message
    all_content = " ".join(m.get("content", "") for m in captured_messages)
    assert "<editorial_context>" not in all_content


# ---------------------------------------------------------------------------
# Test 4: direct source injects block
# ---------------------------------------------------------------------------

def test_direct_source_injects_block(tmp_path: Path) -> None:
    """_generar_dialogo with source='direct' and a matching card injects <editorial_context>."""
    motor, _, _ = _make_motor(tmp_dir=str(tmp_path))

    block = "<editorial_context>\n{\"topic\":\"test\"}\n</editorial_context>"
    provider = MagicMock(return_value=block)
    motor.direct_editorial_context_provider = provider

    captured_messages: list = []

    def fake_ollama_chat_fn(**kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        return {"message": {"content": "nice"}}

    motor._ollama_chat = MagicMock(side_effect=fake_ollama_chat_fn)

    motor._generar_dialogo("host query about the topic", source="direct")

    provider.assert_called_once_with("host query about the topic")
    all_content = " ".join(m.get("content", "") for m in captured_messages)
    assert "<editorial_context>" in all_content


# ---------------------------------------------------------------------------
# Test 5: corrupt store returns None (fail-open)
# ---------------------------------------------------------------------------

def test_corrupt_store_returns_none(tmp_path: Path) -> None:
    """resolve_direct_context returns None when the store raises an exception."""
    store, bridge = _make_bridge(tmp_path)

    # Corrupt the store by patching list_armed to raise
    with patch.object(store, "list_armed", side_effect=RuntimeError("DB corrupt")):
        result = bridge.resolve_direct_context("any query at all")

    assert result is None


# ---------------------------------------------------------------------------
# Test 6: ambiguous cards return None
# ---------------------------------------------------------------------------

def test_ambiguous_cards_returns_none(tmp_path: Path) -> None:
    """When two cards share the same trigger, select_card returns None and so does resolve_direct_context."""
    store, bridge = _make_bridge(tmp_path)

    # Two cards that both match via a shared trigger token
    _arm_card(store, "GTA release date news", triggers=["gta"])
    _arm_card(store, "GTA multiplayer beta", triggers=["gta"])

    # Both cards share "gta" trigger, so select_card sees two equal-score candidates
    result = bridge.resolve_direct_context("kira what about gta")

    assert result is None


# ---------------------------------------------------------------------------
# Test 7: integration — direct flow has block (end-to-end with mocked ollama)
# ---------------------------------------------------------------------------

def test_integration_direct_flow_has_block(tmp_path: Path) -> None:
    """Full integration: motor with real bridge/store, direct source injects editorial_context."""
    store, bridge = _make_bridge(tmp_path)
    _arm_card(store, "GTA delay confirmed", triggers=["gta", "delay"])

    motor, _, _ = _make_motor(tmp_dir=str(tmp_path))
    motor.direct_editorial_context_provider = bridge.resolve_direct_context

    captured_messages: list = []

    def fake_ollama_chat_fn(**kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        return {"message": {"content": "sure thing"}}

    motor._ollama_chat = MagicMock(side_effect=fake_ollama_chat_fn)

    motor._generar_dialogo("kira what do you think about the gta delay", source="direct")

    all_content = " ".join(m.get("content", "") for m in captured_messages)
    assert "<editorial_context>" in all_content

    # Card must remain ARMED — non-consuming
    armed = store.list_armed()
    assert len(armed) == 1
    assert armed[0].status is EditorialCardStatus.ARMED


# ---------------------------------------------------------------------------
# Test 8: integration — chat flow has NO block
# ---------------------------------------------------------------------------

def test_integration_chat_flow_no_block(tmp_path: Path) -> None:
    """Full integration: motor with real bridge/store, chat source does NOT inject."""
    store, bridge = _make_bridge(tmp_path)
    _arm_card(store, "GTA delay confirmed", triggers=["gta", "delay"])

    motor, _, _ = _make_motor(tmp_dir=str(tmp_path))
    motor.direct_editorial_context_provider = bridge.resolve_direct_context

    captured_messages: list = []

    def fake_ollama_chat_fn(**kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        return {"message": {"content": "no context here"}}

    motor._ollama_chat = MagicMock(side_effect=fake_ollama_chat_fn)

    motor._generar_dialogo("kira what do you think about the gta delay", source="chat")

    all_content = " ".join(m.get("content", "") for m in captured_messages)
    assert "<editorial_context>" not in all_content
