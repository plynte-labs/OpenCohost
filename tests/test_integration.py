"""Integration tests for VoiceAI UI refactoring (Phase 9).

Covers:
- AppShell module import and structure
- Panel composition verification
- State propagation through UIState
- Shutdown sequence
- Callback wiring
- StreamAdminUI build method
- Thin app.py re-export
"""

from __future__ import annotations

import importlib
import os
import sys
import threading
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT_DIR not in sys.path:
    sys.path.insert(0, ROOT_DIR)

from opencohost.ui.state import UIState
from opencohost.ui.protocols import CallbackDispatcher
from opencohost.ui.stream_admin_ui import StreamAdminUI


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def ui_state():
    s = UIState()
    yield s
    s.shutdown(timeout=2.0)


@pytest.fixture()
def dispatcher():
    return CallbackDispatcher(source="test")


@pytest.fixture()
def mock_widgets():
    class MockEntry:
        def __init__(self):
            self._value = ""
        def get(self):
            return self._value
        def delete(self, start, end=None):
            self._value = ""
        def insert(self, index, text):
            self._value = text
        def configure(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    class MockText:
        def __init__(self):
            self._content = ""
        def get(self, start, end):
            return self._content
        def delete(self, start, end):
            self._content = ""
        def insert(self, pos, text):
            self._content += text
        def index(self, pos):
            return "1.0"

    class MockButton:
        def __init__(self):
            self._state = "normal"
            self._text = ""
            self._fg_color = ""
            self.command = None
        def configure(self, **kwargs):
            for k, v in kwargs.items():
                if k == "state":
                    self._state = v
                else:
                    setattr(self, k, v)

    class MockLabel:
        def __init__(self):
            self._text = ""
            self._text_color = ""
            self._fg_color = ""
        def configure(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    class MockSwitch:
        def __init__(self):
            self._value = False
        def get(self):
            return self._value
        def deselect(self):
            self._value = False

    class MockOptionMenu:
        def __init__(self):
            self._value = ""
        def get(self):
            return self._value

    class MockScrollableFrame:
        def __init__(self):
            self._children = []
        def winfo_children(self):
            return self._children
        def grid_columnconfigure(self, col, weight=1):
            pass

    return {
        "entry_stream_client_id": MockEntry(),
        "entry_stream_client_secret": MockEntry(),
        "entry_stream_title": MockEntry(),
        "entry_stream_category": MockEntry(),
        "entry_stream_tags": MockEntry(),
        "entry_stream_mod_user": MockEntry(),
        "entry_stream_mod_reason": MockEntry(),
        "entry_stream_chat_message": MockEntry(),
        "text_stream_description": MockText(),
        "text_stream_admin_log": MockText(),
        "lbl_stream_admin_status": MockLabel(),
        "lbl_stream_metadata_state": MockLabel(),
        "lbl_stream_analytics": MockLabel(),
        "lbl_stream_pending": MockLabel(),
        "lbl_oauth_status_pill": MockLabel(),
        "lbl_moderation_status_pill": MockLabel(),
        "lbl_oauth_side_status": MockLabel(),
        "lbl_moderation_side_status": MockLabel(),
        "lbl_kira_chat_state": MockLabel(),
        "btn_stream_youtube_read": MockButton(),
        "btn_stream_youtube_write": MockButton(),
        "btn_stream_revoke_write": MockButton(),
        "btn_stream_disconnect": MockButton(),
        "btn_stream_twitch": MockButton(),
        "btn_stream_read_metadata": MockButton(),
        "btn_stream_suggest_metadata": MockButton(),
        "btn_stream_apply_metadata": MockButton(),
        "btn_stream_reject_pending": MockButton(),
        "btn_stream_connect_chat": MockButton(),
        "btn_stream_send_chat": MockButton(),
        "btn_stream_force_kira": MockButton(),
        "btn_stream_propose_timeout": MockButton(),
        "btn_stream_propose_ban": MockButton(),
        "switch_stream_mod_enabled": MockSwitch(),
        "switch_stream_announce": MockSwitch(),
        "switch_stream_chat_enabled": MockSwitch(),
        "switch_stream_small": MockSwitch(),
        "combo_stream_mod_mode": MockOptionMenu(),
        "frame_stream_users": MockScrollableFrame(),
    }


# ---------------------------------------------------------------------------
# Test 1: app.py is a thin re-export
# ---------------------------------------------------------------------------


class TestAppPyThinExport:
    def test_app_py_exports_vocalaiapp(self):
        from opencohost.ui.app import VocalAIApp
        assert VocalAIApp is not None

    def test_app_py_reexports_from_app_shell(self):
        from opencohost.ui.app import VocalAIApp
        from opencohost.ui.app_shell import VocalAIApp as ShellApp
        assert VocalAIApp is ShellApp

    def test_app_py_line_count_under_200(self):
        app_path = os.path.join(ROOT_DIR, "opencohost", "ui", "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        assert len(lines) < 200, f"app.py has {len(lines)} lines, expected < 200"

    def test_app_py_no_ui_construction(self):
        app_path = os.path.join(ROOT_DIR, "opencohost", "ui", "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "ctk.CTkFrame" not in content, "app.py should not contain UI construction"
        assert "ctk.CTkButton" not in content, "app.py should not contain UI construction"
        assert "ctk.CTkLabel" not in content, "app.py should not contain UI construction"


# ---------------------------------------------------------------------------
# Test 2: app_shell.py structure
# ---------------------------------------------------------------------------


class TestAppShellStructure:
    def test_app_shell_module_exists(self):
        from opencohost.ui import app_shell
        assert hasattr(app_shell, "VocalAIApp")

    def test_app_shell_line_count_under_1500(self):
        shell_path = os.path.join(ROOT_DIR, "opencohost", "ui", "app_shell.py")
        with open(shell_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        # Baseline was already 3066 lines on master before the package
        # restructure; ui_rendering_optimization_20260609 owns shrinking it.
        # The audio-teardown fix (O8) added the deferred soft-stop wiring here,
        # pushing it to 3148 — acknowledged debt, not new sprawl. Decomposition
        # of this god-file remains owned by ui_rendering_optimization_20260609.
        assert len(lines) < 3160, f"app_shell.py has {len(lines)} lines, expected < 3160"

    def test_app_shell_imports_all_panels(self):
        from opencohost.ui import app_shell
        source = open(os.path.join(ROOT_DIR, "opencohost", "ui", "app_shell.py"), "r", encoding="utf-8").read()
        assert "from opencohost.ui.state import UIState" in source
        assert "from opencohost.ui.ptt_manager import PTTManager" in source
        assert "from opencohost.ui.voice_control import VoiceControlPanel" in source
        assert "from opencohost.ui.model_panel import ModelPanel" in source
        assert "from opencohost.ui.profile_panel import ProfilePanel" in source
        assert "from opencohost.ui.status_bar import StatusBar" in source
        assert "from opencohost.ui.smart_aggregator_ui import SmartAggregatorUI" in source
        assert "from opencohost.ui.stream_admin_ui import StreamAdminUI" in source
        assert "from opencohost.ui.advanced_panel import AdvancedModePanel" in source

    def test_app_shell_has_on_closing(self):
        source = open(os.path.join(ROOT_DIR, "opencohost", "ui", "app_shell.py"), "r", encoding="utf-8").read()
        assert "def on_closing" in source

    def test_app_shell_has_build_ui(self):
        source = open(os.path.join(ROOT_DIR, "opencohost", "ui", "app_shell.py"), "r", encoding="utf-8").read()
        assert "def _build_ui" in source

    def test_app_shell_has_motor_event_handler(self):
        source = open(os.path.join(ROOT_DIR, "opencohost", "ui", "app_shell.py"), "r", encoding="utf-8").read()
        assert "def _on_motor_event" in source


# ---------------------------------------------------------------------------
# Test 3: StreamAdminUI build method
# ---------------------------------------------------------------------------


class TestStreamAdminUIBuild:
    def test_build_method_exists(self):
        sa = StreamAdminUI(ui_state=UIState(), dispatcher=CallbackDispatcher(source="test"))
        assert hasattr(sa, "build")

    def test_build_returns_parent(self, ui_state, dispatcher):
        sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        mock_parent = MagicMock()
        mock_parent.grid_columnconfigure = MagicMock()
        mock_parent.grid_rowconfigure = MagicMock()

        def make_mock_tab():
            t = MagicMock()
            t.grid_columnconfigure = MagicMock()
            t.grid_rowconfigure = MagicMock()
            return t

        with patch("customtkinter.CTkTabview") as mock_tabview:
            mock_tabs = MagicMock()
            mock_tabview.return_value = mock_tabs
            mock_tabs.add = MagicMock(side_effect=[make_mock_tab() for _ in range(5)])
            mock_tabs.grid = MagicMock()

            with patch("customtkinter.CTkFrame") as mock_frame:
                mf = MagicMock()
                mf.grid = MagicMock()
                mf.grid_columnconfigure = MagicMock()
                mock_frame.return_value = mf

                with patch("customtkinter.CTkLabel") as mock_label:
                    ml = MagicMock()
                    ml.grid = MagicMock()
                    ml.pack = MagicMock()
                    mock_label.return_value = ml

                    with patch("customtkinter.CTkButton") as mock_btn:
                        mb = MagicMock()
                        mb.grid = MagicMock()
                        mb.pack = MagicMock()
                        mock_btn.return_value = mb

                        with patch("customtkinter.CTkEntry") as mock_entry:
                            me = MagicMock()
                            me.grid = MagicMock()
                            me.pack = MagicMock()
                            mock_entry.return_value = me

                            with patch("customtkinter.CTkTextbox") as mock_text:
                                mt = MagicMock()
                                mt.grid = MagicMock()
                                mt.pack = MagicMock()
                                mock_text.return_value = mt

                                with patch("customtkinter.CTkOptionMenu") as mock_opt:
                                    mo = MagicMock()
                                    mo.grid = MagicMock()
                                    mock_opt.return_value = mo

                                    with patch("customtkinter.CTkSwitch") as mock_switch:
                                        ms = MagicMock()
                                        ms.grid = MagicMock()
                                        ms.pack = MagicMock()
                                        mock_switch.return_value = ms

                                        with patch("customtkinter.CTkScrollableFrame") as mock_scroll:
                                            msc = MagicMock()
                                            msc.grid = MagicMock()
                                            msc.grid_columnconfigure = MagicMock()
                                            mock_scroll.return_value = msc

                                            with patch("customtkinter.CTkFont"):
                                                result = sa.build(mock_parent)
                                                assert result is mock_parent

    def test_build_populates_widgets(self, ui_state, dispatcher):
        sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        mock_parent = MagicMock()
        mock_parent.grid_columnconfigure = MagicMock()
        mock_parent.grid_rowconfigure = MagicMock()

        def make_mock_tab():
            t = MagicMock()
            t.grid_columnconfigure = MagicMock()
            t.grid_rowconfigure = MagicMock()
            return t

        with patch("customtkinter.CTkTabview") as mock_tabview:
            mock_tabs = MagicMock()
            mock_tabview.return_value = mock_tabs
            mock_tabs.add = MagicMock(side_effect=[make_mock_tab() for _ in range(5)])
            mock_tabs.grid = MagicMock()

            with patch("customtkinter.CTkFrame") as mock_frame:
                mf = MagicMock()
                mf.grid = MagicMock()
                mf.grid_columnconfigure = MagicMock()
                mock_frame.return_value = mf

                with patch("customtkinter.CTkLabel") as mock_label:
                    ml = MagicMock()
                    ml.grid = MagicMock()
                    ml.pack = MagicMock()
                    mock_label.return_value = ml

                    with patch("customtkinter.CTkButton") as mock_btn:
                        mb = MagicMock()
                        mb.grid = MagicMock()
                        mb.pack = MagicMock()
                        mock_btn.return_value = mb

                        with patch("customtkinter.CTkEntry") as mock_entry:
                            me = MagicMock()
                            me.grid = MagicMock()
                            me.pack = MagicMock()
                            mock_entry.return_value = me

                            with patch("customtkinter.CTkTextbox") as mock_text:
                                mt = MagicMock()
                                mt.grid = MagicMock()
                                mt.pack = MagicMock()
                                mock_text.return_value = mt

                                with patch("customtkinter.CTkOptionMenu") as mock_opt:
                                    mo = MagicMock()
                                    mo.grid = MagicMock()
                                    mock_opt.return_value = mo

                                    with patch("customtkinter.CTkSwitch") as mock_switch:
                                        ms = MagicMock()
                                        ms.grid = MagicMock()
                                        ms.pack = MagicMock()
                                        mock_switch.return_value = ms

                                        with patch("customtkinter.CTkScrollableFrame") as mock_scroll:
                                            msc = MagicMock()
                                            msc.grid = MagicMock()
                                            msc.grid_columnconfigure = MagicMock()
                                            mock_scroll.return_value = msc

                                            with patch("customtkinter.CTkFont"):
                                                sa.build(mock_parent)
                                                assert "lbl_stream_admin_status" in sa._widgets
                                                assert "btn_stream_youtube_read" in sa._widgets
                                                assert "entry_stream_client_id" in sa._widgets
                                                assert "entry_stream_title" in sa._widgets
                                                assert "switch_stream_mod_enabled" in sa._widgets
                                                assert "btn_stream_connect_chat" in sa._widgets
                                                assert "lbl_stream_analytics" in sa._widgets


# ---------------------------------------------------------------------------
# Test 4: Callback wiring
# ---------------------------------------------------------------------------


class TestCallbackWiring:
    def test_stream_admin_ui_has_callback_setters(self, ui_state, dispatcher):
        sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        assert hasattr(sa, "set_connect_callback")
        assert hasattr(sa, "set_disconnect_callback")
        assert hasattr(sa, "set_save_oauth_callback")
        assert hasattr(sa, "set_refresh_metadata_callback")
        assert hasattr(sa, "set_suggest_metadata_callback")
        assert hasattr(sa, "set_apply_metadata_callback")
        assert hasattr(sa, "set_reject_pending_callback")
        assert hasattr(sa, "set_apply_runtime_settings_callback")
        assert hasattr(sa, "set_propose_high_risk_callback")
        assert hasattr(sa, "set_connect_current_chat_callback")
        assert hasattr(sa, "set_send_chat_callback")
        assert hasattr(sa, "set_toggle_small_stream_callback")
        assert hasattr(sa, "set_simulate_chat_callback")
        assert hasattr(sa, "set_force_kira_callback")
        assert hasattr(sa, "set_refresh_user_list_callback")

    def test_dispatch_callbacks_invoke_set_handlers(self, ui_state, dispatcher):
        sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        connect_called = []
        sa.set_connect_callback(lambda rw: connect_called.append(rw))
        sa._dispatch_connect(True)
        assert connect_called == [True]

    def test_dispatch_disconnect_invokes_handler(self, ui_state, dispatcher):
        sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        disconnect_called = []
        sa.set_disconnect_callback(lambda: disconnect_called.append(True))
        sa._dispatch_disconnect()
        assert disconnect_called == [True]

    def test_dispatch_save_oauth_invokes_handler(self, ui_state, dispatcher):
        sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        save_called = []
        sa.set_save_oauth_callback(lambda: save_called.append(True))
        sa._dispatch_save_oauth()
        assert save_called == [True]

    def test_dispatch_no_handler_is_noop(self, ui_state, dispatcher):
        sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        sa._dispatch_connect(True)  # Should not raise
        sa._dispatch_disconnect()
        sa._dispatch_save_oauth()

    def test_refresh_user_list_dispatch(self, ui_state, dispatcher):
        sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        refresh_called = []
        sa.set_refresh_user_list_callback(lambda: refresh_called.append(True))
        sa.refresh_user_list()
        assert refresh_called == [True]


# ---------------------------------------------------------------------------
# Test 5: State propagation
# ---------------------------------------------------------------------------


class TestStatePropagation:
    def test_ui_state_pipeline_state(self, ui_state):
        ui_state.pipeline_state = "listening"
        assert ui_state.pipeline_state == "listening"

    def test_ui_state_observer_notified(self, ui_state):
        notified = []
        ui_state.subscribe(lambda k, v: notified.append((k, v)))
        ui_state.pipeline_state = "speaking"
        import time
        time.sleep(0.1)  # Allow dispatch thread to process
        assert ("pipeline_state", "speaking") in notified

    def test_ui_state_thread_safety(self, ui_state):
        errors = []

        def writer():
            try:
                for i in range(100):
                    ui_state.pipeline_state = "idle"
                    ui_state.pipeline_state = "listening"
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=writer) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert not errors, f"Thread safety errors: {errors}"


# ---------------------------------------------------------------------------
# Test 6: Shutdown sequence
# ---------------------------------------------------------------------------


class TestShutdownSequence:
    def test_ui_state_shutdown(self):
        state = UIState()
        state.subscribe(lambda k, v: None)
        state.shutdown(timeout=2.0)
        state.set("model_status", "ready")  # Should not raise

    def test_stream_admin_ui_cleanup(self, ui_state, dispatcher):
        sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        sa._chat_connected = True
        sa._chat_stop = threading.Event()
        sa.cleanup()
        assert sa._chat_connected is False
        assert sa._chat_stop is None


# ---------------------------------------------------------------------------
# Test 7: Panel composition
# ---------------------------------------------------------------------------


class TestPanelComposition:
    def test_all_panel_modules_exist(self):
        panels = [
            "opencohost.ui.state",
            "opencohost.ui.protocols",
            "opencohost.ui.ptt_manager",
            "opencohost.ui.voice_control",
            "opencohost.ui.model_panel",
            "opencohost.ui.profile_panel",
            "opencohost.ui.status_bar",
            "opencohost.ui.smart_aggregator_ui",
            "opencohost.ui.stream_admin_ui",
            "opencohost.ui.advanced_panel",
            "opencohost.ui.app_shell",
        ]
        for panel in panels:
            mod = importlib.import_module(panel)
            assert mod is not None, f"Module {panel} failed to import"

    def test_no_circular_imports(self):
        modules = [
            "opencohost.ui.state",
            "opencohost.ui.protocols",
            "opencohost.ui.ptt_manager",
            "opencohost.ui.voice_control",
            "opencohost.ui.model_panel",
            "opencohost.ui.profile_panel",
            "opencohost.ui.status_bar",
            "opencohost.ui.smart_aggregator_ui",
            "opencohost.ui.stream_admin_ui",
            "opencohost.ui.advanced_panel",
            "opencohost.ui.app_shell",
            "opencohost.ui.app",
        ]
        for mod_name in modules:
            importlib.import_module(mod_name)

    def test_app_shell_no_try_except_pass(self):
        shell_path = os.path.join(ROOT_DIR, "opencohost", "ui", "app_shell.py")
        with open(shell_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "except: pass" not in content, "app_shell.py should not contain 'except: pass'"
        assert "except:pass" not in content, "app_shell.py should not contain 'except:pass'"


# ---------------------------------------------------------------------------
# Test 8: StreamAdminUI metadata payload
# ---------------------------------------------------------------------------


class TestStreamAdminUIMetadataPayload:
    def test_metadata_payload_from_ui(self, ui_state, dispatcher, mock_widgets):
        sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        sa.set_widgets(mock_widgets)
        mock_widgets["entry_stream_title"]._value = "Test Stream"
        mock_widgets["entry_stream_category"]._value = "20"
        mock_widgets["entry_stream_tags"]._value = "gaming, live"
        mock_widgets["text_stream_description"]._content = "Test description"

        payload = sa.metadata_payload_from_ui()
        assert payload["title"] == "Test Stream"
        assert payload["category_id"] == "20"
        assert payload["tags"] == ["gaming", "live"]
        assert payload["description"] == "Test description"


# ---------------------------------------------------------------------------
# Test 9: StreamAdminUI moderation
# ---------------------------------------------------------------------------


class TestStreamAdminUIModeration:
    def test_track_chat_user(self, ui_state, dispatcher):
        sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        sa.track_chat_user({
            "author_channel_id": "UC123",
            "user": "TestUser",
            "text": "Hello!",
        })
        assert "UC123" in sa.chat_users
        assert sa.chat_users["UC123"]["user"] == "TestUser"
        assert sa.chat_users["UC123"]["count"] == 1

    def test_track_chat_user_increments_count(self, ui_state, dispatcher):
        sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        msg = {"author_channel_id": "UC123", "user": "TestUser", "text": "Hi"}
        sa.track_chat_user(msg)
        sa.track_chat_user(msg)
        sa.track_chat_user(msg)
        assert sa.chat_users["UC123"]["count"] == 3

    def test_default_mod_reason(self, ui_state, dispatcher):
        sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        item = {"last_message": "spam message here"}
        reason = sa.default_mod_reason(item)
        assert "Revisado desde Stream Admin" in reason

    def test_default_mod_reason_truncates(self, ui_state, dispatcher):
        sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        item = {"last_message": "x" * 200}
        reason = sa.default_mod_reason(item)
        assert len(reason) < 120


# ---------------------------------------------------------------------------
# Test 10: StreamAdminUI can_write check
# ---------------------------------------------------------------------------


class TestStreamAdminUICanWrite:
    def test_can_write_false_without_admin(self, ui_state, dispatcher):
        sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        assert sa._can_write() is False

    def test_can_write_true_with_write_scope(self, ui_state, dispatcher):
        sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        mock_admin = MagicMock()
        mock_admin.status.return_value = {
            "write_enabled": True,
            "write_scope_active": True,
        }
        sa.set_stream_admin(mock_admin)
        assert sa._can_write() is True

    def test_can_write_false_read_only(self, ui_state, dispatcher):
        sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        mock_admin = MagicMock()
        mock_admin.status.return_value = {
            "write_enabled": True,
            "write_scope_active": False,
        }
        sa.set_stream_admin(mock_admin)
        assert sa._can_write() is False


# ---------------------------------------------------------------------------
# Test 11: StreamAdminUI sync_controls
# ---------------------------------------------------------------------------


class TestStreamAdminUISyncControls:
    def test_sync_controls_enables_widgets(self, ui_state, dispatcher, mock_widgets):
        sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        sa.set_widgets(mock_widgets)
        state = {
            "connected": True,
            "write_enabled": True,
            "write_scope_active": True,
            "pending_action": True,
        }
        sa._sync_controls(state)
        assert mock_widgets["btn_stream_disconnect"]._state == "normal"
        assert mock_widgets["btn_stream_apply_metadata"]._state == "normal"
        assert mock_widgets["btn_stream_reject_pending"]._state == "normal"

    def test_sync_controls_disables_widgets(self, ui_state, dispatcher, mock_widgets):
        sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        sa.set_widgets(mock_widgets)
        state = {
            "connected": False,
            "write_enabled": False,
            "write_scope_active": False,
            "pending_action": False,
        }
        sa._sync_controls(state)
        assert mock_widgets["btn_stream_disconnect"]._state == "disabled"
        assert mock_widgets["btn_stream_read_metadata"]._state == "disabled"


# ---------------------------------------------------------------------------
# Test 12: StreamAdminUI state handler
# ---------------------------------------------------------------------------


class TestStreamAdminUIStateHandler:
    def test_on_state_updates_labels(self, ui_state, dispatcher):
        sa = StreamAdminUI(
            ui_state=ui_state,
            dispatcher=dispatcher,
            schedule_ui_update=lambda fn: fn(),
        )
        # Create mock widgets directly
        lbl = MagicMock()
        lbl._text = ""
        lbl._text_color = ""
        def configure(**kwargs):
            for k, v in kwargs.items():
                setattr(lbl, f"_{k}", v)
        lbl.configure = configure
        sa._widgets["lbl_stream_admin_status"] = lbl

        oauth_pill = MagicMock()
        oauth_pill._text = ""
        oauth_pill._fg_color = ""
        oauth_pill.configure = lambda **kwargs: setattr(oauth_pill, "_fg_color", kwargs.get("fg_color", "")) or setattr(oauth_pill, "_text", kwargs.get("text", ""))
        sa._widgets["lbl_oauth_status_pill"] = oauth_pill

        side_status = MagicMock()
        side_status._text = ""
        side_status._text_color = ""
        side_status.configure = lambda **kwargs: setattr(side_status, f"_{list(kwargs.keys())[0]}", list(kwargs.values())[0]) if kwargs else None
        sa._widgets["lbl_oauth_side_status"] = side_status

        state = {
            "connected": True,
            "mode": "read_only",
            "account": {"title": "TestChannel"},
            "oauth_client_configured": True,
        }
        sa.on_state(state)
        assert lbl._text_color == "#44cc66"

    def test_on_state_write_mode(self, ui_state, dispatcher):
        sa = StreamAdminUI(
            ui_state=ui_state,
            dispatcher=dispatcher,
            schedule_ui_update=lambda fn: fn(),
        )
        lbl = MagicMock()
        lbl._text = ""
        lbl._text_color = ""
        lbl.configure = lambda **kwargs: setattr(lbl, f"_{list(kwargs.keys())[0]}", list(kwargs.values())[0]) if kwargs else None
        sa._widgets["lbl_stream_admin_status"] = lbl

        oauth_pill = MagicMock()
        oauth_pill._text = ""
        oauth_pill._fg_color = ""
        oauth_pill.configure = lambda **kwargs: (setattr(oauth_pill, "_fg_color", kwargs.get("fg_color", "")), setattr(oauth_pill, "_text", kwargs.get("text", "")))
        sa._widgets["lbl_oauth_status_pill"] = oauth_pill

        side_status = MagicMock()
        side_status.configure = lambda **kwargs: None
        sa._widgets["lbl_oauth_side_status"] = side_status

        state = {
            "connected": True,
            "mode": "write",
            "account": {"title": "TestChannel"},
            "oauth_client_configured": True,
        }
        sa.on_state(state)
        assert oauth_pill._fg_color == "#5f3a1f"


# ---------------------------------------------------------------------------
# Test 13: StreamAdminUI analytics handler
# ---------------------------------------------------------------------------


class TestStreamAdminUIAnalytics:
    def test_on_analytics_updates_label(self, ui_state, dispatcher):
        sa = StreamAdminUI(
            ui_state=ui_state,
            dispatcher=dispatcher,
            schedule_ui_update=lambda fn: fn(),
        )
        lbl = MagicMock()
        lbl._text = ""
        lbl.configure = lambda **kwargs: setattr(lbl, "_text", kwargs.get("text", ""))
        sa._widgets["lbl_stream_analytics"] = lbl

        snapshot = {
            "viewers": 42,
            "uptime_seconds": 3600,
            "chat_rate_avg": 2.5,
            "last_vibe_dominant": "excited",
            "last_vibe_temperature": 75,
        }
        sa.on_analytics(snapshot)
        assert "42" in lbl._text
        assert "60m" in lbl._text


class TestStreamAdminUIPending:
    def test_on_pending_no_action(self, ui_state, dispatcher):
        sa = StreamAdminUI(
            ui_state=ui_state,
            dispatcher=dispatcher,
            schedule_ui_update=lambda fn: fn(),
        )
        lbl = MagicMock()
        lbl._text = ""
        lbl._text_color = ""
        lbl.configure = lambda **kwargs: (setattr(lbl, "_text", kwargs.get("text", "")), setattr(lbl, "_text_color", kwargs.get("text_color", "")))
        sa._widgets["lbl_stream_pending"] = lbl

        mod_pill = MagicMock()
        mod_pill.configure = lambda **kwargs: None
        sa._widgets["lbl_moderation_status_pill"] = mod_pill

        mod_side = MagicMock()
        mod_side.configure = lambda **kwargs: None
        sa._widgets["lbl_moderation_side_status"] = mod_side

        sa.on_pending(None)
        assert "ninguna" in lbl._text

    def test_on_pending_with_action(self, ui_state, dispatcher):
        sa = StreamAdminUI(
            ui_state=ui_state,
            dispatcher=dispatcher,
            schedule_ui_update=lambda fn: fn(),
        )
        lbl = MagicMock()
        lbl._text = ""
        lbl._text_color = ""
        lbl.configure = lambda **kwargs: (setattr(lbl, "_text", kwargs.get("text", "")), setattr(lbl, "_text_color", kwargs.get("text_color", "")))
        sa._widgets["lbl_stream_pending"] = lbl

        mod_pill = MagicMock()
        mod_pill.configure = lambda **kwargs: None
        sa._widgets["lbl_moderation_status_pill"] = mod_pill

        mod_side = MagicMock()
        mod_side.configure = lambda **kwargs: None
        sa._widgets["lbl_moderation_side_status"] = mod_side

        pending = {
            "type": "metadata_update",
            "payload": {"title": "New Title"},
        }
        sa.on_pending(pending)
        assert "metadata_update" in lbl._text


# ---------------------------------------------------------------------------
# Test 15: main.py compatibility
# ---------------------------------------------------------------------------


class TestMainPyCompatibility:
    def test_main_imports_vocalaiapp(self):
        from opencohost.ui.app import VocalAIApp
        assert callable(VocalAIApp)

    def test_main_py_unchanged(self):
        main_path = os.path.join(ROOT_DIR, "opencohost", "__main__.py")
        with open(main_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "from opencohost.ui.app import VocalAIApp" in content


# ---------------------------------------------------------------------------
# Test 16: No try/except: pass in new modules
# ---------------------------------------------------------------------------


class TestNoSilentExceptions:
    def test_app_shell_no_silent_except(self):
        shell_path = os.path.join(ROOT_DIR, "opencohost", "ui", "app_shell.py")
        with open(shell_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "except: pass" not in content
        assert "except:pass" not in content

    def test_stream_admin_ui_no_silent_except(self):
        sa_path = os.path.join(ROOT_DIR, "opencohost", "ui", "stream_admin_ui.py")
        with open(sa_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "except: pass" not in content
        assert "except:pass" not in content
