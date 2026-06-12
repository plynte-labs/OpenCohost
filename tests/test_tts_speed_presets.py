"""
Piper speaking-rate presets tests.

Covers:
  - Settings: load/save tts_speed persistence, defaults, clamping, corruption.
  - PiperEngine: set_length_scale rebuilds the synthesis config at runtime.
  - Engine: set_tts_speed command applies the rate to Piper and persists it.
  - Engine: startup wires the persisted speed into PiperEngine.
  - UI module: preset table, closest-preset mapping, widget wiring via fake CTk.
  - UI shell: app_shell wires the speed selector (source-level inspection).

IMPORTANT: pytest imports ALL test files at collection time.
  - Never put mocks in sys.modules at module level.
  - Never import customtkinter (no display on CI); use the _ctk seam instead.
"""
from __future__ import annotations

import json
import os
import queue
import sys
from unittest.mock import MagicMock, patch

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


class _FakeSynthesisConfig:
    """Stub so the tests do not depend on the installed piper-tts version."""

    def __init__(self, length_scale=1.0):
        self.length_scale = length_scale


# ===========================================================================
# 1. Settings — load/save tts_speed persistence
# ===========================================================================

class TestTtsSpeedSettings:
    def test_load_returns_default_when_file_missing(self, tmp_path):
        from opencohost.config.settings import load_tts_speed, TTS_LOCAL_LENGTH_SCALE

        missing = str(tmp_path / "nope" / "tts_speed.json")
        assert load_tts_speed(missing) == pytest.approx(TTS_LOCAL_LENGTH_SCALE)

    def test_save_then_load_roundtrip(self, tmp_path):
        from opencohost.config.settings import load_tts_speed, save_tts_speed

        path = str(tmp_path / "config" / "tts_speed.json")
        save_tts_speed(1.30, path)
        assert load_tts_speed(path) == pytest.approx(1.30)

    def test_load_returns_default_on_corrupt_file(self, tmp_path):
        from opencohost.config.settings import load_tts_speed, TTS_LOCAL_LENGTH_SCALE

        path = str(tmp_path / "tts_speed.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{not json")
        assert load_tts_speed(path) == pytest.approx(TTS_LOCAL_LENGTH_SCALE)

    def test_load_clamps_out_of_range_values(self, tmp_path):
        from opencohost.config.settings import load_tts_speed

        path = str(tmp_path / "tts_speed.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"tts_speed": 99.0}, f)
        assert load_tts_speed(path) == pytest.approx(2.0)

        with open(path, "w", encoding="utf-8") as f:
            json.dump({"tts_speed": 0.01}, f)
        assert load_tts_speed(path) == pytest.approx(0.5)


# ===========================================================================
# 2. PiperEngine — runtime set_length_scale
# ===========================================================================

class TestPiperSetLengthScale:
    def test_set_length_scale_rebuilds_syn_config(self):
        with patch("opencohost.core.tts_piper._SynthesisConfig", _FakeSynthesisConfig):
            from opencohost.core.tts_piper import PiperEngine

            engine = PiperEngine("/fake/model.onnx", length_scale=1.15)
            engine.set_length_scale(1.30)

        assert engine._length_scale == pytest.approx(1.30)
        assert engine._syn_config.length_scale == pytest.approx(1.30)

    def test_set_length_scale_back_to_default_clears_config(self):
        with patch("opencohost.core.tts_piper._SynthesisConfig", _FakeSynthesisConfig):
            from opencohost.core.tts_piper import PiperEngine

            engine = PiperEngine("/fake/model.onnx", length_scale=1.15)
            engine.set_length_scale(1.0)

        assert engine._syn_config is None


# ===========================================================================
# 3. Engine — set_tts_speed command + startup wiring
# ===========================================================================

class TestEngineSetTtsSpeedCommand:
    def _make_motor(self):
        from opencohost.core.llm_engine import MotorVocalIA

        return MotorVocalIA(queue.Queue(), lambda event: None)

    def test_command_applies_rate_and_persists(self):
        motor = self._make_motor()
        motor._piper = MagicMock()

        with patch("opencohost.core.llm_engine.save_tts_speed") as save:
            motor._dispatch_command("set_tts_speed", 1.30)

        motor._piper.set_length_scale.assert_called_once_with(1.30)
        save.assert_called_once_with(1.30)

    def test_startup_uses_persisted_speed(self):
        with patch("opencohost.core.llm_engine.load_tts_speed", return_value=1.30):
            motor = self._make_motor()

        assert motor._piper._length_scale == pytest.approx(1.30)


# ===========================================================================
# 4. UI module — presets and widget wiring (no real CTk)
# ===========================================================================

class TestSpeedPresetModule:
    def test_preset_table_covers_original_and_slower_rates(self):
        from opencohost.ui.tts_speed_control import SPEED_PRESETS

        scales = [scale for _label, scale in SPEED_PRESETS]
        assert len(SPEED_PRESETS) in (3, 4)
        assert 1.0 in scales       # original Piper rate
        assert 1.15 in scales      # current default
        assert max(scales) > 1.15  # at least one slower preset

    def test_closest_preset_label_maps_arbitrary_scales(self):
        from opencohost.ui.tts_speed_control import SPEED_PRESETS, closest_preset_label

        for label, scale in SPEED_PRESETS:
            assert closest_preset_label(scale) == label
        # An off-preset persisted value still lands on a sensible neighbour
        assert closest_preset_label(1.17) == closest_preset_label(1.15)

    def test_build_creates_segmented_button_and_dispatches_scale(self):
        from opencohost.ui import tts_speed_control as mod

        fake_ctk = MagicMock()
        dispatched = []
        with patch.object(mod, "_ctk", return_value=fake_ctk), \
             patch.object(mod, "load_tts_speed", return_value=1.15):
            widget = mod.build_tts_speed_selector(MagicMock(), dispatched.append)

        assert widget is fake_ctk.CTkSegmentedButton.return_value
        kwargs = fake_ctk.CTkSegmentedButton.call_args.kwargs
        assert kwargs["values"] == [label for label, _ in mod.SPEED_PRESETS]
        widget.set.assert_called_once_with(mod.closest_preset_label(1.15))

        on_select = kwargs["command"]
        slow_label = mod.SPEED_PRESETS[-1][0]
        on_select(slow_label)
        assert dispatched == [mod.SPEED_PRESETS[-1][1]]

    def test_unknown_label_is_ignored(self):
        from opencohost.ui import tts_speed_control as mod

        fake_ctk = MagicMock()
        dispatched = []
        with patch.object(mod, "_ctk", return_value=fake_ctk), \
             patch.object(mod, "load_tts_speed", return_value=1.15):
            mod.build_tts_speed_selector(MagicMock(), dispatched.append)

        on_select = fake_ctk.CTkSegmentedButton.call_args.kwargs["command"]
        on_select("???")
        assert dispatched == []


# ===========================================================================
# 5. UI shell — wiring (source-level inspection, no CTk import)
# ===========================================================================

class TestAppShellSpeedSelectorWiring:
    def _source(self):
        src_path = os.path.join(ROOT_DIR, "opencohost", "ui", "app_shell.py")
        with open(src_path, "r", encoding="utf-8") as f:
            return f.read()

    def test_app_shell_builds_speed_selector(self):
        assert "build_tts_speed_selector" in self._source(), (
            "app_shell.py must build the Piper speed selector widget"
        )

    def test_app_shell_dispatches_set_tts_speed(self):
        assert "set_tts_speed" in self._source(), (
            "app_shell.py must dispatch 'set_tts_speed' command to motor_ia"
        )
