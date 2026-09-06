"""Stage bundled resources into OpenCohost_UI/src-tauri/resources before Tauri build."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import urllib.request

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


def stage_resources(version: str, engine_payload: Path | None = None) -> None:
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

    print("Staging complete! All bundled resources are ready in OpenCohost_UI/src-tauri/resources/")


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage bundled installer resources for Tauri build.")
    parser.add_argument("--version", default="0.3.0-alpha.1", help="Release semver string")
    parser.add_argument("--engine-payload", type=Path, default=None, help="Prebuilt engine payload zip")
    args = parser.parse_args()

    stage_resources(args.version, args.engine_payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
