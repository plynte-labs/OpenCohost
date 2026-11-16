"""EngineHost vision wiring (vision-on-demand V1.5).

The real start() path with a fake motor and fake vision adapters: no Ollama,
no screen. Pins lazy construction, persisted-settings application, the motor
seam, the status/arm/apply surface V1.6 builds on, the `vision_looking`
event, and daemon shutdown.
"""
from __future__ import annotations

import json
from queue import Queue
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import opencohost.api.engine_host as engine_host_mod
from opencohost.config import settings
from opencohost.config import vision_settings as vs


class FakeDaemon:
    instances: list = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.state = "stopped"
        self.stop_calls = 0
        FakeDaemon.instances.append(self)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.kwargs['port']}"

    def ensure_running(self):
        self.state = "running"
        return SimpleNamespace(state=self.state)

    def status(self):
        return SimpleNamespace(state=self.state)

    def stop(self):
        self.stop_calls += 1
        self.state = "stopped"

    def model_installed(self, model):
        return True

    def warm(self, model, *, keep_alive, timeout_s=None):
        self.warmed = (model, keep_alive)
        return True

    def unload(self, model):
        self.unloaded = model
        return True

    def model_loaded(self, model):
        return getattr(self, "warmed", (None,))[0] == model


class FakeDescriber:
    instances: list = []

    def __init__(self, base_url, model, **kwargs):
        self.base_url = base_url
        self.model = model
        self.kwargs = kwargs
        FakeDescriber.instances.append(self)


class FakeCapture:
    avail = True

    @staticmethod
    def available():
        return FakeCapture.avail

    def capture(self):  # pragma: no cover - never reached here
        raise AssertionError


def _fake_motor():
    motor = MagicMock()
    motor.command_queue = Queue()
    motor.current_model = None
    motor.is_processing = False
    motor.is_speaking = False
    motor.direct_editorial_context_provider = None
    motor.direct_editorial_usage_recorder = None
    motor.agenda_output_recorder = None
    motor._vision = None
    return motor


@pytest.fixture
def host_factory(tmp_path, monkeypatch):
    saved = vs.current()
    FakeDaemon.instances = []
    FakeCapture.avail = True
    vision_file = tmp_path / "vision_settings.json"
    monkeypatch.setattr(settings, "VISION_SETTINGS_FILE", str(vision_file))
    monkeypatch.setattr(engine_host_mod, "HealthMonitor", lambda: MagicMock())
    monkeypatch.setattr(engine_host_mod, "ollama", MagicMock())
    monkeypatch.setattr(engine_host_mod, "EDITORIAL_CARDS_DB", str(tmp_path / "cards.db"))
    monkeypatch.setattr(engine_host_mod, "VisionDaemon", FakeDaemon)
    import opencohost.core.vision.cloud_describer as cloud_describer_mod

    monkeypatch.setattr(cloud_describer_mod, "OllamaVisionDescriber", FakeDescriber)
    monkeypatch.setattr(engine_host_mod, "MssCapture", FakeCapture)
    monkeypatch.setattr(engine_host_mod, "_find_ollama_executable", lambda: "C:/fake/ollama.exe")
    monkeypatch.setattr(engine_host_mod, "_vision_spawn", lambda fn: fn())
    hosts = []

    def _make(file_payload=None):
        if file_payload is not None:
            vision_file.write_text(json.dumps(file_payload), encoding="utf-8")
        motor = _fake_motor()
        monkeypatch.setattr(engine_host_mod, "MotorVocalIA", lambda *a, **kw: motor)
        host = engine_host_mod.EngineHost(lock_path=str(tmp_path / f"engine{len(hosts)}.lock"))
        hosts.append(host)
        host.start()
        return host

    yield _make
    for host in hosts:
        host.stop()
    vs.apply(saved)


def test_vision_off_by_default_builds_nothing(host_factory):
    host = host_factory()

    assert FakeDaemon.instances == []
    assert host.vision is not None
    assert host.vision.step is None
    assert host.vision_status()["enabled"] is False
    assert host.vision_arm_look() is False


