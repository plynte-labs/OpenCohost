"""F1: EngineHost.start() must load persisted agenda state.

Reproduces the reported bug — self.agenda = KiraAgendaController() is built
bare with no AgendaPersistence.load_into() call, so GET /api/agenda is
structurally empty every boot even when EDITORIAL_CARDS_DB has a persisted
topic. Must fail before the fix, pass after.
"""

from queue import Queue
from unittest.mock import MagicMock

import opencohost.api.engine_host as engine_host_mod
from opencohost.core.agenda_persistence import AgendaPersistence
from opencohost.smart_aggregator.kira_agenda_controller import KiraAgendaController


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
