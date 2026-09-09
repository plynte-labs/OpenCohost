"""Stage bundled resources into OpenCohost_UI/src-tauri/resources before Tauri build."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import urllib.request
import zipfile

REPO_ROOT = Path(__file__).resolve().parent.parent
TAURI_RESOURCES = REPO_ROOT / "OpenCohost_UI" / "src-tauri" / "resources"
BOOTSTRAP_MANIFEST = TAURI_RESOURCES / "bootstrap-manifest.json"

PIPER_BASE_URL = "https://huggingface.co/rhasspy/piper-voices/resolve/main/es/es_MX/claude/high"
MINILM_BASE_URL = "https://huggingface.co/Xenova/paraphrase-multilingual-MiniLM-L12-v2/resolve/main"
UV_URL = "https://github.com/astral-sh/uv/releases/download/0.11.6/uv-x86_64-pc-windows-msvc.zip"


def _compute_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _download_or_copy(local_candidate: Path, destination: Path, download_url: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.is_file() and destination.stat().st_size > 0:
        return
    if local_candidate.is_file():
        print(f"Copying local {local_candidate} -> {destination}")
        shutil.copy2(local_candidate, destination)
        return
    print(f"Downloading {download_url} -> {destination}")
    headers = {"User-Agent": "OpenCohost-Packager/1.0"}
    req = urllib.request.Request(download_url, headers=headers)
    with urllib.request.urlopen(req) as resp, open(destination, "wb") as out:
        shutil.copyfileobj(resp, out)


def stage_resources(
    version: str,
    engine_payload: Path | None = None,
    skip_python: bool = False,
) -> None:
    TAURI_RESOURCES.mkdir(parents=True, exist_ok=True)

    # 1. Engine payload
    engine_dest = TAURI_RESOURCES / f"engine-{version}.zip"
    if engine_payload and engine_payload.is_file():
        if engine_payload.resolve() != engine_dest.resolve():
            print(f"Copying engine payload {engine_payload} -> {engine_dest}")
            shutil.copy2(engine_payload, engine_dest)
    elif not engine_dest.is_file():
        import sys
        packaging_dir = str(REPO_ROOT / "packaging")
        if packaging_dir not in sys.path:
            sys.path.insert(0, packaging_dir)
        import build_engine_payload
        print(f"Building engine payload at {engine_dest}")
        build_engine_payload.build_payload(REPO_ROOT, engine_dest)

    payload_sha256 = _compute_sha256(engine_dest)
    payload_size = engine_dest.stat().st_size

    # Update bootstrap-manifest.json
    if BOOTSTRAP_MANIFEST.is_file():
        with open(BOOTSTRAP_MANIFEST, "r", encoding="utf-8") as f:
            manifest = json.load(f)
        manifest["product_version"] = version
        manifest["engine"]["version"] = version
        manifest["engine"]["source"] = f"https://github.com/plynte-labs/OpenCohost/releases/download/v{version}/engine-{version}.zip"
        manifest["engine"]["sha256"] = payload_sha256
        manifest["engine"]["expected_size"] = payload_size
        with open(BOOTSTRAP_MANIFEST, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2)
        print(f"Updated {BOOTSTRAP_MANIFEST} with engine sha256={payload_sha256} size={payload_size}")

    # 2. UV binary
    uv_local = REPO_ROOT / "packaging" / "uv-0.11.6.zip"
    uv_dest = TAURI_RESOURCES / "uv-0.11.6.zip"
    _download_or_copy(uv_local, uv_dest, UV_URL)

    # 3. Piper voice model (es_MX-claude-high)
    piper_onnx_local = REPO_ROOT / "modelos_f5" / "piper" / "es_MX-claude-high.onnx"
    piper_onnx_dest = TAURI_RESOURCES / "piper" / "es_MX-claude-high.onnx"
    _download_or_copy(piper_onnx_local, piper_onnx_dest, f"{PIPER_BASE_URL}/es_MX-claude-high.onnx")

    piper_json_local = REPO_ROOT / "modelos_f5" / "piper" / "es_MX-claude-high.onnx.json"
    piper_json_dest = TAURI_RESOURCES / "piper" / "es_MX-claude-high.onnx.json"
    _download_or_copy(piper_json_local, piper_json_dest, f"{PIPER_BASE_URL}/es_MX-claude-high.onnx.json")

    # 4. MiniLM ONNX embedding model
    minilm_files = [
        ("model.onnx", "onnx/model.onnx"),
        ("tokenizer.json", "tokenizer.json"),
        ("config.json", "config.json"),
    ]
    for filename, remote_subpath in minilm_files:
        local_file = REPO_ROOT / "modelos_f5" / "minilm_l12_onnx" / filename
        dest_file = TAURI_RESOURCES / "modelos_f5" / "minilm_l12_onnx" / filename
        _download_or_copy(local_file, dest_file, f"{MINILM_BASE_URL}/{remote_subpath}")

    # 5. Pre-extracted Runtime (resources/runtime/)
    runtime_dest = TAURI_RESOURCES / "runtime"
    runtime_dest.mkdir(parents=True, exist_ok=True)

    # 5a. Extract opencohost package from engine payload into runtime/
    print(f"Unpacking engine into {runtime_dest}")
    with zipfile.ZipFile(engine_dest, "r") as z:
        for member in z.infolist():
            if member.filename == "payload-manifest.json":
                continue
            z.extract(member, runtime_dest)

    # 5b. Default directory structure and config
    (runtime_dest / "logs").mkdir(parents=True, exist_ok=True)
    (runtime_dest / "data" / "editorial_cards").mkdir(parents=True, exist_ok=True)
    (runtime_dest / "data" / "memorias").mkdir(parents=True, exist_ok=True)
    (runtime_dest / "config").mkdir(parents=True, exist_ok=True)

    default_profiles_src = REPO_ROOT / "opencohost" / "config" / "default_profiles.json"
    default_profiles_dest = runtime_dest / "config" / "default_profiles.json"
    if default_profiles_src.is_file() and not default_profiles_dest.is_file():
        shutil.copy2(default_profiles_src, default_profiles_dest)

    # 5c. Standalone Python distribution (runtime/python)
    if not skip_python:
        python_dest = runtime_dest / "python"
        _stage_standalone_python(python_dest)

    print("Staging complete! All bundled resources are ready in OpenCohost_UI/src-tauri/resources/")


def _ignore_python_junk(dirpath: str, contents: list[str]) -> set[str]:
    ignored = set()
    for c in contents:
        if c in ("__pycache__", ".git", ".pytest_cache", ".lock", "EXTERNALLY-MANAGED"):
            ignored.add(c)
        elif c.endswith((".pyc", ".pyo")):
            ignored.add(c)
    return ignored


def _stage_standalone_python(python_dest: Path) -> None:
    py_exe = python_dest / "python.exe"
    if py_exe.is_file():
        res = subprocess.run([str(py_exe), "-c", "import fastapi, uvicorn; print('ok')"], capture_output=True, text=True)
        if res.returncode == 0:
            print(f"Standalone Python runtime already functional at {python_dest}")
            return

    standalone_src = REPO_ROOT / "packaging" / "standalone_python"
    embedded_src = REPO_ROOT / "packaging" / "embedded_runtime" / "python" / "cpython-3.12.4-windows-x86_64-none"

    source_dir = None
    if (standalone_src / "python.exe").is_file():
        source_dir = standalone_src
    elif (embedded_src / "python.exe").is_file():
        source_dir = embedded_src

    if source_dir is not None:
        print(f"Copying Python distribution from {source_dir} -> {python_dest}")
        shutil.copytree(source_dir, python_dest, dirs_exist_ok=True, ignore=_ignore_python_junk)
    else:
        print(f"No local Python distribution found, downloading via uv into {python_dest}...")
        python_dest.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["uv", "python", "install", "3.12.4", "--install-dir", str(python_dest.parent)],
            check=True,
        )
        installed_dir = python_dest.parent / "cpython-3.12.4-windows-x86_64-none"
        if installed_dir.is_dir() and installed_dir.resolve() != python_dest.resolve():
            shutil.copytree(installed_dir, python_dest, dirs_exist_ok=True, ignore=_ignore_python_junk)
            shutil.rmtree(installed_dir, ignore_errors=True)

    externally_managed = python_dest / "Lib" / "EXTERNALLY-MANAGED"
    if externally_managed.is_file():
        externally_managed.unlink()

    py_exe = python_dest / "python.exe"
    if py_exe.is_file():
        res = subprocess.run([str(py_exe), "-c", "import fastapi, uvicorn; print('ok')"], capture_output=True, text=True)
        if res.returncode != 0:
            print("Installing dependencies into staged Python...")
            subprocess.run(
                ["uv", "pip", "install", "--break-system-packages", ".[local-tts,cloud-tts]", "--python", str(py_exe)],
                cwd=str(REPO_ROOT),
                check=True,
            )
            print("Dependencies successfully installed into staged Python.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage bundled installer resources for Tauri build.")
    parser.add_argument("--version", default="0.3.0-alpha.1", help="Release semver string")
    parser.add_argument("--engine-payload", type=Path, default=None, help="Prebuilt engine payload zip")
    parser.add_argument("--skip-python", action="store_true", help="Skip copying heavy Python runtime")
    args = parser.parse_args()

    stage_resources(args.version, args.engine_payload, skip_python=args.skip_python)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
