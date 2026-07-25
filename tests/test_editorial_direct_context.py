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
from opencohost.i18n import active as i18n_active
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


# ---------------------------------------------------------------------------
# WU2: pending-id lifecycle, commit_direct_injection, cooldown filter (D2/D4)
# ---------------------------------------------------------------------------

def test_resolve_direct_context_sets_pending_card_id_on_match(tmp_path: Path) -> None:
    """D2: resolve_direct_context records the matched card id on the bridge."""
    store, bridge = _make_bridge(tmp_path)
    card = _arm_card(store, "GTA delay confirmed", triggers=["gta", "delay"])

    result = bridge.resolve_direct_context("hey kira what about the gta delay")

    assert result is not None
    assert bridge._pending_direct_card_id == card.id


def test_resolve_direct_context_clears_pending_when_no_match(tmp_path: Path) -> None:
    """D2: a stale pending id is cleared at entry so a later non-matching turn
    never commits a false USED on the previously matched card."""
    store, bridge = _make_bridge(tmp_path)
    card = _arm_card(store, "GTA delay confirmed", triggers=["gta", "delay"])

    bridge.resolve_direct_context("gta delay")
    assert bridge._pending_direct_card_id == card.id

    result = bridge.resolve_direct_context("what is the weather like today")
    assert result is None
    assert bridge._pending_direct_card_id is None


def test_commit_direct_injection_reusable_records_injection_stays_armed(tmp_path: Path) -> None:
    """D2: committing a reusable pending injection records it and keeps ARMED."""
    store, bridge = _make_bridge(tmp_path)
    card = _arm_card(store, "GTA delay confirmed", triggers=["gta", "delay"])  # reusable default

    bridge.resolve_direct_context("gta delay")
    assert bridge._pending_direct_card_id == card.id

    bridge.commit_direct_injection()

    refreshed = store.get(card.id)
    assert refreshed.status is EditorialCardStatus.ARMED
    assert refreshed.last_injected_at is not None
    assert refreshed.use_count == 1
    assert bridge._pending_direct_card_id is None  # cleared after commit


def test_commit_direct_injection_single_use_marks_used(tmp_path: Path) -> None:
    """D2: committing a single_use pending injection retires the card (→USED)."""
    store, bridge = _make_bridge(tmp_path)
    card = store.upsert(EditorialCard(
        topic="One shot topic",
        summary="Summary here.",
        streamer_take="Take here.",
        triggers=["oneshot"],
        single_use=True,
    ))
    store.arm(card.id)

    bridge.resolve_direct_context("tell me about oneshot")
    assert bridge._pending_direct_card_id == card.id

    bridge.commit_direct_injection()

    assert store.get(card.id).status is EditorialCardStatus.USED


def test_commit_direct_injection_noop_when_no_pending(tmp_path: Path) -> None:
    """D2: with no pending id, commit is a no-op — the card is untouched."""
    store, bridge = _make_bridge(tmp_path)
    card = _arm_card(store, "GTA delay confirmed", triggers=["gta", "delay"])

    bridge.commit_direct_injection()

    refreshed = store.get(card.id)
    assert refreshed.status is EditorialCardStatus.ARMED
    assert refreshed.last_injected_at is None
    assert refreshed.use_count == 0


def test_commit_direct_injection_is_fail_open_on_store_error(tmp_path: Path) -> None:
    """D2: a store failure during commit is swallowed and pending is cleared,
    so a retry cannot double-commit."""
    store, bridge = _make_bridge(tmp_path)
    card = _arm_card(store, "GTA delay confirmed", triggers=["gta", "delay"])

    bridge.resolve_direct_context("gta delay")
    assert bridge._pending_direct_card_id == card.id

    with patch.object(store, "get", side_effect=RuntimeError("DB corrupt")):
        bridge.commit_direct_injection()  # must not raise

    assert bridge._pending_direct_card_id is None


def test_resolve_direct_context_skips_card_in_cooldown(tmp_path: Path) -> None:
    """D4: a card injected within the cooldown window is not eligible, so no
    block is returned and no pending id is set."""
    store, bridge = _make_bridge(tmp_path)
    card = _arm_card(store, "GTA delay confirmed", triggers=["gta", "delay"])
    store.record_injection(card.id)  # last_injected_at = now -> in cooldown

    result = bridge.resolve_direct_context("hey kira what about the gta delay")

    assert result is None
    assert bridge._pending_direct_card_id is None


# ---------------------------------------------------------------------------
# WU3: engine usage-recorder seam (D2) — fires only on non-empty dialogo +
# injected block, fail-open on callback exceptions.
# ---------------------------------------------------------------------------

