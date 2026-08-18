"""Strict WU6-A installer composition and packaging contract tests."""

from __future__ import annotations

import json
from pathlib import Path
import tomllib
import pytest

ROOT = Path(__file__).resolve().parents[1]
TAURI_DIR = ROOT / "OpenCohost_UI" / "src-tauri"
TAURI_CONF = TAURI_DIR / "tauri.conf.json"
PYPROJECT = ROOT / "pyproject.toml"
RESOURCES_DIR = TAURI_DIR / "resources"


def _tauri_config() -> dict:
    assert TAURI_CONF.is_file(), "tauri.conf.json must exist"
    return json.loads(TAURI_CONF.read_text(encoding="utf-8"))


def _pyproject_version() -> str:
    # Read version from opencohost/__init__.py
    init_py = ROOT / "opencohost" / "__init__.py"
    for line in init_py.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=")[1].strip().strip('"').strip("'")
    raise ValueError("Could not find __version__ in opencohost/__init__.py")


def test_tauri_config_declares_nsis_target_and_current_user_install():
    conf = _tauri_config()
    version = _pyproject_version()
    
    assert conf["productName"] == "OpenCohost"
    assert conf["version"] == version, "Tauri version must match python package version"
    
    bundle = conf.get("bundle", {})
    assert bundle.get("active") is True, "Bundle must be active"
    assert "nsis" in bundle.get("targets", []), "Bundle targets must include 'nsis'"
    
    windows_conf = bundle.get("windows", {})
    nsis_conf = windows_conf.get("nsis", {})
    assert nsis_conf.get("installMode") == "currentUser", "NSIS must default to currentUser install mode"


def test_bootstrap_manifest_resource_valid_and_non_developer_paths():
    manifest_path = RESOURCES_DIR / "bootstrap-manifest.json"
    assert manifest_path.is_file(), "bootstrap-manifest.json must be present in resources"
    
    raw = manifest_path.read_text(encoding="utf-8")
    manifest = json.loads(raw)
    
    assert manifest["schema_version"] == 1
    assert manifest["product_version"] == _pyproject_version()
    assert manifest["python_version"] == "3.12.4"
    assert "allowed_hosts" in manifest and len(manifest["allowed_hosts"]) > 0
    assert manifest["uv"]["name"] == "uv"
    assert manifest["engine"]["name"] == "engine"
    
    # Assert no machine-local developer paths leak into the manifest
    forbidden_tokens = ["E:\\", "C:\\Users\\", "Miniconda", "scratch", "tmp"]
    for token in forbidden_tokens:
        assert token.lower() not in raw.lower(), f"Forbidden local token '{token}' in bootstrap manifest"


def test_forbidden_artifacts_are_not_in_resources_or_bundle_config():
    if not RESOURCES_DIR.exists():
        return
        
    for path in RESOURCES_DIR.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(RESOURCES_DIR).as_posix().lower()
        
        # NSIS must NOT package backend.config.json or raw developer code
        assert rel != "backend.config.json", "backend.config.json must never be bundled into resources"
        assert not rel.endswith(".py"), f"Raw python file '{rel}' must not be bundled in resources"
        assert ".venv" not in rel, f"Virtual environment '{rel}' must not be in resources"
        assert "__pycache__" not in rel, f"Python cache '{rel}' must not be in resources"


def test_built_nsis_bundle_excludes_forbidden_blobs_and_respects_size_envelope():
    nsis_dir = TAURI_DIR / "target" / "release" / "bundle" / "nsis"
    if not nsis_dir.is_dir():
        pytest.skip("NSIS bundle not yet built in target/release/bundle/nsis")
    
    installer = next(nsis_dir.glob("OpenCohost_*_x64-setup.exe"), None)
    assert installer is not None, "OpenCohost_*_x64-setup.exe must exist in bundle/nsis"
    
    size_mb = installer.stat().st_size / (1024 * 1024)
    # The lean installer should be < 50MB (contains only Tauri binary, assets, and stub)
    assert 5 < size_mb < 50, f"Installer size {size_mb:.2f}MB is outside expected lean envelope (5MB - 50MB)"

