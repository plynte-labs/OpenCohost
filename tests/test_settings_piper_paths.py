"""Piper packaged-path resolution without loading a TTS runtime."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from opencohost.config import settings


@pytest.mark.parametrize(
    ("candidate_kind",),
    [
        ("resources_dir",),
        ("prefix_parent",),
        ("executable_parent",),
    ],
)
def test_resolve_piper_voice_file_uses_packaged_fallbacks(monkeypatch, tmp_path: Path, candidate_kind: str):
    filename = "voice.onnx"
    monkeypatch.setattr(settings, "STORAGE_PATHS", SimpleNamespace(cache_root=tmp_path / "cache"))
    monkeypatch.setattr(settings.os.path, "isfile", lambda path: path == candidate)
    prefix_root = tmp_path / "prefix-root"
    executable_root = tmp_path / "executable-root"
    monkeypatch.setattr(settings.sys, "prefix", str(prefix_root / "venv"))
    monkeypatch.setattr(settings.sys, "executable", str(executable_root / "bin" / "opencohost.exe"))
    monkeypatch.delenv("OPENCOHOST_RESOURCES_DIR", raising=False)

    if candidate_kind == "resources_dir":
        resource_root = tmp_path / "resources-root"
        monkeypatch.setenv("OPENCOHOST_RESOURCES_DIR", str(resource_root))
        candidate = os.path.join(str(resource_root), "resources", "piper", filename)
    elif candidate_kind == "prefix_parent":
        candidate = os.path.join(str(prefix_root), "piper", filename)
    else:
        candidate = os.path.join(str(executable_root), "piper", filename)
        monkeypatch.setattr(settings.sys, "prefix", str(tmp_path / "different-prefix" / "venv"))

    assert settings._resolve_piper_voice_file(filename) == candidate
