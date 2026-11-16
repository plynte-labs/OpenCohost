"""F2 flag cuts (fable-roadmap-20261112): two unused diagnostic flags are gone.

With the env unset the default behavior is unchanged; with the env set, the
removed flags must no longer be read.
"""
from __future__ import annotations

import importlib
import time

import pytest
from fastapi.testclient import TestClient

from tests.test_api_phase1 import FakeHost


@pytest.fixture(autouse=True)
def _reset_host_active():
    import opencohost.api.main as main_mod

    main_mod._host_active = False
    yield
    main_mod._host_active = False


def test_memory_promotion_diagnostics_flag_is_not_read(monkeypatch):
    monkeypatch.setenv("OPENCOHOST_MEMORY_PROMOTION_DIAGNOSTICS", "1")
    from opencohost.config import settings

    importlib.reload(settings)
    try:
        assert not hasattr(settings, "MEMORY_PROMOTION_DIAGNOSTICS")
    finally:
        monkeypatch.delenv("OPENCOHOST_MEMORY_PROMOTION_DIAGNOSTICS")
        importlib.reload(settings)


def test_promotion_diagnostic_emitter_is_removed():
    from opencohost.core.engine import llm_engine_scout as scout

    assert not hasattr(scout, "_PROMOTION_DIAGNOSTIC_LOG")
    assert not any(
        hasattr(cls, "_promotion_diagnostic")
        for cls in vars(scout).values()
        if isinstance(cls, type)
    )


class _FakeSupervisor:
    def __init__(self):
        self.started = 0

    def ensure_started(self):
        self.started += 1

    def close(self):
        pass


def test_prewarm_liveaudio_env_no_longer_starts_supervisor(monkeypatch):
    import opencohost.api.main as main_mod

    monkeypatch.setenv("OPENCOHOST_PREWARM_LIVEAUDIO", "1")
    monkeypatch.setattr(main_mod, "load_ptt_ws_uri", lambda: "ws://127.0.0.1:9090")
    sup = _FakeSupervisor()
    app = main_mod.create_app(
        host_factory=FakeHost,
        cors_origins=["http://localhost:5173"],
        liveaudio_supervisor_factory=lambda: sup,
    )
    with TestClient(app):
        time.sleep(0.3)
    assert sup.started == 0
