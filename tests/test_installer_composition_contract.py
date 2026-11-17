"""Strict WU6-A installer composition and packaging contract tests."""

from __future__ import annotations

import json
from pathlib import Path
import re
try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 (flux_env): tomllib arrived in 3.11
    import tomli as tomllib
import pytest

ROOT = Path(__file__).parent.parent.absolute()
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



def _normalize_to_semver(pep440: str) -> str:
    """Convert PEP 440 prerelease (e.g. '0.2.0rc1') to semver (e.g. '0.2.0-rc.1')."""
    m = re.match(r'^(\d+\.\d+\.\d+)(a|b|rc)(\d+)$', pep440)
    if m:
        label = {"a": "alpha", "b": "beta", "rc": "rc"}[m.group(2)]
        return f"{m.group(1)}-{label}.{m.group(3)}"
    return pep440


def test_tauri_config_declares_nsis_target_and_current_user_install():
    conf = _tauri_config()
    version = _normalize_to_semver(_pyproject_version())
    
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
    assert manifest["product_version"] == _normalize_to_semver(_pyproject_version())
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
        
        # NSIS must NOT package backend.config.json or active developer venvs
        assert "backend.config.json" not in rel, "backend.config.json must never be bundled into resources"
        assert ".venv" not in rel, f"Virtual environment '{rel}' must not be in resources"
        parts = rel.split("/")
        assert not ({"laya_env", "staging-backups"} & set(parts)), f"Developer tree '{rel}' must not be bundled"
        if "site-packages" in parts:
            distribution = parts[parts.index("site-packages") + 1]
            assert distribution not in {"torch", "transformers"}, f"Training distribution '{rel}' must not be bundled"
            assert not distribution.startswith(("torch-", "transformers-")), f"Training metadata '{rel}' must not be bundled"
        assert path.suffix.lower() not in {".db", ".sqlite", ".sqlite3", ".jsonl"}, f"Owner-state blob '{rel}' must not be bundled"
        assert path.name.lower() not in {"api_tokens.json", "perfiles.json", ".env"}, f"Owner configuration '{rel}' must not be bundled"

        # Files outside runtime/ must not be loose python scripts or caches
        if not rel.startswith("runtime/"):
            assert not rel.endswith(".py"), f"Raw python file '{rel}' must not be bundled outside runtime"
            assert "__pycache__" not in rel, f"Python cache '{rel}' must not be outside runtime in resources"


def test_extracted_runtime_structure_when_staged():
    runtime_dir = RESOURCES_DIR / "runtime"
    if not runtime_dir.is_dir():
        pytest.skip("runtime directory not yet staged in resources")

    assert (runtime_dir / "opencohost").is_dir(), "runtime/opencohost directory must exist"
    assert (runtime_dir / "opencohost" / "__init__.py").is_file(), "runtime/opencohost/__init__.py must exist"
    assert (runtime_dir / "config").is_dir(), "runtime/config directory must exist"
    assert (runtime_dir / "data" / "editorial_cards").is_dir(), "runtime/data/editorial_cards must exist"
    assert (runtime_dir / "data" / "memorias").is_dir(), "runtime/data/memorias must exist"
    assert (runtime_dir / "logs").is_dir(), "runtime/logs directory must exist"

    python_exe = runtime_dir / "python" / "python.exe"
    if python_exe.is_file():
        assert (runtime_dir / "python" / "Lib").is_dir(), "runtime/python/Lib must exist"


def test_built_nsis_bundle_excludes_forbidden_blobs_and_respects_size_envelope():
    nsis_dir = TAURI_DIR / "target" / "release" / "bundle" / "nsis"
    if not nsis_dir.is_dir():
        pytest.skip("NSIS bundle not yet built in target/release/bundle/nsis")
    
    version = _normalize_to_semver(_pyproject_version())
    installer = nsis_dir / f"OpenCohost_{version}_x64-setup.exe"
    if not installer.is_file():
        installers = sorted(nsis_dir.glob("OpenCohost_*_x64-setup.exe"), key=lambda p: p.stat().st_mtime, reverse=True)
        installer = installers[0] if installers else None
    assert installer is not None and installer.is_file(), f"OpenCohost_{version}_x64-setup.exe must exist in bundle/nsis"
    
    size_mb = installer.stat().st_size / (1024 * 1024)
    # Six measured ONNX export files total 1,324,617,323 bytes (LP1 inventory).
    # Retain the historical 600 MiB base plus the uncompressed model and 256
    # MiB dependency/headroom allowance. Compression is not assumed as proof.
    maximum_bytes = 600 * 1024 * 1024 + 1_324_617_323 + 256 * 1024 * 1024
    assert 5 * 1024 * 1024 < installer.stat().st_size < maximum_bytes, (
        f"Installer size {size_mb:.2f}MiB is outside measured model-inclusive envelope "
        f"(5MiB - {maximum_bytes / (1024 * 1024):.2f}MiB)"
    )

