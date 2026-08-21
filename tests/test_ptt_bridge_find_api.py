"""find_api must survive a cold backend start.

The Tauri shell now spawns the packaged PTT module alongside uvicorn, and the
backend's lifespan (engine warm-up) can take a long while before /api/health
answers. A single probe sweep that sys.exit()s on failure kills the bridge
before the API exists — find_api has to retry until a deadline instead.
"""

import importlib
import sys

import pytest


@pytest.fixture()
def bridge(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["opencohost-ptt"])
    monkeypatch.delenv("OPENCOHOST_API_URL", raising=False)
    sys.modules.pop("opencohost.api.ptt_f10_bridge", None)
    return importlib.import_module("opencohost.api.ptt_f10_bridge")


class _Resp:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_find_api_retries_until_backend_appears(bridge, monkeypatch):
    calls = {"n": 0}

    def fake_urlopen(url, timeout=0):
        calls["n"] += 1
        if calls["n"] < 5:
            raise OSError("connection refused")
        return _Resp()

    monkeypatch.setattr(bridge.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)

    base = bridge.find_api(deadline_s=60.0, sleep_s=0.0)

    assert base in ("http://127.0.0.1:8770", "http://127.0.0.1:8765")
    assert calls["n"] >= 5


def test_find_api_exits_after_deadline(bridge, monkeypatch):
    def always_refused(url, timeout=0):
        raise OSError("connection refused")

    monkeypatch.setattr(bridge.urllib.request, "urlopen", always_refused)
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)

    with pytest.raises(SystemExit):
        bridge.find_api(deadline_s=0.0, sleep_s=0.0)
