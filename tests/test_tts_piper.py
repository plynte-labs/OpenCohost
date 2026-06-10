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
    from core.tts_piper import PiperEngine
    return PiperEngine(model_path)


# ---------------------------------------------------------------------------
# Task 3.1 — load() failure modes
# ---------------------------------------------------------------------------

class TestLoad:
    def test_load_returns_false_when_piper_not_installed(self):
        """_PIPER_AVAILABLE=False → load() returns False, does not raise."""
        with patch("core.tts_piper._PIPER_AVAILABLE", False):
            engine = _make_engine()
            assert engine.load() is False
            assert engine.is_available() is False

    def test_load_returns_false_on_empty_path(self):
        """Empty model path → load() returns False without calling PiperVoice."""
        with patch("core.tts_piper._PIPER_AVAILABLE", True):
            engine = _make_engine(model_path="")
            assert engine.load() is False
            assert engine.is_available() is False

    def test_load_returns_false_on_file_not_found(self):
        """FileNotFoundError from PiperVoice.load → load() returns False."""
        mock_voice_cls = MagicMock()
        mock_voice_cls.load.side_effect = FileNotFoundError("no such file")
        with patch("core.tts_piper._PIPER_AVAILABLE", True), \
             patch("core.tts_piper._piper_voice") as mock_piper_voice:
            mock_piper_voice.PiperVoice = mock_voice_cls
            engine = _make_engine()
            assert engine.load() is False
            assert engine.is_available() is False

    def test_load_returns_false_on_generic_exception(self):
        """Any Exception from PiperVoice.load → load() returns False, does not raise."""
        mock_voice_cls = MagicMock()
        mock_voice_cls.load.side_effect = RuntimeError("onnx crash")
        with patch("core.tts_piper._PIPER_AVAILABLE", True), \
             patch("core.tts_piper._piper_voice") as mock_piper_voice:
            mock_piper_voice.PiperVoice = mock_voice_cls
            engine = _make_engine()
            assert engine.load() is False

    def test_load_returns_true_on_success(self):
        """Successful PiperVoice.load → load() returns True and marks available."""
        mock_voice_instance = MagicMock()
        mock_voice_cls = MagicMock()
        mock_voice_cls.load.return_value = mock_voice_instance
        with patch("core.tts_piper._PIPER_AVAILABLE", True), \
             patch("core.tts_piper._piper_voice") as mock_piper_voice:
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

        with patch("core.tts_piper._PIPER_AVAILABLE", True), \
             patch("core.tts_piper._piper_voice") as mock_piper_voice:
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

        with patch("core.tts_piper._PIPER_AVAILABLE", True), \
             patch("core.tts_piper._piper_voice") as mock_piper_voice:
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
        with patch("core.tts_piper._PIPER_AVAILABLE", True), \
             patch("core.tts_piper._piper_voice") as mock_piper_voice:
            mock_piper_voice.PiperVoice = mock_voice_cls
            engine = _make_engine()
            engine.load()
            assert engine.is_available() is True


# ---------------------------------------------------------------------------
# Task 3.5 — _is_connection_error() classification
# ---------------------------------------------------------------------------

class TestIsConnectionError:
    """Import _is_connection_error from llm_engine (module-level helper)."""

    @pytest.fixture(autouse=True)
    def import_helper(self):
        from core.llm_engine import _is_connection_error
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

        with patch("core.tts_piper._PIPER_AVAILABLE", True), \
             patch("core.tts_piper._piper_voice") as mock_piper_voice:
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
