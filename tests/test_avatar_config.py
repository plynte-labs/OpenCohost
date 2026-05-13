"""Tests for avatar configuration loader/saver."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from avatar.avatar_config import (
    AvatarConfig,
    OBSConfig,
    VALID_STATES,
    SUPPORTED_EXTENSIONS,
    load_avatar_config,
    save_avatar_config,
    assign_image_to_state,
)


def _write_yaml(path: Path, content: str) -> None:
    import yaml
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def _make_temp_image(path: Path) -> Path:
    """Create a minimal valid PNG file."""
    # Minimal 1x1 transparent PNG (67 bytes)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
        b"\r\n\xb4"
        b"\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    path.write_bytes(png_bytes)
    return path


class TestAvatarConfigDefaults:
    def test_default_config_has_all_states(self):
        config = AvatarConfig()
        for state in VALID_STATES:
            assert state in VALID_STATES

    def test_default_enabled_is_true(self):
        config = AvatarConfig()
        assert config.enabled is True

    def test_default_mode_is_image_states(self):
        config = AvatarConfig()
        assert config.mode == "image_states"


class TestLoadAvatarConfig:
    def test_load_missing_file_returns_defaults(self):
        config = load_avatar_config(Path("/nonexistent/avatar.yaml"))
        assert config.enabled is True
        assert config.mode == "image_states"
        assert config.state_images == {}

    def test_load_empty_file_returns_defaults(self):
        with tempfile.NamedTemporaryFile(suffix=".yaml", delete=False) as f:
            f.write(b"")
            tmp = Path(f.name)
        try:
            config = load_avatar_config(tmp)
            assert config.enabled is True
        finally:
            tmp.unlink()

    def test_load_with_state_images(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "avatar.yaml"
            img_path = Path(td) / "idle.png"
            _make_temp_image(img_path)
            # Use forward slashes to avoid YAML backslash escape issues on Windows
            img_str = str(img_path).replace("\\", "/")
            _write_yaml(cfg_path, f"""
avatar:
  enabled: true
  mode: image_states
  state_images:
    idle: "{img_str}"
    listening: ""
""")
            config = load_avatar_config(cfg_path)
            assert config.enabled is True
            assert "idle" in config.state_images

    def test_load_ignores_invalid_states(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "avatar.yaml"
            _write_yaml(cfg_path, """
avatar:
  state_images:
    idle: "/tmp/x.png"
    invalid_state: "/tmp/y.png"
""")
            config = load_avatar_config(cfg_path)
            assert "idle" in config.state_images
            assert "invalid_state" not in config.state_images


class TestSaveAvatarConfig:
    def test_save_and_reload(self):
        with tempfile.TemporaryDirectory() as td:
            cfg_path = Path(td) / "avatar.yaml"
            img_path = Path(td) / "speaking.png"
            _make_temp_image(img_path)
            config = AvatarConfig(
                enabled=True,
                mode="image_states",
                state_images={"speaking": img_path},
            )
            save_avatar_config(config, cfg_path)
            assert cfg_path.exists()

            reloaded = load_avatar_config(cfg_path)
            assert reloaded.enabled is True
            assert "speaking" in reloaded.state_images


class TestGetImageForState:
    def test_returns_path_when_exists(self):
        with tempfile.TemporaryDirectory() as td:
            img = Path(td) / "idle.png"
            _make_temp_image(img)
            config = AvatarConfig(state_images={"idle": img})
            result = config.get_image_for_state("idle")
            assert result == img

    def test_returns_none_when_missing(self):
        config = AvatarConfig()
        result = config.get_image_for_state("idle")
        assert result is None

    def test_falls_back_to_idle(self):
        with tempfile.TemporaryDirectory() as td:
            img = Path(td) / "idle.png"
            _make_temp_image(img)
            config = AvatarConfig(state_images={"idle": img})
            # speaking not configured, should fallback to idle
            result = config.get_image_for_state("speaking")
            assert result == img

    def test_no_fallback_for_idle_itself(self):
        config = AvatarConfig()
        result = config.get_image_for_state("idle")
        assert result is None


class TestAssignImageToState:
    def test_copies_image_and_updates_config(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "source.png"
            _make_temp_image(src)
            config = AvatarConfig()
            new_config = assign_image_to_state(config, "idle", src)
            assert "idle" in new_config.state_images
            assert new_config.state_images["idle"].exists()

    def test_preserves_obs_config_when_updating_state_image(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "source.png"
            _make_temp_image(src)
            obs = OBSConfig(
                enabled=True,
                host="192.168.1.10",
                port=4456,
                password="secret",
                source_name="KiraCustom",
                scene_name="Main",
            )
            config = AvatarConfig(obs=obs)

            new_config = assign_image_to_state(config, "idle", src)

            assert new_config.obs == obs

    def test_rejects_invalid_state(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "x.png"
            _make_temp_image(src)
            config = AvatarConfig()
            with pytest.raises(ValueError, match="Invalid avatar state"):
                assign_image_to_state(config, "nonexistent", src)

    def test_rejects_missing_source(self):
        config = AvatarConfig()
        with pytest.raises(FileNotFoundError):
            assign_image_to_state(config, "idle", "/nonexistent/image.png")

    def test_rejects_unsupported_format(self):
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "image.gif"
            src.write_bytes(b"fake gif")
            config = AvatarConfig()
            with pytest.raises(ValueError, match="Unsupported image format"):
                assign_image_to_state(config, "idle", src)
