"""
Packaging Task 1: edge_tts is an optional dependency.

Tests verify that:
1. When edge_tts module is None (package absent), MotorVocalIA sets
   _edge_tts_offline=True at construction so synthesis routes to Piper
   without any network attempt.
2. Normal behavior is unchanged when edge_tts is present (the module-level
   import succeeds and the latch starts False).
"""
from __future__ import annotations

import os
import queue
import sys

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
    return motor, log_q, ui_events


# ---------------------------------------------------------------------------
# Task 1a — edge_tts absent → latch is True at construction
# ---------------------------------------------------------------------------

class TestEdgeTtsAbsentSetsOfflineLatch:
    def test_none_module_sets_latch_true(self, monkeypatch):
        """Monkeypatching edge_tts to None must set _edge_tts_offline=True."""
        import opencohost.core.llm_engine as eng

        # Simulate the package being absent by setting the module-level name to None
        monkeypatch.setattr(eng, "edge_tts", None, raising=False)

        motor, *_ = _make_motor()

        assert motor._edge_tts_offline is True, (
            "When edge_tts is None, _edge_tts_offline must be True so synthesis "
            "routes straight to Piper."
        )

    def test_none_module_does_not_raise_on_construction(self, monkeypatch):
        """Constructing MotorVocalIA with edge_tts=None must not raise."""
        import opencohost.core.llm_engine as eng

        monkeypatch.setattr(eng, "edge_tts", None, raising=False)

        try:
            motor, *_ = _make_motor()
        except Exception as exc:
            pytest.fail(f"MotorVocalIA.__init__ raised with edge_tts=None: {exc}")

    def test_none_module_logs_not_installed(self, monkeypatch, caplog):
        """A log message mentioning Edge-TTS not installed must appear in the log."""
        import logging
        import opencohost.core.llm_engine as eng

        monkeypatch.setattr(eng, "edge_tts", None, raising=False)

        with caplog.at_level(logging.INFO):
            motor, *_ = _make_motor()

        text = caplog.text.lower()
        assert "edge" in text or "not installed" in text, (
            f"Expected a log record about Edge-TTS not being installed. Got: {caplog.text!r}"
        )


# ---------------------------------------------------------------------------
# Task 1b — edge_tts present → latch stays False (normal behavior unchanged)
# ---------------------------------------------------------------------------

class TestEdgeTtsPresentKeepsNormalBehavior:
    def test_latch_is_false_when_edge_tts_available(self):
        """When edge_tts module is importable, _edge_tts_offline starts False."""
        import opencohost.core.llm_engine as eng

        # Only run this test if edge_tts is actually importable
        if eng.edge_tts is None:
            pytest.skip("edge_tts not installed in this environment")

        motor, *_ = _make_motor()

        assert motor._edge_tts_offline is False, (
            "When edge_tts is present, the offline latch must start False."
        )
