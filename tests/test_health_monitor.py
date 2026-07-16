"""Unit tests for health monitor components.

Tests all sub-components: VRAMGuard, OllamaWatchdog, RTFTracker,
QwenProcessManager, HealthMonitor, and MonitorState.

All tests use mocking — no GPU, no Ollama, no Qwen server required.
"""

from __future__ import annotations

import threading
import time
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from opencohost.core.health_monitor import (
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
        with patch("opencohost.core.health_monitor.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            wd.poll()
        assert wd.status == "healthy"
        assert wd.consecutive_failures == 0

    def test_down_after_threshold_failures(self):
        """Returns 'down' after OLLAMA_FAILURE_THRESHOLD consecutive failures."""
        wd = OllamaWatchdog()
        with patch("opencohost.core.health_monitor.requests.get", side_effect=Exception("connection refused")):
            for _ in range(4):  # threshold is 3
                wd.poll()
        assert wd.status == "down"
        assert wd.consecutive_failures >= 3

    def test_recovery_after_success(self):
        """Resets failure count on successful poll."""
        wd = OllamaWatchdog()
        with patch("opencohost.core.health_monitor.requests.get", side_effect=Exception("fail")):
            for _ in range(2):
                wd.poll()
        assert wd.consecutive_failures == 2

        # Now succeed
        with patch("opencohost.core.health_monitor.requests.get") as mock_get:
            mock_get.return_value = MagicMock(status_code=200)
            wd.poll()
        assert wd.status == "healthy"
        assert wd.consecutive_failures == 0

    def test_timeout_does_not_crash(self):
        """Timeout exception doesn't crash the watchdog."""
        wd = OllamaWatchdog()
        import requests
        with patch("opencohost.core.health_monitor.requests.get", side_effect=requests.exceptions.Timeout):
            wd.poll()  # Should not raise

    def test_startup_failures_wait_before_degraded(self):
        """Transient Ollama startup failures are waiting, not immediate down."""
        wd = OllamaWatchdog()
        with patch("opencohost.core.health_monitor.requests.get", side_effect=Exception("connection refused")):
            wd.poll()

        assert wd.status == "unknown"
        assert wd.lifecycle_state == "waiting"
        assert "Waiting" in wd.message

    def test_exhausted_startup_retries_become_degraded(self):
        """After retry threshold, Ollama exposes actionable degraded state."""
        wd = OllamaWatchdog()
        with patch("opencohost.core.health_monitor.requests.get", side_effect=Exception("connection refused")):
            for _ in range(4):
                wd.poll()

        assert wd.status == "down"
        assert wd.lifecycle_state == "degraded"


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

    def test_windows_start_uses_new_process_group_for_ctrl_break(self):
        """Windows managed Qwen must be in its own group for CTRL_BREAK_EVENT."""
        from pathlib import Path

        mgr = QwenProcessManager()

        with patch("opencohost.core.health_monitor.resolve_xtts_python", return_value=Path("/fake/python")):
            with patch("opencohost.core.health_monitor.sys.platform", "win32"):
                with patch("opencohost.core.health_monitor.subprocess.Popen") as mock_popen:
                    with patch.object(mgr, "_check_health", return_value=True):
                        mock_popen.return_value.poll.return_value = None

                        assert mgr.start() is True

        flags = mock_popen.call_args.kwargs["creationflags"]
        assert flags & subprocess.CREATE_NEW_PROCESS_GROUP

    def test_start_redirects_qwen_output_to_dedicated_logs(self, tmp_path):
        """Qwen startup tracebacks must be preserved in logs, not swallowed."""
        from pathlib import Path

        mgr = QwenProcessManager()

        with patch("opencohost.core.health_monitor.resolve_xtts_python", return_value=Path("/fake/python")):
            with patch("opencohost.core.health_monitor.LOG_DIR", str(tmp_path)):
                with patch("opencohost.core.health_monitor.subprocess.Popen") as mock_popen:
                    with patch.object(mgr, "_check_health", return_value=True):
                        mock_popen.return_value.poll.return_value = None

                        assert mgr.start() is True

        kwargs = mock_popen.call_args.kwargs
        assert kwargs["stdout"] is not subprocess.DEVNULL
        assert kwargs["stderr"] is not subprocess.DEVNULL
        assert kwargs["stdout"].name == str(tmp_path / "server_qwen_stdout.log")
        assert kwargs["stderr"].name == str(tmp_path / "server_qwen_stderr.log")

        mgr._close_subprocess_logs()

    def test_start_closes_stale_qwen_log_handles_before_reopening(self, tmp_path):
        """Restart attempts must not leak old Qwen subprocess log handles."""
        from pathlib import Path

        mgr = QwenProcessManager()
        stale_stdout = MagicMock()
        stale_stderr = MagicMock()
        mgr._stdout_log = stale_stdout
        mgr._stderr_log = stale_stderr

        with patch("opencohost.core.health_monitor.resolve_xtts_python", return_value=Path("/fake/python")):
            with patch("opencohost.core.health_monitor.LOG_DIR", str(tmp_path)):
                with patch("opencohost.core.health_monitor.subprocess.Popen") as mock_popen:
                    with patch.object(mgr, "_check_health", return_value=True):
                        mock_popen.return_value.poll.return_value = None

                        assert mgr.start() is True

        stale_stdout.close.assert_called_once()
        stale_stderr.close.assert_called_once()
        assert mgr._stdout_log is not stale_stdout
        assert mgr._stderr_log is not stale_stderr

        mgr._close_subprocess_logs()

    def test_stop_kills_when_graceful_signal_fails(self):
        """A failed CTRL_BREAK_EVENT must fall back to kill() to release VRAM."""
        mgr = QwenProcessManager()
        proc = MagicMock()
        proc.poll.return_value = None
        proc.send_signal.side_effect = RuntimeError("signal failed")
        mgr._process = proc

        with patch("opencohost.core.health_monitor.sys.platform", "win32"):
            mgr.stop()

        proc.kill.assert_called_once()
        proc.wait.assert_called_once_with(timeout=2.0)

    def test_stop_never_kills_external_manual_server(self):
        """External/manual Qwen servers are protected during shutdown."""
        mgr = QwenProcessManager()
        proc = MagicMock()
        mgr._process = proc
        mgr._is_manual = True

        mgr.stop()

        proc.kill.assert_not_called()
        proc.terminate.assert_not_called()
        proc.send_signal.assert_not_called()
        assert mgr.ownership == "external"

    def test_stop_timeout_returns_after_short_wait_and_kills_owned_process(self):
        """Owned Qwen shutdown is bounded and may kill only owned child process."""
        mgr = QwenProcessManager()
        proc = MagicMock()
        proc.poll.return_value = None
        proc.wait.side_effect = [subprocess.TimeoutExpired("qwen", 0.1), None]
        mgr._process = proc

        mgr.stop(graceful_timeout=0.1, kill_timeout=0.2)

        proc.kill.assert_called_once()
        assert proc.wait.call_args_list[-1].kwargs == {"timeout": 0.2}

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
        assert mgr.ownership == "external"

    def test_check_health_returns_false_on_error(self):
        """_check_health returns False on connection error."""
        mgr = QwenProcessManager()
        with patch("opencohost.core.health_monitor.requests.get", side_effect=Exception("fail")):
            assert mgr._check_health() is False

    def test_check_health_returns_true_on_200(self):
        """_check_health returns True on 200 response."""
        mgr = QwenProcessManager()
        with patch("opencohost.core.health_monitor.requests.get") as mock_get:
            response = MagicMock(status_code=200)
            response.json.side_effect = ValueError("not json")
            mock_get.return_value = response
            assert mgr._check_health() is True

    def test_check_health_rejects_wrong_app_identifier(self):
        """_check_health rejects healthy JSON from a different app on port 5000."""
        mgr = QwenProcessManager()
        with patch("opencohost.core.health_monitor.requests.get") as mock_get:
            response = MagicMock(status_code=200)
            response.json.return_value = {
                "app": "some-other-service",
                "status": "ok",
                "model_loaded": True,
            }
            mock_get.return_value = response

            assert mgr._check_health() is False

    def test_check_health_accepts_correct_app_identifier(self):
        """_check_health accepts VoiceAI Qwen health JSON with the expected app id."""
        mgr = QwenProcessManager()
        with patch("opencohost.core.health_monitor.requests.get") as mock_get:
            response = MagicMock(status_code=200)
            response.json.return_value = {
                "app": QwenProcessManager.APP_ID,
                "status": "ok",
                "model_loaded": True,
            }
            mock_get.return_value = response

            assert mgr._check_health() is True

    def test_check_health_requires_loaded_model_when_json_available(self):
        """_check_health rejects alive Qwen server when model is not loaded."""
        mgr = QwenProcessManager()
        with patch("opencohost.core.health_monitor.requests.get") as mock_get:
            response = MagicMock(status_code=200)
            response.json.return_value = {"status": "error", "model_loaded": False}
            mock_get.return_value = response

            assert mgr._check_health() is False

    def test_check_health_accepts_loaded_model_without_status_for_compat(self):
        """Manual compatible servers may only expose model_loaded in JSON."""
        mgr = QwenProcessManager()
        with patch("opencohost.core.health_monitor.requests.get") as mock_get:
            response = MagicMock(status_code=200)
            response.json.return_value = {"model_loaded": True}
            mock_get.return_value = response

            assert mgr._check_health() is True

    def test_check_health_returns_false_on_503_model_not_loaded(self):
        """_check_health rejects Qwen /health 503 even if the port responds."""
        mgr = QwenProcessManager()
        with patch("opencohost.core.health_monitor.requests.get") as mock_get:
            response = MagicMock(status_code=503)
            response.json.return_value = {"status": "error", "model_loaded": False}
            mock_get.return_value = response

            assert mgr._check_health() is False

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
        assert state.ollama_lifecycle == "starting"
        assert state.qwen_lifecycle == "starting"

    def test_state_is_snapshot_not_reference(self):
        """state returns a copy, not a reference."""
        monitor = self._make_monitor()
        s1 = monitor.state
        s2 = monitor.state
        assert s1 is not s2

    def test_run_logs_poll_exception_and_continues(self, caplog):
        """Unexpected poll exceptions are logged and do not kill the loop."""
        monitor = self._make_monitor()
        poll_calls = 0

        def poll_once_then_stop():
            nonlocal poll_calls
            poll_calls += 1
            if poll_calls == 1:
                raise RuntimeError("boom")
            monitor._stop_event.set()

        monitor._poll_all = MagicMock(side_effect=poll_once_then_stop)

        with caplog.at_level("ERROR", logger="HealthMonitor"):
            with patch("opencohost.core.health_monitor.HEALTH_POLL_INTERVAL", 0.01):
                monitor.start()
                monitor.join(timeout=1)

        assert monitor.is_alive() is False
        assert monitor._poll_all.call_count == 2
        assert "HealthMonitor: polling cycle failed" in caplog.text
        assert any(record.exc_info for record in caplog.records)

    def test_run_marks_state_unknown_after_poll_exception(self):
        """A failed poll cycle does not leave a stale green snapshot exposed."""
        monitor = self._make_monitor()
        poll_calls = 0
        with monitor._lock:
            monitor._state = MonitorState(
                vram_status="normal",
                rtf_status="normal",
                ollama_status="healthy",
                qwen_status="healthy",
                overall_status="green",
                free_vram_mb=5000.0,
                last_updated=1.0,
            )

        def poll_once_then_stop():
            nonlocal poll_calls
            poll_calls += 1
            if poll_calls == 1:
                raise RuntimeError("boom")
            monitor._stop_event.set()

        monitor._poll_all = MagicMock(side_effect=poll_once_then_stop)

        with patch("opencohost.core.health_monitor.HEALTH_POLL_INTERVAL", 0.01):
            monitor.start()
            monitor.join(timeout=1)

        state = monitor.state
        assert state.overall_status == "unknown"
        assert state.vram_status == "unknown"
        assert state.ollama_status == "unknown"
        assert state.qwen_status == "unknown"
        assert state.last_updated > 1.0

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

    def test_heavy_tts_block_reason_reports_vram_threshold(self):
        """Fallback logs must expose the exact VRAM gate that blocked Qwen."""
        monitor = self._make_monitor()
        monitor._vram._status = "low"
        monitor._vram._free_mb = 1500.0
        monitor._ollama._status = "healthy"
        monitor._poll_all()

        reason = monitor.heavy_tts_block_reason(auto_fallback_enabled=True, manual_motor="pesado")

        assert reason == "vram_low free=1500MB required>=2048MB"

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

    def test_heavy_tts_block_reason_none_when_healthy(self):
        """No fallback reason is reported when Qwen is allowed."""
        monitor = self._make_monitor()
        monitor._vram._status = "normal"
        monitor._vram._free_mb = 5000.0
        monitor._ollama._status = "healthy"
        monitor._poll_all()

        assert monitor.heavy_tts_block_reason(auto_fallback_enabled=True, manual_motor="pesado") is None

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

    def test_qwen_alive_but_unhealthy_is_not_green(self):
        """Alive Qwen process without loaded model must not publish fake green."""
        monitor = self._make_monitor()
        monitor._vram._status = "normal"
        monitor._vram._free_mb = 5000.0
        monitor._ollama._status = "healthy"
        monitor._qwen.is_running = True
        monitor._qwen.is_manual = False
        monitor._qwen.is_healthy = False

        monitor._poll_all()

        state = monitor.state
        assert state.qwen_status == "unhealthy"
        assert state.overall_status == "red"
        assert monitor.should_use_heavy_tts(auto_fallback_enabled=True, manual_motor="pesado") is False

    def test_qwen_never_started_status_is_unavailable_not_unknown(self):
        """Owner decision: a Qwen that never started (Piper/Edge session, no
        process spawned, no manual attach) must report 'unavailable', not the
        old 'unknown' fallback — never-in-use Qwen is tolerable, not a failure.
        """
        monitor = self._make_monitor()
        monitor._vram._status = "normal"
        monitor._vram._free_mb = 5000.0
        monitor._ollama._status = "healthy"
        monitor._qwen.is_running = False
        monitor._qwen.is_manual = False
        monitor._qwen.is_healthy = False
        monitor._qwen.ownership = "none"

        monitor._poll_all()

        state = monitor.state
        assert state.qwen_status == "unavailable"
        assert state.overall_status == "green"

    def test_qwen_owned_and_died_status_is_unhealthy_and_red(self):
        """A Qwen WE spawned (ownership 'owned') that died without a manual
        stop() is a real failure and must keep overall red — distinct from
        the never-started case above.
        """
        monitor = self._make_monitor()
        monitor._vram._status = "normal"
        monitor._vram._free_mb = 5000.0
        monitor._ollama._status = "healthy"
        monitor._qwen.is_running = False
        monitor._qwen.is_manual = False
        monitor._qwen.is_healthy = False
        monitor._qwen.ownership = "owned"

        monitor._poll_all()

        state = monitor.state
        assert state.qwen_status == "unhealthy"
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


# ──────────────────────────────────────────────
# Phase 0 — Qwen TTS lifecycle foundations (T0.2, T0.4)
# ──────────────────────────────────────────────

def test_lifecycle_and_keepwarm_constants():
    """T0.2 — keep-warm / blip constants and the new STOPPED lifecycle state exist."""
    from opencohost.config.settings import (
        QWEN_BLIP_BACKOFF,
        QWEN_KEEP_WARM_SECONDS,
        QWEN_VRAM_FOOTPRINT_MB,
    )
    from opencohost.core.health_monitor import LIFECYCLE_STOPPED
    assert QWEN_KEEP_WARM_SECONDS == 30
    # blip backoff + VRAM footprint are owner-pending values — pin existence/type, not the number
    assert isinstance(QWEN_BLIP_BACKOFF, (int, float)) and QWEN_BLIP_BACKOFF > 0
    assert isinstance(QWEN_VRAM_FOOTPRINT_MB, (int, float)) and QWEN_VRAM_FOOTPRINT_MB > 0
    assert LIFECYCLE_STOPPED == "stopped"


def test_stop_resets_idle_and_lifecycle():
    """T0.4 — stopping an OWNED server resets the idle clock and marks it STOPPED."""
    from opencohost.core.health_monitor import LIFECYCLE_READY, LIFECYCLE_STOPPED
    mgr = QwenProcessManager()
    mgr._is_manual = False
    proc = MagicMock()
    proc.poll.return_value = 0  # already exited -> stop() takes the fast path
    mgr._process = proc
    mgr._last_health_time = time.time() - 1000
    mgr._lifecycle_state = LIFECYCLE_READY

    mgr.stop()

    assert mgr.idle_seconds == 0.0
    assert mgr.lifecycle_state == LIFECYCLE_STOPPED


def test_stop_manual_does_not_reset():
    """T0.4 — a manual/external server is never auto-stopped, so its state is untouched."""
    from opencohost.core.health_monitor import LIFECYCLE_READY
    mgr = QwenProcessManager()
    mgr._is_manual = True
    mgr._lifecycle_state = LIFECYCLE_READY
    mgr._last_health_time = time.time() - 50

    mgr.stop()

    assert mgr.lifecycle_state == LIFECYCLE_READY  # no reset on the manual path


def test_stop_preserves_failed_verdict():
    """T0.4 — stop() cleaning up a FAILED startup must NOT mask the failure as STOPPED.

    start() sets FAILED on a startup timeout and then calls stop(); the idle-clock reset
    must not overwrite that failure verdict (the health pill / failure classifier rely on it).
    """
    from opencohost.core.health_monitor import LIFECYCLE_FAILED
    mgr = QwenProcessManager()
    mgr._is_manual = False
    proc = MagicMock()
    proc.poll.return_value = 0  # already exited
    mgr._process = proc
    mgr._lifecycle_state = LIFECYCLE_FAILED

    mgr.stop()

    assert mgr.lifecycle_state == LIFECYCLE_FAILED  # failure verdict preserved
    assert mgr.idle_seconds == 0.0
