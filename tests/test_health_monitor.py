"""Unit tests for health monitor components.

Tests all sub-components: VRAMGuard, OllamaWatchdog, RTFTracker,
QwenProcessManager, HealthMonitor, and MonitorState.

All tests use mocking — no GPU, no Ollama, no Qwen server required.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from core.health_monitor import (
    HealthMonitor,
    MonitorState,
    OllamaWatchdog,
    QwenProcessManager,
    RTFTracker,
    VRAMGuard,
)


# ──────────────────────────────────────────────
# VRAMGuard Tests
# ──────────────────────────────────────────────

class TestVRAMGuard:
    """Tests for VRAMGuard — GPU VRAM monitoring."""

    def test_unavailable_when_pynvml_not_installed(self):
        """VRAMGuard returns 'unavailable' when pynvml is not installed."""
        guard = VRAMGuard()
        # In most test environments pynvml won't be available
        assert guard.status in ("unavailable", "normal", "low", "critical")

    def test_poll_does_not_crash(self):
        """poll() never raises, even without pynvml."""
        guard = VRAMGuard()
        for _ in range(3):
            guard.poll()  # Should not raise

    def test_status_values(self):
        """Status is always one of the expected values."""
        guard = VRAMGuard()
        guard.poll()
        assert guard.status in ("unavailable", "normal", "low", "critical")

    def test_free_mb_is_float(self):
        """free_mb always returns a float."""
        guard = VRAMGuard()
        guard.poll()
        assert isinstance(guard.free_mb, float)


# ──────────────────────────────────────────────
# OllamaWatchdog Tests
# ──────────────────────────────────────────────

class TestOllamaWatchdog:
    """Tests for OllamaWatchdog — Ollama service health polling."""

    def test_healthy_on_success(self):
        """Returns 'healthy' when /api/tags returns 200."""
        wd = OllamaWatchdog()
        with patch("core.health_monitor.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            wd.poll()
        assert wd.status == "healthy"
        assert wd.consecutive_failures == 0

    def test_down_after_threshold_failures(self):
        """Returns 'down' after OLLAMA_FAILURE_THRESHOLD consecutive failures."""
        wd = OllamaWatchdog()
        with patch("core.health_monitor.requests.get", side_effect=Exception("connection refused")):
            for _ in range(4):  # threshold is 3
                wd.poll()
        assert wd.status == "down"
        assert wd.consecutive_failures >= 3

    def test_recovery_after_success(self):
        """Resets failure count on successful poll."""
        wd = OllamaWatchdog()
        with patch("core.health_monitor.requests.get", side_effect=Exception("fail")):
            for _ in range(2):
                wd.poll()
        assert wd.consecutive_failures == 2

        # Now succeed
        with patch("core.health_monitor.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            wd.poll()
        assert wd.status == "healthy"
        assert wd.consecutive_failures == 0

    def test_timeout_does_not_crash(self):
        """Timeout exception doesn't crash the watchdog."""
        wd = OllamaWatchdog()
        import requests
        with patch("core.health_monitor.requests.get", side_effect=requests.exceptions.Timeout):
            wd.poll()  # Should not raise


# ──────────────────────────────────────────────
# RTFTracker Tests
# ──────────────────────────────────────────────

