"""Fix A — stale ``model_status='error'`` cleared after a rolled-back switch.

Regression target: ``status_bar_stale_state_20260617`` design, Fix A.

When a model switch fails but the motor rolled back to a working model
(``motor_ia.current_model`` is set), the transient ``"error"`` pill must be
cleared back to ``"ready"`` so it does not stick for the whole session. When
the switch fully failed (no rollback, ``current_model`` falsy), the error
state must remain so the operator keeps seeing it.

The reset is scheduled via ``_safe_after(func, delay_ms)`` — these tests run
it synchronously by stubbing ``_safe_after`` with the real ``(func, delay_ms)``
signature.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from opencohost.ui.app_shell import VocalAIApp
from opencohost.ui.state import UIState


def _make_app(current_model):
    """Build a VocalAIApp shell with only what _on_motor_switch_failed touches."""
    app = VocalAIApp.__new__(VocalAIApp)
    app._ui_state = UIState()
    # Real signature is _safe_after(func, delay_ms=0); run synchronously.
    app._safe_after = lambda func, delay_ms=0: func()
    app._print_log = lambda *a, **k: None
    motor = MagicMock()
    motor.current_model = current_model
    motor._last_switch_failure = None
    app.motor_ia = motor
    app.model_panel = MagicMock()
    return app


class TestStaleModelStatusReset:
    def test_model_status_cleared_when_rollback_succeeded(self):
        app = _make_app(current_model="gemma4:e2b")
        try:
            app._on_motor_switch_failed()
            assert app._ui_state.model_status == "ready"
        finally:
            app._ui_state.shutdown(timeout=2.0)

    def test_model_status_stays_error_without_current_model(self):
        app = _make_app(current_model=None)
        try:
            app._on_motor_switch_failed()
            assert app._ui_state.model_status == "error"
        finally:
            app._ui_state.shutdown(timeout=2.0)

    def test_model_status_stays_error_on_empty_current_model(self):
        # An empty string is falsy — no working model to fall back on.
        app = _make_app(current_model="")
        try:
            app._on_motor_switch_failed()
            assert app._ui_state.model_status == "error"
        finally:
            app._ui_state.shutdown(timeout=2.0)
