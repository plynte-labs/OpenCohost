"""Strict WU7-Core release workflow and version parity contract tests."""

from __future__ import annotations

import json
from pathlib import Path
import re
import tomllib
import yaml
import pytest

ROOT = Path(__file__).resolve().parents[1]
RELEASE_YML = ROOT / ".github" / "workflows" / "release.yml"
PYPROJECT = ROOT / "pyproject.toml"
INIT_PY = ROOT / "opencohost" / "__init__.py"
TAURI_CONF = ROOT / "OpenCohost_UI" / "src-tauri" / "tauri.conf.json"
PACKAGE_JSON = ROOT / "OpenCohost_UI" / "package.json"


def _get_pyproject_version() -> str:
    text = INIT_PY.read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("__version__"):
            return line.split("=")[1].strip().strip('"').strip("'")
    raise ValueError("Could not find __version__ in opencohost/__init__.py")


def test_version_parity_across_repository_components():
    py_version = _get_pyproject_version()
    
    # Tauri conf version
    tauri_conf = json.loads(TAURI_CONF.read_text(encoding="utf-8"))
    assert tauri_conf["version"] == py_version, (
        f"Tauri config version ({tauri_conf['version']}) does not match python version ({py_version})"
    )
    
    # UI package.json version
    package_json = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    assert package_json["version"] == py_version, (
        f"UI package.json version ({package_json['version']}) does not match python version ({py_version})"
    )


def test_release_workflow_uses_tauri_nsis_and_submodules():
    assert RELEASE_YML.is_file(), "release.yml workflow must exist"
    content = RELEASE_YML.read_text(encoding="utf-8")
    data = yaml.safe_load(content)
    
    # Assert workflow trigger
    on_triggers = data.get("on", {})
    assert "push" in on_triggers or "workflow_dispatch" in on_triggers
    
    # Assert no obsolete PyInstaller or legacy launcher
    assert "pyinstaller" not in content.lower(), "release.yml must not use legacy PyInstaller"
    assert "launcher.spec" not in content.lower(), "release.yml must not reference launcher.spec"
    assert "miniconda" not in content.lower(), "release.yml must not reference miniconda"
    
    # Assert submodules checkout is recursive
    assert "submodules: recursive" in content or "submodules: 'recursive'" in content, (
        "release.yml checkout steps must specify submodules: recursive"
    )
    
    # Assert Tauri build and engine payload build
    assert "build_engine_payload.py" in content, "release.yml must build engine payload using build_engine_payload.py"
    assert "tauri build" in content or "tauri" in content, "release.yml must build the Tauri frontend and installer"
    assert "sha256" in content.lower(), "release.yml must compute SHA256 checksums for published artifacts"