def test_usage_recorder_fires_once_on_direct_turn_with_block(tmp_path: Path) -> None:
    """D2: direct_editorial_usage_recorder is invoked exactly once, no args,
    when the turn injected a block and produced a non-empty dialogo."""
    motor, _, _ = _make_motor(tmp_dir=str(tmp_path))
    block = "<editorial_context>\n{\"topic\":\"test\"}\n</editorial_context>"
    motor.direct_editorial_context_provider = MagicMock(return_value=block)
    recorder = MagicMock()
    motor.direct_editorial_usage_recorder = recorder
    motor._ollama_chat = MagicMock(side_effect=lambda **kw: {"message": {"content": "nice reply"}})

    motor._generar_dialogo("host query about the topic", source="direct")

    recorder.assert_called_once_with()


def test_usage_recorder_not_fired_when_no_block(tmp_path: Path) -> None:
    """D2: no card matched (block empty) -> the recorder must not fire even
    though the dialogo is non-empty."""
    motor, _, _ = _make_motor(tmp_dir=str(tmp_path))
    motor.direct_editorial_context_provider = MagicMock(return_value=None)
    recorder = MagicMock()
    motor.direct_editorial_usage_recorder = recorder
    motor._ollama_chat = MagicMock(side_effect=lambda **kw: {"message": {"content": "nice reply"}})

    motor._generar_dialogo("host query about the topic", source="direct")

    recorder.assert_not_called()


def test_usage_recorder_not_fired_when_dialogo_empty(tmp_path: Path) -> None:
    """D2: a block was injected but generation produced an empty dialogo ->
    the recorder must not fire (no successful generation to commit)."""
    motor, _, _ = _make_motor(tmp_dir=str(tmp_path))
    block = "<editorial_context>\n{}\n</editorial_context>"
    motor.direct_editorial_context_provider = MagicMock(return_value=block)
    recorder = MagicMock()
    motor.direct_editorial_usage_recorder = recorder
    motor._ollama_chat = MagicMock(side_effect=lambda **kw: {"message": {"content": "   "}})

    motor._generar_dialogo("host query about the topic", source="direct")

    recorder.assert_not_called()


def test_grounding_rules_reach_the_system_content(tmp_path: Path) -> None:
    """grounding_authority_temporal_humility: the grounding/humility rules are
    appended at the SYSTEM position on every generation."""
    motor, _, _ = _make_motor(tmp_dir=str(tmp_path))
    motor.use_system_role = True
    captured: list = []
    motor._ollama_chat = MagicMock(
        side_effect=lambda **kw: (captured.extend(kw.get("messages", [])), {"message": {"content": "ok"}})[1]
    )

    motor._generar_dialogo("hablemos de algo", source="direct")

    assert captured[0]["role"] == "system"
    assert i18n_active.grounding_rules() in captured[0]["content"]


def test_grounding_rules_survive_a_profile_prompt_override(tmp_path: Path) -> None:
    """ROOT CAUSE GUARD: set_profile REPLACES self.system_prompt with the
    profile's own prompt (llm_engine.py `payload.get("prompt", ...)`), so every
    shipped profile (Akira, Comunidad, Show...) bypasses llm.system_prompt
    entirely. The rules must be appended by the engine, never live only inside
    the locale persona — otherwise they are dead text for every profile user."""
    motor, _, _ = _make_motor(tmp_dir=str(tmp_path))
    motor.use_system_role = True
    motor.system_prompt = "AKIRA_PROFILE_PROMPT sin reglas propias"
    captured: list = []
    motor._ollama_chat = MagicMock(
        side_effect=lambda **kw: (captured.extend(kw.get("messages", [])), {"message": {"content": "ok"}})[1]
    )

    motor._generar_dialogo("hablemos de algo", source="direct")

    system_content = captured[0]["content"]
    assert "AKIRA_PROFILE_PROMPT" in system_content
    assert i18n_active.grounding_rules() in system_content
    # Persona first, rules after it — never the other way around.
    assert system_content.index("AKIRA_PROFILE_PROMPT") < system_content.index(
        i18n_active.grounding_rules()
    )


def test_grounding_rules_present_for_chat_and_agenda_sources(tmp_path: Path) -> None:
    """Incident 2 (denying that shipped models exist) is not direct-only, so the
    humility rule is unconditional — unlike the editorial block, which stays
    gated on source == 'direct'."""
    for source in ("chat", "ptt", "agenda"):
        motor, _, _ = _make_motor(tmp_dir=str(tmp_path))
        motor.use_system_role = True
        captured: list = []
        motor._ollama_chat = MagicMock(
            side_effect=lambda **kw: (captured.extend(kw.get("messages", [])), {"message": {"content": "ok"}})[1]
        )

        motor._generar_dialogo("algo", source=source)

        assert i18n_active.grounding_rules() in captured[0]["content"], source


