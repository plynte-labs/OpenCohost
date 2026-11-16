"""Read-only legacy install detection (fresh-start-notice N2).

``winreg`` is faked: nothing here touches the real registry.
"""

import sys
import types

import pytest
from fastapi.testclient import TestClient

from opencohost.config import settings, setup_state
from opencohost.core.engine import llm_readiness
from tests.test_api_phase1 import FakeHost

_KEY = r"Software\opencohost\OpenCohost"


def _fake_winreg(monkeypatch, *, value=None, open_error=None, query_error=None):
    calls = {"opened": [], "queried": []}
    mod = types.ModuleType("winreg")
    mod.HKEY_CURRENT_USER = "HKCU"

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def open_key(root, sub, *a, **k):
        calls["opened"].append((root, sub))
        if open_error is not None:
            raise open_error
        return _Key()

    def query_value_ex(key, name):
        calls["queried"].append(name)
        if query_error is not None:
            raise query_error
        return (value, 1)

    mod.OpenKey = open_key
    mod.QueryValueEx = query_value_ex
    monkeypatch.setitem(sys.modules, "winreg", mod)
    monkeypatch.setattr(sys, "platform", "win32")
    return calls


def test_present_value_is_a_legacy_install(monkeypatch):
    calls = _fake_winreg(monkeypatch, value=r"C:\old\OpenCohost")
    assert setup_state.detect_legacy_install() is True
    assert calls["opened"] == [("HKCU", _KEY)]
    assert calls["queried"] == [""]


@pytest.mark.parametrize("value", ["", "   ", None])
def test_empty_value_is_not_legacy(monkeypatch, value):
    _fake_winreg(monkeypatch, value=value)
    assert setup_state.detect_legacy_install() is False


def test_missing_key_is_not_legacy(monkeypatch):
    _fake_winreg(monkeypatch, open_error=FileNotFoundError())
    assert setup_state.detect_legacy_install() is False


def test_any_error_is_not_legacy(monkeypatch):
    _fake_winreg(monkeypatch, query_error=PermissionError())
    assert setup_state.detect_legacy_install() is False


def test_non_windows_is_not_legacy(monkeypatch):
    calls = _fake_winreg(monkeypatch, value="x")
    monkeypatch.setattr(sys, "platform", "linux")
    assert setup_state.detect_legacy_install() is False
    assert calls["opened"] == []


def test_state_response_exposes_the_flag(tmp_path, monkeypatch):
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    monkeypatch.setattr(settings, "SETUP_STATE_FILE", str(tmp_path / "setup_state.json"))
    monkeypatch.setattr(settings, "SETUP_CHECKPOINT_FILE", str(tmp_path / "setup_checkpoint.json"))
    monkeypatch.setattr(llm_readiness, "_ping_ollama", lambda *a, **k: (False, None, []))
    monkeypatch.setattr(llm_readiness, "_check_cloud_key_configured", lambda *a, **k: False)
    from opencohost.api.routers import setup as setup_router

    for flag in (True, False):
        monkeypatch.setattr(setup_router.setup_state, "detect_legacy_install", lambda flag=flag: flag)
        app = main_mod.create_app(host_factory=FakeHost, cors_origins=["http://localhost:5173"])
        with TestClient(app) as client:
            assert client.get("/api/setup/state").json()["legacy_install"] is flag
        main_mod._host_active = False
