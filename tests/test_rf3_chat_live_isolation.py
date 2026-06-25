"""Guard tests: RF3 Chat Live callbacks wired, RF4 shell methods removed.

Stage 2a of ui_rendering_optimization_20260609 Phase 6:
- After _wire_stream_admin_callbacks(), the 4 RF3 setters MUST be called on StreamAdminUI.
- The RF4 setter methods MUST NOT be called on StreamAdminUI.
- VocalAIApp MUST NOT define the RF4 methods moved to legacy.
"""

from __future__ import annotations

import importlib
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, call


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
    old_module = sys.modules.pop("opencohost.ui.app_shell", None)
    with patch.dict(sys.modules, modules):
        module = importlib.import_module("opencohost.ui.app_shell")
    return module, old_module


def _restore_app_shell_module(old_module) -> None:
    if old_module is not None:
        sys.modules["opencohost.ui.app_shell"] = old_module
        return
    sys.modules.pop("opencohost.ui.app_shell", None)
    ui_module = sys.modules.get("opencohost.ui")
    if ui_module is not None and hasattr(ui_module, "app_shell"):
        delattr(ui_module, "app_shell")


# ---------------------------------------------------------------------------
# RF3 wires present, RF4 wires absent after _wire_stream_admin_callbacks()
# ---------------------------------------------------------------------------

def test_rf3_chat_live_still_wires_after_rf4_removal():
    """The 4 RF3 setters are called; the 18+ RF4 setters are NOT called."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        app = object.__new__(app_shell.VocalAIApp)

        sa = MagicMock()
        app.stream_admin_ui = sa

        # Provide stubs so RF3 lambdas do not blow up on attribute access
        app._on_stream_admin_connect_chat_live = MagicMock()
        app._on_stream_admin_button_twitch = MagicMock()
        app._on_stream_admin_threshold_preset = MagicMock()
        app._on_stream_admin_cooldown_preset = MagicMock()

        # Agenda and kira stubs (kept or removed — the test checks call names)
        app._kira_agenda_add_topic = MagicMock()
        app._kira_agenda_enable = MagicMock()
        app._kira_agenda_soft_stop = MagicMock()
        app._kira_agenda_emergency_stop = MagicMock()

        app._wire_stream_admin_callbacks()

        # --- RF3 wires MUST be present ---
        assert sa.set_connect_chat_live_callback.called, \
            "set_connect_chat_live_callback must be wired"
        assert sa.set_connect_chat_twitch_callback.called, \
            "set_connect_chat_twitch_callback must be wired"
        assert sa.set_threshold_preset_callback.called, \
            "set_threshold_preset_callback must be wired"
        assert sa.set_cooldown_preset_callback.called, \
            "set_cooldown_preset_callback must be wired"

        # --- RF4 wires MUST be absent ---
        rf4_setters = [
            "set_connect_callback",
            "set_disconnect_callback",
            "set_save_oauth_callback",
            "set_refresh_metadata_callback",
            "set_suggest_metadata_callback",
            "set_apply_metadata_callback",
            "set_reject_pending_callback",
            "set_apply_runtime_settings_callback",
            "set_propose_high_risk_callback",
            "set_connect_current_chat_callback",
            "set_send_chat_callback",
            "set_toggle_small_stream_callback",
            "set_simulate_chat_callback",
            "set_force_kira_callback",
            "set_refresh_user_list_callback",
            "set_agenda_add_topic_callback",
            "set_agenda_enable_callback",
            "set_agenda_soft_stop_callback",
            "set_agenda_emergency_stop_callback",
        ]
        called_names = {c[0] for c in sa.method_calls}
        for setter in rf4_setters:
            assert setter not in called_names, \
                f"RF4 setter {setter!r} must NOT be called by _wire_stream_admin_callbacks()"

    finally:
        _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# RF4 methods must NOT exist on VocalAIApp after Stage 2a move
# ---------------------------------------------------------------------------

_RF4_METHODS_MOVED_TO_LEGACY = [
    "_init_stream_admin",
    "_populate_stream_oauth_client_fields",
    "_run_stream_admin_task",
    "_stream_admin_connect",
    "_stream_admin_save_oauth_client",
    "_stream_admin_disconnect",
    "_stream_admin_revoke_write",
    "_stream_admin_refresh_metadata",
    "_stream_admin_suggest_metadata",
    "_stream_admin_apply_metadata",
    "_stream_admin_reject_pending",
    "_stream_admin_connect_current_chat",
    "_stream_admin_connect_api_chat",
    "_stream_admin_disconnect_api_chat",
    "_stream_admin_send_chat",
    "_stream_admin_force_kira_comment",
    "_stream_admin_toggle_small_stream",
    "_stream_admin_simulate_chat",
    "_stream_admin_propose_high_risk",
    "_stream_admin_refresh_user_list",
    "_stream_admin_moderate_user_from_list",
    "_stream_admin_apply_runtime_settings",
    "_stream_admin_can_write",
    "_sync_stream_admin_controls",
    "_on_stream_admin_state",
    "_on_stream_admin_metadata",
    "_on_stream_admin_pending",
    "_on_stream_admin_analytics",
    "_stream_admin_inject_silent_context",
]


def test_rf4_methods_removed_from_vocal_ai_app():
    """VocalAIApp must not define the RF4 methods moved to legacy."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        for method_name in _RF4_METHODS_MOVED_TO_LEGACY:
            assert not hasattr(app_shell.VocalAIApp, method_name), (
                f"VocalAIApp still defines {method_name!r} — "
                f"it must be removed (moved to legacy package)"
            )
    finally:
        _restore_app_shell_module(old_module)


def test_module_level_rf4_function_removed_from_app_shell():
    """_stream_admin_should_process_chat_message must NOT be a module-level name in app_shell."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        assert not hasattr(app_shell, "_stream_admin_should_process_chat_message"), (
            "_stream_admin_should_process_chat_message must be removed from app_shell "
            "(moved to legacy package)"
        )
    finally:
        _restore_app_shell_module(old_module)


# ---------------------------------------------------------------------------
# RF3-kept methods remain on VocalAIApp
# ---------------------------------------------------------------------------

_RF3_METHODS_KEPT = [
    "_stream_admin_track_chat_user",
    "_stream_admin_ingest_rf3_event",
    "_on_stream_admin_connect_chat_live",
    "_on_stream_admin_button_twitch",
    "_on_stream_admin_threshold_preset",
    "_on_stream_admin_cooldown_preset",
    "_on_stream_admin_log",
    "_append_stream_admin_log",
    "_notify_operator",
]


def test_rf3_methods_remain_on_vocal_ai_app():
    """The RF3 and shared helper methods must remain on VocalAIApp."""
    app_shell, old_module = _import_app_shell_with_ui_deps_mocked()
    try:
        for method_name in _RF3_METHODS_KEPT:
            assert hasattr(app_shell.VocalAIApp, method_name), (
                f"VocalAIApp must still define {method_name!r} (RF3-kept)"
            )
    finally:
        _restore_app_shell_module(old_module)
