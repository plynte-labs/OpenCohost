"""FIX-A: EngineHost.start() must seed the active profile at boot.

Root cause reproduced: MotorVocalIA._current_profile_id stays None until the
first set_profile dispatch, so /api/status.active_profile_id is null and the FE
MemoryCard can neither list memorias nor explain why. The seed dispatches a
normal set_profile for the startup profile (persisted last-used, else the first
profile on disk) through the REAL command-queue path, so the engine applies the
stable uuid under its own _history_lock — the private field is never poked.

Also covers the last_profile.json read/write round-trip in settings.
"""

import threading
import uuid
from queue import Queue
from unittest.mock import MagicMock

import opencohost.api.engine_host as engine_host_mod
from opencohost.config import settings


class _ProcessingMotor:
    """Minimal MotorVocalIA stand-in that PROCESSES queued set_profile
    commands like the real engine thread (llm_engine.py set_profile handler:
    _current_profile_id / _current_profile_name are taken from the payload).

    Lets the seed test assert active_profile_id end-to-end after start() with
    no live Ollama. Any attribute the agenda driver / obs runtime probes that
    is not explicitly modeled here degrades to a MagicMock — matching the
    existing MagicMock-based engine-host tests.
    """

    def __init__(self):
        self.command_queue = Queue()
        self.current_model = None
        # Explicit booleans: a bare MagicMock attribute is truthy, which would
        # make the agenda driver treat the motor as permanently busy.
        self.is_processing = False
        self.is_speaking = False
        self._current_profile_id = None
        self._current_profile_name = "default"
        self._applied = threading.Event()
        self._thread = None

    def __getattr__(self, name):
        # Only reached for attributes not set in __init__ (health_monitor,
        # agenda_* guardrails set via setattr are found normally).
        return MagicMock()

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True, name="fake-motor")
        self._thread.start()

    def _run(self):
        while True:
            item = self.command_queue.get()
            if item is None:  # engine_host.stop() sentinel
                return
            if not isinstance(item, tuple) or len(item) != 2:
                continue
            command, payload = item
            if command == "set_profile" and isinstance(payload, dict):
                self._current_profile_id = payload.get("id")
                self._current_profile_name = payload.get("_profile_name")
                self._applied.set()

    def wait_applied(self, timeout: float = 3.0) -> bool:
        return self._applied.wait(timeout)


def _perfiles():
    return {
        "Akira": {"id": str(uuid.uuid4()), "prompt": "p", "use_system": False},
        "Bravo": {"id": str(uuid.uuid4()), "prompt": "q", "use_system": True},
    }


def _start_host(tmp_path, monkeypatch, motor, perfiles, last_used):
    monkeypatch.setattr(engine_host_mod, "MotorVocalIA", lambda *a, **kw: motor)
    monkeypatch.setattr(engine_host_mod, "HealthMonitor", lambda: MagicMock())
    monkeypatch.setattr(engine_host_mod, "ollama", MagicMock())
    monkeypatch.setattr(engine_host_mod, "EDITORIAL_CARDS_DB", str(tmp_path / "cards.db"))
    monkeypatch.setattr(engine_host_mod, "cargar_perfiles", lambda: perfiles)
    monkeypatch.setattr(engine_host_mod, "load_last_profile", lambda: last_used)
    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    host.start()
    return host


def test_seed_sets_active_profile_id_to_real_uuid_after_start(tmp_path, monkeypatch):
    perfiles = _perfiles()
    motor = _ProcessingMotor()
    host = _start_host(tmp_path, monkeypatch, motor, perfiles, last_used=None)
    try:
        assert motor.wait_applied()
        # A real uuid, and one of the ids actually in perfiles.json.
        pid = host.motor._current_profile_id
        assert pid is not None
        uuid.UUID(pid)  # raises if not a well-formed uuid
        assert pid in {p["id"] for p in perfiles.values()}
    finally:
        host.stop()


def test_seed_uses_last_used_profile_when_it_still_exists(tmp_path, monkeypatch):
    perfiles = _perfiles()
    motor = _ProcessingMotor()
    host = _start_host(tmp_path, monkeypatch, motor, perfiles, last_used="Bravo")
    try:
        assert motor.wait_applied()
        assert host.motor._current_profile_id == perfiles["Bravo"]["id"]
        assert host.motor._current_profile_name == "Bravo"
    finally:
        host.stop()


def test_seed_falls_back_to_first_profile_when_no_last_used(tmp_path, monkeypatch):
    perfiles = _perfiles()
    motor = _ProcessingMotor()
    host = _start_host(tmp_path, monkeypatch, motor, perfiles, last_used=None)
    try:
        assert motor.wait_applied()
        # "Akira" is first in insertion order.
        assert host.motor._current_profile_id == perfiles["Akira"]["id"]
        assert host.motor._current_profile_name == "Akira"
    finally:
        host.stop()


def test_seed_falls_back_to_first_profile_when_last_used_missing(tmp_path, monkeypatch):
    perfiles = _perfiles()
    motor = _ProcessingMotor()
    host = _start_host(tmp_path, monkeypatch, motor, perfiles, last_used="Deleted")
    try:
        assert motor.wait_applied()
        assert host.motor._current_profile_id == perfiles["Akira"]["id"]
    finally:
        host.stop()


def test_save_and_load_last_profile_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LAST_PROFILE_FILE", str(tmp_path / "last_profile.json"))
    assert settings.load_last_profile() is None  # absent -> None
    settings.save_last_profile("Akira")
    assert settings.load_last_profile() == "Akira"
    # Overwrite persists the newer choice.
    settings.save_last_profile("Bravo")
    assert settings.load_last_profile() == "Bravo"


def test_load_last_profile_empty_name_returns_none(tmp_path, monkeypatch):
    path = tmp_path / "last_profile.json"
    path.write_text('{"profile": "   "}', encoding="utf-8")
    monkeypatch.setattr(settings, "LAST_PROFILE_FILE", str(path))
    assert settings.load_last_profile() is None
