"""Tests for seamless historial continuity across a model switch (D1/D2/D3,
track model_switch_memory_continuity_20260723).

Owner-chosen behavior: a model switch must feel invisible — `historial`
carries over verbatim to the new model, on both the success path
(`switch_model`) and the implicit-activation path (`download_model`), and a
FAILED switch must never lose history either (the old unconditional
`.clear()` ran before the new model was even confirmed to load).
"""

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers — mirrors _make_motor() from test_pipeline_memory.py
# ---------------------------------------------------------------------------

def _make_motor():
    """Construct a MotorVocalIA with all heavy I/O mocked out.

    Mirrors the pattern in test_heavy_model_inference_recovery.py /
    test_pipeline_memory.py: create via constructor (no run()), then assign
    mock dependencies.
    """
    import queue
    from opencohost.core.llm_engine import MotorVocalIA

    log_q = queue.Queue()
    ui_events = []

    def ui_cb(event):
        ui_events.append(event)

    motor = MotorVocalIA(log_q, ui_cb)
    motor.ollama = MagicMock()
    motor.pygame = MagicMock()
    motor.is_ready = True
    return motor, log_q, ui_events


def _seed_historial(motor):
    """Append one user/assistant pair, mirroring _commit_history's entry shape."""
    motor.historial.append({
        'role': 'user', 'content': 'hola, ¿cómo va el stream?',
        'source': 'direct', 'private': False,
    })
    motor.historial.append({
        'role': 'assistant', 'content': 'todo joya, seguimos con el juego',
        'source': 'direct', 'private': False,
    })


class TestModelSwitchHistorialContinuity:
    """D1/D2: historial carries over verbatim across every model-change path."""

    def test_historial_carries_over_switch_model(self):
        motor, _, _ = _make_motor()
        _seed_historial(motor)
        historial_before = list(motor.historial)

        with (
            patch.object(motor, "_prepare_model", return_value=True),
            patch("opencohost.core.llm_engine.save_last_model"),
        ):
            motor._dispatch_command("switch_model", "otro-modelo:latest")

        assert list(motor.historial) == historial_before

    def test_historial_intact_after_failed_switch(self):
        """Q4: a failed switch must never lose history — the exception path
        already restores current_model/_desired_model; historial must be
        untouched too (nothing clears it anymore)."""
        motor, _, _ = _make_motor()
        _seed_historial(motor)
        historial_before = list(motor.historial)
        previous_model = motor.current_model
        previous_desired = motor._desired_model

        with patch.object(motor, "_prepare_model", return_value=False):
            result = motor._apply_model_switch("modelo-inexistente:latest")

        assert result is False
        assert motor.current_model == previous_model
        assert motor._desired_model == previous_desired
        assert list(motor.historial) == historial_before

    def test_historial_carries_over_download_activate(self):
        """D2: the download-then-activate path (_download_model_worker) must
        preserve historial too — same 'model changed mid-session' event
        family as switch_model."""
        motor, _, _ = _make_motor()
        _seed_historial(motor)
        historial_before = list(motor.historial)

        fake_progress = MagicMock()
        fake_progress.status = "success"
        fake_progress.total = None
        fake_progress.completed = None
        motor.ollama.pull.return_value = iter([fake_progress])

        with patch("opencohost.core.llm_engine.save_last_model"):
            motor._download_model_worker("nuevo-modelo:latest")

        assert list(motor.historial) == historial_before