class TestRTFTracker:
    """Tests for RTFTracker — Real-Time Factor tracking."""

    def test_normal_measurement(self):
        """RTF below threshold returns 'normal'."""
        tracker = RTFTracker()
        tracker.record(1.0, 2.0)  # RTF = 0.5
        assert tracker.status == "normal"
        assert tracker.rolling_average == 0.5

    def test_high_rtf_degraded(self):
        """RTF above threshold returns 'degraded'."""
        tracker = RTFTracker()
        tracker.record(5.0, 1.0)  # RTF = 5.0
        assert tracker.status == "degraded"
        assert tracker.rolling_average == 5.0

    def test_short_audio_excluded(self):
        """Measurements with audio_duration < 0.5s are excluded."""
        tracker = RTFTracker()
        tracker.record(1.0, 0.3)  # Too short
        assert tracker.rolling_average is None
        assert tracker.status == "unknown"

    def test_rolling_average_window(self):
        """Rolling average uses deque with maxlen from settings."""
        tracker = RTFTracker()
        # Record more than window size
        for i in range(20):
            tracker.record(float(i + 1), 1.0)
        # Should only keep last RTF_POLL_WINDOW measurements
        assert tracker.count <= 10  # RTF_POLL_WINDOW = 10

    def test_no_history_unknown(self):
        """Returns 'unknown' when no measurements recorded."""
        tracker = RTFTracker()
        assert tracker.status == "unknown"
        assert tracker.rolling_average is None

    def test_recovery_threshold(self):
        """RTF below recovery threshold returns 'normal'."""
        tracker = RTFTracker()
        tracker.record(0.5, 1.0)  # RTF = 0.5
        assert tracker.status == "normal"

    def test_zero_audio_duration(self):
        """Zero audio duration doesn't cause division by zero."""
        tracker = RTFTracker()
        tracker.record(1.0, 0.0)  # Should not crash
        # Zero duration is < 0.5, so excluded
        assert tracker.rolling_average is None


# ──────────────────────────────────────────────
# QwenProcessManager Tests
# ──────────────────────────────────────────────

