"""Focused tests for app shell OBS reconnect resilience."""

from __future__ import annotations

import sys
import importlib
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
