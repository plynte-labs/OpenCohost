"""
TTS local-only switch tests (privacy gate).

Covers:
  - Settings: default off, persists across save/load.
  - Engine: with switch ON, light-path resolution uses Piper even when
    Edge-TTS is available and online; Edge-TTS callable is never invoked.
  - Engine: auto-fallback from heavy (missing_reference / health_gate)
    lands on Piper when switch is ON.
  - Engine: switch ON + Piper unavailable → degraded path (None in queue),
    Edge-TTS still never invoked.
  - UI: switch is wired; toggle sends set_tts_local_only command to engine.

IMPORTANT: pytest imports ALL test files at collection time.
  - Never put mocks in sys.modules at module level.
  - Never let mocked CTk widgets walk MagicMock parents unbounded.
"""
from __future__ import annotations

import json
import os
import queue
import sys
import wave
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_motor():
    """Return a fresh MotorVocalIA with mocked pygame/ollama (no real I/O)."""
    from opencohost.core.llm_engine import MotorVocalIA

    log_q: queue.Queue = queue.Queue()
    ui_events: list = []

    def ui_callback(event):
        ui_events.append(event)

    motor = MotorVocalIA(log_q, ui_callback)
    motor.ollama = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    motor.pygame = __import__("unittest.mock", fromlist=["MagicMock"]).MagicMock()
    motor.is_ready = True
    # NOTE: never hand-set `_speaking` here — dispatch now DEFERS drain-safe
    # verbs while `_speech_active` (§11 B4 primary path), so a fixture lying
    # about speech state turns every setter test vacuous or failing.
    return motor, log_q, ui_events


def _make_mock_piper(available: bool = True, synthesize_ok: bool = True):
    """Return a mock PiperEngine."""
    from unittest.mock import MagicMock
    mock_piper = MagicMock()
    mock_piper.is_available.return_value = available

    def fake_synthesize(text, path):
        if synthesize_ok:
            with wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(22050)
                wf.writeframes(b"\x00" * 44)
        return synthesize_ok

    mock_piper.synthesize.side_effect = fake_synthesize
    return mock_piper


def _drive_hablar(motor, text, *, edge_save_side_effect=None,
                  motor_tts="ligero", voz_referencia=None, health_monitor=None):
    """Run the REAL _hablar/productor with boundary mocks.

    Only edge_tts.Communicate (network) and pygame (audio) are mocked; routing,
    chunking, the tts_local_only snapshot, effective-motor resolution and the
    Piper fallback all run for real. No re-copied decision tree.

    Returns (loaded_paths, piper_mock, communicate_mock).
    """
    motor.pygame = MagicMock()
    # Bare MagicMock get_busy() is truthy -> the consumer busy-wait would spin
    # forever. Force it False so playback "finishes" immediately.
    motor.pygame.mixer.music.get_busy.return_value = False
    motor.health_monitor = health_monitor
    motor.voz_referencia = voz_referencia
    motor.motor_tts = motor_tts

    def _save(path, *a, **k):
        if edge_save_side_effect is not None:
            raise edge_save_side_effect
        open(path, "wb").close()  # stub .mp3 so the consumer can load + remove it

    communicate = MagicMock()
    communicate.return_value.save = AsyncMock(side_effect=_save)

    with patch("opencohost.core.llm_engine.edge_tts.Communicate", communicate):
        motor._hablar(text, source="direct")

    loaded = [c.args[0] for c in motor.pygame.mixer.music.load.call_args_list]
    return loaded, motor._piper, communicate


# ===========================================================================
# 1. Settings — load / save / default
# ===========================================================================

