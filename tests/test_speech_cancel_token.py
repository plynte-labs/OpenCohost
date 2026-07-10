"""WU2 — emergency-stop speech cancellation token.

Strict TDD for the straggler bug (Bug B): drop_pending_sources() only removes
turns still IN the priority queue, but a turn popped during its GENERATION phase
before the stop landed is no longer in the queue — it proceeds into _hablar()
and plays after emergency_stop. _hablar() unconditionally sets _speaking=True at
entry, so a stop requested during the preceding generation phase does not stop
that turn from starting playback.

Fix: a cancellation token (self._cancelled_speech_prefixes) set by the emergency
paths BEFORE interrupt_speaking(); _hablar() refuses at entry any source whose
prefix matches, before setting _speaking / emitting speaking_start.
"""
from __future__ import annotations

import queue
import threading
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from opencohost.core.llm_engine import MotorVocalIA
from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController
from tests.test_api_phase1 import FakeHost

_DEFAULT_TEST_ORIGINS = ["http://localhost:5173"]


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


def _make_motor():
    log_q: queue.Queue = queue.Queue()
    events: list[str] = []
    motor = MotorVocalIA(log_q, events.append)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.pygame.mixer.music.get_busy.return_value = False
    motor.is_ready = True
    motor._edge_tts_offline = True
    # Unavailable piper -> chunks drop fast; the point of these tests is the
    # entry guard, not synthesis.
    fake_piper = MagicMock()
    fake_piper.is_available.return_value = False
    motor._piper = fake_piper
    return motor, events


def test_straggler_suppressed_at_hablar_entry():
    motor, events = _make_motor()
    motor.cancel_speech_for_sources(("kira-agenda",))
    motor._hablar(
        "una frase de prueba suficientemente larga para fragmentar",
        source="kira-agenda",
    )
    assert "speaking_start" not in events
    assert motor._speaking is False


def test_direct_source_not_suppressed():
    motor, events = _make_motor()
    motor.cancel_speech_for_sources(("kira-agenda",))
    motor._hablar(
        "una frase de prueba suficientemente larga para fragmentar",
        source="direct",
    )
    assert "speaking_start" in events


def test_clear_reenables_next_turn():
    motor, events = _make_motor()
    motor.cancel_speech_for_sources(("kira-agenda",))
    motor.clear_speech_cancel()
    motor._hablar(
        "una frase de prueba suficientemente larga para fragmentar",
        source="kira-agenda",
    )
    assert "speaking_start" in events


class _OrderRecordingMotor:
    """Records the order of the emergency-stop method calls."""

    def __init__(self):
        self.order: list[str] = []
        self.is_processing = False
        self.is_speaking = False
        self.command_queue = queue.Queue()
        self.current_model = None

    def cancel_speech_for_sources(self, prefixes):
        self.order.append(f"cancel_speech_for_sources:{prefixes}")

    def clear_speech_cancel(self):
        self.order.append("clear_speech_cancel")

    def interrupt_speaking(self):
        self.order.append("interrupt_speaking")

    def drop_pending_sources(self, prefixes):
        self.order.append("drop_pending_sources")
        return 0

    def enqueue(self, payload, priority=1, source="chat", history_text=None):
        pass

    def replace_pending(self, payload, priority=1, source="chat"):
        pass

    def clear_prefetched_agenda(self):
        pass


def _app_with(agenda, motor):
    import opencohost.api.main as main_mod

    def factory():
        host = FakeHost()
        host.agenda = agenda
        host.motor = motor
        return host

    return main_mod.create_app(host_factory=factory, cors_origins=_DEFAULT_TEST_ORIGINS)


def test_api_emergency_sets_token_before_interrupt():
    controller = KiraAgendaController()
    controller.active_topic = controller.add_topic("Tema activo")
    motor = _OrderRecordingMotor()
    app = _app_with(controller, motor)
    with TestClient(app) as client:
        resp = client.post("/api/agenda/session/action", json={"action": "emergency_stop"})
        assert resp.status_code == 200
    assert motor.order == [
        "cancel_speech_for_sources:('kira-agenda',)",
        "interrupt_speaking",
        "drop_pending_sources",
    ]


def test_api_enable_clears_token():
    controller = KiraAgendaController()
    topic = controller.add_topic("Tema", approved=True)
    controller.queue_topic(topic.id)
    motor = _OrderRecordingMotor()
    app = _app_with(controller, motor)
    with TestClient(app) as client:
        resp = client.post("/api/agenda/session/action", json={"action": "enable"})
        assert resp.status_code == 200
        assert resp.json()["applied"] is True
    assert "clear_speech_cancel" in motor.order
