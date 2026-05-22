"""Focused tests for app shell OBS reconnect resilience."""

from __future__ import annotations

import sys
import importlib
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _import_app_shell_with_ui_deps_mocked():
    class DummyWidget:
        pass

    class DummyCustomTkinter(SimpleNamespace):
        def __getattr__(self, _name):
            return DummyWidget

    modules = {
        "customtkinter": DummyCustomTkinter(CTk=DummyWidget, CTkToplevel=DummyWidget),
        "numpy": MagicMock(),
        "sounddevice": MagicMock(),
        "soundfile": MagicMock(),
        "pynput": MagicMock(),
        "pynput.keyboard": MagicMock(),
        "pynput.mouse": MagicMock(),
    }
    old_module = sys.modules.pop("ui.app_shell", None)
    with patch.dict(sys.modules, modules):
        module = importlib.import_module("ui.app_shell")

    return module, old_module


def _restore_app_shell_module(old_module) -> None:
    if old_module is not None:
        sys.modules["ui.app_shell"] = old_module
        return
    sys.modules.pop("ui.app_shell", None)
    ui_module = sys.modules.get("ui")
    if ui_module is not None and hasattr(ui_module, "app_shell"):
        delattr(ui_module, "app_shell")


def test_obs_reconnect_loop_logs_exception_and_keeps_retrying():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        obs_client = MagicMock()
        obs_client.connect.side_effect = [RuntimeError("socket exploded"), True]
        app._obs_client = obs_client
        app._avatar_bridge = MagicMock()
        app._avatar_bridge.get_state.return_value = "idle"
        app._avatar_panel = MagicMock()
        app._print_log = MagicMock()
        app.winfo_exists = MagicMock(return_value=False)
        app.after = MagicMock()

        with patch.object(app_shell.time, "sleep") as mock_sleep:
            with patch.object(app_shell.logger, "exception") as mock_exception:
                app._connect_obs_loop(retry_delay=0.01)

        assert obs_client.connect.call_count == 2
        mock_exception.assert_called_once_with("Fallo inesperado en loop de OBS")
        mock_sleep.assert_called_once_with(0.01)
        obs_client.subscribe_bridge.assert_called_once_with(app._avatar_bridge)
        obs_client.on_state_change.assert_called_once_with("idle")
    finally:
        _restore_app_shell_module(old_module)


class _TrackingLock:
    def __init__(self):
        self.locked = False
        self.enter_count = 0

    def __enter__(self):
        self.locked = True
        self.enter_count += 1

    def __exit__(self, exc_type, exc, tb):
        self.locked = False


class _GuardedSeenIds:
    def __init__(self, values, lock):
        self._values = set(values)
        self._lock = lock

    def _assert_locked(self):
        assert self._lock.locked, "_seen_chat_ids accessed without _chat_lock"

    def __contains__(self, value):
        self._assert_locked()
        return value in self._values

    def add(self, value):
        self._assert_locked()
        self._values.add(value)

    def __len__(self):
        self._assert_locked()
        return len(self._values)

    def __iter__(self):
        self._assert_locked()
        return iter(self._values)


def test_authenticated_chat_duplicate_gate_uses_stream_admin_lock():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        lock = _TrackingLock()
        stream_admin_ui = SimpleNamespace(
            _chat_lock=lock,
            _seen_chat_ids=_GuardedSeenIds({"already-seen"}, lock),
        )

        assert app_shell._stream_admin_should_process_chat_message(stream_admin_ui, "already-seen") is False
        assert app_shell._stream_admin_should_process_chat_message(stream_admin_ui, "new-id") is True
        assert lock.enter_count == 2
    finally:
        _restore_app_shell_module(old_module)


def test_authenticated_chat_duplicate_gate_preserves_missing_id_and_truncation_behavior():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        stream_admin_ui = SimpleNamespace(
            _chat_lock=threading.Lock(),
            _seen_chat_ids={str(i) for i in range(2000)},
        )

        assert app_shell._stream_admin_should_process_chat_message(stream_admin_ui, None) is True
        assert len(stream_admin_ui._seen_chat_ids) == 2000

        assert app_shell._stream_admin_should_process_chat_message(stream_admin_ui, "new-id") is True
        assert len(stream_admin_ui._seen_chat_ids) == 1000
    finally:
        _restore_app_shell_module(old_module)


def test_motor_heartbeat_reports_dead_started_motor_once():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app.motor_ia = SimpleNamespace(is_alive=MagicMock(return_value=False))
        app._motor_started = True
        app._closing = False
        app._motor_heartbeat_failure_reported = False
        app._ui_state = SimpleNamespace(health_status="green")
        app._print_log = MagicMock()

        with patch.object(app_shell.logger, "critical") as mock_critical:
            app._check_motor_heartbeat()
            app._check_motor_heartbeat()

        mock_critical.assert_called_once_with(
            "MotorVocalIA thread died unexpectedly; UI remains open but Kira is offline"
        )
        assert app._ui_state.health_status == "red"
        app._print_log.assert_called_once_with(
            "[CRITICO] MotorVocalIA se detuvo inesperadamente. Kira esta offline; reinicia la app."
        )
    finally:
        _restore_app_shell_module(old_module)


def test_motor_heartbeat_ignores_not_started_or_closing_motor():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app.motor_ia = SimpleNamespace(is_alive=MagicMock(return_value=False))
        app._motor_heartbeat_failure_reported = False
        app._ui_state = SimpleNamespace(health_status="green")
        app._print_log = MagicMock()

        with patch.object(app_shell.logger, "critical") as mock_critical:
            app._motor_started = False
            app._closing = False
            app._check_motor_heartbeat()

            app._motor_started = True
            app._closing = True
            app._check_motor_heartbeat()

        mock_critical.assert_not_called()
        assert app._ui_state.health_status == "green"
        app._print_log.assert_not_called()
    finally:
        _restore_app_shell_module(old_module)


def test_poll_health_status_preserves_red_after_motor_failure_reported():
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)
        app.health_monitor = SimpleNamespace(state=SimpleNamespace(overall_status="green"))
        app.motor_ia = SimpleNamespace(is_alive=MagicMock(return_value=False))
        app._motor_started = True
        app._closing = False
        app._motor_heartbeat_failure_reported = True
        app._ui_state = SimpleNamespace(health_status="red")
        app.after = MagicMock()

        app._poll_health_status()

        assert app._ui_state.health_status == "red"
        app.after.assert_called_once_with(2000, app._poll_health_status)
    finally:
        _restore_app_shell_module(old_module)