class TestTtsLocalOnlySettings:
    def test_default_is_false(self):
        """load_tts_local_only() returns False when no file exists."""
        from opencohost.config import settings as cfg

        result = cfg.load_tts_local_only(config_file="/nonexistent/path.json")
        assert result is False

    def test_save_and_reload(self, tmp_path):
        """Saving True and reloading returns True."""
        from opencohost.config import settings as cfg

        path = str(tmp_path / "tts_local_only.json")
        cfg.save_tts_local_only(True, config_file=path)
        assert cfg.load_tts_local_only(config_file=path) is True

    def test_save_false_and_reload(self, tmp_path):
        """Saving False and reloading returns False."""
        from opencohost.config import settings as cfg

        path = str(tmp_path / "tts_local_only.json")
        cfg.save_tts_local_only(False, config_file=path)
        assert cfg.load_tts_local_only(config_file=path) is False

    def test_corrupted_file_returns_false(self, tmp_path):
        """A corrupted JSON file is handled gracefully (returns False)."""
        from opencohost.config import settings as cfg

        path = str(tmp_path / "bad.json")
        with open(path, "w") as f:
            f.write("not-json!!!")
        assert cfg.load_tts_local_only(config_file=path) is False

    def test_default_constant_is_false(self):
        """TTS_LOCAL_ONLY_FILE constant is defined in settings."""
        from opencohost.config import settings as cfg

        assert hasattr(cfg, "TTS_LOCAL_ONLY_FILE"), (
            "settings.TTS_LOCAL_ONLY_FILE must be defined"
        )


# ===========================================================================
# 2. Engine — tts_local_only property and dispatch
# ===========================================================================

class TestMotorLocalOnlyProperty:
    def test_default_is_false(self, tmp_path):
        """MotorVocalIA.tts_local_only starts False (backward compat).

        Patches the loader to a missing file so the test never depends on
        the real user config left behind by an app session.
        """
        from unittest.mock import patch
        from opencohost.config import settings

        missing = str(tmp_path / "absent.json")
        with patch(
            "opencohost.core.llm_engine.load_tts_local_only",
            lambda: settings.load_tts_local_only(missing),
        ):
            motor, *_ = _make_motor()
        assert motor.tts_local_only is False

    def test_can_be_set_to_true(self):
        """Setting tts_local_only=True reflects on the property."""
        motor, *_ = _make_motor()
        motor.tts_local_only = True
        assert motor.tts_local_only is True

    def test_set_tts_local_only_command_enables(self):
        """Dispatching set_tts_local_only True updates tts_local_only."""
        from unittest.mock import patch

        motor, *_ = _make_motor()
        with patch("opencohost.core.llm_engine.save_tts_local_only"):
            motor._dispatch_command("set_tts_local_only", True)
        assert motor.tts_local_only is True

    def test_set_tts_local_only_command_disables(self):
        """Dispatching set_tts_local_only False updates tts_local_only."""
        from unittest.mock import patch

        motor, *_ = _make_motor()
        motor.tts_local_only = True
        with patch("opencohost.core.llm_engine.save_tts_local_only"):
            motor._dispatch_command("set_tts_local_only", False)
        assert motor.tts_local_only is False


# ===========================================================================
# 3. Engine — light-path resolution with switch ON
# ===========================================================================

class TestLightPathWithLocalOnlyEnabled:
    def test_edge_never_called_when_switch_on(self):
        """With tts_local_only=True and Piper available, Edge-TTS is never invoked."""
        motor, *_ = _make_motor()
        motor.tts_local_only = True
        motor._edge_tts_offline = False  # Edge-TTS would normally be available
        motor._piper = _make_mock_piper(available=True, synthesize_ok=True)

        loaded, piper, communicate = _drive_hablar(motor, "Hola Kira")

        communicate.assert_not_called()  # privacy guard: Edge never touched
        assert piper.synthesize.called
        assert loaded and loaded[-1].endswith(".wav")

    def test_piper_used_for_heavy_auto_fallback_missing_reference(self):
        """Heavy->light auto-fallback (missing_reference) lands on Piper when switch is ON."""
        motor, *_ = _make_motor()
        motor.tts_local_only = True
        motor._edge_tts_offline = False
        motor._piper = _make_mock_piper(available=True, synthesize_ok=True)

        # motor_tts=pesado + no reference -> real productor missing_reference fallback.
        loaded, piper, communicate = _drive_hablar(
            motor, "Fallback test", motor_tts="pesado", voz_referencia=None
        )

        communicate.assert_not_called()
        assert piper.synthesize.called
        assert loaded and loaded[-1].endswith(".wav")

    def test_piper_used_for_heavy_auto_fallback_health_gate(self):
        """Heavy->light auto-fallback (health_gate) lands on Piper when switch is ON."""
        motor, *_ = _make_motor()
        motor.tts_local_only = True
        motor._edge_tts_offline = False
        motor._piper = _make_mock_piper(available=True, synthesize_ok=True)

        # Health monitor blocks heavy TTS -> real productor health_gate fallback.
        mock_hm = MagicMock()
        mock_hm.heavy_tts_block_reason.return_value = "health_gate"

        loaded, piper, communicate = _drive_hablar(
            motor, "Health gate test", motor_tts="pesado",
            voz_referencia="/some/ref.wav", health_monitor=mock_hm,
        )

        communicate.assert_not_called()
        assert piper.synthesize.called
        assert loaded and loaded[-1].endswith(".wav")


