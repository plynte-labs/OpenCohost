"""
Packaging Task 5: asset-absence graceful degradation.

Verifies that:
1. MusicLibrary.load() does not crash when the library dir and config file are absent.
2. AvatarConfig.get_image_for_state() returns None gracefully for all states
   when no images are configured (assets/avatar/kira/ absent).
3. load_avatar_config() returns a usable default when the YAML config is absent.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


# ---------------------------------------------------------------------------
# Task 5a — MusicLibrary does not crash when dir and config file are absent
# ---------------------------------------------------------------------------

class TestMusicLibraryAbsentAssets:
    def test_load_does_not_crash_when_dir_absent(self, tmp_path):
        """MusicLibrary.load() returns silently when neither dir nor config exist."""
        from opencohost.core.music.music_library import MusicLibrary

        absent_dir = tmp_path / "music"
        absent_config = tmp_path / "music_library.json"

        # Neither exists
        assert not absent_dir.exists()
        assert not absent_config.exists()

        library = MusicLibrary(library_dir=absent_dir, config_file=absent_config)
        try:
            library.load()
        except Exception as exc:
            pytest.fail(f"MusicLibrary.load() raised with absent assets: {exc}")

        assert library.tracks == {}, "tracks must be empty when config is absent"

    def test_counts_by_mood_empty_when_no_tracks(self, tmp_path):
        """counts_by_mood() returns empty dict (no crash) when library is empty."""
        from opencohost.core.music.music_library import MusicLibrary

        library = MusicLibrary(
            library_dir=tmp_path / "music",
            config_file=tmp_path / "no_config.json",
        )
        library.load()
        counts = library.counts_by_mood()
        assert isinstance(counts, dict)

    def test_select_for_mood_returns_none_when_empty(self, tmp_path):
        """select_for_mood() returns None gracefully when no tracks are loaded."""
        from opencohost.core.music.music_library import MusicLibrary

        library = MusicLibrary(
            library_dir=tmp_path / "music",
            config_file=tmp_path / "no_config.json",
        )
        library.load()
        result = library.select_for_mood("normal")
        assert result is None, "select_for_mood must return None when library is empty"


# ---------------------------------------------------------------------------
# Task 5b — AvatarConfig: absent images → get_image_for_state returns None
# ---------------------------------------------------------------------------

class TestAvatarConfigAbsentImages:
    def test_absent_state_images_returns_none(self):
        """AvatarConfig with no state_images returns None for any state."""
        from opencohost.avatar.avatar_config import AvatarConfig, VALID_STATES

        config = AvatarConfig(state_images={})
        for state in VALID_STATES:
            result = config.get_image_for_state(state)
            assert result is None, (
                f"get_image_for_state({state!r}) should return None when no images configured"
            )

    def test_load_avatar_config_with_absent_yaml_returns_defaults(self, tmp_path):
        """load_avatar_config() with nonexistent YAML must return a valid default."""
        from opencohost.avatar.avatar_config import load_avatar_config, AvatarConfig

        absent_config = tmp_path / "avatar.yaml"
        assert not absent_config.exists()

        config = load_avatar_config(absent_config)
        assert isinstance(config, AvatarConfig), (
            "load_avatar_config must return AvatarConfig even when YAML is absent"
        )
        assert isinstance(config.enabled, bool), (
            "AvatarConfig.enabled must have a usable boolean default when YAML is absent"
        )

    def test_load_avatar_config_with_absent_assets_folder_does_not_crash(self, tmp_path):
        """load_avatar_config succeeds when assets/avatar/kira/ does not exist."""
        from opencohost.avatar.avatar_config import load_avatar_config

        absent_config = tmp_path / "avatar.yaml"
        # Write minimal YAML pointing to an absent folder
        absent_config.write_text(
            "avatar:\n  enabled: true\n  assets_folder: /nonexistent/kira/assets\n",
            encoding="utf-8",
        )

        try:
            config = load_avatar_config(absent_config)
        except Exception as exc:
            pytest.fail(f"load_avatar_config raised with absent assets folder: {exc}")

        # get_image_for_state must not raise even with the absent folder
        result = config.get_image_for_state("idle")
        assert result is None, "No images should be returned when assets folder is absent"
