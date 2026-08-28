"""
tests/test_health_integration.py

Integration tests for health monitor auto-fallback and MotorVocalIA integration.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

from opencohost.core.observability.health_monitor import HealthMonitor, MonitorState


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
# End-to-end scenarios
# ──────────────────────────────────────────────

class TestEndToEndScenarios:
    """End-to-end scenario tests for HealthMonitor."""

    def test_simulate_vram_degradation_fallback(self):
        """Simulate VRAM degradation → verify fallback decision."""
        monitor = HealthMonitor()
        monitor._vram.poll = MagicMock()
        monitor._ollama.poll = MagicMock()
        monitor._ollama._status = "healthy"
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
