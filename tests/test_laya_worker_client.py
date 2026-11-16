"""laya-decision-layer-20261009 T2: LayaWorkerClient against a fake worker.

The fake worker (tests/fixtures/fake_laya_worker.py) runs under
``sys.executable -I`` and speaks the real protocol with canned answers, so no
test imports ``laya`` or loads a model.
"""
from __future__ import annotations

import json
import os
import sys
import time

import pytest

from opencohost.core.decision import DecisionBus
from opencohost.core.decision import runtime as decision_runtime
from opencohost.core.decision.laya_client import LayaWorkerClient
from opencohost.core.decision.questions import QUESTIONS

FAKE = os.path.join(os.path.dirname(__file__), "fixtures", "fake_laya_worker.py")
Q = {"vision": QUESTIONS["vision"], "memory": QUESTIONS["memory"]}


@pytest.fixture
def make_client(monkeypatch, tmp_path):
    clients = []

    def _make(mode="ok", answers=None, **kwargs):
        monkeypatch.setenv("FAKE_LAYA_MODE", mode)
        monkeypatch.setenv("FAKE_LAYA_SPAWNS", str(tmp_path / "spawns.txt"))
        if answers is not None:
            monkeypatch.setenv("FAKE_LAYA_ANSWERS", json.dumps(answers))
        kwargs.setdefault("startup_timeout_s", 10.0)
        kwargs.setdefault("engine", "torch")
        python = kwargs.pop("python", sys.executable)
        client = LayaWorkerClient(python, FAKE, **kwargs)
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.stop()


def _spawns(tmp_path):
    path = tmp_path / "spawns.txt"
    return len(path.read_text().splitlines()) if path.exists() else 0


