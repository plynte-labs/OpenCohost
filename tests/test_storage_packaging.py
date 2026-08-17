"""Tests for storage path resolution in packaging scenarios.

Covers custom disk locations, environment isolation, and
scenarios that simulate a frozen (packaged) app where
__file__ no longer resolves to the project root.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from opencohost.config.storage import (
    StoragePaths,
    apply_storage_environment,
    resolve_storage_paths,
)


def test_explicit_data_root_controls_owned_paths_and_supports_unicode(tmp_path):
    data_root = tmp_path / "Open Cohost \u00fcnicode"
    with patch.dict(os.environ, {"OPENCOHOST_DATA_ROOT": str(data_root)}, clear=False):
        paths = resolve_storage_paths({"cache_root": "auto", "temp_root": "auto", "ollama_models": "auto"})

    assert paths.cache_root == data_root / "cache"
    assert paths.temp_root == data_root / "temp"
    assert paths.hf_home == data_root / "cache"
    assert paths.hf_hub_cache == data_root / "cache" / "hub"
    assert paths.torch_home == data_root / "cache" / "torch"
    assert paths.ollama_models != data_root / "cache" / "ollama_models"


def test_explicit_data_root_does_not_take_ownership_of_ollama_models(tmp_path):
    data_root = tmp_path / "data"
    ollama_root = tmp_path / "ollama-owned-by-user"
    with patch.dict(
        os.environ,
        {"OPENCOHOST_DATA_ROOT": str(data_root), "OLLAMA_MODELS": str(ollama_root)},
        clear=False,
    ):
        paths = resolve_storage_paths({"cache_root": "auto", "temp_root": "auto", "ollama_models": "auto"})

    assert paths.ollama_models == ollama_root.resolve()
    assert not str(paths.ollama_models).startswith(str(data_root.resolve()))


def test_storage_paths_custom_disk_overrides():
    """Custom cache, temp and ollama_models paths are resolved correctly."""
    config = {
        "cache_root": "/fake/opencohost-cache",
        "temp_root": "/fake/opencohost-temp",
        "ollama_models": "/fake/ollama-storage",
    }

    paths = resolve_storage_paths(config)

    assert paths.cache_root == Path("/fake/opencohost-cache").resolve()
    assert paths.temp_root == Path("/fake/opencohost-temp").resolve()
    assert paths.ollama_models == Path("/fake/ollama-storage").resolve()
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
    original_data_root = os.environ.get("OPENCOHOST_DATA_ROOT")

    try:
        result = apply_storage_environment(paths)

        assert os.environ["TEMP"] == str(result.temp_root)
        assert os.environ["TMP"] == str(result.temp_root)
        assert os.environ["HF_HOME"] == str(result.hf_home)
        assert os.environ["HUGGINGFACE_HUB_CACHE"] == str(result.hf_hub_cache)
        assert os.environ["TRANSFORMERS_CACHE"] == str(result.hf_hub_cache)
        assert os.environ["TORCH_HOME"] == str(result.torch_home)
        assert os.environ["OLLAMA_MODELS"] == str(result.ollama_models)

        # OpenCohost-owned directories were created; Ollama remains external
        # and user-owned, so this seam must never mkdir it.
        assert result.temp_root.exists()
        assert result.cache_root.exists()
        assert not result.ollama_models.exists()
    finally:
        # Restore to avoid contaminating other tests
        for var, val in [
            ("TEMP", original_temp),
            ("HF_HOME", original_hf),
            ("OLLAMA_MODELS", original_ollama),
            ("OPENCOHOST_DATA_ROOT", original_data_root),
        ]:
            if val is not None:
                os.environ[var] = val
            else:
                os.environ.pop(var, None)


def test_apply_storage_environment_never_creates_external_ollama_models(tmp_path):
    data_root = tmp_path / "Open Cohost \u00fcnicode"
    ollama_root = tmp_path / "ollama-user-owned"
    with patch.dict(os.environ, {"OPENCOHOST_DATA_ROOT": str(data_root)}, clear=False):
        paths = resolve_storage_paths({
            "cache_root": "auto",
            "temp_root": "auto",
            "ollama_models": str(ollama_root),
        })
        apply_storage_environment(paths)
    assert not ollama_root.exists()
    assert (data_root / "cache").exists()


def test_storage_paths_ollama_env_var_respected():
    """If OLLAMA_MODELS env var is set and config is auto, respect it."""
    config = {
        "cache_root": "auto",
        "temp_root": "auto",
        "ollama_models": "auto",
    }

    with patch.dict(os.environ, {"OLLAMA_MODELS": "F:/CustomOllama"}, clear=False):  # path-ok: test exercises drive-letter path handling
        paths = resolve_storage_paths(config)

    assert str(paths.ollama_models) == str(Path("F:/CustomOllama").resolve())  # path-ok: test exercises drive-letter path handling


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
