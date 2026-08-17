"""Strict WU1 packaging boundary tests.

These tests intentionally inspect project metadata and the immutable payload
contract rather than the developer's already-populated virtual environment.
That keeps the core/legacy split reviewable and prevents a local install from
masking missing distribution metadata.
"""

from __future__ import annotations

import importlib.util
import json
import re
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
UV_LOCK = ROOT / "uv.lock"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _project_metadata() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _load_payload_builder():
    path = ROOT / "packaging" / "build_engine_payload.py"
    assert path.is_file(), "WU1 must provide a deterministic engine payload builder"
    spec = importlib.util.spec_from_file_location("build_engine_payload", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_core_metadata_declares_tauri_runtime_and_keeps_legacy_out():
    project = _project_metadata()["project"]
    base_names = {
        requirement.split("[", 1)[0].split(">", 1)[0].split("=", 1)[0].split("<", 1)[0]
        .strip()
        .lower()
        for requirement in project["dependencies"]
    }

    required = {"fastapi", "uvicorn", "ollama", "requests", "websockets", "pygame", "pyyaml", "httpx", "pydantic"}
    forbidden = {
        "customtkinter",
        "numpy",
        "sounddevice",
        "soundfile",
        "pillow",
        "pynput",
        "keyboard",
        "google-auth",
        "google-api-python-client",
        "websocket-client",
        "piper-tts",
        "torch",
        "transformers",
        "qwen-tts",
        "qwen_tts",
    }

    assert required <= base_names
    assert not (base_names & forbidden)

    extras = project.get("optional-dependencies", {})
    assert "api" not in extras, "FastAPI/uvicorn are core for the Tauri composition root"
    assert {"local-tts", "heavy-tts", "integrations", "dev"} <= set(extras)


def test_lock_root_package_matches_the_core_boundary():
    lock = tomllib.loads(UV_LOCK.read_text(encoding="utf-8"))
    root = next(package for package in lock["package"] if package["name"] == "opencohost")
    names = {dependency["name"] for dependency in root["dependencies"]}
    assert {"fastapi", "httpx", "ollama", "pydantic", "pygame", "pyyaml", "requests", "uvicorn", "websockets"} <= names
    assert not names & {"customtkinter", "piper-tts", "torch", "transformers", "qwen_tts", "numpy", "pynput"}


def test_explicit_api_and_ptt_entry_points_are_package_owned():
    project = _project_metadata()["project"]
    scripts = project.get("scripts", {})
    assert scripts["opencohost-api"] == "opencohost.api.cli:main"
    assert scripts["opencohost-ptt"] == "opencohost.api.ptt_f10_bridge:main"


def test_api_import_does_not_import_legacy_ctk():
    code = "import sys; import opencohost.api.main; assert 'customtkinter' not in sys.modules"
    completed = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def test_ptt_bridge_is_importable_from_the_package():
    code = "from opencohost.api.ptt_f10_bridge import find_api, main; assert callable(find_api) and callable(main)"
    completed = subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr


def test_engine_payload_is_allowlisted_and_hash_manifested(tmp_path):
    builder = _load_payload_builder()
    archive = tmp_path / "engine-payload.zip"
    builder.build_payload(ROOT, archive)

    with zipfile.ZipFile(archive) as payload:
        names = payload.namelist()
        assert names == sorted(names)
        assert "pyproject.toml" in names
        assert "uv.lock" in names
        assert "opencohost/api/main.py" in names
        assert "opencohost/api/ptt_f10_bridge.py" in names
        assert "payload-manifest.json" in names
        assert not any(name.startswith("OpenCohost_UI/") for name in names)
        assert not any(name.startswith("opencohost/data/") for name in names)
        assert not any(
            name.startswith(prefix)
            for prefix in ("data/", "logs/", "runtime/", "user/", "cache/")
            for name in names
        )
        assert not any("customtkinter" in name.lower() for name in names)
        assert not any(name.endswith("ptt_f10_bridge.py") and name != "opencohost/api/ptt_f10_bridge.py" for name in names)

        manifest = json.loads(payload.read("payload-manifest.json"))
        assert manifest["schema"] == 1
        assert manifest["files"] == sorted(manifest["files"], key=lambda item: item["path"])
        listed = {item["path"]: item for item in manifest["files"]}
        for name, item in listed.items():
            data = payload.read(name)
            import hashlib

            assert item["size"] == len(data)
            assert item["sha256"] == hashlib.sha256(data).hexdigest()


def test_engine_payload_build_is_byte_deterministic(tmp_path):
    builder = _load_payload_builder()
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    builder.build_payload(ROOT, first)
    builder.build_payload(ROOT, second)
    assert first.read_bytes() == second.read_bytes()


def test_ci_sync_extras_are_declared_and_select_legacy_suite():
    project = _project_metadata()["project"]
    workflow = CI_WORKFLOW.read_text(encoding="utf-8")
    sync_commands = re.findall(r"uv sync[^\r\n]+", workflow)
    used_extras = {
        extra
        for command in sync_commands
        for extra in re.findall(r"--extra\s+([A-Za-z0-9_-]+)", command)
    }
    declared_extras = set(project.get("optional-dependencies", {}))

    assert "api" not in used_extras
    assert "legacy-ui" in used_extras
    assert used_extras <= declared_extras
