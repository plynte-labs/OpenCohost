"""Read-aloud boundary consumers (readaloud-speech T2).

A read-aloud job rides the router like any other, so it emits the boundary
pair (`speaking_start` / `speaking_end`). The avatar/UI consumers SHOULD see
it (lip-sync), but the agenda controller must not: it routes those events by
its OWN state, not by source, so a read-aloud boundary landing while an
agenda beat is GENERATING would accept the generation and then complete a
beat that never spoke.
"""
from queue import Queue
from unittest.mock import MagicMock

import opencohost.api.engine_host as engine_host_mod
from opencohost.smart_aggregator.kira_agenda_controller import AgendaState
from tests.test_engine_host_agenda_load import _fake_motor, _start_host
from tests.test_speech_outcome_capture import _make_motor


def _generating_agenda(host):
    host._agenda_driver.stop()
    agenda = host.agenda
    topic = agenda.add_topic("Tema uno", approved=True)
    agenda.queue_topic(topic.id)
    agenda.enable()
    with host.agenda_lock:
        agenda.next_action()
    assert agenda.state == AgendaState.GENERATING
    return agenda, topic


def test_readaloud_boundary_does_not_advance_the_agenda(tmp_path, monkeypatch):
    motor = _fake_motor()
    host = _start_host(tmp_path, monkeypatch, motor, tmp_path / "cards.db")
    try:
        agenda, topic = _generating_agenda(host)
        motor._speech_boundary_source = "readaloud"

        host._dispatch_motor_event("speaking_start")
        assert agenda.state == AgendaState.GENERATING, "must not accept the generation"
        host._dispatch_motor_event("speaking_end")
        assert agenda.state == AgendaState.GENERATING, "must not complete the beat"
        assert topic.turns_spoken == 0
    finally:
        host.stop()


def test_agenda_boundary_still_advances_the_agenda(tmp_path, monkeypatch):
    motor = _fake_motor()
    host = _start_host(tmp_path, monkeypatch, motor, tmp_path / "cards.db")
    try:
        agenda, topic = _generating_agenda(host)
        motor._speech_boundary_source = "kira-agenda"

        host._dispatch_motor_event("speaking_start")
        assert agenda.state == AgendaState.SPEAKING
        host._dispatch_motor_event("speaking_end")
        assert agenda.state == AgendaState.WAITING_SIGNAL
        assert topic.turns_spoken >= 1
    finally:
        host.stop()


def test_boundary_start_records_the_source_and_end_keeps_it_for_the_event():
    motor, _, _ = _make_motor()
    motor._speech_boundary_start("readaloud")
    assert motor._speech_boundary_source == "readaloud"
    motor._speech_boundary_end()
    # Kept after the end: the speaking_end handler runs AFTER the end clears
    # `_current_speech_source`, so this is the only place it can still read it.
    assert motor._speech_boundary_source == "readaloud"
