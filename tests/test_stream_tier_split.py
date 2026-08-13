"""Tier split for the dispatch priority queue (tauri_stream_chat_20260812 §3.2 phase 1).

Target model, owner-approved 2026-08-12:

    0  PTT / voice
    1  direct                 <- ALWAYS above stream
    2  stream  <->  agenda    <- relative order is a STREAMER SETTING
    3

Covers:
  - `turn_priority` module: constants, the order setting, the TTL setting,
    and the agenda-first TTL floor (THE TRAP guard).
  - `MotorVocalIA.enqueue` resolving the tier from `source` when the caller
    omits `priority` (the old default `priority=1` put viewer chat in the
    direct tier).
  - KiraAgendaController mint sites: topic actions take the agenda tier,
    the agenda-minted chat action takes the STREAM tier (failure mode 4 --
    it used to mint priority=1, skipping its own tier).
  - The TTL sweep reading the module setting for stream items, and the
    agenda-first floor making the mute-co-host combination unreachable.
"""
from __future__ import annotations

import queue
import time
from unittest.mock import MagicMock

import pytest

from opencohost.core import turn_priority
from opencohost.core.llm_engine import MotorVocalIA
from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController


@pytest.fixture(autouse=True)
def _default_tier_settings(monkeypatch):
    """Pin the process-global settings so test order never leaks state."""
    monkeypatch.setattr(turn_priority, "STREAM_OVER_AGENDA", True)
    monkeypatch.setattr(turn_priority, "STREAM_TTL_SECONDS", 120.0)


def _motor() -> MotorVocalIA:
    motor = MotorVocalIA(queue.Queue(), lambda event: None)
    motor._ejecutar_inferencia = MagicMock()
    return motor


# ── the tier model itself ────────────────────────────────────────────────────


def test_dispatch_priority_default_order():
    assert turn_priority.dispatch_priority_for_source("ptt") == 0
    assert turn_priority.dispatch_priority_for_source("direct") == 1
    # Default: stream above agenda (today's effective behavior).
    assert turn_priority.dispatch_priority_for_source("chat") == 2
    assert turn_priority.dispatch_priority_for_source("kira-agenda") == 3
    assert turn_priority.dispatch_priority_for_source("kira-agenda-stop") == 3


def test_agenda_first_swaps_only_the_bottom_tiers(monkeypatch):
    monkeypatch.setattr(turn_priority, "STREAM_OVER_AGENDA", False)
    assert turn_priority.dispatch_priority_for_source("ptt") == 0
    assert turn_priority.dispatch_priority_for_source("direct") == 1
    assert turn_priority.dispatch_priority_for_source("kira-agenda") == 2
    assert turn_priority.dispatch_priority_for_source("chat") == 3


def test_unknown_source_gets_stream_tier():
    # Fail-closed to the least-trusted band: a new/unknown source must never
    # outrank an owner question by default.
    assert turn_priority.dispatch_priority_for_source("mystery") == turn_priority.stream_priority()


# ── enqueue() resolves the tier from source ──────────────────────────────────


def test_enqueue_without_priority_puts_direct_above_stream():
    """Failure mode 1: direct used to share tier 1 with stream and queue
    behind it FIFO. Omitting `priority` must resolve per-source tiers."""
    motor = _motor()
    motor.enqueue("viewer context", source="chat")
    motor.enqueue("owner question", source="direct")

    assert motor._priority_queue[0][3] == "direct"
    assert motor._priority_queue[0][0] == 1
    assert motor._priority_queue[1][3] == "chat"
    assert motor._priority_queue[1][0] == 2


def test_enqueue_explicit_priority_still_wins():
    motor = _motor()
    motor.enqueue("forced", priority=0, source="chat")
    assert motor._priority_queue[0][0] == 0


def test_enqueue_agenda_first_setting_flips_stream_below_agenda(monkeypatch):
    monkeypatch.setattr(turn_priority, "STREAM_OVER_AGENDA", False)
    motor = _motor()
    motor.enqueue("viewer context", source="chat")
    motor.enqueue("agenda block", source="kira-agenda")

    assert motor._priority_queue[0][3] == "kira-agenda"
    assert motor._priority_queue[0][0] == 2
    assert motor._priority_queue[1][3] == "chat"
    assert motor._priority_queue[1][0] == 3


# ── agenda controller mint sites ─────────────────────────────────────────────


