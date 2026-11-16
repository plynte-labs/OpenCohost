"""laya-decision-layer-20261009 T2: the real worker script's protocol handling.

The script is loaded from its path with a fake router injected, so ``laya``
is never imported. It must not import ``opencohost`` (it runs in another env).
"""
from __future__ import annotations

import importlib.util
import io
import json
import os

import pytest

from opencohost.core.decision.questions import QUESTIONS

WORKER = os.path.join(
    os.path.dirname(os.path.dirname(__file__)), "opencohost", "core", "decision", "laya_worker.py"
)
SECRET = "zanahoriaconfidencial"


@pytest.fixture
def worker():
    spec = importlib.util.spec_from_file_location("laya_worker_under_test", WORKER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeRouter:
    def __init__(self, result=None, exc=None):
        self.result = result
        self.exc = exc
        self.calls = []

    def predict(self, state, questions, **kwargs):
        self.calls.append((state, questions, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.result


def _result():
    return {
        "answers": {
            "vision": {
                "choice": "look",
                "probabilities": {"look": 0.93, "no_look": 0.07},
                "answer_confidence": 0.93,
                "confidence": 0.88,
                "action": "dropped",
                "type": "choice",
            }
        },
        "routing": {"model": "multilingual"},
    }


def test_script_does_not_import_opencohost():
    with open(WORKER, encoding="utf-8") as f:
        src = f.read()
    assert "opencohost" not in src.replace("OPENCOHOST_", "")


def test_request_answer_shape(worker):
    router = FakeRouter(_result())
    resp = worker.handle(router, {"id": 7, "state": "mira", "questions": {"vision": QUESTIONS["vision"]}})
    assert resp == {
        "id": 7,
        "ok": True,
        "answers": {
            "vision": {
                "choice": "look",
                "probabilities": {"look": 0.93, "no_look": 0.07},
                "answer_confidence": 0.93,
                "confidence": 0.88,
            }
        },
        "routing_model": "multilingual",
    }
    assert router.calls[0][0] == "mira"


def test_max_len_is_forwarded(worker):
    router = FakeRouter(_result())
    worker.handle(router, {"id": 1, "state": "x", "questions": {"vision": QUESTIONS["vision"]}, "max_len": 128})
    assert router.calls[0][2] == {"max_len": 128}


def test_ping(worker):
    assert worker.handle(object(), {"op": "ping", "id": "p1"}) == {"id": "p1", "ok": True, "ready": True}
    assert worker.handle(None, {"op": "ping"}) == {"id": None, "ok": True, "ready": False}


def test_not_ready_request(worker):
    assert worker.handle(None, {"id": 2, "state": "x", "questions": {}}) == {
        "id": 2, "ok": False, "error": "not_ready",
    }


@pytest.mark.parametrize(
    "msg,code",
    [
        ({"id": 3, "questions": {}}, "bad_request"),
        ({"id": 3, "state": "x", "questions": []}, "bad_request"),
        ([1, 2], "bad_request"),
    ],
)
def test_bad_requests(worker, msg, code):
    resp = worker.handle(FakeRouter(_result()), msg)
    assert resp["ok"] is False and resp["error"] == code


def test_predict_exception_is_short_code_without_state(worker):
    router = FakeRouter(exc=RuntimeError(f"boom {SECRET}"))
    resp = worker.handle(router, {"id": 4, "state": SECRET, "questions": {"vision": QUESTIONS["vision"]}})
    assert resp == {"id": 4, "ok": False, "error": "predict_RuntimeError"}


def test_malformed_result_is_error(worker):
    resp = worker.handle(FakeRouter("nope"), {"id": 5, "state": "x", "questions": {"vision": QUESTIONS["vision"]}})
    assert resp == {"id": 5, "ok": False, "error": "bad_result"}


def test_serve_loop_handles_bad_json_and_never_echoes_state(worker):
    router = FakeRouter(_result())
    lines = [
        b"not json\n",
        (json.dumps({"id": 9, "state": SECRET, "questions": {"vision": QUESTIONS["vision"]}}) + "\n").encode(),
        b"\n",
    ]
    out = io.BytesIO()
    worker.serve(router, io.BytesIO(b"".join(lines)), out)
    replies = [json.loads(l) for l in out.getvalue().decode().splitlines()]
    assert replies[0] == {"id": None, "ok": False, "error": "bad_json"}
    assert replies[1]["id"] == 9 and replies[1]["ok"] is True
    assert SECRET not in out.getvalue().decode()


def test_checkpoint_is_pinned_per_predict(worker):
    router = FakeRouter(_result())
    worker.handle(router, {"id": 1, "state": "x", "questions": {"vision": QUESTIONS["vision"]}}, "multilingual")
    assert router.calls[0][2] == {"model": "multilingual"}
    worker.handle(router, {"id": 2, "state": "x", "questions": {"vision": QUESTIONS["vision"]}})
    assert router.calls[1][2] == {}


def test_load_router_preloads(worker, monkeypatch):
    calls = []

    class FakeLaya:
        class Router:
            def __init__(self, **kwargs):
                calls.append(kwargs)

    monkeypatch.setattr(worker, "_import_laya", lambda: FakeLaya)
    worker.load_router()
    assert calls == [{"preload": True}]


# --- engine selection (T8) -----------------------------------------------------


@pytest.mark.parametrize("raw,engine", [
    (None, "onnx"), ("", "onnx"), (" ONNX ", "onnx"), ("bogus", "onnx"), ("torch", "torch"), (" Torch", "torch"),
])
def test_resolve_engine_defaults_to_onnx(worker, raw, engine):
    assert worker.resolve_engine(raw) == engine


def test_onnx_engine_missing_artifacts_is_a_reason_code(worker, tmp_path):
    with pytest.raises(Exception) as err:
        worker.load_router("onnx", str(tmp_path / "missing"))
    assert worker.load_error_code("onnx", err.value) == "onnx_artifacts_missing"


def test_onnx_engine_loads_the_sibling_runtime_by_path(worker, monkeypatch, tmp_path):
    seen = {}

    class FakeRuntime:
        @staticmethod
        def LayaOnnx(path):
            seen["path"] = path
            return "model"

    monkeypatch.setattr(worker, "_import_laya_onnx", lambda: FakeRuntime)
    assert worker.load_router("onnx", str(tmp_path)) == "model"
    assert seen == {"path": str(tmp_path)}
    runtime = worker._load_sibling("laya_onnx")
    assert os.path.dirname(runtime.__file__) == os.path.dirname(WORKER)


def test_load_error_codes(worker):
    assert worker.load_error_code("onnx", RuntimeError("x")) == "onnx_load_RuntimeError"
    assert worker.load_error_code("torch", RuntimeError("x")) is None


def test_ping_reports_the_load_error(worker):
    assert worker.handle(None, {"op": "ping", "id": 1}, load_error="onnx_sha256_mismatch") == {
        "id": 1, "ok": True, "ready": False, "error": "onnx_sha256_mismatch",
    }
    assert worker.handle(object(), {"op": "ping", "id": 2}, load_error="ignored") == {
        "id": 2, "ok": True, "ready": True,
    }


def test_real_worker_process_reports_missing_artifacts(tmp_path):
    """The shipped script, run as the client runs it (``python -I``) with the
    ONNX engine and no artifacts: it answers the ping as not ready with the
    reason code, and never imports laya."""
    import subprocess
    import sys

    env = dict(os.environ)
    env["OPENCOHOST_LAYA_ENGINE"] = "onnx"
    env["OPENCOHOST_LAYA_ONNX_DIR"] = str(tmp_path / "missing")
    proc = subprocess.run(
        [sys.executable, "-I", WORKER],
        input=b'{"op": "ping", "id": 1}\n',
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert json.loads(proc.stdout.decode().strip()) == {
        "id": 1, "ok": True, "ready": False, "error": "onnx_artifacts_missing",
    }
