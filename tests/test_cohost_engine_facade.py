"""Phase 2 characterization for the ``CohostEngine`` composition seam."""

from __future__ import annotations

from queue import Queue
from types import SimpleNamespace
from unittest.mock import MagicMock

from opencohost.api import engine_host as engine_host_mod
from opencohost.core.cohost_engine import CohostEngine


def test_facade_delegates_reads_calls_writes_and_deletes():
    runtime = SimpleNamespace(value=3, double=lambda number: number * 2)
    engine = CohostEngine(runtime)

    assert engine.runtime is runtime
    assert engine.value == 3
    assert engine.double(4) == 8

    engine.value = 7
    engine.callback = lambda: "wired"

    assert runtime.value == 7
    assert runtime.callback() == "wired"
    assert "callback" in dir(engine)

    del engine.value
    assert not hasattr(runtime, "value")


class _FakeMotor:
    def __init__(self) -> None:
        self.command_queue = Queue()
        self.current_model = None
        self.started = 0
        self.flushed = 0

    def start(self) -> None:
        self.started += 1

    def flush_memorias(self) -> None:
        self.flushed += 1


class _FakeMonitor:
    def __init__(self) -> None:
        self.qwen_manager = SimpleNamespace(attach_existing=lambda: None)
        self.started = 0
        self.stopped = 0

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


class _FakeObsRuntime:
    def start_from_config(self) -> None:
        pass

    def handle_motor_event(self, _status) -> None:
        pass

    def stop(self) -> None:
        pass


class _FakeAggregator:
    def __init__(self, **_kwargs) -> None:
        self.on_filtered_message = None
        self.on_source_changed = None
        self.on_aggregated_context = None

    def disconnect(self) -> None:
        pass


class _FakeMusicLibrary:
    def load(self) -> None:
        pass


def test_engine_host_composes_and_exposes_the_facade(tmp_path, monkeypatch):
    runtime = _FakeMotor()
    monitor = _FakeMonitor()
    reactor_inputs = []

    monkeypatch.setattr(engine_host_mod, "MotorVocalIA", lambda *args, **kwargs: runtime)
    monkeypatch.setattr(engine_host_mod, "HealthMonitor", lambda: monitor)
    monkeypatch.setattr(engine_host_mod, "ObsRuntime", _FakeObsRuntime)
    monkeypatch.setattr(engine_host_mod, "Aggregator", _FakeAggregator)
    monkeypatch.setattr(
        engine_host_mod,
        "ChatReactionCore",
        lambda motor: reactor_inputs.append(motor) or MagicMock(),
    )
    monkeypatch.setattr(
        engine_host_mod,
        "KiraAgendaController",
        MagicMock(side_effect=RuntimeError("agenda unavailable")),
    )
    monkeypatch.setattr(engine_host_mod, "MusicLibrary", _FakeMusicLibrary)
    monkeypatch.setattr(engine_host_mod, "cargar_perfiles", lambda: {})
    monkeypatch.setattr(engine_host_mod, "ollama", MagicMock())

    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "engine.lock"))
    host._wake_ollama_eager = lambda: None
    host._wire_editorial_bridge = lambda: None

    try:
        host.start()

        assert isinstance(host.motor, CohostEngine)
        assert host.motor.runtime is runtime
        assert runtime.started == 1
        assert runtime.health_monitor is monitor
        assert runtime.on_ctx_pressure_high == host._on_ctx_pressure_high
        assert runtime.on_cloud_probe_scheduled == host._on_cloud_probe_scheduled
        assert runtime.on_memoria_promoted == host._on_memoria_promoted
        assert reactor_inputs == [host.motor]
    finally:
        host.stop()

    assert runtime.flushed == 1
    assert runtime.command_queue.get_nowait() is None
    assert monitor.started == 1
    assert monitor.stopped == 1
