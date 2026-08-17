"""Installed data-root ownership tests for the API engine lock."""

from pathlib import Path


def test_engine_host_default_lock_follows_explicit_data_root(tmp_path, monkeypatch):
    import opencohost.api.engine_host as engine_host

    data_root = tmp_path / "Open Cohost \u00fcnicode"
    monkeypatch.setenv("OPENCOHOST_DATA_ROOT", str(data_root))

    host = engine_host.EngineHost()

    assert Path(host._lock_path) == data_root.resolve() / "state" / "opencohost_api_engine.lock"


def test_engine_host_default_lock_keeps_development_temp_fallback(monkeypatch):
    import opencohost.api.engine_host as engine_host

    monkeypatch.delenv("OPENCOHOST_DATA_ROOT", raising=False)

    host = engine_host.EngineHost()

    assert Path(host._lock_path) == Path(engine_host.tempfile.gettempdir()) / "opencohost_api_engine.lock"
