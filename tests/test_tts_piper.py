"""
Unit tests for core/tts_piper.py — PiperEngine and _is_connection_error.

All tests mock piper-tts internals so no real model file is required.
"""
import socket
import ssl
import threading
import wave
from unittest.mock import MagicMock, patch, call

import pytest

import sys
import os

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_engine(model_path: str = "/fake/model.onnx"):
    """Return a PiperEngine without triggering any real imports."""
    from opencohost.core.tts_piper import PiperEngine
    return PiperEngine(model_path)


# ---------------------------------------------------------------------------
# Task 3.1 — load() failure modes
# ---------------------------------------------------------------------------

class TestLoad:
    def test_load_returns_false_when_piper_not_installed(self):
        """_PIPER_AVAILABLE=False → load() returns False, does not raise."""
        with patch("opencohost.core.tts_piper._PIPER_AVAILABLE", False):
            engine = _make_engine()
            assert engine.load() is False
            assert engine.is_available() is False

    def test_load_returns_false_on_empty_path(self):
        """Empty model path → load() returns False without calling PiperVoice."""
        with patch("opencohost.core.tts_piper._PIPER_AVAILABLE", True):
            engine = _make_engine(model_path="")
            assert engine.load() is False
            assert engine.is_available() is False

    def test_load_returns_false_on_file_not_found(self):
        """FileNotFoundError from PiperVoice.load → load() returns False."""
        mock_voice_cls = MagicMock()
        mock_voice_cls.load.side_effect = FileNotFoundError("no such file")
        with patch("opencohost.core.tts_piper._PIPER_AVAILABLE", True), \
             patch("opencohost.core.tts_piper._piper_voice") as mock_piper_voice:
            mock_piper_voice.PiperVoice = mock_voice_cls
            engine = _make_engine()
            assert engine.load() is False
            assert engine.is_available() is False

    def test_load_returns_false_on_generic_exception(self):
        """Any Exception from PiperVoice.load → load() returns False, does not raise."""
        mock_voice_cls = MagicMock()
        mock_voice_cls.load.side_effect = RuntimeError("onnx crash")
        with patch("opencohost.core.tts_piper._PIPER_AVAILABLE", True), \
             patch("opencohost.core.tts_piper._piper_voice") as mock_piper_voice:
            mock_piper_voice.PiperVoice = mock_voice_cls
            engine = _make_engine()
            assert engine.load() is False

    def test_load_returns_true_on_success(self):
        """Successful PiperVoice.load → load() returns True and marks available."""
        mock_voice_instance = MagicMock()
        mock_voice_cls = MagicMock()
        mock_voice_cls.load.return_value = mock_voice_instance
        with patch("opencohost.core.tts_piper._PIPER_AVAILABLE", True), \
             patch("opencohost.core.tts_piper._piper_voice") as mock_piper_voice:
            mock_piper_voice.PiperVoice = mock_voice_cls
            engine = _make_engine()
            assert engine.load() is True
            assert engine.is_available() is True


# ---------------------------------------------------------------------------
# Task 3.2 — synthesize() success
# ---------------------------------------------------------------------------

class TestSynthesizeSuccess:
    def test_synthesize_returns_true_and_calls_synthesize_wav(self, tmp_path):
        """On success: returns True and invokes voice.synthesize_wav."""
        output_path = str(tmp_path / "out.wav")

        def fake_synthesize_wav(text, wav_file):
            # Write a minimal valid WAV so wave.open doesn't error
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)
            wav_file.writeframes(b"\x00" * 44)

        mock_voice = MagicMock()
        mock_voice.synthesize_wav.side_effect = fake_synthesize_wav

        with patch("opencohost.core.tts_piper._PIPER_AVAILABLE", True), \
             patch("opencohost.core.tts_piper._piper_voice") as mock_piper_voice:
            mock_piper_voice.PiperVoice.load.return_value = mock_voice
            engine = _make_engine()
            engine.load()
            result = engine.synthesize("Hello world", output_path)

        assert result is True
        mock_voice.synthesize_wav.assert_called_once()