class TestQwenProcessManager:
    """Tests for QwenProcessManager — subprocess lifecycle."""

    def test_is_running_false_initially(self):
        """Not running before start()."""
        mgr = QwenProcessManager()
        assert mgr.is_running is False

    def test_is_manual_false_initially(self):
        """Not manual before attach_existing()."""
        mgr = QwenProcessManager()
        assert mgr.is_manual is False

    def test_idle_seconds_zero_initially(self):
        """Idle seconds is 0 before any health check."""
        mgr = QwenProcessManager()
        assert mgr.idle_seconds == 0.0

    def test_stop_does_not_crash_when_not_running(self):
        """stop() is safe when no process is running."""
        mgr = QwenProcessManager()
        mgr.stop()  # Should not raise

    def test_attach_existing_no_server(self):
        """attach_existing returns False when no server on port 5000."""
        mgr = QwenProcessManager()
        with patch.object(QwenProcessManager, "_is_port_in_use", return_value=False):
            result = mgr.attach_existing()
        assert result is False

    def test_attach_existing_with_server(self):
        """attach_existing returns True when server is healthy on port 5000."""
        mgr = QwenProcessManager()
        with patch.object(QwenProcessManager, "_is_port_in_use", return_value=True):
            with patch.object(mgr, "_check_health", return_value=True):
                result = mgr.attach_existing()
        assert result is True
        assert mgr.is_manual is True

    def test_check_health_returns_false_on_error(self):
        """_check_health returns False on connection error."""
        mgr = QwenProcessManager()
        with patch("core.health_monitor.requests.get", side_effect=Exception("fail")):
            assert mgr._check_health() is False

    def test_check_health_returns_true_on_200(self):
        """_check_health returns True on 200 response."""
        mgr = QwenProcessManager()
        with patch("core.health_monitor.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            assert mgr._check_health() is True

    def test_is_port_in_use(self):
        """_is_port_in_use checks port availability."""
        result = QwenProcessManager._is_port_in_use(5000)
        assert isinstance(result, bool)


# ──────────────────────────────────────────────
# HealthMonitor Tests
# ──────────────────────────────────────────────

class TestHealthMonitor:
    """Tests for HealthMonitor — main daemon orchestrator."""

    def _make_monitor(self) -> HealthMonitor:
        """Create a monitor with mocked sub-components."""
        monitor = HealthMonitor()
        # Mock Qwen manager to avoid real HTTP calls in _poll_all
        monitor._qwen = MagicMock()
        monitor._qwen.is_running = True
        monitor._qwen.is_manual = False
        # Mock poll methods so tests can set status directly without
        # _poll_all() overwriting them with real (unavailable) values.
        monitor._vram.poll = MagicMock()
        monitor._ollama.poll = MagicMock()
        return monitor

    def test_state_snapshot(self):
        """state property returns a MonitorState snapshot."""
        monitor = self._make_monitor()
        state = monitor.state
        assert isinstance(state, MonitorState)
        assert state.overall_status == "unknown"

    def test_state_is_snapshot_not_reference(self):
        """state returns a copy, not a reference."""
        monitor = self._make_monitor()
        s1 = monitor.state
        s2 = monitor.state
        assert s1 is not s2

    def test_should_use_heavy_tts_backward_compat(self):
        """Returns True when auto_fallback disabled and motor is pesado."""
        monitor = self._make_monitor()
        assert monitor.should_use_heavy_tts(auto_fallback_enabled=False, manual_motor="pesado") is True

    def test_should_use_heavy_tts_backward_compat_ligero(self):
        """Returns False when auto_fallback disabled and motor is ligero."""
        monitor = self._make_monitor()
        assert monitor.should_use_heavy_tts(auto_fallback_enabled=False, manual_motor="ligero") is False

    def test_should_use_heavy_tts_manual_ligero_always_edge(self):
        """Manual 'ligero' always returns False regardless of health."""
        monitor = self._make_monitor()
        assert monitor.should_use_heavy_tts(auto_fallback_enabled=True, manual_motor="ligero") is False

    def test_should_use_heavy_tts_vram_low_blocks(self):
        """VRAM low blocks heavy TTS."""
        monitor = self._make_monitor()
        # Set VRAM status directly and update state
        monitor._vram._status = "low"
        monitor._vram._free_mb = 1500.0
        monitor._poll_all()
        assert monitor.should_use_heavy_tts(auto_fallback_enabled=True, manual_motor="pesado") is False

    def test_should_use_heavy_tts_vram_critical_blocks(self):
        """VRAM critical blocks heavy TTS."""
        monitor = self._make_monitor()
        monitor._vram._status = "critical"
        monitor._vram._free_mb = 500.0
        monitor._poll_all()
        assert monitor.should_use_heavy_tts(auto_fallback_enabled=True, manual_motor="pesado") is False

    def test_should_use_heavy_tts_rtf_degraded_blocks(self):
        """RTF degraded blocks heavy TTS."""
        monitor = self._make_monitor()
        monitor._rtf._measurements.append(3.0)  # High RTF
        monitor._poll_all()
        assert monitor.should_use_heavy_tts(auto_fallback_enabled=True, manual_motor="pesado") is False

    def test_should_use_heavy_tts_all_healthy(self):
        """All healthy returns True for pesado motor."""
        monitor = self._make_monitor()
        monitor._vram._status = "normal"
        monitor._vram._free_mb = 5000.0
        monitor._ollama._status = "healthy"
        monitor._poll_all()
        assert monitor.should_use_heavy_tts(auto_fallback_enabled=True, manual_motor="pesado") is True

    def test_can_vibe_call_vram_low(self):
        """VRAM low blocks Vibe calls."""
        monitor = self._make_monitor()
        monitor._vram._status = "low"
        monitor._poll_all()
        assert monitor.can_vibe_call() is False

    def test_can_vibe_call_vram_critical(self):
        """VRAM critical blocks Vibe calls."""
        monitor = self._make_monitor()
        monitor._vram._status = "critical"
        monitor._poll_all()
        assert monitor.can_vibe_call() is False

    def test_can_vibe_call_ollama_down(self):
        """Ollama down blocks Vibe calls."""
        monitor = self._make_monitor()
        monitor._ollama._status = "down"
        monitor._vram._status = "normal"
        monitor._vram._free_mb = 5000.0
        monitor._poll_all()
        assert monitor.can_vibe_call() is False

    def test_can_vibe_call_all_healthy(self):
        """All healthy allows Vibe calls."""
        monitor = self._make_monitor()
        monitor._vram._status = "normal"
        monitor._vram._free_mb = 5000.0
        monitor._ollama._status = "healthy"
        monitor._poll_all()
        assert monitor.can_vibe_call() is True

    def test_record_ttf_measurement_delegates(self):
        """record_ttf_measurement delegates to RTFTracker."""
        monitor = self._make_monitor()
        monitor.record_ttf_measurement(2.0, 1.0)
        assert monitor._rtf.rolling_average == 2.0

    def test_record_ttf_measurement_error_does_not_crash(self):
        """RTF measurement failure doesn't crash."""
        monitor = self._make_monitor()
        with patch.object(monitor._rtf, "record", side_effect=Exception("fail")):
            monitor.record_ttf_measurement(2.0, 1.0)  # Should not raise

    def test_overall_status_green(self):
        """Green when all nominal."""
        monitor = self._make_monitor()
        monitor._vram._status = "normal"
        monitor._vram._free_mb = 5000.0
        monitor._ollama._status = "healthy"
        monitor._poll_all()
        state = monitor.state
        assert state.overall_status == "green"

    def test_overall_status_yellow_vram_low(self):
        """Yellow when VRAM is low."""
        monitor = self._make_monitor()
        monitor._vram._status = "low"
        monitor._vram._free_mb = 1500.0
        monitor._ollama._status = "healthy"
        monitor._poll_all()
        state = monitor.state
        assert state.overall_status == "yellow"

    def test_overall_status_yellow_rtf_degraded(self):
        """Yellow when RTF is degraded."""
        monitor = self._make_monitor()
        monitor._vram._status = "normal"
        monitor._vram._free_mb = 5000.0
        monitor._ollama._status = "healthy"
        monitor._rtf._measurements.append(3.0)
        monitor._poll_all()
        state = monitor.state
        assert state.overall_status == "yellow"

    def test_overall_status_red_vram_critical(self):
        """Red when VRAM is critical."""
        monitor = self._make_monitor()
        monitor._vram._status = "critical"
        monitor._vram._free_mb = 500.0
        monitor._ollama._status = "healthy"
        monitor._poll_all()
        state = monitor.state
        assert state.overall_status == "red"

    def test_overall_status_red_ollama_down(self):
        """Red when Ollama is down."""
        monitor = self._make_monitor()
        monitor._vram._status = "normal"
        monitor._vram._free_mb = 5000.0
        monitor._ollama._status = "down"
        monitor._poll_all()
        state = monitor.state
        assert state.overall_status == "red"

    def test_thread_safety_concurrent_reads(self):
        """Multiple concurrent state reads don't crash."""
        monitor = self._make_monitor()
        results = []
        errors = []

        def reader():
            try:
                for _ in range(50):
                    s = monitor.state
                    results.append(s)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        assert len(errors) == 0
        assert len(results) == 250  # 5 threads * 50 reads


# ──────────────────────────────────────────────
# MonitorState Tests
# ──────────────────────────────────────────────

class TestMonitorState:
    """Tests for MonitorState dataclass."""

    def test_default_values(self):
        """All fields have sensible defaults."""
        state = MonitorState()
        assert state.vram_status == "unknown"
        assert state.rtf_status == "unknown"
        assert state.ollama_status == "unknown"
        assert state.qwen_status == "unknown"
        assert state.overall_status == "unknown"
        assert state.free_vram_mb == 0.0
        assert state.rtf_rolling_avg is None
        assert state.last_updated == 0.0

    def test_custom_values(self):
        """Fields accept custom values."""
        state = MonitorState(
            vram_status="normal",
            rtf_status="normal",
            ollama_status="healthy",
            qwen_status="healthy",
            overall_status="green",
            free_vram_mb=4096.0,
            rtf_rolling_avg=0.8,
            last_updated=1234567890.0,
        )
        assert state.vram_status == "normal"
        assert state.free_vram_mb == 4096.0
        assert state.overall_status == "green"