def test_bridge_rendered_block_carries_active_locale_authority(tmp_path: Path) -> None:
    """The bridge (the real runtime renderer for both the direct and agenda
    paths) supplies the active locale's card instruction."""
    store, bridge = _make_bridge(tmp_path)
    _arm_card(store, "Look Outside", triggers=["look outside"])

    block = bridge.resolve_direct_context("kira que sabes de look outside")

    assert block is not None
    assert i18n_active.editorial_card_instruction() in block


def test_usage_recorder_exception_does_not_propagate(tmp_path: Path) -> None:
    """D2: a recorder that raises must be swallowed (fail-open) — the
    generation call still completes and the recorder was invoked once."""
    motor, _, _ = _make_motor(tmp_dir=str(tmp_path))
    block = "<editorial_context>\n{}\n</editorial_context>"
    motor.direct_editorial_context_provider = MagicMock(return_value=block)
    recorder = MagicMock(side_effect=RuntimeError("boom"))
    motor.direct_editorial_usage_recorder = recorder
    motor._ollama_chat = MagicMock(side_effect=lambda **kw: {"message": {"content": "nice reply"}})

    motor._generar_dialogo("host query about the topic", source="direct")  # must not raise

    recorder.assert_called_once_with()


# ---------------------------------------------------------------------------
# F1 regression pin: a voice (ptt) turn must get the SAME prompt enrichments
# as a typed turn — the armed editorial card AND the L1 memory digest.
# ---------------------------------------------------------------------------

def test_ptt_source_receives_editorial_and_digest_blocks(tmp_path: Path) -> None:
    """Threading the real source through the dispatcher must not silently drop
    enrichments: an F10 PTT turn asking about an ARMED cue card needs its facts,
    and a PTT turn asking "what were we talking about" needs <memoria_de_fondo>.
    ptt already FEEDS the digest (_DIGEST_CAPTURE_SOURCES) — it must READ it too.
    """
    motor, _, _ = _make_motor(tmp_dir=str(tmp_path))

    block = "<editorial_context>\n{\"topic\":\"test\"}\n</editorial_context>"
    provider = MagicMock(return_value=block)
    motor.direct_editorial_context_provider = provider
    motor._memory_digest.append("contexto: el parche de bloodborne → Kira: salio ayer.")

    captured_messages: list = []

    def fake_ollama_chat_fn(**kwargs):
        captured_messages.extend(kwargs.get("messages", []))
        return {"message": {"content": "nice"}}

    motor._ollama_chat = MagicMock(side_effect=fake_ollama_chat_fn)

    motor._generar_dialogo("host query about the topic", source="ptt")

    provider.assert_called_once_with("host query about the topic")
    all_content = " ".join(m.get("content", "") for m in captured_messages)
    assert "<editorial_context>" in all_content
    assert i18n_active.memory_block_open() in all_content
    assert "el parche de bloodborne" in all_content


def test_ptt_turn_consumes_single_use_armed_card(tmp_path: Path) -> None:
    """RECORDED CONSEQUENCE OF F1 — NOT YET SIGNED OFF BY THE OWNER.

    Widening _EDITORIAL_INJECT_SOURCES to include "ptt" gave voice turns the
    armed card's facts (test above). It also made them CONSUME the card: the
    usage-recorder hook at llm_engine.py fires on `editorial_block and dialogo`
    with no source gate, so a single_use card answered by voice goes ARMED→USED
    and is gone from the agenda. Before F1 the voice turn got nothing and the
    card stayed armed.

    This is symmetric with typed turns — a card that was answered has been used,
    whichever way the question arrived — but it was a side effect, not a decision
    anyone made. This test PINS the current behavior so it cannot silently flip.
    If you are here to change it, you are changing an unratified decision, not
    fixing a bug: get the owner's call first.
    """
    store, bridge = _make_bridge(tmp_path)
    card = store.upsert(EditorialCard(
        topic="Look Outside review",
        summary="Summary for Look Outside.",
        streamer_take="Streamer take on Look Outside.",
        triggers=["look outside"],
        single_use=True,
    ))
    store.arm(card.id)

    motor, _, _ = _make_motor(tmp_dir=str(tmp_path))
    motor.direct_editorial_context_provider = bridge.resolve_direct_context
    motor.direct_editorial_usage_recorder = bridge.commit_direct_injection
    motor._ollama_chat = MagicMock(side_effect=lambda **kw: {"message": {"content": "nice"}})

    motor._generar_dialogo("kira que sabes de look outside", source="ptt")

    assert store.get(card.id).status is EditorialCardStatus.USED
    assert store.list_armed() == []
