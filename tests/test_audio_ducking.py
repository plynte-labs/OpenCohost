"""Strict-TDD tests for Music-Mode Ducking (track music_mode_ducking_20260618).

Covers:
  Bug B — duck-state regression on track change (audio_bed.py:228): a new channel
          allocated by _play_selected must inherit the current duck state instead
          of always starting at base_volume.
  Feature A — configurable base/ducked volumes with clamping + settings round-trip.

All audio is mocked; no real pygame mixer is initialized.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import pytest

from opencohost.core.music.audio_bed import AudioBedEngine, AudioBedPolicy
from opencohost.core.music.music_library import MusicLibrary, MusicTrack


def _tracks(n):
    return [
        MusicTrack(
            id=f"t{i}",
            original_name=f"{i}.wav",
            path=f"/fake/{i}.wav",
            mood="normal",
            label=f"l{i}",
            variant_index=i,
        )
        for i in range(n)
    ]


@pytest.fixture
def bed_factory():
    """Build AudioBedEngine instances with pygame mocked; auto-shutdown after."""
    created = []

    def _make(num_tracks=2):
        lib = MagicMock(spec=MusicLibrary)
        tracks = _tracks(num_tracks)
        lib.valid_tracks.return_value = tracks
        bed = AudioBedEngine(library=lib, policy=AudioBedPolicy(fade_ms=0))
        pyg = MagicMock()
        pyg.mixer.Sound.return_value = MagicMock()
        bed._pygame = pyg
        created.append(bed)
        return bed, pyg, tracks

    yield _make
    for b in created:
        try:
            b.shutdown()
        except Exception:
            pass


# ── 4.1 Duck-state regression (Bug B) ──────────────────────────────────────


class TestDuckStatePreservedOnTrackChange:
    def test_track_change_while_ducked_starts_new_channel_at_ducked_volume(self, bed_factory):
        bed, pyg, tracks = bed_factory(2)
        new_channel = MagicMock()
        pyg.mixer.find_channel.return_value = new_channel
        bed.duck()  # Kira mid-utterance: duck state active (no channel yet)
        bed.current_track = tracks[0]
        bed._play_selected("normal")  # track change -> selects t1, new channel
        new_channel.set_volume.assert_called_once_with(bed.policy.ducked_volume)

    def test_track_change_while_not_ducked_starts_new_channel_at_base_volume(self, bed_factory):
        bed, pyg, tracks = bed_factory(2)
        new_channel = MagicMock()
        pyg.mixer.find_channel.return_value = new_channel
        bed.current_track = tracks[0]
        bed._play_selected("normal")
        new_channel.set_volume.assert_called_once_with(bed.policy.base_volume)

    def test_is_ducked_flag_set_by_duck_cleared_by_unduck(self, bed_factory):
        bed, _pyg, _tracks = bed_factory(1)
        assert bed._is_ducked is False
        bed.duck()
        assert bed._is_ducked is True
        bed.unduck()
        assert bed._is_ducked is False

    def test_multiple_track_changes_while_ducked_all_start_ducked(self, bed_factory):
        bed, pyg, tracks = bed_factory(2)
        channels = [MagicMock(), MagicMock()]
        pyg.mixer.find_channel.side_effect = channels
        bed.duck()
        bed.current_track = tracks[0]
        bed._play_selected("normal")  # -> t1 on channels[0]
        bed._play_selected("normal")  # -> t0 on channels[1]
        for ch in channels:
            ch.set_volume.assert_called_once_with(bed.policy.ducked_volume)

    def test_unduck_then_track_change_starts_at_base_volume(self, bed_factory):
        bed, pyg, tracks = bed_factory(2)
        new_channel = MagicMock()
        pyg.mixer.find_channel.return_value = new_channel
        bed.duck()
        bed.unduck()
        bed.current_track = tracks[0]
        bed._play_selected("normal")
        new_channel.set_volume.assert_called_once_with(bed.policy.base_volume)


# ── 4.2 Volume clamping (Feature A) ─────────────────────────────────────────


class TestVolumePolicyClamping:
    def test_ducked_volume_clamped_below_zero(self):
        policy = AudioBedPolicy(base_volume=0.28, ducked_volume=-0.5)
        assert policy.ducked_volume == 0.0

    def test_ducked_volume_clamped_above_base(self):
        policy = AudioBedPolicy(base_volume=0.28, ducked_volume=0.40)
        assert policy.ducked_volume == policy.base_volume == 0.28

    def test_base_volume_clamped_to_valid_range(self):
        policy = AudioBedPolicy(base_volume=1.5, ducked_volume=0.08)
        assert policy.base_volume == 1.0

    def test_settings_roundtrip_base_and_ducked_volume(self, tmp_path):
        from opencohost.config.settings import load_music_volumes, save_music_volumes

        cfg = str(tmp_path / "music_volume.json")
        save_music_volumes(0.35, 0.10, config_file=cfg)
        base, ducked = load_music_volumes(config_file=cfg)
        policy = AudioBedPolicy(base_volume=base, ducked_volume=ducked)
        assert policy.base_volume == 0.35
        assert policy.ducked_volume == 0.10

    def test_settings_missing_file_falls_back_to_defaults(self, tmp_path):
        from opencohost.config.settings import (
            MUSIC_BASE_VOLUME,
            MUSIC_DUCKED_VOLUME,
            load_music_volumes,
        )

        cfg = str(tmp_path / "does_not_exist.json")
        base, ducked = load_music_volumes(config_file=cfg)
        assert base == MUSIC_BASE_VOLUME
        assert ducked == MUSIC_DUCKED_VOLUME


# ── 4.3 Lock safety (regression guard) ──────────────────────────────────────


class TestDuckLockSafety:
    def test_duck_unduck_play_concurrent_no_deadlock(self, bed_factory):
        bed, pyg, tracks = bed_factory(2)
        pyg.mixer.find_channel.side_effect = lambda **kw: MagicMock()
        bed.current_track = tracks[0]
        stop = threading.Event()

        def ducker():
            while not stop.is_set():
                bed.duck()
                bed.unduck()

        def player():
            while not stop.is_set():
                try:
                    bed._play_selected("normal")
                except Exception:
                    pass

        threads = [threading.Thread(target=ducker), threading.Thread(target=player)]
        for t in threads:
            t.start()
        time.sleep(0.3)
        stop.set()
        for t in threads:
            t.join(2.0)
        assert all(not t.is_alive() for t in threads)
        assert isinstance(bed._is_ducked, bool)