# ---------------------------------------------------------------------------
# Task 3.3 — synthesize() failure
# ---------------------------------------------------------------------------

class TestSynthesizeFailure:
    def test_synthesize_returns_false_on_exception(self, tmp_path):
        """synthesize_wav raising → returns False, does not propagate."""
        output_path = str(tmp_path / "out.wav")
        mock_voice = MagicMock()
        mock_voice.synthesize_wav.side_effect = RuntimeError("synthesis failed")

        with patch("opencohost.core.tts_piper._PIPER_AVAILABLE", True), \
             patch("opencohost.core.tts_piper._piper_voice") as mock_piper_voice:
            mock_piper_voice.PiperVoice.load.return_value = mock_voice
            engine = _make_engine()
            engine.load()
            result = engine.synthesize("Hello world", output_path)

        assert result is False  # no exception propagated


# ---------------------------------------------------------------------------
# Task 3.4 — is_available()
# ---------------------------------------------------------------------------

class TestIsAvailable:
    def test_is_available_false_before_load(self):
        engine = _make_engine()
        assert engine.is_available() is False

    def test_is_available_true_after_successful_load(self):
        mock_voice_instance = MagicMock()
        mock_voice_cls = MagicMock()
        mock_voice_cls.load.return_value = mock_voice_instance
        with patch("opencohost.core.tts_piper._PIPER_AVAILABLE", True), \
             patch("opencohost.core.tts_piper._piper_voice") as mock_piper_voice:
            mock_piper_voice.PiperVoice = mock_voice_cls
            engine = _make_engine()
            engine.load()
            assert engine.is_available() is True


# ---------------------------------------------------------------------------
# Speaking-rate control — length_scale via SynthesisConfig
# ---------------------------------------------------------------------------

class _FakeSynthesisConfig:
    """Stub so the tests do not depend on the installed piper-tts version."""

    def __init__(self, length_scale=1.0):
        self.length_scale = length_scale


class TestLengthScale:
    def _loaded_engine(self, mock_piper_voice, length_scale=None):
        from opencohost.core.tts_piper import PiperEngine

        def fake_synthesize_wav(text, wav_file, **kwargs):
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)
            wav_file.writeframes(b"\x00" * 44)

        mock_voice = MagicMock()
        mock_voice.synthesize_wav.side_effect = fake_synthesize_wav
        mock_piper_voice.PiperVoice.load.return_value = mock_voice
        if length_scale is None:
            engine = PiperEngine("/fake/model.onnx")
        else:
            engine = PiperEngine("/fake/model.onnx", length_scale=length_scale)
        engine.load()
        return engine, mock_voice

    def test_default_engine_passes_no_syn_config(self, tmp_path):
        """length_scale=1.0 (default) must keep the legacy two-arg call."""
        with patch("opencohost.core.tts_piper._PIPER_AVAILABLE", True), \
             patch("opencohost.core.tts_piper._piper_voice") as mock_piper_voice:
            engine, mock_voice = self._loaded_engine(mock_piper_voice)
            assert engine.synthesize("hola", str(tmp_path / "out.wav")) is True

        kwargs = mock_voice.synthesize_wav.call_args.kwargs
        assert "syn_config" not in kwargs

    def test_custom_length_scale_passes_syn_config(self, tmp_path):
        """length_scale != 1.0 must reach synthesize_wav via syn_config."""
        with patch("opencohost.core.tts_piper._PIPER_AVAILABLE", True), \
             patch("opencohost.core.tts_piper._SynthesisConfig", _FakeSynthesisConfig), \
             patch("opencohost.core.tts_piper._piper_voice") as mock_piper_voice:
            engine, mock_voice = self._loaded_engine(mock_piper_voice, length_scale=1.2)
            assert engine.synthesize("hola", str(tmp_path / "out.wav")) is True

        syn_config = mock_voice.synthesize_wav.call_args.kwargs["syn_config"]
        assert syn_config.length_scale == pytest.approx(1.2)

    def test_missing_synthesis_config_falls_back_to_default_call(self, tmp_path):
        """Older piper-tts without SynthesisConfig must not crash synthesis."""
        with patch("opencohost.core.tts_piper._PIPER_AVAILABLE", True), \
             patch("opencohost.core.tts_piper._SynthesisConfig", None), \
             patch("opencohost.core.tts_piper._piper_voice") as mock_piper_voice:
            engine, mock_voice = self._loaded_engine(mock_piper_voice, length_scale=1.2)
            assert engine.synthesize("hola", str(tmp_path / "out.wav")) is True

        kwargs = mock_voice.synthesize_wav.call_args.kwargs
        assert "syn_config" not in kwargs

    def test_motor_wires_length_scale_from_settings(self):
        """MotorVocalIA must build its PiperEngine with the persisted speed."""
        import queue
        from opencohost.core import llm_engine

        with patch("opencohost.core.llm_engine.load_tts_speed", return_value=1.23):
            motor = llm_engine.MotorVocalIA(queue.Queue(), lambda event: None)

        assert motor._piper._length_scale == pytest.approx(1.23)


