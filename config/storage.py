"""Centralized storage/cache configuration for VoiceAI.

This module must stay lightweight: it is imported before heavy ML libraries so
TEMP/TMP/HF/Torch/Ollama paths are set before those libraries initialize.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - settings fallback only
    yaml = None


def get_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def get_user_data_dir() -> Path:
    if getattr(sys, "frozen", False):
        if sys.platform == "win32":
            return Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / "VoiceAI"
        else:
            return Path.home() / ".config" / "VoiceAI"
    return Path(__file__).resolve().parent.parent


BASE_DIR = get_app_dir()
USER_DATA_DIR = get_user_data_dir()
STORAGE_CONFIG_FILE = BASE_DIR / "config" / "storage.yaml"


@dataclass(frozen=True)
class StoragePaths:
    cache_root: Path
    temp_root: Path
    hf_home: Path
    hf_hub_cache: Path
    torch_home: Path
    ollama_models: Path


def load_storage_config(config_file: Path | None = None) -> dict[str, Any]:
    config_file = config_file or STORAGE_CONFIG_FILE
    if yaml is None or not config_file.exists():
        return {}
    with open(config_file, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return data.get("storage", {}) or {}


def _resolve_path(value: str | None, fallback: Path) -> Path:
    if not value or str(value).strip().lower() == "auto":
        return fallback
    return Path(os.path.expandvars(os.path.expanduser(str(value)))).resolve()


def resolve_storage_paths(config: dict[str, Any] | None = None) -> StoragePaths:
    config = config or load_storage_config()

    # Backward-compatible auto defaults:
    # - keep Qwen/HF cache in the existing project-local modelos_f5 directory
    # - keep app temp in project-local temp
    # - respect OLLAMA_MODELS if already configured by the user/session
    cache_root = _resolve_path(config.get("cache_root"), BASE_DIR / "modelos_f5")
    temp_root = _resolve_path(config.get("temp_root"), BASE_DIR / "temp")
    ollama_models = _resolve_path(
        config.get("ollama_models"),
        Path(os.environ.get("OLLAMA_MODELS", str(cache_root / "ollama_models"))),
    )

    return StoragePaths(
        cache_root=cache_root,
        temp_root=temp_root,
        hf_home=cache_root,
        hf_hub_cache=cache_root / "hub",
        torch_home=cache_root / "torch",
        ollama_models=ollama_models,
    )


def ensure_storage_dirs(paths: StoragePaths) -> None:
    for path in (
        paths.cache_root,
        paths.temp_root,
        paths.hf_home,
        paths.hf_hub_cache,
        paths.torch_home,
        paths.ollama_models,
        USER_DATA_DIR,
        USER_DATA_DIR / "config",
        USER_DATA_DIR / "logs",
        USER_DATA_DIR / "assets" / "music",
        USER_DATA_DIR / "assets" / "avatar" / "kira",
    ):
        path.mkdir(parents=True, exist_ok=True)


def apply_storage_environment(paths: StoragePaths | None = None) -> StoragePaths:
    paths = paths or resolve_storage_paths()
    ensure_storage_dirs(paths)

    # Force per-process temp/cache locations away from the Windows user temp on C:.
    os.environ["TEMP"] = str(paths.temp_root)
    os.environ["TMP"] = str(paths.temp_root)
    os.environ["HF_HOME"] = str(paths.hf_home)
    os.environ["HUGGINGFACE_HUB_CACHE"] = str(paths.hf_hub_cache)
    os.environ["TRANSFORMERS_CACHE"] = str(paths.hf_hub_cache)
    os.environ["TORCH_HOME"] = str(paths.torch_home)
    os.environ["OLLAMA_MODELS"] = str(paths.ollama_models)

    return paths


# Lazily resolved STORAGE_PATHS schema definition without hitting the disk at import time.
# The physical directories and environment mutations are explicitly initialized during main() startup.
STORAGE_PATHS = resolve_storage_paths()
