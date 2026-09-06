import os

import pytest


@pytest.fixture(autouse=True)
def _iso_tmp(monkeypatch, tmp_path):
    td = str(tmp_path / "iso")
    os.makedirs(td, exist_ok=True)
    monkeypatch.setenv("OPENCOHOST_DATA_ROOT", td)
    monkeypatch.setenv("OPENCOHOST_MEMORY_V5_MODE", "OFF")
    monkeypatch.delenv("OPENCOHOST_MEMORY_V5_SHADOW_DB", raising=False)
    yield
