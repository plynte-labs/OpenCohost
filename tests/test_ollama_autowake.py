"""S4 tests: eager Ollama wake on `EngineHost.start()` (P2 warming visibility).

Covers: single-flight under concurrent triggers, no-spawn when already
ready, non-blocking trigger, and `GET /api/status` exposing the warming
flag. Uses injected fakes for `OllamaStartupManager`/`OllamaWatchdog`/exe
discovery — no real Ollama/subprocess involved.
"""

from __future__ import annotations

import threading
import time
from queue import Queue
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


def _make_host(tmp_path, monkeypatch):
    import opencohost.api.engine_host as engine_host_mod

    fake_motor = MagicMock()
    fake_motor.command_queue = Queue()
    fake_motor.current_model = None
    fake_monitor = MagicMock()

    monkeypatch.setattr(engine_host_mod, "MotorVocalIA", lambda *a, **kw: fake_motor)
    monkeypatch.setattr(engine_host_mod, "HealthMonitor", lambda: fake_monitor)
    monkeypatch.setattr(engine_host_mod, "ollama", MagicMock())

    host = engine_host_mod.EngineHost(lock_path=str(tmp_path / "wake.lock"))
    return host, engine_host_mod


class _FakeReadyWatchdog:
    """Mirrors `OllamaWatchdog.poll()`/`.status` when Ollama is up."""

    def poll(self) -> None:
        pass

    @property
    def status(self) -> str:
        return "healthy"


class _SpyStartupManager:
    """Mirrors `OllamaStartupManager.start_and_wait`'s is_ready shortcut
    (real class: `already_running` short-circuits before any `Popen`)."""

    calls: list = []
    spawn_calls: list = []
    init_kwargs: list = []

    def __init__(self, *, is_ready, **kwargs):
        self._is_ready = is_ready
        type(self).init_kwargs.append(kwargs)

    def start_and_wait(self, ollama_exe, ollama_models_dir):
        type(self).calls.append((ollama_exe, ollama_models_dir))
        if self._is_ready():
            return object()
        type(self).spawn_calls.append(1)
        return object()


@pytest.fixture(autouse=True)
def _reset_spy():
    _SpyStartupManager.calls = []
    _SpyStartupManager.spawn_calls = []
    _SpyStartupManager.init_kwargs = []
    yield


def _join_wake_threads(timeout: float = 5.0) -> None:
    for t in threading.enumerate():
        if t is not threading.current_thread() and t.name == "ollama-wake":
            t.join(timeout=timeout)


