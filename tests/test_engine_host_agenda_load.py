"""F1: EngineHost.start() must load persisted agenda state.

Reproduces the reported bug — self.agenda = KiraAgendaController() is built
bare with no AgendaPersistence.load_into() call, so GET /api/agenda is
structurally empty every boot even when EDITORIAL_CARDS_DB has a persisted
topic. Must fail before the fix, pass after.
"""

from queue import Queue
from unittest.mock import MagicMock

import opencohost.api.engine_host as engine_host_mod
from opencohost.core.agenda.agenda_persistence import AgendaPersistence
from opencohost.smart_aggregator.kira_agenda_controller import AgendaState, KiraAgendaController


def _seeded_db(tmp_path):
    db_path = tmp_path / "cards.db"
    seed_controller = KiraAgendaController()
    seed_controller.add_topic("Tema persistido", "angulo", approved=True)
    persistence = AgendaPersistence(db_path)
    persistence.save_if_changed(seed_controller)
    return db_path


def test_engine_host_start_loads_persisted_agenda(tmp_path, monkeypatch):
    db_path = _seeded_db(tmp_path)

    fake_motor = MagicMock()
    fake_motor.command_queue = Queue()
    fake_motor.current_model = None
    fake_monitor = MagicMock()

    monkeypatch.setattr(engine_host_mod, "MotorVocalIA", lambda *a, **kw: fake_motor)
    monkeypatch.setattr(engine_host_mod, "HealthMonitor", lambda: fake_monitor)
    monkeypatch.setattr(engine_host_mod, "ollama", MagicMock())
    monkeypatch.setattr(engine_host_mod, "EDITORIAL_CARDS_DB", str(db_path))

    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    try:
        host.start()
        assert host.agenda is not None
        titles = [t.title for t in host.agenda.topics]
        assert "Tema persistido" in titles
    finally:
        host.stop()


# ──────────────────────────────────────────────────────────────────────────
# FIX-C: EngineHost.start() wires the agenda driver + guardrails + feedback
# ──────────────────────────────────────────────────────────────────────────


def _fake_motor():
    motor = MagicMock()
    motor.command_queue = Queue()
    motor.current_model = None
    # Explicit booleans: a bare MagicMock attribute is truthy, which would make
    # next_action treat the motor as permanently busy.
    motor.is_processing = False
    motor.is_speaking = False
    return motor


def _start_host(tmp_path, monkeypatch, motor, db_path):
    monkeypatch.setattr(engine_host_mod, "MotorVocalIA", lambda *a, **kw: motor)
    monkeypatch.setattr(engine_host_mod, "HealthMonitor", lambda: MagicMock())
    monkeypatch.setattr(engine_host_mod, "ollama", MagicMock())
    monkeypatch.setattr(engine_host_mod, "EDITORIAL_CARDS_DB", str(db_path))
    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    host.start()
    return host


def test_engine_host_wires_agenda_guardrails_and_driver(tmp_path, monkeypatch):
    motor = _fake_motor()
    host = _start_host(tmp_path, monkeypatch, motor, tmp_path / "cards.db")
    try:
        assert host.agenda is not None
        # Driver thread is constructed and running.
        assert host._agenda_driver is not None
        assert host._agenda_driver.is_running()
        # Guardrails wired (CTK app_shell.py:188-192 parity), lock-wrapped.
        assert host.motor.agenda_controller is host.agenda
        assert callable(host.motor.agenda_output_validator)
        assert callable(host.motor.agenda_output_preview_validator)
        assert callable(host.motor.agenda_output_transformer)
        # The wrapped transformer really delegates to the controller.
        assert host.motor.agenda_output_transformer("hola mundo") == "hola mundo"
        # Feedback handler registered into the router (append, not replace).
        assert host._handle_agenda_motor_event in host._motor_event_handlers
    finally:
        host.stop()
        # stop() joins the driver thread.
        assert not host._agenda_driver.is_running()


def test_engine_host_routes_motor_events_to_agenda(tmp_path, monkeypatch):
    motor = _fake_motor()
    host = _start_host(tmp_path, monkeypatch, motor, tmp_path / "cards.db")
    try:
        # Stop the background driver so state can be driven deterministically.
        host._agenda_driver.stop()
        agenda = host.agenda
        topic = agenda.add_topic("Tema uno", approved=True)
        agenda.queue_topic(topic.id)
        agenda.enable()
        with host.agenda_lock:
            action = agenda.next_action()
        assert action.source == "kira-agenda"
        assert agenda.state == AgendaState.GENERATING

        # Motor events flow through the SAME router that also feeds OBS.
        host._dispatch_motor_event("speaking_start")
        assert agenda.state == AgendaState.SPEAKING

        host._dispatch_motor_event("speaking_end")
        assert agenda.state == AgendaState.WAITING_SIGNAL
        assert topic.turns_spoken >= 1
    finally:
        host.stop()


