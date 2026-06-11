"""Integration tests for health monitor auto-fallback.

Tests the full flow: HealthMonitor + MotorVocalIA fallback + SmartAggregatorUI
health gate + UIState propagation.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from opencohost.core.health_monitor import HealthMonitor, MonitorState
from opencohost.ui.state import UIState


# ──────────────────────────────────────────────
# Integration: MotorVocalIA + HealthMonitor fallback
# ──────────────────────────────────────────────

class TestMotorVocalIAHealthFallback:
    """Integration tests for TTS auto-fallback with health monitor."""

    def test_health_monitor_none_backward_compat(self):
        """When health_monitor is None, original behavior is unchanged."""
        from opencohost.core.llm_engine import MotorVocalIA

        log_queue = MagicMock()
        callback = MagicMock()
        motor = MotorVocalIA(log_queue, callback)

        assert motor.health_monitor is None
        # should_use_heavy_tts is called on the health_monitor, not the motor
        # When health_monitor is None, the fallback check is skipped

    def test_health_monitor_wired_to_motor(self):
        """HealthMonitor can be wired to MotorVocalIA."""
        from opencohost.core.llm_engine import MotorVocalIA

        log_queue = MagicMock()
        callback = MagicMock()
        motor = MotorVocalIA(log_queue, callback)

        monitor = HealthMonitor()
        motor.health_monitor = monitor

        assert motor.health_monitor is monitor

    def test_fallback_triggered_on_vram_low(self):
        """should_use_heavy_tts returns False when VRAM is low."""
        monitor = HealthMonitor()
        monitor._vram.poll = MagicMock()  # prevent poll() from resetting status
        monitor._vram._status = "low"
        monitor._vram._free_mb = 1500.0
        monitor._ollama.poll = MagicMock()
        monitor._ollama._status = "healthy"
        monitor._poll_all()

        result = monitor.should_use_heavy_tts(auto_fallback_enabled=True, manual_motor="pesado")
        assert result is False

    def test_fallback_not_triggered_when_healthy(self):
        """should_use_heavy_tts returns True when all healthy."""
        monitor = HealthMonitor()
        monitor._vram.poll = MagicMock()
        monitor._vram._status = "normal"
        monitor._vram._free_mb = 5000.0
        monitor._ollama.poll = MagicMock()
        monitor._ollama._status = "healthy"
        # Mock qwen to avoid real process checks
        monitor._qwen = MagicMock()
        monitor._qwen.is_running = True
        monitor._qwen.is_manual = False
        monitor._poll_all()

        result = monitor.should_use_heavy_tts(auto_fallback_enabled=True, manual_motor="pesado")
        assert result is True


# ──────────────────────────────────────────────
# Integration: SmartAggregatorUI + health gate
# ──────────────────────────────────────────────

class TestSmartAggregatorUIHealthGate:
    """Integration tests for Vibe health gate."""

    def _make_mock_ui(self, health_monitor=None):
        """Create a SmartAggregatorUI with mocked dependencies."""
        from opencohost.ui.smart_aggregator_ui import SmartAggregatorUI
        from opencohost.ui.protocols import CallbackDispatcher

        ui_state = UIState()
        dispatcher = CallbackDispatcher(source="test")
        smart_agg = MagicMock()
        motor_ia = MagicMock()
        motor_ia.is_processing = False
        motor_ia.is_speaking = False
        motor_ia.current_model = "llama3"

        return SmartAggregatorUI(
            ui_state=ui_state,
            dispatcher=dispatcher,
            smart_agg=smart_agg,
            motor_ia=motor_ia,
            health_monitor=health_monitor,
        )

    def test_no_health_monitor_allows_calls(self):
        """When health_monitor is None, llm_interface works unchanged."""
        ui = self._make_mock_ui(health_monitor=None)

        with patch("ollama.chat") as mock_chat:
            mock_chat.return_value = {"message": {"content": "test response"}}
            # This should not raise
            try:
                ui.llm_interface("test prompt")
            except RuntimeError:
                pytest.fail("llm_interface raised RuntimeError without health monitor")

    def test_vram_low_raises_runtime_error(self):
        """VRAM low/critical raises RuntimeError with appropriate message."""
        monitor = HealthMonitor()
        monitor._vram.poll = MagicMock()
        monitor._vram._status = "low"
        monitor._vram._free_mb = 1500.0
        monitor._ollama.poll = MagicMock()
        monitor._ollama._status = "healthy"
        monitor._poll_all()

        ui = self._make_mock_ui(health_monitor=monitor)

        with pytest.raises(RuntimeError, match="Vibe paused: low VRAM"):
            ui.llm_interface("test prompt")

    def test_ollama_down_raises_runtime_error(self):
        """Ollama down raises RuntimeError with appropriate message."""
        monitor = HealthMonitor()
        monitor._vram.poll = MagicMock()
        monitor._vram._status = "normal"
        monitor._vram._free_mb = 5000.0
        monitor._ollama.poll = MagicMock()
        monitor._ollama._status = "down"
        monitor._poll_all()

        ui = self._make_mock_ui(health_monitor=monitor)

        with pytest.raises(RuntimeError, match="Vibe unavailable: Ollama not running"):
            ui.llm_interface("test prompt")

    def test_both_degraded_vram_takes_priority(self):
        """When both VRAM and Ollama are degraded, VRAM reason takes priority."""
        monitor = HealthMonitor()
        monitor._vram.poll = MagicMock()
        monitor._vram._status = "low"
        monitor._vram._free_mb = 1500.0
        monitor._ollama.poll = MagicMock()
        monitor._ollama._status = "down"
        monitor._poll_all()

        ui = self._make_mock_ui(health_monitor=monitor)

        with pytest.raises(RuntimeError, match="Vibe paused: low VRAM"):
            ui.llm_interface("test prompt")

    def test_healthy_allows_vibe_calls(self):
        """When all healthy, Vibe calls proceed normally."""
        monitor = HealthMonitor()
        monitor._vram.poll = MagicMock()
        monitor._vram._status = "normal"
        monitor._vram._free_mb = 5000.0
        monitor._ollama.poll = MagicMock()
        monitor._ollama._status = "healthy"
        monitor._poll_all()

        ui = self._make_mock_ui(health_monitor=monitor)

        # Configure the mock ollama response chain properly
        ui._motor_ia.ollama.chat.return_value = {
            "message": {"content": "test response"}
        }
        result = ui.llm_interface("test prompt")
        assert result == "test response"


# ──────────────────────────────────────────────
# Integration: UIState health_status propagation
# ──────────────────────────────────────────────

class TestUIStateHealthPropagation:
    """Integration tests for health_status propagation through UIState."""

    def test_health_status_default_unknown(self):
        """Default health_status is 'unknown'."""
        state = UIState()
        assert state.health_status == "unknown"

    def test_health_status_set_and_get(self):
        """health_status can be set and retrieved."""
        state = UIState()
        state.health_status = "green"
        assert state.health_status == "green"

    def test_health_status_validation(self):
        """Invalid health_status raises ValueError."""
        state = UIState()
        with pytest.raises(ValueError, match="Invalid health_status"):
            state.health_status = "invalid"

    def test_health_status_observer_notification(self):
        """Setting health_status notifies observers."""
        state = UIState()
        notifications = []

        def observer(key, value):
            if key == "health_status":
                notifications.append(value)

        state.subscribe(observer)
        state.health_status = "green"

        # Allow dispatch thread to process
        time = __import__("time")
        time.sleep(0.1)

        assert "green" in notifications

    def test_health_status_valid_values(self):
        """All valid health status values work."""
        from opencohost.ui.state import VALID_HEALTH_STATUSES

        state = UIState()
        for val in VALID_HEALTH_STATUSES:
            state.health_status = val
            assert state.health_status == val


# ──────────────────────────────────────────────
# End-to-end scenarios
# ──────────────────────────────────────────────

class TestEndToEndScenarios:
    """End-to-end scenario tests."""

    def test_simulate_vram_degradation_fallback(self):
        """Simulate VRAM degradation → verify fallback decision."""
        monitor = HealthMonitor()
        monitor._vram.poll = MagicMock()
        monitor._ollama.poll = MagicMock()
        monitor._ollama._status = "healthy"
        # Mock qwen to avoid real process checks
        monitor._qwen = MagicMock()
        monitor._qwen.is_running = True
        monitor._qwen.is_manual = False

        # Start healthy
        monitor._vram._status = "normal"
        monitor._vram._free_mb = 5000.0
        monitor._poll_all()
        assert monitor.should_use_heavy_tts(True, "pesado") is True

        # Simulate degradation
        monitor._vram._status = "low"
        monitor._vram._free_mb = 1500.0
        monitor._poll_all()
        assert monitor.should_use_heavy_tts(True, "pesado") is False

        # Simulate critical
        monitor._vram._status = "critical"
        monitor._vram._free_mb = 500.0
        monitor._poll_all()
        assert monitor.should_use_heavy_tts(True, "pesado") is False

    def test_simulate_ollama_down_vibe_blocked(self):
        """Simulate Ollama down → verify Vibe blocked."""
        monitor = HealthMonitor()
        monitor._vram.poll = MagicMock()
        monitor._vram._status = "normal"
        monitor._vram._free_mb = 5000.0
        monitor._ollama.poll = MagicMock()

        # Ollama healthy
        monitor._ollama._status = "healthy"
        monitor._poll_all()
        assert monitor.can_vibe_call() is True

        # Ollama down
        monitor._ollama._status = "down"
        monitor._poll_all()
        assert monitor.can_vibe_call() is False

    def test_rtf_measurement_affects_status(self):
        """RTF measurements affect overall status."""
        monitor = HealthMonitor()
        monitor._vram.poll = MagicMock()
        monitor._vram._status = "normal"
        monitor._vram._free_mb = 5000.0
        monitor._ollama.poll = MagicMock()
        monitor._ollama._status = "healthy"
        # Mock qwen to avoid real process checks
        monitor._qwen = MagicMock()
        monitor._qwen.is_running = True
        monitor._qwen.is_manual = False

        # Normal RTF
        monitor._rtf.record(0.5, 1.0)  # RTF = 0.5
        monitor._poll_all()
        assert monitor.state.overall_status == "green"

        # High RTF
        monitor._rtf._measurements.clear()
        monitor._rtf.record(5.0, 1.0)  # RTF = 5.0
        monitor._poll_all()
        assert monitor.state.overall_status == "yellow"