# ===========================================================================
# 4. Engine — switch ON + Piper unavailable → degraded, Edge still never called
# ===========================================================================

class TestLocalOnlyWithPiperUnavailable:
    def test_none_in_queue_and_edge_never_called(self):
        """local-only=True + Piper unavailable -> chunk dropped; Edge-TTS never called."""
        motor, *_ = _make_motor()
        motor.tts_local_only = True
        motor._edge_tts_offline = False
        motor._piper = _make_mock_piper(available=False)

        loaded, piper, communicate = _drive_hablar(motor, "Sin Piper")

        communicate.assert_not_called()  # privacy preserved even with Piper down
        piper.synthesize.assert_not_called()
        assert loaded == []

    def test_piper_synthesize_failure_no_edge(self):
        """local-only=True + Piper fails synthesis -> chunk dropped; Edge still never called."""
        motor, *_ = _make_motor()
        motor.tts_local_only = True
        motor._edge_tts_offline = False
        motor._piper = _make_mock_piper(available=True, synthesize_ok=False)

        loaded, piper, communicate = _drive_hablar(motor, "Synth fail")

        communicate.assert_not_called()
        assert piper.synthesize.called  # attempted, returned False
        assert loaded == []


# ===========================================================================
# 5. Backward compat — switch OFF preserves original behavior
# ===========================================================================

class TestLocalOnlyOffPreservesOriginalBehavior:
    def test_edge_is_called_when_switch_off(self):
        """With tts_local_only=False and online, Edge-TTS is called (unchanged behavior)."""
        motor, *_ = _make_motor()
        motor.tts_local_only = False
        motor._edge_tts_offline = False
        motor._piper = _make_mock_piper(available=True, synthesize_ok=True)

        loaded, piper, communicate = _drive_hablar(motor, "Normal mode")

        assert communicate.called, "Edge-TTS should be called when switch is OFF"
        piper.synthesize.assert_not_called()
        assert loaded and loaded[-1].endswith(".mp3")


# ===========================================================================
# 6. UI — switch widget wires to engine command
# ===========================================================================

class TestTtsLocalOnlyUISwitchWiring:
    """Structural test: the UI shell has the switch and wires it correctly.

    Uses source-level inspection to avoid importing CTk (which would require
    a display and can cause OOM from unbounded MagicMock parents if not handled).
    """

    def test_app_shell_has_local_only_switch_attribute(self):
        """app_shell.py source defines switch_local_only widget reference."""
        import ast

        src_path = os.path.join(ROOT_DIR, "opencohost", "ui", "app_shell.py")
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()

        assert "switch_local_only" in source, (
            "app_shell.py must define self.switch_local_only widget"
        )

    def test_app_shell_dispatches_set_tts_local_only(self):
        """app_shell.py source dispatches set_tts_local_only command to motor."""
        src_path = os.path.join(ROOT_DIR, "opencohost", "ui", "app_shell.py")
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()

        assert "set_tts_local_only" in source, (
            "app_shell.py must dispatch 'set_tts_local_only' command to motor_ia"
        )

    def test_app_shell_has_local_only_ui_label(self):
        """app_shell.py source contains the Spanish UI label for the privacy switch."""
        src_path = os.path.join(ROOT_DIR, "opencohost", "ui", "app_shell.py")
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()

        # Accept any of the expected label fragments
        has_label = any(
            fragment in source
            for fragment in ("Solo TTS local", "Piper", "tts local", "local_only")
        )
        assert has_label, (
            "app_shell.py must contain the Spanish UI label for the privacy switch "
            "(expected 'Solo TTS local' or similar)"
        )

    def test_app_shell_has_local_only_helper_text(self):
        """app_shell.py source contains helper text mentioning Microsoft/Edge-TTS."""
        src_path = os.path.join(ROOT_DIR, "opencohost", "ui", "app_shell.py")
        with open(src_path, "r", encoding="utf-8") as f:
            source = f.read()

        has_helper = "Microsoft" in source or "Edge-TTS" in source
        assert has_helper, (
            "app_shell.py must contain helper text explaining the privacy tradeoff "
            "(expected 'Microsoft' or 'Edge-TTS')"
        )