def _agenda_controller_with_topic() -> KiraAgendaController:
    controller = KiraAgendaController()
    topic = controller.add_topic("Tema tier", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()
    return controller


def test_topic_action_mints_agenda_tier_default():
    action = _agenda_controller_with_topic().next_action()
    assert action.kind == "enqueue"
    assert action.source == "kira-agenda"
    assert action.priority == 3  # agenda below stream by default


def test_topic_action_mints_agenda_tier_agenda_first(monkeypatch):
    monkeypatch.setattr(turn_priority, "STREAM_OVER_AGENDA", False)
    action = _agenda_controller_with_topic().next_action()
    assert action.priority == 2


def test_chat_action_mints_stream_tier_not_direct():
    """Failure mode 4: the agenda minted its chat replies at priority=1
    (the direct tier), skipping its own tier. They are stream content."""
    controller = KiraAgendaController(chat_cadence_blocks=1)
    topic = controller.add_topic("Tema de fondo", approved=True)
    controller.queue_topic(topic.id)
    controller.enable()
    controller.next_action()
    controller.mark_generation_accepted()
    controller.mark_speech_complete()

    action = controller.next_action(compact_chat="El chat pregunta por mods.")

    assert action.source == "chat"
    assert action.priority == turn_priority.stream_priority()
    assert action.priority == 2  # default order


# ── stream TTL: module setting + THE TRAP floor ──────────────────────────────


def test_stream_ttl_reads_module_setting_not_pq_ttl(monkeypatch):
    """The stream discard window is the configurable module setting; the
    engine's `_pq_ttl_seconds` stays the base TTL for other sources only."""
    monkeypatch.setattr(turn_priority, "STREAM_TTL_SECONDS", 0.01)
    motor = _motor()
    motor._pq_ttl_seconds = 999.0  # must NOT govern stream items any more
    motor._priority_queue = [
        (turn_priority.stream_priority(), time.time() - 5.0, "old stream", "chat")
    ]
    expired: list = []
    motor.on_chat_item_expired = lambda info: expired.append(info)

    motor._process_priority_queue()

    assert len(expired) == 1
    motor._ejecutar_inferencia.assert_not_called()
    assert motor._priority_queue == []


def test_trap_agenda_first_floors_the_ttl(monkeypatch):
    """THE TRAP: agenda-first + a short TTL used to mean every stream item
    dies waiting out agenda monologues -- a MUTE co-host with no error.
    With agenda-first selected, the effective TTL is floored so the two
    settings cannot silently defeat each other."""
    monkeypatch.setattr(turn_priority, "STREAM_OVER_AGENDA", False)
    monkeypatch.setattr(turn_priority, "STREAM_TTL_SECONDS", 0.01)
    motor = _motor()
    # Aged far past the configured TTL, far under the floor.
    motor._priority_queue = [
        (turn_priority.stream_priority(), time.time() - 5.0, "waited out agenda", "chat")
    ]
    expired: list = []
    motor.on_chat_item_expired = lambda info: expired.append(info)

    motor._process_priority_queue()

    assert expired == []
    motor._ejecutar_inferencia.assert_called_once()
    assert motor._ejecutar_inferencia.call_args[0][0] == "waited out agenda"


def test_effective_stream_ttl_math(monkeypatch):
    monkeypatch.setattr(turn_priority, "STREAM_TTL_SECONDS", 45.0)
    assert turn_priority.effective_stream_ttl() == 45.0

    monkeypatch.setattr(turn_priority, "STREAM_OVER_AGENDA", False)
    assert (
        turn_priority.effective_stream_ttl()
        == turn_priority.AGENDA_FIRST_STREAM_TTL_FLOOR_SECONDS
    )

    # A configured window LONGER than the floor is respected as-is.
    monkeypatch.setattr(
        turn_priority,
        "STREAM_TTL_SECONDS",
        turn_priority.AGENDA_FIRST_STREAM_TTL_FLOOR_SECONDS + 100.0,
    )
    assert (
        turn_priority.effective_stream_ttl()
        == turn_priority.AGENDA_FIRST_STREAM_TTL_FLOOR_SECONDS + 100.0
    )


def test_direct_ttl_contract_untouched():
    """Renumbering must not disturb the direct item's own documented bound
    (DIRECT_ANSWER_MAX_WAIT_SECONDS), pinned by test_direct_bounded_wait --
    here just the tier: direct stays priority 1 in the sweep's `prio > 0`
    TTL-eligible band."""
    motor = _motor()
    motor.enqueue("pregunta", source="direct")
    assert motor._priority_queue[0][0] == 1