# ---------------------------------------------------------------------------
# Task 3.5 — _is_connection_error() classification
# ---------------------------------------------------------------------------

class TestIsConnectionError:
    """Import _is_connection_error from llm_engine (module-level helper)."""

    @pytest.fixture(autouse=True)
    def import_helper(self):
        from opencohost.core.llm_engine import _is_connection_error
        self.fn = _is_connection_error

    def test_gaierror_returns_true(self):
        exc = socket.gaierror("name or service not known")
        assert self.fn(exc) is True

    def test_ssl_error_returns_true(self):
        exc = ssl.SSLError("certificate verify failed")
        assert self.fn(exc) is True

    def test_aiohttp_client_connector_error_returns_true(self):
        try:
            import aiohttp
            exc = aiohttp.ClientConnectorError(MagicMock(), OSError("conn refused"))
            assert self.fn(exc) is True
        except ImportError:
            pytest.skip("aiohttp not installed")

    def test_wrapped_cause_gaierror_returns_true(self):
        """Classify even when gaierror is nested via __cause__."""
        inner = socket.gaierror("dns fail")
        outer = RuntimeError("wrapped")
        outer.__cause__ = inner
        assert self.fn(outer) is True

    def test_timeout_error_returns_false(self):
        exc = asyncio.TimeoutError()
        assert self.fn(exc) is False

    def test_value_error_returns_false(self):
        assert self.fn(ValueError("bad value")) is False

    def test_bare_exception_returns_false(self):
        assert self.fn(Exception("generic")) is False


import asyncio  # noqa: E402  (needed by TestIsConnectionError)


# ---------------------------------------------------------------------------
# Task 3.6 — Thread safety
# ---------------------------------------------------------------------------

class TestThreadSafety:
    def test_concurrent_synthesize_no_exception(self, tmp_path):
        """Two threads calling synthesize() concurrently must not raise."""
        results = []
        errors = []

        def fake_synthesize_wav(text, wav_file):
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(22050)
            wav_file.writeframes(b"\x00" * 44)

        mock_voice = MagicMock()
        mock_voice.synthesize_wav.side_effect = fake_synthesize_wav

        with patch("opencohost.core.tts_piper._PIPER_AVAILABLE", True), \
             patch("opencohost.core.tts_piper._piper_voice") as mock_piper_voice:
            mock_piper_voice.PiperVoice.load.return_value = mock_voice
            engine = _make_engine()
            engine.load()

            def worker(idx):
                path = str(tmp_path / f"thread_{idx}.wav")
                try:
                    r = engine.synthesize(f"sentence {idx}", path)
                    results.append(r)
                except Exception as exc:
                    errors.append(exc)

            t1 = threading.Thread(target=worker, args=(1,))
            t2 = threading.Thread(target=worker, args=(2,))
            t1.start()
            t2.start()
            t1.join(timeout=5)
            t2.join(timeout=5)

        assert errors == [], f"Unexpected exceptions: {errors}"
        assert len(results) == 2
        assert all(r is True for r in results)