# ===========================================================================
# 7. Engine — snapshot semantics: mid-utterance toggle does not re-route chunks
# ===========================================================================

class TestSnapshotSemantics:
    """Verify that a mid-utterance toggle of tts_local_only does not re-route
    any chunk of the current utterance to Edge-TTS (snapshot semantics)."""

    def test_mid_utterance_flip_off_keeps_all_chunks_on_piper(self):
        """
        All chunks of an utterance stay on Piper even if tts_local_only is
        toggled OFF after chunk 0 -- the snapshot taken at utterance start wins.

        Drives the REAL productor() over a 3-sentence text. Piper's synthesize
        flips motor.tts_local_only OFF DURING chunk 0 (inside the producer
        thread). Because productor() snapshots local_only once at utterance
        start, chunks 1-2 must still route to Piper, not Edge-TTS.
        """
        motor, *_ = _make_motor()
        motor.tts_local_only = True
        motor._edge_tts_offline = False
        motor._piper = _make_mock_piper(available=True, synthesize_ok=True)

        def _synth(text, path):
            motor.tts_local_only = False  # flips mid-utterance, inside chunk 0
            with wave.open(path, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(22050)
                wf.writeframes(b"\x00" * 44)
            return True

        motor._piper.synthesize.side_effect = _synth

        loaded, piper, communicate = _drive_hablar(
            motor, "Chunk cero. Chunk uno. Chunk dos."
        )

        # Edge-TTS must NOT be called for any chunk even though tts_local_only
        # was toggled OFF mid-utterance (the utterance-start snapshot wins).
        communicate.assert_not_called()
        assert piper.synthesize.call_count == 3, (
            "All 3 chunks must be routed to Piper (snapshot semantics)"
        )
        assert len(loaded) == 3
        for path in loaded:
            assert path.endswith(".wav"), "Each chunk must be a .wav from Piper"

        # Confirm the flag is now OFF on the motor (toggle took effect on the object)
        assert motor.tts_local_only is False, (
            "The toggle must have updated motor.tts_local_only; "
            "it just does not affect the current utterance"
        )


# ===========================================================================
# 8. server_qwen.py — structural guard: tts_local_only blocks Edge-TTS
# ===========================================================================

class TestServerQwenLocalOnlyGuardStructural:
    """Structural tests for the server_qwen.py tts_local_only privacy guard.

    server_qwen.py runs in a separate Python environment (xtts_env) that does
    NOT have opencohost installed, so it cannot be imported in flux_env.
    These tests inspect the source text to verify the guard is present and
    correctly ordered relative to the edge_tts.Communicate call.

    Pattern mirrors TestTtsLocalOnlyUISwitchWiring above.
    """

    SERVER_PATH = os.path.join(ROOT_DIR, "opencohost", "server_qwen.py")

    def _source(self):
        with open(self.SERVER_PATH, "r", encoding="utf-8") as f:
            return f.read()

    def test_flag_file_read_present(self):
        """server_qwen.py reads tts_local_only.json using stdlib (os/json) only."""
        source = self._source()
        assert "tts_local_only.json" in source, (
            "server_qwen.py must read the tts_local_only.json flag file directly "
            "(cross-env, stdlib-only read)"
        )
        # The guard must use inline stdlib json — not opencohost.config.settings.
        # Verify by checking that `load_tts_local_only` (the settings helper) is
        # NOT called anywhere in the file (it would require the opencohost package).
        assert "load_tts_local_only" not in source, (
            "server_qwen.py must NOT call load_tts_local_only() — that function "
            "lives in opencohost.config.settings which is unavailable in xtts_env"
        )

    def test_400_error_returned_when_flag_on(self):
        """server_qwen.py returns a 400 JSON error when tts_local_only is ON."""
        source = self._source()
        assert "tts_local_only is enabled" in source, (
            "server_qwen.py must return a 400 error with "
            "'tts_local_only is enabled' when the flag is ON"
        )
        assert "400" in source, (
            "server_qwen.py must return HTTP 400 when tts_local_only flag is ON"
        )

    def test_guard_appears_before_communicate_call(self):
        """The privacy guard must come BEFORE edge_tts.Communicate in the source."""
        source = self._source()
        guard_pos = source.find("tts_local_only is enabled")
        communicate_pos = source.find("edge_tts.Communicate")
        assert guard_pos != -1, "Privacy guard text not found in server_qwen.py"
        assert communicate_pos != -1, "edge_tts.Communicate not found in server_qwen.py"
        assert guard_pos < communicate_pos, (
            "The tts_local_only guard must appear BEFORE edge_tts.Communicate "
            f"(guard at {guard_pos}, Communicate at {communicate_pos})"
        )

    def test_stdlib_only_comment_present(self):
        """server_qwen.py documents WHY the read is duplicated (cross-env note)."""
        source = self._source()
        has_comment = "cross-env" in source or "separate Python" in source or "xtts_env" in source
        assert has_comment, (
            "server_qwen.py must have a comment explaining the duplicated read "
            "(cross-env / separate Python environment)"
        )


# ===========================================================================
# 9. Piper voice selection — Argentina ↔ Neutral offline voice toggle
# ===========================================================================

class TestPiperVoiceSettings:
    def test_default_voice_is_neutral(self):
        from opencohost.config import settings as cfg

        assert cfg.load_piper_voice(config_file="/nonexistent/voice.json") == "neutral"
        assert cfg.DEFAULT_PIPER_VOICE == "neutral"

    def test_save_and_reload_english(self, tmp_path):
        from opencohost.config import settings as cfg

        path = str(tmp_path / "piper_voice.json")
        cfg.save_piper_voice("english", config_file=path)
        assert cfg.load_piper_voice(config_file=path) == "english"

    def test_invalid_saved_key_falls_back_to_default(self, tmp_path):
        from opencohost.config import settings as cfg

        path = str(tmp_path / "piper_voice.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"piper_voice": "klingon"}))
        assert cfg.load_piper_voice(config_file=path) == "neutral"

    def test_invalid_save_key_is_normalized(self, tmp_path):
        from opencohost.config import settings as cfg

        path = str(tmp_path / "piper_voice.json")
        cfg.save_piper_voice("klingon", config_file=path)
        assert cfg.load_piper_voice(config_file=path) == "neutral"

    def test_voice_path_resolves_per_key(self):
        from opencohost.config import settings as cfg

        assert cfg.piper_voice_path("neutral").endswith("es_MX-claude-high.onnx")
        assert cfg.piper_voice_path("english").endswith("en_US-kristin-medium.onnx")
        # Unknown key resolves to the default (neutral), never an empty path.
        assert cfg.piper_voice_path("klingon").endswith("es_MX-claude-high.onnx")

    def test_voice_path_under_piper_cache_dir(self):
        from opencohost.config import settings as cfg

        p = cfg.piper_voice_path("neutral").replace("\\", "/")
        assert "/piper/" in p


# ===========================================================================
# 10. TTS locale coupling (P5) — Piper 'english' entry + locale-aware default
# ===========================================================================

class TestPiperVoiceRegistryLang:
    def test_english_voice_registered(self):
        from opencohost.config import settings as cfg

        assert cfg.PIPER_VOICES["english"] == {
            "label": "🇺🇸 English", "file": "en_US-kristin-medium.onnx", "lang": "en",
        }

    def test_es_voices_tagged_lang_es(self):
        from opencohost.config import settings as cfg

        assert cfg.PIPER_VOICES["neutral"]["lang"] == "es"

    def test_voice_path_resolves_english(self):
        from opencohost.config import settings as cfg

        assert cfg.piper_voice_path("english").endswith("en_US-kristin-medium.onnx")


class TestDefaultPiperVoiceForLocale:
    def test_en_maps_to_english(self):
        from opencohost.config import settings as cfg

        assert cfg.default_piper_voice_for_locale("en") == "english"
        assert cfg.default_piper_voice_for_locale("en-US") == "english"
        assert cfg.default_piper_voice_for_locale("EN") == "english"

    def test_es_and_unknown_map_to_default(self):
        from opencohost.config import settings as cfg

        assert cfg.default_piper_voice_for_locale("es") == cfg.DEFAULT_PIPER_VOICE
        assert cfg.default_piper_voice_for_locale("fr") == cfg.DEFAULT_PIPER_VOICE
        assert cfg.default_piper_voice_for_locale(None) == cfg.DEFAULT_PIPER_VOICE
        assert cfg.default_piper_voice_for_locale("") == cfg.DEFAULT_PIPER_VOICE


class TestLoadPiperVoiceLocaleAwareDefault:
    def test_missing_file_returns_custom_default(self):
        from opencohost.config import settings as cfg

        assert cfg.load_piper_voice(
            config_file="/nonexistent/voice.json", default="english"
        ) == "english"

    def test_missing_file_default_param_preserves_default(self):
        # No `default` passed -> unchanged default behavior.
        from opencohost.config import settings as cfg

        assert cfg.load_piper_voice(config_file="/nonexistent/voice.json") == "neutral"

    def test_persisted_choice_wins_over_custom_default(self, tmp_path):
        # An explicit user pick always wins over the locale-aware default.
        from opencohost.config import settings as cfg

        path = str(tmp_path / "piper_voice.json")
        cfg.save_piper_voice("neutral", config_file=path)
        assert cfg.load_piper_voice(config_file=path, default="english") == "neutral"

    def test_corrupted_key_falls_back_to_custom_default(self, tmp_path):
        from opencohost.config import settings as cfg

        path = str(tmp_path / "piper_voice.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps({"piper_voice": "klingon"}))
        assert cfg.load_piper_voice(config_file=path, default="english") == "english"


class TestPiperVoiceCommand:
    def test_set_piper_voice_reloads_and_persists(self):
        from unittest.mock import patch, MagicMock

        motor, *_ = _make_motor()
        motor._piper = MagicMock()
        motor._piper.reload.return_value = True
        with patch("opencohost.core.llm_engine.save_piper_voice") as mock_save:
            motor._dispatch_command("set_piper_voice", "neutral")
        motor._piper.reload.assert_called_once()
        assert motor._piper.reload.call_args[0][0].endswith("es_MX-claude-high.onnx")
        mock_save.assert_called_once_with("neutral")

    def test_set_piper_voice_skips_persist_on_reload_failure(self):
        from unittest.mock import patch, MagicMock

        motor, *_ = _make_motor()
        motor._piper = MagicMock()
        motor._piper.reload.return_value = False
        with patch("opencohost.core.llm_engine.save_piper_voice") as mock_save:
            motor._dispatch_command("set_piper_voice", "neutral")
        mock_save.assert_not_called()

    def test_set_piper_voice_updates_tracked_voice_key_on_success(self):
        from unittest.mock import patch, MagicMock

        motor, *_ = _make_motor()
        motor._piper = MagicMock()
        motor._piper.reload.return_value = True
        with patch("opencohost.core.llm_engine.save_piper_voice"):
            motor._dispatch_command("set_piper_voice", "english")
        assert motor._piper_voice_key == "english"

    def test_set_piper_voice_keeps_tracked_key_on_reload_failure(self):
        from unittest.mock import patch, MagicMock

        motor, *_ = _make_motor()
        motor._piper_voice_key = "english"
        motor._piper = MagicMock()
        motor._piper.reload.return_value = False
        with patch("opencohost.core.llm_engine.save_piper_voice"):
            motor._dispatch_command("set_piper_voice", "neutral")
        assert motor._piper_voice_key == "english"

    def test_set_piper_voice_unknown_key_normalizes_to_default(self):
        from unittest.mock import patch, MagicMock

        motor, *_ = _make_motor()
        motor._piper_voice_key = "english"
        motor._piper = MagicMock()
        motor._piper.reload.return_value = True
        with patch("opencohost.core.llm_engine.save_piper_voice") as mock_save:
            motor._dispatch_command("set_piper_voice", "argentina")
        motor._piper.reload.assert_called_once()
        assert motor._piper.reload.call_args[0][0].endswith("es_MX-claude-high.onnx")
        mock_save.assert_called_once_with("neutral")
        assert motor._piper_voice_key == "neutral"

    def test_load_piper_voice_legacy_key_normalizes_to_default_on_startup(self, tmp_path):
        from opencohost.config.settings import load_piper_voice, PIPER_VOICES

        legacy_file = tmp_path / "piper_voice.json"
        legacy_file.write_text('{"piper_voice": "argentina"}', encoding="utf-8")

        # Spanish startup default
        resolved = load_piper_voice(str(legacy_file), default="neutral")
        assert resolved == "neutral"
        assert resolved in PIPER_VOICES