def test_persisted_enable_builds_daemon_step_and_motor_seam(host_factory):
    host = host_factory({"enabled": True, "look_on_ptt": True})

    assert settings.VISION_ENABLED is True
    assert len(FakeDaemon.instances) == 1
    daemon = FakeDaemon.instances[0]
    assert daemon.kwargs["port"] == settings.VISION_PORT
    assert daemon.kwargs["ollama_exe"] == "C:/fake/ollama.exe"
    assert daemon.kwargs["models_dir"] == engine_host_mod.OLLAMA_MODELS_DIR
    assert daemon.kwargs["stdout_log_path"].endswith("ollama_vision_stdout.log")
    assert daemon.kwargs["stderr_log_path"].endswith("ollama_vision_stderr.log")
    assert host.motor._vision is host.vision
    status = host.vision_status()
    assert status["enabled"] is True
    assert status["daemon_state"] == "running"
    assert status["capture_available"] is True
    assert status["model_installed"] is True


def test_capture_unavailable_is_reported(host_factory):
    FakeCapture.avail = False
    host = host_factory({"enabled": True})

    assert host.vision_status()["capture_available"] is False
    assert host.vision.step is not None


def test_arm_look_and_status(host_factory):
    host = host_factory({"enabled": True})

    assert host.vision_arm_look() is True
    assert host.vision_status()["armed"] is True


def test_apply_settings_persists_then_applies(host_factory):
    host = host_factory()

    applied = host.vision_apply_settings({"enabled": True, "capture_delay_s": 1.5})

    assert applied is not None and applied.enabled is True
    assert settings.VISION_ENABLED is True
    assert settings.VISION_CAPTURE_DELAY_S == 1.5
    assert vs.load(base=vs.current()).enabled is True
    assert host.vision.step is not None
    assert len(FakeDaemon.instances) == 1


def test_apply_settings_refuses_when_save_fails(host_factory, monkeypatch):
    host = host_factory()
    monkeypatch.setattr(engine_host_mod._vision_settings, "save", lambda *_a, **_k: False)

    assert host.vision_apply_settings({"enabled": True}) is None
    assert settings.VISION_ENABLED is False
    assert host.vision.step is None


def test_disable_at_runtime_stops_daemon(host_factory):
    host = host_factory({"enabled": True})

    host.vision_apply_settings({"enabled": False})

    assert FakeDaemon.instances[0].stop_calls == 1
    assert host.vision.step is None


def test_host_stop_stops_vision_daemon(host_factory):
    host = host_factory({"enabled": True})

    host.stop()

    assert FakeDaemon.instances[0].stop_calls >= 1


def test_looking_event_carries_a_boolean_only(host_factory):
    host = host_factory()

    host._on_vision_looking(True)
    host._on_vision_looking(False)

    events = [e for e in host.event_log.since(0)["events"] if e["action"] == "vision_looking"]
    assert [e["detail"] for e in events] == [{"looking": True}, {"looking": False}]
    assert all(e["source"] == "vision" for e in events)


def test_event_model_accepts_the_boolean_detail():
    from opencohost.api.models import EventOut

    out = EventOut(seq=1, ts=0.0, source="vision", action="vision_looking", detail={"looking": True})
    assert out.detail == {"looking": True}
    assert out.detail["looking"] is True


