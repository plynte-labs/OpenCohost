"""Comprehensive tests for ui.stream_admin_ui.StreamAdminUI.

Covers:
- StreamAdminUI class initialization
- Widget injection and access
- OAuth / connection flows
- Metadata management (read, suggest, apply, reject)
- Moderation (propose, track users, moderate)
- Chat (send, toggle small stream, connect, disconnect)
- Analytics and state handlers
- Runtime settings
- RF3 event ingestion and silent context
- Edge cases (missing widgets, no stream_admin, write checks)
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from opencohost.ui.state import UIState
from opencohost.ui.protocols import CallbackDispatcher
from opencohost.ui.stream_admin_ui import StreamAdminUI


ROOT = Path(__file__).resolve().parents[1]
STREAM_ADMIN_UI = ROOT / "opencohost" / "ui" / "stream_admin_ui.py"


def read_stream_admin_source() -> str:
    return STREAM_ADMIN_UI.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def ui_state():
    """Fresh UIState instance, shut down after each test."""
    s = UIState()
    yield s
    s.shutdown(timeout=2.0)


@pytest.fixture()
def dispatcher():
    """Fresh CallbackDispatcher instance."""
    return CallbackDispatcher(source="StreamAdminUI")


@pytest.fixture()
def mock_widgets():
    """Dict of mock widgets for injection."""
    widgets = {}

    class MockEntry:
        def __init__(self):
            self._value = ""
            self.placeholder_text = ""

        def get(self):
            return self._value

        def delete(self, start, end=None):
            self._value = ""

        def insert(self, index, text):
            if index == 0:
                self._value = text + self._value
            else:
                self._value = self._value + text

        def configure(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    class MockText:
        def __init__(self):
            self._content = ""

        def get(self, start, end=None):
            return self._content

        def delete(self, start, end=None):
            self._content = ""

        def insert(self, index, text):
            self._content += text

        def index(self, pos):
            lines = self._content.count("\n") + 1
            return f"{lines}.0"

    class MockLabel:
        def __init__(self):
            self.text = ""
            self.text_color = ""
            self.fg_color = ""

        def configure(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    class MockButton:
        def __init__(self):
            self.state = "normal"
            self.text = ""
            self.fg_color = ""

        def configure(self, **kwargs):
            for k, v in kwargs.items():
                setattr(self, k, v)

    class MockSwitch:
        def __init__(self, initial=False):
            self._value = initial

        def get(self):
            return self._value

        def deselect(self):
            self._value = False

        def select(self):
            self._value = True

    class MockOptionMenu:
        def __init__(self):
            self._value = "alerts_only"

        def get(self):
            return self._value

        def set(self, value):
            self._value = value

    class MockFrame:
        def __init__(self):
            self._children = []

        def winfo_children(self):
            return self._children

        def grid_columnconfigure(self, col, **kwargs):
            pass

    widgets["entry_stream_client_id"] = MockEntry()
    widgets["entry_stream_client_secret"] = MockEntry()
    widgets["entry_stream_title"] = MockEntry()
    widgets["entry_stream_category"] = MockEntry()
    widgets["entry_stream_tags"] = MockEntry()
    widgets["entry_stream_mod_user"] = MockEntry()
    widgets["entry_stream_mod_reason"] = MockEntry()
    widgets["entry_stream_chat_message"] = MockEntry()
    widgets["entry_kira_agenda_title"] = MockEntry()
    widgets["entry_kira_agenda_angle"] = MockEntry()
    widgets["entry_kira_agenda_constraints"] = MockEntry()
    widgets["text_stream_description"] = MockText()
    widgets["text_stream_admin_log"] = MockText()
    widgets["lbl_stream_admin_status"] = MockLabel()
    widgets["lbl_stream_metadata_state"] = MockLabel()
    widgets["lbl_stream_analytics"] = MockLabel()
    widgets["lbl_stream_pending"] = MockLabel()
    widgets["lbl_oauth_status_pill"] = MockLabel()
    widgets["lbl_moderation_status_pill"] = MockLabel()
    widgets["lbl_oauth_side_status"] = MockLabel()
    widgets["lbl_moderation_side_status"] = MockLabel()
    widgets["lbl_kira_chat_state"] = MockLabel()
    widgets["lbl_kira_agenda_state"] = MockLabel()
    widgets["btn_stream_youtube_read"] = MockButton()
    widgets["btn_stream_youtube_write"] = MockButton()
    widgets["btn_stream_revoke_write"] = MockButton()
    widgets["btn_stream_disconnect"] = MockButton()
    widgets["btn_stream_twitch"] = MockButton()
    widgets["btn_stream_read_metadata"] = MockButton()
    widgets["btn_stream_suggest_metadata"] = MockButton()
    widgets["btn_stream_apply_metadata"] = MockButton()
    widgets["btn_stream_reject_pending"] = MockButton()
    widgets["btn_stream_connect_chat"] = MockButton()
    widgets["btn_stream_send_chat"] = MockButton()
    widgets["btn_stream_simulate_chat"] = MockButton()
    widgets["btn_stream_force_kira"] = MockButton()
    widgets["btn_kira_agenda_add_topic"] = MockButton()
    widgets["btn_kira_agenda_enable"] = MockButton()
    widgets["btn_kira_agenda_soft_stop"] = MockButton()
    widgets["btn_kira_agenda_emergency_stop"] = MockButton()
    widgets["btn_stream_propose_timeout"] = MockButton()
    widgets["btn_stream_propose_ban"] = MockButton()
    widgets["switch_stream_mod_enabled"] = MockSwitch(False)
    widgets["switch_stream_announce"] = MockSwitch(False)
    widgets["switch_stream_chat_enabled"] = MockSwitch(False)
    widgets["switch_stream_small"] = MockSwitch(False)
    widgets["combo_stream_mod_mode"] = MockOptionMenu()
    widgets["frame_stream_users"] = MockFrame()

    return widgets


@pytest.fixture()
def mock_stream_admin():
    """Mock AdminManager instance."""
    admin = MagicMock()
    admin.config = {"moderation": {}, "chat": {}}
    admin.moderation = MagicMock()
    admin.moderation.enabled = False
    admin.moderation.mode = "alerts_only"
    admin.pending_action = None
    admin._write_activated_at = None
    admin.status.return_value = {
        "connected": False,
        "write_enabled": False,
        "write_scope_active": False,
        "oauth_client_configured": False,
        "mode": "read_only",
        "account": {"title": "TestChannel"},
        "pending_action": None,
    }
    admin.get_oauth_client_config.return_value = {
        "client_id": "",
        "has_client_secret": False,
    }
    return admin


@pytest.fixture()
def mock_smart_agg():
    """Mock Aggregator instance."""
    agg = MagicMock()
    agg._session_id = None
    return agg


@pytest.fixture()
def mock_motor_ia():
    """Mock MotorVocalIA instance."""
    motor = MagicMock()
    motor.historial = []
    return motor


@pytest.fixture()
def stream_admin_ui(ui_state, dispatcher, mock_widgets, mock_stream_admin, mock_smart_agg, mock_motor_ia):
    """StreamAdminUI instance with all dependencies."""
    log_messages = []

    def on_log(msg):
        log_messages.append(msg)

    ui = StreamAdminUI(
        ui_state=ui_state,
        dispatcher=dispatcher,
        stream_admin=mock_stream_admin,
        smart_agg=mock_smart_agg,
        motor_ia=mock_motor_ia,
        on_log=on_log,
    )
    ui.set_widgets(mock_widgets)
    ui._log_messages = log_messages  # for test assertions
    return ui


# ---------------------------------------------------------------------------
# Test: Stream UI Layout Safety
# ---------------------------------------------------------------------------


class TestStreamAdminUILayoutSafety:
    """Source-level safety checks for Stream UI visual reorganization."""

    def test_stream_layout_uses_three_operator_tabs(self):
        """Stream Admin groups controls into fewer user-facing tabs."""
        source = read_stream_admin_source()

        assert "self.tab_stream_live" in source
        assert "self.tab_stream_actions" in source
        assert 'stream_tabs.add("Conexión")' not in source
        assert 'stream_tabs.add("Metadata")' not in source
        assert 'stream_tabs.add("Moderación")' not in source
        assert 'stream_tabs.add("Chat")' not in source

    def test_stream_layout_preserves_key_widget_contracts(self):
        """Reorganization must not rename controls used by AppShell/tests."""
        source = read_stream_admin_source()

        for widget_name in (
            "lbl_stream_admin_status",
            "btn_stream_youtube_read",
            "btn_stream_youtube_write",
            "btn_stream_revoke_write",
            "btn_stream_disconnect",
            "btn_stream_twitch",
            "entry_stream_client_id",
            "entry_stream_client_secret",
            "lbl_stream_metadata_state",
            "btn_stream_read_metadata",
            "btn_stream_suggest_metadata",
            "btn_stream_apply_metadata",
            "btn_stream_reject_pending",
            "entry_stream_title",
            "entry_stream_category",
            "entry_stream_tags",
            "text_stream_description",
            "switch_stream_mod_enabled",
            "combo_stream_mod_mode",
            "switch_stream_announce",
            "entry_stream_mod_user",
            "entry_stream_mod_reason",
            "btn_stream_propose_timeout",
            "btn_stream_propose_ban",
            "frame_stream_users",
            "btn_stream_connect_chat",
            "switch_stream_chat_enabled",
            "switch_stream_small",
            "entry_stream_chat_message",
            "btn_stream_send_chat",
            "btn_stream_simulate_chat",
            "btn_stream_force_kira",
            "lbl_kira_agenda_state",
            "entry_kira_agenda_title",
            "entry_kira_agenda_angle",
            "entry_kira_agenda_constraints",
            "btn_kira_agenda_add_topic",
            "btn_kira_agenda_enable",
            "btn_kira_agenda_soft_stop",
            "btn_kira_agenda_emergency_stop",
            "lbl_stream_analytics",
            "lbl_stream_pending",
        ):
            assert f'"{widget_name}"' in source

    def test_stream_layout_preserves_callback_dispatchers(self):
        """Controls keep the same callback path after moving sections."""
        source = read_stream_admin_source()

        for callback in (
            "self._dispatch_connect(False)",
            "self._dispatch_connect(True)",
            "self.revoke_write",
            "self._dispatch_disconnect()",
            "self._dispatch_save_oauth",
            "self._dispatch_refresh_metadata()",
            "self._dispatch_suggest_metadata()",
            "self._dispatch_apply_metadata()",
            "self._dispatch_reject_pending()",
            "self._dispatch_apply_runtime_settings()",
            "self._dispatch_propose_high_risk(\"timeout\")",
            "self._dispatch_propose_high_risk(\"ban\")",
            "self.refresh_user_list",
            "self._dispatch_connect_current_chat",
            "self._dispatch_toggle_small_stream",
            "self._dispatch_simulate_chat",
            "self._dispatch_send_chat()",
            "self._dispatch_force_kira",
            "self._dispatch_agenda_add_topic",
            "self._dispatch_agenda_enable",
            "self._dispatch_agenda_soft_stop",
            "self._dispatch_agenda_emergency_stop",
        ):
            assert callback in source

    def test_stream_layout_uses_vertical_scrollable_containment(self):
        """Stream cards should stack vertically inside scrollable containers."""
        source = read_stream_admin_source()

        assert "vertical scrollable containment" in source
        assert source.count("CTkScrollableFrame") >= 3
        assert 'sticky="ew", padx=10' in source
        assert "columnspan=8" not in source
        assert "columnspan=7" not in source

    def test_stream_status_labels_wrap_instead_of_clipping(self):
        """Long Stream status text must wrap inside cards."""
        source = read_stream_admin_source()

        assert "wraplength=520" in source
        assert "justify=\"left\"" in source


class TestKiraAgendaControls:
    def test_agenda_add_topic_dispatches_compact_form(self, ui_state, dispatcher, mock_widgets):
        ui = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        ui.set_widgets(mock_widgets)
        received = []
        ui.set_agenda_add_topic_callback(lambda title, angle, constraints: received.append((title, angle, constraints)))

        mock_widgets["entry_kira_agenda_title"].insert(0, " Minecraft industria ")
        mock_widgets["entry_kira_agenda_angle"].insert(0, " simple y divertido ")
        mock_widgets["entry_kira_agenda_constraints"].insert(0, " no académico ; 1 frase ")

        ui._dispatch_agenda_add_topic()

        assert received == [("Minecraft industria", "simple y divertido", ["no académico", "1 frase"])]

    def test_agenda_control_dispatchers(self, ui_state, dispatcher, mock_widgets):
        ui = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        ui.set_widgets(mock_widgets)
        calls = []
        ui.set_agenda_enable_callback(lambda: calls.append("enable"))
        ui.set_agenda_soft_stop_callback(lambda: calls.append("soft"))
        ui.set_agenda_emergency_stop_callback(lambda: calls.append("emergency"))

        ui._dispatch_agenda_enable()
        ui._dispatch_agenda_soft_stop()
        ui._dispatch_agenda_emergency_stop()

        assert calls == ["enable", "soft", "emergency"]

    def test_agenda_status_updates_operator_label(self, ui_state, dispatcher, mock_widgets):
        ui = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        ui.set_widgets(mock_widgets)

        ui.set_agenda_status("Kira está esperando PTT", "#ffaa00")

        assert mock_widgets["lbl_kira_agenda_state"].text == "Kira está esperando PTT"
        assert mock_widgets["lbl_kira_agenda_state"].text_color == "#ffaa00"


# ---------------------------------------------------------------------------
# Test: Initialization
# ---------------------------------------------------------------------------


class TestStreamAdminUIInit:
    """Tests for StreamAdminUI construction and initialization."""

    def test_init_with_all_deps(self, ui_state, dispatcher, mock_stream_admin, mock_smart_agg, mock_motor_ia):
        """Construction with all dependencies succeeds."""
        ui = StreamAdminUI(
            ui_state=ui_state,
            dispatcher=dispatcher,
            stream_admin=mock_stream_admin,
            smart_agg=mock_smart_agg,
            motor_ia=mock_motor_ia,
        )
        assert ui._stream_admin is mock_stream_admin
        assert ui._smart_agg is mock_smart_agg
        assert ui._motor_ia is mock_motor_ia
        assert ui.chat_connected is False
        assert ui.last_metadata == {}
        assert ui.sim_round == 0
        assert ui.chat_users == {}

    def test_init_with_defaults(self, ui_state, dispatcher):
        """Construction with minimal deps uses defaults."""
        ui = StreamAdminUI(
            ui_state=ui_state,
            dispatcher=dispatcher,
        )
        assert ui._stream_admin is None
        assert ui._smart_agg is None
        assert ui._motor_ia is None
        assert ui._widgets == {}

    def test_init_custom_log_callback(self, ui_state, dispatcher):
        """Custom on_log callback is used."""
        calls = []
        ui = StreamAdminUI(
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=lambda msg: calls.append(msg),
        )
        ui._log("test message")
        assert calls == ["test message"]

    def test_init_custom_schedule(self, ui_state, dispatcher):
        """Custom schedule_ui_update callback is used."""
        scheduled = []
        ui = StreamAdminUI(
            ui_state=ui_state,
            dispatcher=dispatcher,
            schedule_ui_update=lambda fn: scheduled.append(fn),
        )
        fn = lambda: None
        ui._schedule_ui_update(fn)
        assert fn in scheduled

    def test_set_widgets(self, stream_admin_ui, mock_widgets):
        """set_widgets stores widget references."""
        assert stream_admin_ui._widget("lbl_stream_admin_status") is mock_widgets["lbl_stream_admin_status"]

    def test_set_widgets_merge(self, stream_admin_ui):
        """set_widgets merges with existing widgets."""
        stream_admin_ui.set_widgets({"new_widget": "value"})
        assert stream_admin_ui._widget("new_widget") == "value"
        assert stream_admin_ui._widget("lbl_stream_admin_status") is not None

    def test_widget_missing_returns_none(self, stream_admin_ui):
        """_widget returns None for unknown names."""
        assert stream_admin_ui._widget("nonexistent") is None

    def test_set_stream_admin(self, stream_admin_ui):
        """set_stream_admin updates the reference."""
        new_admin = MagicMock()
        stream_admin_ui.set_stream_admin(new_admin)
        assert stream_admin_ui._stream_admin is new_admin

    def test_set_smart_agg(self, stream_admin_ui):
        """set_smart_agg updates the reference."""
        new_agg = MagicMock()
        stream_admin_ui.set_smart_agg(new_agg)
        assert stream_admin_ui._smart_agg is new_agg

    def test_set_motor_ia(self, stream_admin_ui):
        """set_motor_ia updates the reference."""
        new_motor = MagicMock()
        stream_admin_ui.set_motor_ia(new_motor)
        assert stream_admin_ui._motor_ia is new_motor

    def test_set_smart_agg_defaults(self, stream_admin_ui):
        """set_smart_agg_defaults stores defaults."""
        stream_admin_ui.set_smart_agg_defaults({"threshold": 2.0, "cooldown": 30.0})
        assert stream_admin_ui._smart_agg_default_activity == {"threshold": 2.0, "cooldown": 30.0}


# ---------------------------------------------------------------------------
# Test: Properties
# ---------------------------------------------------------------------------


class TestStreamAdminUIProperties:
    """Tests for property accessors."""

    def test_chat_connected_getter_setter(self, stream_admin_ui):
        """chat_connected property works."""
        assert stream_admin_ui.chat_connected is False
        stream_admin_ui.chat_connected = True
        assert stream_admin_ui.chat_connected is True

    def test_sim_round_getter_setter(self, stream_admin_ui):
        """sim_round property works."""
        assert stream_admin_ui.sim_round == 0
        stream_admin_ui.sim_round = 5
        assert stream_admin_ui.sim_round == 5

    def test_last_metadata_initial(self, stream_admin_ui):
        """last_metadata starts empty."""
        assert stream_admin_ui.last_metadata == {}

    def test_chat_users_initial(self, stream_admin_ui):
        """chat_users starts empty."""
        assert stream_admin_ui.chat_users == {}


# ---------------------------------------------------------------------------
# Test: OAuth / Connection
# ---------------------------------------------------------------------------


class TestStreamAdminUIOAuth:
    """Tests for OAuth and connection flows."""

    def test_connect_read_only(self, stream_admin_ui):
        """connect with request_write=False calls connect_func."""
        connect_func = MagicMock()
        stream_admin_ui.connect(False, connect_func)
        # Task runs in thread, give it time
        time.sleep(0.1)
        connect_func.assert_called_once_with(False)

    def test_connect_write(self, stream_admin_ui):
        """connect with request_write=True calls connect_func."""
        connect_func = MagicMock()
        stream_admin_ui.connect(True, connect_func)
        time.sleep(0.1)
        connect_func.assert_called_once_with(True)

    def test_connect_no_admin(self, ui_state, dispatcher):
        """connect does nothing without stream_admin."""
        ui = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        connect_func = MagicMock()
        ui.connect(False, connect_func)
        time.sleep(0.1)
        connect_func.assert_not_called()

    def test_save_oauth_client(self, stream_admin_ui):
        """save_oauth_client calls save_func with credentials."""
        save_func = MagicMock()
        stream_admin_ui.save_oauth_client("my-id", "my-secret", save_func)
        time.sleep(0.1)
        save_func.assert_called_once_with("my-id", "my-secret")

    def test_populate_oauth_fields_no_admin(self, stream_admin_ui):
        """populate_oauth_fields does nothing without admin."""
        stream_admin_ui._stream_admin = None
        stream_admin_ui.populate_oauth_fields()
        entry = stream_admin_ui._widget("entry_stream_client_id")
        assert entry.get() == ""

    def test_populate_oauth_fields_with_config(self, stream_admin_ui, mock_stream_admin):
        """populate_oauth_fields fills entry from config."""
        mock_stream_admin.get_oauth_client_config.return_value = {
            "client_id": "test-id.apps.googleusercontent.com",
            "has_client_secret": True,
        }
        stream_admin_ui.populate_oauth_fields()
        entry = stream_admin_ui._widget("entry_stream_client_id")
        assert "test-id" in entry.get()

    def test_populate_oauth_fields_placeholder_secret(self, stream_admin_ui, mock_stream_admin):
        """populate_oauth_fields updates secret placeholder when secret exists."""
        mock_stream_admin.get_oauth_client_config.return_value = {
            "client_id": "test-id",
            "has_client_secret": True,
        }
        stream_admin_ui.populate_oauth_fields()
        entry_secret = stream_admin_ui._widget("entry_stream_client_secret")
        assert "guardado" in entry_secret.placeholder_text

    def test_populate_oauth_fields_no_entry_widget(self, stream_admin_ui):
        """populate_oauth_fields handles missing entry widget."""
        stream_admin_ui._widgets.pop("entry_stream_client_id", None)
        stream_admin_ui.populate_oauth_fields()  # should not raise

    def test_disconnect(self, stream_admin_ui):
        """disconnect calls disconnect_func."""
        disconnect_func = MagicMock()
        stream_admin_ui.disconnect(disconnect_func)
        time.sleep(0.1)
        disconnect_func.assert_called_once()

    def test_revoke_write(self, stream_admin_ui, mock_stream_admin):
        """revoke_write calls revoke_write_mode on admin."""
        stream_admin_ui.revoke_write()
        mock_stream_admin.revoke_write_mode.assert_called_once()
        assert any("revocada" in m for m in stream_admin_ui._log_messages)

    def test_revoke_write_no_admin(self, ui_state, dispatcher):
        """revoke_write does nothing without admin."""
        ui = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        ui.revoke_write()  # should not raise


# ---------------------------------------------------------------------------
# Test: Metadata
# ---------------------------------------------------------------------------


class TestStreamAdminUIMetadata:
    """Tests for metadata management."""

    def test_refresh_metadata(self, stream_admin_ui):
        """refresh_metadata calls refresh_func."""
        refresh_func = MagicMock()
        stream_admin_ui.refresh_metadata(refresh_func)
        time.sleep(0.1)
        refresh_func.assert_called_once()

    def test_suggest_metadata(self, stream_admin_ui):
        """suggest_metadata passes context to suggest_func."""
        suggest_func = MagicMock()
        stream_admin_ui.suggest_metadata("chat context", suggest_func)
        time.sleep(0.1)
        suggest_func.assert_called_once_with("chat context")

    def test_apply_metadata_no_write(self, stream_admin_ui, mock_stream_admin):
        """apply_metadata does nothing when write is not available."""
        mock_stream_admin.status.return_value = {
            "write_enabled": False,
            "write_scope_active": False,
        }
        stream_admin_ui.apply_metadata({"title": "test"})
        mock_stream_admin.apply_metadata.assert_not_called()

    def test_apply_metadata_with_pending(self, stream_admin_ui, mock_stream_admin):
        """apply_metadata uses apply_pending_action when pending exists."""
        mock_stream_admin.status.return_value = {
            "write_enabled": True,
            "write_scope_active": True,
        }
        mock_stream_admin.pending_action = {"type": "metadata_update"}
        stream_admin_ui.apply_metadata({"title": "new title"})
        time.sleep(0.1)
        mock_stream_admin.apply_pending_action.assert_called_once()

    def test_apply_metadata_no_pending(self, stream_admin_ui, mock_stream_admin):
        """apply_metadata uses apply_metadata when no pending."""
        mock_stream_admin.status.return_value = {
            "write_enabled": True,
            "write_scope_active": True,
        }
        mock_stream_admin.pending_action = None
        stream_admin_ui.apply_metadata({"title": "new title"})
        time.sleep(0.1)
        mock_stream_admin.apply_metadata.assert_called_once()

    def test_reject_pending(self, stream_admin_ui):
        """reject_pending calls reject_func."""
        reject_func = MagicMock()
        stream_admin_ui.reject_pending(reject_func)
        time.sleep(0.1)
        reject_func.assert_called_once()

    def test_metadata_payload_from_ui(self, stream_admin_ui):
        """metadata_payload_from_ui builds correct payload."""
        stream_admin_ui._widget("entry_stream_title").insert(0, "My Stream")
        stream_admin_ui._widget("entry_stream_category").insert(0, "20")
        stream_admin_ui._widget("entry_stream_tags").insert(0, "gaming, live")
        stream_admin_ui._widget("text_stream_description").insert("1.0", "Description here")

        payload = stream_admin_ui.metadata_payload_from_ui()
        assert payload["title"] == "My Stream"
        assert payload["category_id"] == "20"
        assert payload["tags"] == ["gaming", "live"]
        assert payload["description"] == "Description here"

    def test_metadata_payload_empty(self, stream_admin_ui):
        """metadata_payload_from_ui handles empty fields."""
        payload = stream_admin_ui.metadata_payload_from_ui()
        assert payload["title"] == ""
        assert payload["tags"] == []

    def test_populate_metadata(self, stream_admin_ui):
        """populate_metadata fills all metadata fields."""
        metadata = {
            "title": "Test Stream",
            "category_id": "20",
            "tags": ["gaming", "live"],
            "description": "A test stream",
            "video_id": "abc123",
            "status": "live",
        }
        stream_admin_ui.populate_metadata(metadata)

        assert stream_admin_ui._widget("entry_stream_title").get() == "Test Stream"
        assert stream_admin_ui._widget("entry_stream_category").get() == "20"
        assert "gaming, live" in stream_admin_ui._widget("entry_stream_tags").get()
        assert stream_admin_ui._widget("text_stream_description").get("1.0") == "A test stream"

    def test_populate_metadata_label_update(self, stream_admin_ui):
        """populate_metadata updates the metadata state label."""
        metadata = {"video_id": "abc123", "status": "live"}
        stream_admin_ui.populate_metadata(metadata)
        lbl = stream_admin_ui._widget("lbl_stream_metadata_state")
        assert "abc123" in lbl.text
        assert lbl.text_color == "#44cc66"

    def test_populate_metadata_no_video(self, stream_admin_ui):
        """populate_metadata shows warning color when no video_id."""
        metadata = {"video_id": "", "status": "unknown"}
        stream_admin_ui.populate_metadata(metadata)
        lbl = stream_admin_ui._widget("lbl_stream_metadata_state")
        assert lbl.text_color == "#ffaa00"

    def test_populate_metadata_none(self, stream_admin_ui):
        """populate_metadata handles None metadata."""
        stream_admin_ui.populate_metadata(None)  # should not raise

    def test_on_metadata_stores_metadata(self, stream_admin_ui):
        """on_metadata updates _last_metadata."""
        metadata = {"video_id": "xyz", "title": "Test"}
        stream_admin_ui.on_metadata(metadata)
        assert stream_admin_ui.last_metadata == metadata

    def test_on_metadata_schedules_widget_updates(self, ui_state, dispatcher, mock_widgets):
        """on_metadata must schedule widget mutation on the UI thread."""
        scheduled = []
        ui = StreamAdminUI(
            ui_state=ui_state,
            dispatcher=dispatcher,
            schedule_ui_update=lambda fn: scheduled.append(fn),
        )
        ui.set_widgets(mock_widgets)

        metadata = {"video_id": "xyz", "title": "Scheduled"}
        ui.on_metadata(metadata)

        assert ui.last_metadata == metadata
        assert len(scheduled) == 1
        assert ui._widget("entry_stream_title").get() == ""

        scheduled[0]()

        assert ui._widget("entry_stream_title").get() == "Scheduled"


# ---------------------------------------------------------------------------
# Test: Moderation
# ---------------------------------------------------------------------------


class TestStreamAdminUIModeration:
    """Tests for moderation functionality."""

    def test_propose_high_risk(self, stream_admin_ui):
        """propose_high_risk calls propose_func with correct args."""
        propose_func = MagicMock()
        stream_admin_ui.propose_high_risk("timeout", "UC123", "spam", propose_func)
        time.sleep(0.1)
        propose_func.assert_called_once_with("timeout", "UC123", "spam", 300)

    def test_track_chat_user(self, stream_admin_ui):
        """track_chat_user adds user to chat_users."""
        message = {
            "author_channel_id": "UC123",
            "user": "TestUser",
            "text": "Hello chat",
            "is_owner": False,
            "is_moderator": True,
            "is_member": False,
        }
        stream_admin_ui.track_chat_user(message)
        assert "UC123" in stream_admin_ui.chat_users
        user_data = stream_admin_ui.chat_users["UC123"]
        assert user_data["user"] == "TestUser"
        assert user_data["count"] == 1
        assert user_data["is_moderator"] is True

    def test_track_chat_user_duplicate(self, stream_admin_ui):
        """track_chat_user increments count for existing user."""
        message = {"channel_id": "UC123", "user": "TestUser", "text": "msg1"}
        stream_admin_ui.track_chat_user(message)
        stream_admin_ui.track_chat_user(message)
        assert stream_admin_ui.chat_users["UC123"]["count"] == 2

    def test_track_chat_user_no_channel_id(self, stream_admin_ui):
        """track_chat_user ignores messages without channel_id."""
        message = {"user": "TestUser", "text": "Hello"}
        stream_admin_ui.track_chat_user(message)
        assert stream_admin_ui.chat_users == {}

    def test_default_mod_reason_with_message(self, stream_admin_ui):
        """default_mod_reason uses last_message."""
        item = {"last_message": "This is a spam message"}
        reason = stream_admin_ui.default_mod_reason(item)
        assert "Revisado desde Stream Admin" in reason
        assert "spam message" in reason

    def test_default_mod_reason_long_message(self, stream_admin_ui):
        """default_mod_reason truncates long messages."""
        item = {"last_message": "A" * 100}
        reason = stream_admin_ui.default_mod_reason(item)
        assert len(reason) < 110
        assert "..." in reason

    def test_default_mod_reason_no_message(self, stream_admin_ui):
        """default_mod_reason uses fallback when no message."""
        item = {"last_message": ""}
        reason = stream_admin_ui.default_mod_reason(item)
        assert "Moderación manual" in reason

    def test_moderate_user_from_list_no_write(self, stream_admin_ui, mock_stream_admin):
        """moderate_user_from_list does nothing without write."""
        mock_stream_admin.status.return_value = {
            "write_enabled": False,
            "write_scope_active": False,
        }
        moderate_func = MagicMock()
        stream_admin_ui.moderate_user_from_list("ban", "UC123", "User", "reason", moderate_func)
        moderate_func.assert_not_called()

    def test_moderate_user_from_list_no_channel_id(self, stream_admin_ui, mock_stream_admin):
        """moderate_user_from_list does nothing without channel_id."""
        mock_stream_admin.status.return_value = {
            "write_enabled": True,
            "write_scope_active": True,
        }
        moderate_func = MagicMock()
        stream_admin_ui.moderate_user_from_list("ban", "", "User", "reason", moderate_func)
        moderate_func.assert_not_called()

    def test_moderate_user_from_list_success(self, stream_admin_ui, mock_stream_admin):
        """moderate_user_from_list calls moderate_func."""
        mock_stream_admin.status.return_value = {
            "write_enabled": True,
            "write_scope_active": True,
        }
        moderate_func = MagicMock()
        stream_admin_ui.moderate_user_from_list("timeout", "UC123", "User", "spam", moderate_func)
        time.sleep(0.1)
        moderate_func.assert_called_once_with("timeout", "UC123", "spam", 300)


# ---------------------------------------------------------------------------
# Test: Runtime Settings
# ---------------------------------------------------------------------------


class TestStreamAdminUIRuntimeSettings:
    """Tests for runtime settings application."""

    def test_apply_runtime_settings(self, stream_admin_ui, mock_stream_admin):
        """apply_runtime_settings updates admin config."""
        stream_admin_ui._widget("switch_stream_mod_enabled").select()
        stream_admin_ui._widget("switch_stream_announce").select()
        stream_admin_ui._widget("switch_stream_chat_enabled").select()

        stream_admin_ui.apply_runtime_settings(log=False)

        assert mock_stream_admin.config["moderation"]["enabled"] is True
        assert mock_stream_admin.config["moderation"]["announce_actions_to_chat"] is True
        assert mock_stream_admin.config["chat"]["allow_kira_chat_messages"] is True
        assert mock_stream_admin.moderation.enabled is True

    def test_apply_runtime_settings_default_mode(self, stream_admin_ui, mock_stream_admin):
        """apply_runtime_settings uses default mode when combo not set."""
        stream_admin_ui.apply_runtime_settings(log=False)
        assert mock_stream_admin.config["moderation"]["mode"] == "alerts_only"

    def test_apply_runtime_settings_no_admin(self, ui_state, dispatcher):
        """apply_runtime_settings does nothing without admin."""
        ui = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        ui.apply_runtime_settings()  # should not raise


# ---------------------------------------------------------------------------
# Test: Chat
# ---------------------------------------------------------------------------


class TestStreamAdminUIChat:
    """Tests for chat functionality."""

    def test_send_chat_no_write(self, stream_admin_ui, mock_stream_admin):
        """send_chat does nothing without write."""
        mock_stream_admin.status.return_value = {
            "write_enabled": False,
            "write_scope_active": False,
        }
        send_func = MagicMock()
        stream_admin_ui.send_chat("Hello", send_func)
        send_func.assert_not_called()

    def test_send_chat_empty_message(self, stream_admin_ui, mock_stream_admin):
        """send_chat does nothing with empty message."""
        mock_stream_admin.status.return_value = {
            "write_enabled": True,
            "write_scope_active": True,
        }
        send_func = MagicMock()
        stream_admin_ui.send_chat("", send_func)
        send_func.assert_not_called()

    def test_send_chat_success(self, stream_admin_ui, mock_stream_admin):
        """send_chat calls send_func with message."""
        mock_stream_admin.status.return_value = {
            "write_enabled": True,
            "write_scope_active": True,
        }
        send_func = MagicMock()
        stream_admin_ui.send_chat("Hello chat!", send_func)
        time.sleep(0.1)
        send_func.assert_called_once_with("Hello chat!")

    def test_toggle_small_stream_on(self, stream_admin_ui, mock_smart_agg):
        """toggle_small_stream sets low activity limits."""
        stream_admin_ui._widget("switch_stream_small").select()
        stream_admin_ui.toggle_small_stream()
        mock_smart_agg.set_activity_limits.assert_called_once_with(
            threshold_per_second=0.2, cooldown_seconds=20.0, reset=True
        )
        mock_smart_agg.set_spam_limits.assert_called_once_with(max_messages_per_user=30)

    def test_toggle_small_stream_off(self, stream_admin_ui, mock_smart_agg):
        """toggle_small_stream restores default limits."""
        stream_admin_ui.set_smart_agg_defaults({"threshold": 1.5, "cooldown": 60.0})
        stream_admin_ui.toggle_small_stream()
        mock_smart_agg.set_activity_limits.assert_called_once_with(
            threshold_per_second=1.5, cooldown_seconds=60.0, reset=True
        )

    def test_toggle_small_stream_no_agg(self, ui_state, dispatcher):
        """toggle_small_stream does nothing without smart_agg."""
        ui = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        ui.toggle_small_stream()  # should not raise

    def test_connect_chat(self, stream_admin_ui, mock_smart_agg):
        """connect_chat starts session and sets flags."""
        connect_func = MagicMock()
        stream_admin_ui.connect_chat("vid123", "chat123", connect_func)
        assert stream_admin_ui.chat_connected is True
        assert stream_admin_ui._seen_chat_ids == set()
        mock_smart_agg.start_session.assert_called_once_with("youtube", "vid123")
        connect_func.assert_called_once_with("vid123", "chat123")

    def test_disconnect_chat(self, stream_admin_ui, mock_smart_agg):
        """disconnect_chat ends session and clears flags."""
        stream_admin_ui.chat_connected = True
        disconnect_func = MagicMock()
        stream_admin_ui.disconnect_chat(disconnect_func)
        assert stream_admin_ui.chat_connected is False
        mock_smart_agg.end_session.assert_called_once()
        disconnect_func.assert_called_once()

    def test_disconnect_chat_agg_error(self, stream_admin_ui, mock_smart_agg):
        """disconnect_chat handles end_session error."""
        mock_smart_agg.end_session.side_effect = Exception("fail")
        stream_admin_ui.chat_connected = True
        disconnect_func = MagicMock()
        stream_admin_ui.disconnect_chat(disconnect_func)  # should not raise
        disconnect_func.assert_called_once()


# ---------------------------------------------------------------------------
# Test: State Handlers
# ---------------------------------------------------------------------------


class TestStreamAdminUIStateHandlers:
    """Tests for on_state, on_pending, on_analytics handlers."""

    def test_on_state_connected_read_only(self, stream_admin_ui, mock_stream_admin):
        """on_state updates UI for connected read-only state."""
        state = {
            "connected": True,
            "write_enabled": False,
            "write_scope_active": False,
            "oauth_client_configured": True,
            "mode": "read_only",
            "account": {"title": "MyChannel"},
            "pending_action": None,
        }
        stream_admin_ui.on_state(state)
        lbl = stream_admin_ui._widget("lbl_stream_admin_status")
        assert "conectado" in lbl.text
        assert "MyChannel" in lbl.text

    def test_on_state_connected_write(self, stream_admin_ui, mock_stream_admin):
        """on_state shows write mode with remaining time."""
        mock_stream_admin._write_activated_at = time.time() - 60
        state = {
            "connected": True,
            "write_enabled": True,
            "write_scope_active": True,
            "oauth_client_configured": True,
            "mode": "write",
            "account": {"title": "MyChannel"},
            "pending_action": None,
        }
        stream_admin_ui.on_state(state)
        lbl = self._get_widget_text(stream_admin_ui, "lbl_stream_admin_status")
        assert "write" in lbl

    def _get_widget_text(self, ui, name):
        widget = ui._widget(name)
        return widget.text if widget else ""

    def test_on_state_disconnected(self, stream_admin_ui):
        """on_state shows disconnected state."""
        state = {
            "connected": False,
            "write_enabled": False,
            "write_scope_active": False,
            "oauth_client_configured": False,
            "mode": "read_only",
            "account": {"title": ""},
            "pending_action": None,
        }
        stream_admin_ui.on_state(state)
        lbl = stream_admin_ui._widget("lbl_stream_admin_status")
        assert "desconectado" in lbl.text
        assert lbl.text_color == "#aaaaaa"

    def test_on_state_oauth_pill_colors(self, stream_admin_ui):
        """on_state sets correct OAuth pill colors."""
        state = {
            "connected": True,
            "write_enabled": True,
            "write_scope_active": True,
            "mode": "write",
            "account": {"title": "Test"},
            "oauth_client_configured": True,
            "pending_action": None,
        }
        stream_admin_ui.on_state(state)
        pill = stream_admin_ui._widget("lbl_oauth_status_pill")
        assert pill.fg_color == "#5f3a1f"  # amber for write

    def test_on_state_oauth_pill_read_color(self, stream_admin_ui):
        """on_state sets read-only OAuth pill color."""
        state = {
            "connected": True,
            "write_enabled": False,
            "write_scope_active": False,
            "mode": "read_only",
            "account": {"title": "Test"},
            "oauth_client_configured": True,
            "pending_action": None,
        }
        stream_admin_ui.on_state(state)
        pill = stream_admin_ui._widget("lbl_oauth_status_pill")
        assert pill.fg_color == "#1f3f6f"  # blue for read

    def test_on_state_oauth_pill_disconnected_color(self, stream_admin_ui):
        """on_state sets disconnected OAuth pill color."""
        state = {
            "connected": False,
            "mode": "read_only",
            "account": {"title": "Test"},
            "oauth_client_configured": False,
            "pending_action": None,
        }
        stream_admin_ui.on_state(state)
        pill = stream_admin_ui._widget("lbl_oauth_status_pill")
        assert pill.fg_color == "#1b2633"

    def test_on_pending_none(self, stream_admin_ui):
        """on_pending clears pending display when None."""
        stream_admin_ui.on_pending(None)
        lbl = stream_admin_ui._widget("lbl_stream_pending")
        assert "ninguna" in lbl.text

    def test_on_pending_metadata_update(self, stream_admin_ui):
        """on_pending populates metadata for pending metadata_update."""
        pending = {
            "type": "metadata_update",
            "payload": {
                "title": "Suggested Title",
                "category_id": "20",
                "description": "Suggested desc",
                "tags": ["new"],
            },
        }
        stream_admin_ui.on_pending(pending)
        lbl = stream_admin_ui._widget("lbl_stream_pending")
        assert "metadata_update" in lbl.text

    def test_on_pending_generic(self, stream_admin_ui):
        """on_pending shows generic pending action."""
        pending = {
            "type": "moderation",
            "payload": {"action": "ban"},
        }
        stream_admin_ui.on_pending(pending)
        lbl = stream_admin_ui._widget("lbl_stream_pending")
        assert "moderation" in lbl.text

    def test_on_analytics(self, stream_admin_ui):
        """on_analytics updates analytics label."""
        snapshot = {
            "viewers": 150,
            "uptime_seconds": 3600,
            "chat_rate_avg": 2.5,
            "last_vibe_dominant": "excitement",
            "last_vibe_temperature": 75,
        }
        stream_admin_ui.on_analytics(snapshot)
        lbl = stream_admin_ui._widget("lbl_stream_analytics")
        assert "150" in lbl.text
        assert "60m" in lbl.text
        assert "2.50" in lbl.text
        assert "excitement" in lbl.text

    def test_on_analytics_no_viewers(self, stream_admin_ui):
        """on_analytics shows N/A for missing viewers."""
        snapshot = {"viewers": None, "uptime_seconds": 0, "chat_rate_avg": 0.0}
        stream_admin_ui.on_analytics(snapshot)
        lbl = stream_admin_ui._widget("lbl_stream_analytics")
        assert "N/A" in lbl.text


# ---------------------------------------------------------------------------
# Test: Log Handling
# ---------------------------------------------------------------------------


class TestStreamAdminUILog:
    """Tests for log message handling."""

    def test_on_log_strips_prefix(self, stream_admin_ui):
        """on_log strips [StreamAdmin] prefix for action log."""
        stream_admin_ui.on_log("[StreamAdmin] Test message")
        assert any("Test message" in m for m in stream_admin_ui._log_messages)

    def test_on_log_appends_to_text_widget(self, stream_admin_ui):
        """on_log appends to text_stream_admin_log widget."""
        stream_admin_ui.on_log("[StreamAdmin] Log entry")
        text_widget = stream_admin_ui._widget("text_stream_admin_log")
        assert "Log entry" in text_widget._content or "[StreamAdmin] Log entry" in text_widget._content

    def test_append_log(self, stream_admin_ui):
        """_append_log adds message to text widget."""
        text_widget = stream_admin_ui._widget("text_stream_admin_log")
        stream_admin_ui._append_log(text_widget, "Test log")
        assert "Test log" in text_widget._content


# ---------------------------------------------------------------------------
# Test: RF3 Event Ingestion
# ---------------------------------------------------------------------------


class TestStreamAdminUIRF3Events:
    """Tests for RF3 event ingestion and silent context."""

    def test_ingest_rf3_event_no_admin(self, ui_state, dispatcher):
        """ingest_rf3_event does nothing without admin."""
        ui = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        ui.ingest_rf3_event("chat_message", {"text": "hello"})  # should not raise

    def test_ingest_rf3_event_success(self, stream_admin_ui, mock_stream_admin):
        """ingest_rf3_event calls ingest and may inject context."""
        mock_stream_admin.analytics_context_if_due.return_value = None
        stream_admin_ui.ingest_rf3_event("chat_message", {"text": "hello"})
        mock_stream_admin.ingest_rf3_event.assert_called_once_with("chat_message", {"text": "hello"})

    def test_ingest_rf3_event_with_context(self, stream_admin_ui, mock_stream_admin, mock_motor_ia):
        """ingest_rf3_event injects silent context when available."""
        mock_stream_admin.analytics_context_if_due.return_value = {"vibe": "excited"}
        stream_admin_ui.ingest_rf3_event("analytics", {})
        mock_stream_admin.ingest_rf3_event.assert_called_once()
        assert len(mock_motor_ia.historial) == 1
        assert "Contexto administrativo silencioso" in mock_motor_ia.historial[0]["content"]

    def test_ingest_rf3_event_error(self, stream_admin_ui, mock_stream_admin):
        """ingest_rf3_event logs error on failure."""
        mock_stream_admin.ingest_rf3_event.side_effect = Exception("fail")
        stream_admin_ui.ingest_rf3_event("bad_event", {})
        assert any("bad_event" in m for m in stream_admin_ui._log_messages)

    def test_inject_silent_context(self, stream_admin_ui, mock_motor_ia):
        """inject_silent_context adds to motor_ia historial."""
        stream_admin_ui.inject_silent_context("test context")
        assert len(mock_motor_ia.historial) == 1
        assert "test context" in mock_motor_ia.historial[0]["content"]

    def test_inject_silent_context_no_motor(self, ui_state, dispatcher):
        """inject_silent_context does nothing without motor_ia."""
        ui = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        ui.inject_silent_context("test")  # should not raise

    def test_inject_silent_context_motor_no_historial(self, ui_state, dispatcher):
        """inject_silent_context handles motor without historial."""
        motor = MagicMock()
        del motor.historial
        ui = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher, motor_ia=motor)
        ui.inject_silent_context("test")  # should not raise


# ---------------------------------------------------------------------------
# Test: Write Permission Checks
# ---------------------------------------------------------------------------


class TestStreamAdminUIWriteChecks:
    """Tests for _can_write and related write permission logic."""

    def test_can_write_true(self, stream_admin_ui, mock_stream_admin):
        """_can_write returns True when write enabled and active."""
        mock_stream_admin.status.return_value = {
            "write_enabled": True,
            "write_scope_active": True,
        }
        assert stream_admin_ui._can_write() is True

    def test_can_write_false_disabled(self, stream_admin_ui, mock_stream_admin):
        """_can_write returns False when write disabled."""
        mock_stream_admin.status.return_value = {
            "write_enabled": False,
            "write_scope_active": True,
        }
        assert stream_admin_ui._can_write() is False

    def test_can_write_false_not_active(self, stream_admin_ui, mock_stream_admin):
        """_can_write returns False when scope not active."""
        mock_stream_admin.status.return_value = {
            "write_enabled": True,
            "write_scope_active": False,
        }
        assert stream_admin_ui._can_write() is False

    def test_can_write_no_admin(self, ui_state, dispatcher):
        """_can_write returns False without admin."""
        ui = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        assert ui._can_write() is False

    def test_can_write_status_error(self, stream_admin_ui, mock_stream_admin):
        """_can_write returns False when status() raises."""
        mock_stream_admin.status.side_effect = Exception("fail")
        assert stream_admin_ui._can_write() is False


# ---------------------------------------------------------------------------
# Test: Control Synchronization
# ---------------------------------------------------------------------------


class TestStreamAdminUIControlSync:
    """Tests for _sync_controls widget state management."""

    def test_sync_controls_connected(self, stream_admin_ui):
        """_sync_controls enables connection-related buttons."""
        state = {"connected": True, "write_enabled": False, "write_scope_active": False, "pending_action": None}
        stream_admin_ui._sync_controls(state)
        assert stream_admin_ui._widget("btn_stream_disconnect").state == "normal"
        assert stream_admin_ui._widget("btn_stream_read_metadata").state == "normal"

    def test_sync_controls_write_ready(self, stream_admin_ui):
        """_sync_controls enables write-related buttons."""
        state = {"connected": True, "write_enabled": True, "write_scope_active": True, "pending_action": None}
        stream_admin_ui._sync_controls(state)
        assert stream_admin_ui._widget("btn_stream_apply_metadata").state == "normal"
        assert stream_admin_ui._widget("btn_stream_send_chat").state == "normal"
        assert stream_admin_ui._widget("btn_stream_propose_timeout").state == "normal"

    def test_sync_controls_pending(self, stream_admin_ui):
        """_sync_controls enables reject button when pending."""
        state = {"connected": True, "write_enabled": False, "write_scope_active": False, "pending_action": {"type": "test"}}
        stream_admin_ui._sync_controls(state)
        assert stream_admin_ui._widget("btn_stream_reject_pending").state == "normal"

    def test_sync_controls_deselect_switches_no_write(self, stream_admin_ui):
        """_sync_controls deselects chat/announce switches when no write."""
        stream_admin_ui._widget("switch_stream_chat_enabled").select()
        stream_admin_ui._widget("switch_stream_announce").select()
        state = {"connected": True, "write_enabled": False, "write_scope_active": False, "pending_action": None}
        stream_admin_ui._sync_controls(state)
        assert stream_admin_ui._widget("switch_stream_chat_enabled").get() is False
        assert stream_admin_ui._widget("switch_stream_announce").get() is False


# ---------------------------------------------------------------------------
# Test: Cleanup
# ---------------------------------------------------------------------------


class TestStreamAdminUICleanup:
    """Tests for cleanup and lifecycle."""

    def test_cleanup_disconnects_chat(self, stream_admin_ui):
        """cleanup sets chat_connected to False."""
        stream_admin_ui.chat_connected = True
        stream_admin_ui.cleanup()
        assert stream_admin_ui.chat_connected is False

    def test_cleanup_sets_chat_stop(self, stream_admin_ui):
        """cleanup sets the chat stop event."""
        stop_event = MagicMock()
        stream_admin_ui._chat_stop = stop_event
        stream_admin_ui.cleanup()
        stop_event.set.assert_called_once()
        assert stream_admin_ui._chat_stop is None

    def test_cleanup_no_chat_stop(self, stream_admin_ui):
        """cleanup handles missing chat_stop gracefully."""
        stream_admin_ui._chat_stop = None
        stream_admin_ui.cleanup()  # should not raise


# ---------------------------------------------------------------------------
# Test: Task Runner
# ---------------------------------------------------------------------------


class TestStreamAdminUITaskRunner:
    """Tests for _run_task background execution."""

    def test_run_task_no_admin(self, ui_state, dispatcher):
        """_run_task does nothing without stream_admin."""
        ui = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
        ui._run_task("test", lambda: None)  # should not raise

    def test_run_task_success(self, stream_admin_ui):
        """_run_task executes func in background thread."""
        result_holder = []
        stream_admin_ui._run_task("test", lambda: result_holder.append("done"))
        time.sleep(0.1)
        assert "done" in result_holder

    def test_run_task_exception_logged(self, stream_admin_ui):
        """_run_task logs exceptions from func."""
        def failing_func():
            raise ValueError("test error")

        stream_admin_ui._run_task("failing", failing_func)
        time.sleep(0.1)
        assert any("failing falló" in m for m in stream_admin_ui._log_messages)

    def test_run_task_write_hint(self, stream_admin_ui):
        """_run_task adds write hint for scope errors."""
        def scope_error():
            raise Exception("Falta scope de escritura")

        stream_admin_ui._run_task("scope_test", scope_error)
        time.sleep(0.1)
        assert any("Reconectar Escritura" in m for m in stream_admin_ui._log_messages)