def test_engine_host_speaking_end_consumes_prefetch_synchronously(tmp_path, monkeypatch):
    # WU2 (design-fase2.md §2.4): speaking_end of an AGENDA turn must consume the
    # ready draft synchronously here (worker thread, before the post-turn pop) via
    # the driver's _maybe_consume_prefetch — not defer it to a later driver tick.
    motor = _fake_motor()
    motor.current_speech_source = "kira-agenda"
    motor.has_pending_priority_before.return_value = False
    motor.prefetch_agenda.return_value = True
    host = _start_host(tmp_path, monkeypatch, motor, tmp_path / "cards.db")
    try:
        driver = host._agenda_driver
        driver.stop()  # drive deterministically
        # Spy on the consume so we assert the WIRING, not the driver internals.
        consume_calls = []
        driver._maybe_consume_prefetch = lambda agenda, m: (consume_calls.append((agenda, m)) or True)

        agenda = host.agenda
        topic = agenda.add_topic("Tema uno", approved=True)
        agenda.queue_topic(topic.id)
        agenda.enable()
        with host.agenda_lock:
            agenda.next_action()  # -> GENERATING, opens the topic
        host._dispatch_motor_event("speaking_start")  # -> SPEAKING
        assert agenda.state == AgendaState.SPEAKING

        host._dispatch_motor_event("speaking_end")  # -> WAITING_SIGNAL + consume
        assert consume_calls, "speaking_end must consume the prefetch synchronously"
        assert consume_calls[0] == (host.agenda, host.motor), "consume gets the host agenda+motor"

        # Triangulation: a speaking_end while the controller is NOT agenda-speaking
        # (mark_speech_complete does not run) must NOT fire the consume.
        consume_calls.clear()
        assert agenda.state == AgendaState.WAITING_SIGNAL
        host._dispatch_motor_event("speaking_end")
        assert consume_calls == [], "consume must not fire outside an agenda speaking_end"
    finally:
        host.stop()


def test_engine_host_wires_guardrail_rejected_to_event_log(tmp_path, monkeypatch):
    # WU4 4b (design-fase2.md §3): the engine's optional on_guardrail_rejected
    # callback must be wired to a direct EventLogSink emission — code only,
    # never dialogue text.
    motor = _fake_motor()
    host = _start_host(tmp_path, monkeypatch, motor, tmp_path / "cards.db")
    try:
        assert callable(host.motor.on_guardrail_rejected)

        host.motor.on_guardrail_rejected("contains_internal_leak")

        events = host.event_log.since(0)["events"]
        assert events[-1]["source"] == "kira-agenda"
        assert events[-1]["action"] == "guardrail:contains_internal_leak"
        assert events[-1]["detail"] is None
    finally:
        host.stop()


def test_guardrail_rejected_callback_is_safe_from_a_non_worker_thread(tmp_path, monkeypatch):
    # 4b: the callback fires on the PREGEN WORKER THREAD in production —
    # EventLogSink.record must be safe to call from any thread.
    import threading

    motor = _fake_motor()
    host = _start_host(tmp_path, monkeypatch, motor, tmp_path / "cards.db")
    try:
        t = threading.Thread(target=host.motor.on_guardrail_rejected, args=("some_code",))
        t.start()
        t.join(2.0)

        events = host.event_log.since(0)["events"]
        assert any(e["action"] == "guardrail:some_code" for e in events)
    finally:
        host.stop()


def test_engine_host_speaking_start_wires_prefetch_hook(tmp_path, monkeypatch):
    # Fase 1 (no-dead-air): speaking_start must spawn a background prefetch of
    # the next agenda turn while the current one is still playing.
    motor = _fake_motor()
    motor.current_speech_source = "kira-agenda"
    motor.has_pending_priority_before.return_value = False
    motor.prefetch_agenda.return_value = True
    host = _start_host(tmp_path, monkeypatch, motor, tmp_path / "cards.db")
    try:
        host._agenda_driver.stop()  # drive deterministically
        agenda = host.agenda
        topic = agenda.add_topic("Tema uno", approved=True)
        agenda.queue_topic(topic.id)
        agenda.enable()
        with host.agenda_lock:
            agenda.next_action()  # -> GENERATING, opens the topic
        assert agenda.state == AgendaState.GENERATING

        host._dispatch_motor_event("speaking_start")  # -> SPEAKING + prefetch hook

        assert agenda.state == AgendaState.SPEAKING
        assert motor.prefetch_agenda.called, "speaking_start must spawn a background prefetch"
        assert host._agenda_driver._prefetch is not None
    finally:
        host.stop()
