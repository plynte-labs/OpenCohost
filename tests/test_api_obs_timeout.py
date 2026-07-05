"""WU1 — POST /api/obs/test self-bounding timeout + websocket-leak guard.

The blocker: OBSClient.test_connection() opened an obsws websocket with NO
socket timeout, and _test_obs_connection_bounded wrapped it in a
`with ThreadPoolExecutor(...)` whose __exit__ shutdown(wait=True) JOINED the
stuck worker after the TimeoutError — so the "5s bound" never actually
returned. Fix: thread a real socket timeout into obsws ReqClient (self-bounding
connect) and use a manually-managed executor with shutdown(wait=False,
cancel_futures=True) as the backstop. Client lifetime is try/finally so
disconnect() always runs (folds the P3 websocket leak).

Seam: monkeypatch.setitem(sys.modules, "obsws_python", <fake>) — the import
happens INSIDE test_connection, so module injection takes effect per call.
"""

import sys
import threading
import time
import types

import pytest

from opencohost.avatar.obs_client import OBSClient, OBSConfig


def _make_client(tmp_path):
    return OBSClient(OBSConfig(host="localhost", port=4455, password="pw"), tmp_path)


def _fake_module(reqclient_cls):
    mod = types.ModuleType("obsws_python")
    mod.ReqClient = reqclient_cls
    return mod


def _record_reqclient():
    """Fake ReqClient factory that records init kwargs + disconnect calls.

    get_version() returns immediately with an obs_version attribute (success
    path). Returns (cls, records) where records is the shared state.
    """
    records = {"init_kwargs": None, "disconnected": False, "constructed": 0}

    class _FakeReqClient:
        def __init__(self, **kwargs):
            records["init_kwargs"] = kwargs
            records["constructed"] += 1

        def get_version(self):
            return types.SimpleNamespace(obs_version="30.1.2")

        def disconnect(self):
            records["disconnected"] = True

    return _FakeReqClient, records


# ──────────────────────────────────────────────────────────────────────────
# (1) blackhole: bounded call MUST return promptly and NOT join a stuck worker
# ──────────────────────────────────────────────────────────────────────────


def test_bounded_returns_on_stuck_worker_without_joining(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod

    # release is the safety net so a stuck fake worker can never hang the whole
    # suite: get_version blocks on it (bounded), and the test always sets it.
    release = threading.Event()

    class _BlackholeReqClient:
        def __init__(self, **kwargs):
            pass

        def get_version(self):
            release.wait(timeout=15.0)  # blocks like a dead socket would
            return types.SimpleNamespace(obs_version="?")

        def disconnect(self):
            pass

    monkeypatch.setitem(sys.modules, "obsws_python", _fake_module(_BlackholeReqClient))
    client = _make_client(tmp_path)

    result = {}
    done = threading.Event()

    def _run():
        result["val"] = main_mod._test_obs_connection_bounded(client, timeout=0.5)
        done.set()

    threading.Thread(target=_run, daemon=True).start()
    completed = done.wait(timeout=4.0)
    release.set()  # unblock the fake worker no matter what

    # Under the OLD code the `with ThreadPoolExecutor` __exit__ joins the stuck
    # worker -> _test_obs_connection_bounded never returns -> completed is False.
    assert completed, "_test_obs_connection_bounded did not return — it joined a stuck worker"
    ok, msg = result["val"]
    assert ok is False
    assert "timed out" in msg.lower()


# ──────────────────────────────────────────────────────────────────────────
# (2) the socket timeout is actually threaded into obsws ReqClient
# ──────────────────────────────────────────────────────────────────────────


def test_timeout_kwarg_reaches_reqclient(tmp_path, monkeypatch):
    fake_cls, records = _record_reqclient()
    monkeypatch.setitem(sys.modules, "obsws_python", _fake_module(fake_cls))
    client = _make_client(tmp_path)

    ok, _msg = client.test_connection(timeout=3.5)
    assert ok is True
    assert records["init_kwargs"]["timeout"] == 3.5


def test_bounded_passes_default_timeout_to_reqclient(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod

    fake_cls, records = _record_reqclient()
    monkeypatch.setitem(sys.modules, "obsws_python", _fake_module(fake_cls))
    client = _make_client(tmp_path)

    ok, _msg = main_mod._test_obs_connection_bounded(client)
    assert ok is True
    assert records["init_kwargs"]["timeout"] == main_mod._OBS_TEST_TIMEOUT_SECONDS


# ──────────────────────────────────────────────────────────────────────────
# (3)+(4) disconnect() runs on every exit path (websocket-leak guard)
# ──────────────────────────────────────────────────────────────────────────


def test_disconnect_called_on_success(tmp_path, monkeypatch):
    fake_cls, records = _record_reqclient()
    monkeypatch.setitem(sys.modules, "obsws_python", _fake_module(fake_cls))
    client = _make_client(tmp_path)

    ok, _msg = client.test_connection(timeout=1.0)
    assert ok is True
    assert records["disconnected"] is True


def test_disconnect_called_when_get_version_raises(tmp_path, monkeypatch):
    records = {"disconnected": False}

    class _RaisingReqClient:
        def __init__(self, **kwargs):
            pass

        def get_version(self):
            raise RuntimeError("boom mid-request")

        def disconnect(self):
            records["disconnected"] = True

    monkeypatch.setitem(sys.modules, "obsws_python", _fake_module(_RaisingReqClient))
    client = _make_client(tmp_path)

    ok, msg = client.test_connection(timeout=1.0)
    assert ok is False
    assert "boom" in msg
    assert records["disconnected"] is True  # leak guard: finally ran