def _wait_state(client, states, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.status()["state"] in states:
            return client.status()["state"]
        time.sleep(0.02)
    return client.status()["state"]


# --- configuration -----------------------------------------------------------


@pytest.mark.parametrize("python", ["", None, "Z:/does/not/exist/python.exe"])
def test_unconfigured_is_unavailable(python):
    client = LayaWorkerClient(python, FAKE, engine="torch")
    assert client.status() == {
        "configured": False, "state": "unavailable", "checkpoint": None, "last_error": None,
        "engine": "torch",
    }
    assert client.available() is False
    client.start()
    assert client.status()["state"] == "unavailable"
    assert client.ask("hola", Q, 0.5) is None


def test_missing_worker_script_is_unavailable(tmp_path):
    client = LayaWorkerClient(sys.executable, str(tmp_path / "nope.py"), engine="torch")
    assert client.status()["configured"] is False


def test_default_worker_path_is_the_shipped_script():
    client = LayaWorkerClient(sys.executable, engine="torch")
    assert client.worker_path.endswith(os.path.join("core", "decision", "laya_worker.py"))
    assert os.path.isfile(client.worker_path)


# --- lifecycle ---------------------------------------------------------------


def test_start_is_non_blocking_then_ready(make_client):
    client = make_client(checkpoint="multilingual")
    started = time.monotonic()
    client.start()
    assert time.monotonic() - started < 0.5
    assert client.status()["state"] in ("starting", "ready")
    assert _wait_state(client, {"ready"}) == "ready"
    status = client.status()
    assert status["configured"] is True and status["checkpoint"] == "multilingual"
    assert client.available() is True


def test_ask_returns_worker_answers(make_client):
    client = make_client(answers={"vision": {"choice": "look", "answer_confidence": 0.97}})
    client.start()
    assert client.wait_ready(10)
    resp = client.ask("mira esto", Q, 5.0)
    assert resp["ok"] is True
    assert resp["answers"]["vision"] == {"choice": "look", "answer_confidence": 0.97}
    assert resp["answers"]["memory"]["choice"] == "recall"
    assert resp["state_len"] == len("mira esto")


def test_ask_before_ready_returns_none(make_client):
    client = make_client(mode="slow_ping")
    client.start()
    assert client.ask("hola", Q, 0.2) is None


def test_not_ready_worker_is_error(make_client):
    client = make_client(mode="not_ready")
    client.start()
    assert _wait_state(client, {"error"}) == "error"
    assert client.status()["last_error"] == "not_ready"
    assert client.available() is False


def test_startup_timeout_is_error(make_client):
    client = make_client(mode="slow_ping", startup_timeout_s=0.5)
    client.start()
    assert _wait_state(client, {"error"}) == "error"
    assert client.status()["last_error"] == "startup_timeout"


# --- failure modes -----------------------------------------------------------


def test_timeout_returns_none_and_late_answers_are_discarded(make_client):
    client = make_client(mode="hang")
    client.start()
    assert client.wait_ready(10)
    started = time.monotonic()
    assert client.ask("hola", Q, 0.3) is None
    assert time.monotonic() - started < 2.0
    assert client._pending == {}


def test_malformed_output_times_out_and_stays_ready(make_client):
    client = make_client(mode="malformed")
    client.start()
    assert client.wait_ready(10)
    assert client.ask("hola", Q, 0.5) is None
    assert client.status()["state"] == "ready"
    assert client.status()["last_error"] == "malformed_output"


def test_crash_restarts_once_then_recovers(make_client, tmp_path):
    client = make_client(mode="crash_once")
    client.start()
    assert client.wait_ready(10)
    assert client.ask("hola", Q, 2.0) is None  # the worker dies on this request
    assert _wait_state(client, {"ready"}) == "ready"
    assert _spawns(tmp_path) == 2
    assert client.ask("hola", Q, 5.0)["ok"] is True


def test_second_crash_stays_unavailable_until_restart(make_client, tmp_path):
    client = make_client(mode="crash")
    client.start()
    assert client.wait_ready(10)
    client.ask("hola", Q, 2.0)
    assert client.wait_ready(10)  # restarted once
    client.ask("hola", Q, 2.0)
    assert _wait_state(client, {"error"}) == "error"
    time.sleep(0.3)
    assert _spawns(tmp_path) == 2
    assert client.status()["last_error"] == "worker_exited"
    assert client.available() is False
    client.restart()
    assert client.wait_ready(10)
    assert _spawns(tmp_path) == 3


def test_exit_at_start_restarts_once_then_error(make_client, tmp_path):
    client = make_client(mode="exit_at_start")
    client.start()
    assert _wait_state(client, {"error"}) == "error"
    time.sleep(0.3)
    assert _spawns(tmp_path) == 2


def test_stop_terminates_the_worker(make_client):
    client = make_client()
    client.start()
    assert client.wait_ready(10)
    proc = client._proc
    client.stop()
    assert proc.poll() is not None
    assert client.status()["state"] == "off"
    assert client.ask("hola", Q, 0.5) is None
    client.stop()  # idempotent


# --- bus + runtime -----------------------------------------------------------


def test_bus_end_to_end_with_fake_worker(make_client):
    client = make_client(answers={"vision": {"choice": "look", "answer_confidence": 0.97}})
    client.start()
    assert client.wait_ready(10)
    turn = DecisionBus(client).decide("mira esto", "direct", ids=("vision",))
    out = turn.resolve("vision", "no_look")
    assert (out.status, out.laya_value, out.acted_by) == ("ok", "look", "rule")


def test_runtime_start_unconfigured_leaves_bus_without_backend():
    client = decision_runtime.start()
    assert client is not None and client.status()["state"] == "unavailable"
    # conftest points OPENCOHOST_LAYA_ONNX_DIR at an empty tmp dir: onnx is the default engine
    assert client.status()["engine"] == "onnx"
    assert client.status()["last_error"] == "onnx_artifacts_missing"
    assert decision_runtime.get_bus().backend is None
    assert decision_runtime.worker_status()["configured"] is False
    decision_runtime.stop()


def test_runtime_start_and_stop_with_fake_worker(make_client):
    client = make_client()
    assert decision_runtime.start(lambda: client) is client
    assert decision_runtime.get_bus().backend is client
    assert client.wait_ready(10)
    assert decision_runtime.worker_status()["state"] == "ready"
    proc = client._proc
    decision_runtime.stop()
    assert proc.poll() is not None
    assert decision_runtime.get_bus().backend is None
    assert decision_runtime.worker_status()["state"] == "unavailable"


def test_shadow_respond_is_noop_without_backend():
    assert decision_runtime.shadow_respond("hola", "ignore") is False


# --- ONNX engine (T8) ----------------------------------------------------------


def _fake_artifacts(dirpath):
    import hashlib

    from opencohost.core.decision import laya_onnx

    files = {}
    for name in laya_onnx.REQUIRED_FILES:
        data = name.encode()
        (dirpath / name).write_bytes(data)
        files[name] = hashlib.sha256(data).hexdigest()
    (dirpath / laya_onnx.MANIFEST).write_text(json.dumps({"format": 1, "files": files}), encoding="utf-8")
    return str(dirpath)


def test_onnx_is_the_default_engine_and_runs_under_this_interpreter(tmp_path):
    client = LayaWorkerClient(None, FAKE, onnx_dir=str(tmp_path))
    assert client.engine == "onnx"
    assert client.python == sys.executable


def test_onnx_honours_an_explicit_interpreter(tmp_path):
    client = LayaWorkerClient("Z:/other/python.exe", FAKE, onnx_dir=str(tmp_path))
    assert client.python == "Z:/other/python.exe"


def test_onnx_missing_artifacts_is_unavailable_with_reason(tmp_path):
    client = LayaWorkerClient(None, FAKE, onnx_dir=str(tmp_path / "missing"))
    assert client.status() == {
        "configured": False, "state": "unavailable", "checkpoint": None,
        "last_error": "onnx_artifacts_missing", "engine": "onnx",
    }
    client.start()
    assert client.status()["state"] == "unavailable"
    assert client._proc is None
    assert client.ask("hola", Q, 0.5) is None


def test_unknown_engine_falls_back_to_onnx(tmp_path):
    assert LayaWorkerClient(None, FAKE, engine="bogus", onnx_dir=str(tmp_path)).engine == "onnx"


def test_onnx_worker_gets_engine_and_dir_and_becomes_ready(make_client, monkeypatch, tmp_path):
    art = tmp_path / "art"
    art.mkdir()
    env_out = tmp_path / "env.json"
    monkeypatch.setenv("FAKE_LAYA_ENV_OUT", str(env_out))
    client = make_client(engine="onnx", python=None, onnx_dir=_fake_artifacts(art))
    client.start()
    assert client.wait_ready(10)
    assert client.status()["engine"] == "onnx" and client.status()["configured"] is True
    assert json.loads(env_out.read_text(encoding="utf-8")) == {
        "OPENCOHOST_LAYA_ENGINE": "onnx", "OPENCOHOST_LAYA_ONNX_DIR": str(art),
    }


def test_worker_reported_unavailable_artifacts_are_unavailable_not_error(make_client, tmp_path):
    art = tmp_path / "art"
    art.mkdir()
    client = make_client(mode="unavailable", engine="onnx", python=None, onnx_dir=_fake_artifacts(art))
    client.start()
    assert _wait_state(client, {"unavailable", "error"}) == "unavailable"
    assert client.status()["last_error"] == "onnx_sha256_mismatch"
    assert client.available() is False
    deadline = time.monotonic() + 5
    while client._proc is not None and time.monotonic() < deadline:
        time.sleep(0.02)
    assert client._proc is None  # the useless worker is not left running
