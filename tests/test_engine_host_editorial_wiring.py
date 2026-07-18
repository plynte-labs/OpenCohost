"""WU1: EngineHost.start() must wire the editorial cue-card direct provider.

Reproduces the reported bug — the FastAPI EngineHost never sets
`motor.direct_editorial_context_provider`, so an armed cue card never injects
on a typed turn in the Tauri app (the gate at llm_engine.py:1625 reads a
provider that stays None). See design.md D1/D4 and tasks.md WU1.

WU1 covers design test-plan items (a), (d), (e):
  (a) after start(), motor.direct_editorial_context_provider is the bridge's
      resolve_direct_context bound method
  (d) agenda-construction failure still wires the store-only direct path
      (agenda=None: provider set, recorder NOT wired)
  (e) a forced bridge-wiring failure leaves EngineHost functional (start()
      completes, editorial_bridge is None, provider never set)
"""

from queue import Queue
from unittest.mock import MagicMock

import opencohost.api.engine_host as engine_host_mod


def _fake_motor():
    """Mirror tests/test_engine_host_agenda_load.py::_fake_motor.

    direct_editorial_context_provider and agenda_output_recorder default to
    None exactly like the real MotorVocalIA (llm_engine.py:423/426), so an
    assertion of "still None" means the wiring never touched it.
    """
    motor = MagicMock()
    motor.command_queue = Queue()
    motor.current_model = None
    motor.is_processing = False
    motor.is_speaking = False
    motor.direct_editorial_context_provider = None
    motor.agenda_output_recorder = None
    return motor


def _base_monkeypatch(monkeypatch, motor, db_path):
    monkeypatch.setattr(engine_host_mod, "MotorVocalIA", lambda *a, **kw: motor)
    monkeypatch.setattr(engine_host_mod, "HealthMonitor", lambda: MagicMock())
    monkeypatch.setattr(engine_host_mod, "ollama", MagicMock())
    monkeypatch.setattr(engine_host_mod, "EDITORIAL_CARDS_DB", str(db_path))


def test_engine_host_start_wires_direct_editorial_provider(tmp_path, monkeypatch):
    # (a) After start(), the motor's direct provider is the bridge's bound
    # resolve_direct_context — not a lambda, not left None.
    motor = _fake_motor()
    _base_monkeypatch(monkeypatch, motor, tmp_path / "cards.db")
    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    try:
        host.start()
        assert host.editorial_bridge is not None
        provider = host.motor.direct_editorial_context_provider
        bound = host.editorial_bridge.resolve_direct_context
        assert provider.__func__ is bound.__func__
        assert provider.__self__ is host.editorial_bridge
    finally:
        host.stop()


def test_engine_host_start_wires_direct_provider_when_agenda_construction_fails(tmp_path, monkeypatch):
    # (d) Agenda construction fails -> host.agenda stays None, but the
    # store-only direct path must still wire. register_provider/recorder
    # (both touch the controller) must be skipped: agenda_output_recorder
    # stays None (also a forward pin for WU2).
    motor = _fake_motor()
    _base_monkeypatch(monkeypatch, motor, tmp_path / "cards.db")

    def _boom(*_a, **_kw):
        raise RuntimeError("agenda construction failed")

    monkeypatch.setattr(engine_host_mod, "KiraAgendaController", _boom)
    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    try:
        host.start()
        assert host.agenda is None
        provider = host.motor.direct_editorial_context_provider
        assert provider is not None
        assert callable(provider)
        # Recorder wiring touches the controller — must not run with agenda=None.
        assert host.motor.agenda_output_recorder is None
    finally:
        host.stop()


def test_engine_host_start_survives_editorial_bridge_wiring_failure(tmp_path, monkeypatch):
    # (e) A wiring failure must not brick the host (D4). start() completes,
    # editorial_bridge is None, and the provider was never set to a bridge
    # method (status quo — stays None).
    motor = _fake_motor()
    _base_monkeypatch(monkeypatch, motor, tmp_path / "cards.db")

    def _boom(*_a, **_kw):
        raise RuntimeError("store construction failed")

    monkeypatch.setattr(engine_host_mod, "EditorialCardStore", _boom)
    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    try:
        host.start()  # must not raise
        assert host.editorial_bridge is None
        assert host.motor.direct_editorial_context_provider is None
    finally:
        host.stop()