def test_eager_wake_single_flight_under_concurrent_triggers(tmp_path, monkeypatch):
    host, engine_host_mod = _make_host(tmp_path, monkeypatch)
    monkeypatch.setattr(engine_host_mod, "OllamaStartupManager", _SpyStartupManager)
    monkeypatch.setattr(engine_host_mod, "OllamaWatchdog", _FakeReadyWatchdog)
    monkeypatch.setattr(engine_host_mod, "_find_ollama_executable", lambda: "ollama.exe")

    threads = [threading.Thread(target=host._wake_ollama_eager) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    _join_wake_threads()

    assert len(_SpyStartupManager.calls) == 1


def test_eager_wake_builds_manager_with_log_file_paths_not_pipe(tmp_path, monkeypatch):
    """FIX-B1: `ollama serve` must never inherit the default PIPE stdout/
    stderr — nothing drains those pipes, so once the ~64KB OS buffer fills
    the child blocks on write and Ollama stalls. Constructing with
    stdout_log_path/stderr_log_path (mirrors model_panel.py's UI path)
    redirects to log files instead."""
    host, engine_host_mod = _make_host(tmp_path, monkeypatch)
    monkeypatch.setattr(engine_host_mod, "OllamaStartupManager", _SpyStartupManager)
    monkeypatch.setattr(engine_host_mod, "OllamaWatchdog", _FakeReadyWatchdog)
    monkeypatch.setattr(engine_host_mod, "_find_ollama_executable", lambda: "ollama.exe")

    host._wake_ollama_eager()
    _join_wake_threads()

    assert len(_SpyStartupManager.init_kwargs) == 1
    kwargs = _SpyStartupManager.init_kwargs[0]
    assert kwargs.get("stdout_log_path")
    assert kwargs.get("stderr_log_path")


def test_eager_wake_never_spawns_when_already_ready(tmp_path, monkeypatch):
    host, engine_host_mod = _make_host(tmp_path, monkeypatch)
    monkeypatch.setattr(engine_host_mod, "OllamaStartupManager", _SpyStartupManager)
    monkeypatch.setattr(engine_host_mod, "OllamaWatchdog", _FakeReadyWatchdog)
    monkeypatch.setattr(engine_host_mod, "_find_ollama_executable", lambda: "ollama.exe")

    host._wake_ollama_eager()
    _join_wake_threads()

    assert _SpyStartupManager.spawn_calls == []
    assert len(_SpyStartupManager.calls) == 1
    assert host.ollama_warming is False


def test_eager_wake_trigger_returns_immediately(tmp_path, monkeypatch):
    host, engine_host_mod = _make_host(tmp_path, monkeypatch)
    monkeypatch.setattr(engine_host_mod, "_find_ollama_executable", lambda: "ollama.exe")

    class _SlowStartupManager:
        def __init__(self, *, is_ready, **_kwargs):
            self._is_ready = is_ready

        def start_and_wait(self, ollama_exe, ollama_models_dir):
            time.sleep(2.0)
            return object()

    monkeypatch.setattr(engine_host_mod, "OllamaStartupManager", _SlowStartupManager)
    monkeypatch.setattr(engine_host_mod, "OllamaWatchdog", _FakeReadyWatchdog)

    start = time.monotonic()
    host._wake_ollama_eager()
    elapsed = time.monotonic() - start

    assert elapsed < 0.5
    assert host.ollama_warming is True  # worker still running in background
    _join_wake_threads()


def test_eager_wake_releases_lock_after_start_and_wait_raises(tmp_path, monkeypatch):
    """FIX-BE: if `start_and_wait` raises inside the worker, the wake lock
    must be released too (not just `ollama_warming` reset) — otherwise a
    later retry can never re-acquire it and Ollama can never be re-woken
    without a process restart."""
    host, engine_host_mod = _make_host(tmp_path, monkeypatch)
    monkeypatch.setattr(engine_host_mod, "_find_ollama_executable", lambda: "ollama.exe")
    monkeypatch.setattr(engine_host_mod, "OllamaWatchdog", _FakeReadyWatchdog)

    class _RaisingStartupManager:
        def __init__(self, *, is_ready, **_kwargs):
            self._is_ready = is_ready

        def start_and_wait(self, ollama_exe, ollama_models_dir):
            raise RuntimeError("boom")

    monkeypatch.setattr(engine_host_mod, "OllamaStartupManager", _RaisingStartupManager)

    host._wake_ollama_eager()
    _join_wake_threads()

    assert host.ollama_warming is False
    # Lock released on the failure path too, so a later retry can fire.
    assert host.ollama_wake_lock.acquire(blocking=False) is True


def test_eager_wake_skips_when_ollama_exe_not_found(tmp_path, monkeypatch):
    host, engine_host_mod = _make_host(tmp_path, monkeypatch)
    monkeypatch.setattr(engine_host_mod, "OllamaStartupManager", _SpyStartupManager)
    monkeypatch.setattr(engine_host_mod, "_find_ollama_executable", lambda: None)

    host._wake_ollama_eager()
    _join_wake_threads()

    assert _SpyStartupManager.calls == []
    assert host.ollama_warming is False
    # Lock released on the not-found path, so a later retry could still fire.
    assert host.ollama_wake_lock.acquire(blocking=False) is True


def test_status_endpoint_exposes_ollama_warming():
    import opencohost.api.main as main_mod

    class _FakeHost:
        def __init__(self):
            self.motor = MagicMock()
            self.motor.is_ready = True
            self.motor.current_model = "qwen3:8b"
            self.motor.is_speaking = False
            self.motor.is_processing = False
            self.motor._current_profile_name = "default"
            self.motor._current_profile_id = None
            self.motor.command_queue = Queue()
            # F4 (runtime_findings_batch_20260731 1.3): get_status() now reads
            # this real dict instead of a bare MagicMock (which would fail
            # StatusResponse's str/bool validation for provider/transport).
            self.motor.provider_runtime_state.return_value = {
                "provider": "local",
                "transport": "local",
                "fallback_active": False,
                "generation_model": "qwen3:8b",
            }
            self.monitor = MagicMock()
            from opencohost.core.health_monitor import MonitorState

            self.monitor.state = MonitorState(
                vram_status="ok",
                rtf_status="ok",
                ollama_status="ok",
                qwen_status="ok",
                overall_status="ok",
                ollama_lifecycle="running",
                qwen_lifecycle="running",
                free_vram_mb=1024.0,
                rtf_rolling_avg=0.5,
                last_updated=123.0,
            )
            self.aggregator = None
            self.aggregator_lock = threading.Lock()
            self.agenda = None
            self.agenda_lock = threading.Lock()
            self.music_library = None
            from opencohost.api.engine_host import ChatReplySink

            self.chat_sink = ChatReplySink()
            self.ollama_warming = True

        def start(self):
            pass

        def stop(self):
            pass

    app = main_mod.create_app(host_factory=_FakeHost, cors_origins=["http://localhost:5173"])
    with TestClient(app) as client:
        resp = client.get("/api/status")
        assert resp.status_code == 200
        assert resp.json()["ollama_warming"] is True