def test_vision_wiring_failure_does_not_brick_start(host_factory, monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("vision construction failed")

    monkeypatch.setattr(engine_host_mod, "VisionController", _boom)
    host = host_factory({"enabled": True})

    assert host.vision is None
    assert host.vision_status()["enabled"] is False
    assert host.vision_arm_look() is False


# -- V1.8: warm, status contract, vision_result event ------------------------------


def test_enable_warms_the_model_resident_and_reports_ready(host_factory):
    FakeDescriber.instances = []
    host = host_factory({"enabled": True})

    daemon = FakeDaemon.instances[0]
    assert daemon.warmed == (settings.VISION_MODEL, -1)
    # the describer refreshes the same residency, never a shorter one
    assert FakeDescriber.instances[-1].kwargs.get("keep_alive") == -1
    status = host.vision_status()
    assert status["state"] == "ready"
    assert status["model_loaded"] is True
    assert status["last_result"] is None


def test_status_before_wiring_carries_the_v18_keys(host_factory, monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("vision construction failed")

    monkeypatch.setattr(engine_host_mod, "VisionController", _boom)
    host = host_factory({"enabled": True})

    status = host.vision_status()
    assert status["state"] == "off"
    assert status["last_error"] is None
    assert status["model_loaded"] is None
    assert status["last_result"] is None


def test_vision_result_event_detail_is_an_object_with_the_five_keys(host_factory):
    host = host_factory()
    payload = {"outcome": "describe_timeout", "id": "look-1", "describe_ms": 1234,
               "frames_sent": 6, "mode": "clip"}

    host._on_vision_result(payload)

    events = [e for e in host.event_log.since(0)["events"] if e["action"] == "vision_result"]
    assert [(e["source"], e["detail"]) for e in events] == [("vision", payload)]
    assert set(events[0]["detail"]) == {"outcome", "id", "describe_ms", "frames_sent", "mode"}


def test_vision_result_event_never_carries_text_or_bytes(host_factory):
    host = host_factory()

    host._on_vision_result({"outcome": "ok", "id": "x", "describe_ms": None, "frames_sent": 1,
                            "mode": "normal", "description": "SECRET", "jpeg": b"x", "question": "Q"})

    [event] = [e for e in host.event_log.since(0)["events"] if e["action"] == "vision_result"]
    assert set(event["detail"]) == {"outcome", "id", "describe_ms", "frames_sent", "mode"}


def test_event_out_accepts_the_vision_result_object_detail():
    from opencohost.api.models import EventOut

    detail = {"outcome": "ok", "id": "look-1", "describe_ms": None, "frames_sent": 3, "mode": "text"}
    out = EventOut(seq=1, ts=0.0, source="vision", action="vision_result", detail=detail)
    assert out.detail == detail


def test_disable_unloads_before_stopping(host_factory):
    host = host_factory({"enabled": True})

    host.vision_apply_settings({"enabled": False})

    daemon = FakeDaemon.instances[0]
    assert daemon.unloaded == settings.VISION_MODEL
    assert daemon.stop_calls == 1


def test_event_model_accepts_the_outcome_string():
    from opencohost.api.models import EventOut

    out = EventOut(seq=1, ts=0.0, source="vision", action="vision_result", detail="ok")
    assert out.detail == "ok"


# --------------------------------------------------------- V1.11 burst wiring


def test_host_wires_a_foreground_probe_into_the_step(host_factory, monkeypatch):
    sentinel = SimpleNamespace(is_self_foreground=lambda: False)
    monkeypatch.setattr(engine_host_mod, "default_foreground_probe", lambda: sentinel)
    host = host_factory({"enabled": True})

    assert host.vision.step._foreground_probe is sentinel


def test_host_thumbnails_are_none_for_unknown_looks_and_when_off(host_factory):
    host = host_factory()
    assert host.vision_thumbnails("nope") is None

    host = host_factory({"enabled": True})
    assert host.vision_thumbnails("nope") is None


def test_host_thumbnails_without_a_controller_is_none(host_factory, monkeypatch):
    monkeypatch.setattr(engine_host_mod, "VisionController", lambda **_k: (_ for _ in ()).throw(RuntimeError()))
    host = host_factory({"enabled": True})
    assert host.vision_thumbnails("x") is None


def test_host_thumbnails_returns_the_controller_data_urls(host_factory):
    host = host_factory({"enabled": True})
    host.vision._store_thumbnails("abc", [b"\xff\xd8"])
    assert host.vision_thumbnails("abc") == ["data:image/jpeg;base64,/9g="]


# --------------------------------------------------------- V1.13 describing phase


def test_describing_event_carries_a_boolean_only(host_factory):
    host = host_factory()

    host._on_vision_describing()

    events = [e for e in host.event_log.since(0)["events"] if e["action"] == "vision_describing"]
    assert [(e["source"], e["detail"]) for e in events] == [("vision", {"describing": True})]


def test_host_wires_the_describing_phase_into_the_step(host_factory):
    host = host_factory({"enabled": True})

    host.vision.step._on_describing()

    actions = [e["action"] for e in host.event_log.since(0)["events"]]
    assert "vision_describing" in actions


def test_event_model_accepts_the_describing_detail():
    from opencohost.api.models import EventOut

    out = EventOut(seq=1, ts=0.0, source="vision", action="vision_describing", detail={"describing": True})
    assert out.detail == {"describing": True}


# ------------------------------------------- V1.17 model inventory and selection


class FakeInventory:
    def __init__(self, reachable=True, installed=None):
        self.reachable = reachable
        self.installed = installed if installed is not None else []
        self.calls = []

    def snapshot(self, base_urls):
        self.calls.append(list(base_urls))
        return SimpleNamespace(reachable=self.reachable, installed=list(self.installed))


def test_vision_models_uses_the_main_daemon_while_vision_is_off(host_factory, monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    host = host_factory()
    host._vision_inventory = FakeInventory(
        installed=[{"tag": "llama3", "vision": False, "size_bytes": 1}]
    )

    body = host.vision_models()

    assert host._vision_inventory.calls == [["http://127.0.0.1:11434"]]
    assert body["current"] == settings.VISION_MODEL
    assert body["installed"] == [{"tag": "llama3", "vision": False, "size_bytes": 1}]
    assert body["ollama_reachable"] is True
    assert [m["tag"] for m in body["suggested"]] == ["qwen3-vl:4b-instruct", "qwen3-vl:2b-instruct"]


def test_vision_models_includes_the_static_benchmarks(host_factory):
    from opencohost.core.vision.catalog import VISION_BENCHMARKS

    host = host_factory()
    host._vision_inventory = FakeInventory()

    body = host.vision_models()

    assert body["benchmarks"] == VISION_BENCHMARKS
    assert body["benchmarks"]["qwen3-vl:4b-instruct"]["verdict"] == "recommended"


def test_vision_models_prefers_the_running_vision_daemon(host_factory, monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    host = host_factory({"enabled": True})
    host._vision_inventory = FakeInventory()

    host.vision_models()

    port = settings.VISION_PORT
    assert host._vision_inventory.calls == [[f"http://127.0.0.1:{port}", "http://127.0.0.1:11434"]]


def test_vision_models_fails_open_when_ollama_is_unreachable(host_factory):
    host = host_factory()
    host._vision_inventory = FakeInventory(reachable=False)

    body = host.vision_models()

    assert body["installed"] == [] and body["ollama_reachable"] is False
    assert "gemma4:e4b" in body["benchmarks"]


def test_vision_models_survives_an_inventory_crash(host_factory):
    host = host_factory()

    class _Boom:
        def snapshot(self, base_urls):
            raise RuntimeError("boom")

    host._vision_inventory = _Boom()

    body = host.vision_models()

    assert body["installed"] == [] and body["ollama_reachable"] is False
    assert body["current"] == settings.VISION_MODEL
    assert body["benchmarks"]["qwen3-vl:2b-instruct"]["verdict"] == "light"


def test_model_change_while_enabled_unloads_old_and_warms_new(host_factory):
    host = host_factory({"enabled": True})
    old_model = settings.VISION_MODEL
    old_daemon = FakeDaemon.instances[0]

    host.vision_apply_settings({"model": "qwen3-vl:2b-instruct"})

    assert old_daemon.unloaded == old_model
    assert old_daemon.stop_calls == 1
    new_daemon = FakeDaemon.instances[-1]
    assert new_daemon is not old_daemon
    assert new_daemon.warmed == ("qwen3-vl:2b-instruct", -1)
    status = host.vision_status()
    assert status["state"] == "ready"
    assert status["model"] == "qwen3-vl:2b-instruct"
    assert vs.load(base=vs.current()).model == "qwen3-vl:2b-instruct"
