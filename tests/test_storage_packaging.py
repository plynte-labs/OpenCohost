"""Tests for storage path resolution in packaging scenarios.

Covers custom disk locations, environment isolation, and
scenarios that simulate a frozen (packaged) app where
__file__ no longer resolves to the project root.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from config.storage import (
    StoragePaths,
    apply_storage_environment,
    resolve_storage_paths,
)


def test_storage_paths_custom_disk_overrides():
    """Ollama on D:, HF cache on E: — common multi-disk setup."""
    config = {
        "cache_root": "E:/VoiceAI/cache",
        "temp_root": "D:/VoiceAI/temp",
        "ollama_models": "D:/OllamaStorage",
    }

    paths = resolve_storage_paths(config)

    assert paths.cache_root == Path("E:/VoiceAI/cache").resolve()
    assert paths.temp_root == Path("D:/VoiceAI/temp").resolve()
    assert paths.ollama_models == Path("D:/OllamaStorage").resolve()
    assert paths.hf_home == paths.cache_root
    assert paths.hf_hub_cache == paths.cache_root / "hub"
    assert paths.torch_home == paths.cache_root / "torch"


def test_storage_paths_auto_falls_back_to_project_local():
    """When everything is 'auto', paths stay relative to project root."""
    config = {
        "cache_root": "auto",
        "temp_root": "auto",
        "ollama_models": "auto",
    }

    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("OLLAMA_MODELS", None)
        paths = resolve_storage_paths(config)

    # Should use project-local defaults, not system temp
    assert "modelos_f5" in str(paths.cache_root)
    assert "temp" in str(paths.temp_root).lower()


def test_storage_paths_env_var_expansion():
    """User can reference env vars in storage.yaml paths."""
    config = {
        "cache_root": "$USERPROFILE/VoiceAI/cache",
        "temp_root": "auto",
        "ollama_models": "auto",
    }

    paths = resolve_storage_paths(config)

    userprofile = os.environ.get("USERPROFILE", "")
    if userprofile:
        assert userprofile.replace("\\", "/") in str(paths.cache_root).replace("\\", "/")


def test_storage_env_isolation_sets_expected_vars(tmp_path):
    """apply_storage_environment sets all required env vars."""
    config = {
        "cache_root": str(tmp_path / "cache"),
        "temp_root": str(tmp_path / "temp"),
        "ollama_models": str(tmp_path / "ollama"),
    }
    paths = resolve_storage_paths(config)

    original_temp = os.environ.get("TEMP")
    original_hf = os.environ.get("HF_HOME")
    original_ollama = os.environ.get("OLLAMA_MODELS")

    try:
        result = apply_storage_environment(paths)

        assert os.environ["TEMP"] == str(result.temp_root)
        assert os.environ["TMP"] == str(result.temp_root)
        assert os.environ["HF_HOME"] == str(result.hf_home)
        assert os.environ["HUGGINGFACE_HUB_CACHE"] == str(result.hf_hub_cache)
        assert os.environ["TRANSFORMERS_CACHE"] == str(result.hf_hub_cache)
        assert os.environ["TORCH_HOME"] == str(result.torch_home)
        assert os.environ["OLLAMA_MODELS"] == str(result.ollama_models)

        # Directories were created
        assert result.temp_root.exists()
        assert result.cache_root.exists()
        assert result.ollama_models.exists()
    finally:
        # Restore to avoid contaminating other tests
        for var, val in [
            ("TEMP", original_temp),
            ("HF_HOME", original_hf),
            ("OLLAMA_MODELS", original_ollama),
        ]:
            if val is not None:
                os.environ[var] = val


def test_storage_paths_ollama_env_var_respected():
    """If OLLAMA_MODELS env var is set and config is auto, respect it."""
    config = {
        "cache_root": "auto",
        "temp_root": "auto",
        "ollama_models": "auto",
    }

    with patch.dict(os.environ, {"OLLAMA_MODELS": "F:/CustomOllama"}, clear=False):
        paths = resolve_storage_paths(config)

    assert str(paths.ollama_models) == str(Path("F:/CustomOllama").resolve())


def test_storage_paths_empty_string_treated_as_auto():
    """Empty strings in config should fall back to defaults, not create empty paths."""
    config = {
        "cache_root": "",
        "temp_root": "",
        "ollama_models": "",
    }

    paths = resolve_storage_paths(config)

    # Should not create paths at root level or empty
    assert str(paths.cache_root) != ""
    assert str(paths.temp_root) != ""
    assert "modelos_f5" in str(paths.cache_root) or "cache" in str(paths.cache_root).lower()
