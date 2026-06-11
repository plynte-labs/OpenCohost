"""Comprehensive tests for ui.advanced_panel.AdvancedModePanel.

Covers:
- AdvancedModePanel class initialization
- Log panel building (widgets created, correct defaults)
- Toggle advanced mode / logs visibility
- Log display (_print_log, _append_limited_textbox)
- Action logging (_log_accion)
- Log queue processing (_process_logs)
- Kira response panel updates
- Edge cases (None widgets, empty queue, compact mode interaction)
- UIState observer integration
- CallbackDispatcher usage
"""

from __future__ import annotations

import json
import os
import queue
import tempfile
import time
from unittest.mock import MagicMock, patch, call

import pytest

from opencohost.ui.state import UIState
from opencohost.ui.protocols import CallbackDispatcher


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
    return CallbackDispatcher(source="AdvancedModePanel")


@pytest.fixture()
def mock_ctk():
    """Mock customtkinter module with realistic widget behavior."""
    mock_module = MagicMock()

    class MockLabel:
        def __init__(self, master=None, **kwargs):
            self.master = master
            self.kwargs = kwargs
            self.text = kwargs.get("text", "")
            self.fg_color = kwargs.get("fg_color", "")
            self.text_color = kwargs.get("text_color", "")

        def configure(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

        def grid(self, **kwargs):
            self.grid_kwargs = kwargs

        def pack(self, **kwargs):
            self.pack_kwargs = kwargs

    class MockFrame:
        def __init__(self, master=None, **kwargs):
            self.master = master
            self.kwargs = kwargs
            self._grid_visible = True

        def configure(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

        def grid(self, **kwargs):
            self._grid_visible = True
            self.grid_kwargs = kwargs

        def grid_remove(self):
            self._grid_visible = False

        def grid_propagate(self, flag):
            self.propagate_flag = flag

        def grid_columnconfigure(self, idx, **kwargs):
            pass

        def grid_rowconfigure(self, idx, **kwargs):
            pass

    class MockTextbox:
        def __init__(self, master=None, **kwargs):
            self.master = master
            self.kwargs = kwargs
            self._content = ""
            self._state = kwargs.get("state", "normal")
            self._line_count = 0

        def configure(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)
                if key == "state":
                    self._state = value

        def insert(self, pos, text):
            if self._state != "disabled":
                self._content += text
                self._line_count += text.count("\n")

        def delete(self, start, end):
            if self._state != "disabled":
                if start == "1.0" and end == "end":
                    self._content = ""
                    self._line_count = 0
                elif end.endswith(".0"):
                    lines = self._content.split("\n")
                    line_num = int(end.split(".")[0])
                    self._content = "\n".join(lines[line_num:])
                    self._line_count = self._content.count("\n") + 1

        def get(self, start, end):
            return self._content

        def index(self, pos):
            if pos == "end-1c":
                return f"{self._line_count}.0"
            return pos

        def see(self, pos):
            pass

        def pack(self, **kwargs):
            self.pack_kwargs = kwargs

    class MockTabview:
        def __init__(self, master=None, **kwargs):
            self.master = master
            self.kwargs = kwargs
            self._tabs = {}
            self._command = kwargs.get("command", None)

        def add(self, name):
            frame = MockFrame(master=self)
            self._tabs[name] = frame
            return frame

        def grid(self, **kwargs):
            self.grid_kwargs = kwargs

    class MockFont:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    mock_module.CTkLabel = MockLabel
    mock_module.CTkFrame = MockFrame
    mock_module.CTkTextbox = MockTextbox
    mock_module.CTkTabview = MockTabview
    mock_module.CTkFont = MockFont

    return mock_module


@pytest.fixture()
def mock_parent():
    """Mock parent frame."""
    parent = MagicMock()
    parent.grid = MagicMock()
    parent.grid_remove = MagicMock()
    parent.grid_rowconfigure = MagicMock()
    parent.configure = MagicMock()
    return parent


@pytest.fixture()
def log_queue():
    """Fresh log queue."""
    return queue.Queue()


# ---------------------------------------------------------------------------
# Helper to create AdvancedModePanel with mocked ctk
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _patch_panel_ctk(mock_ctk):
    """Force the panel module to use the mocked ctk regardless of import cache.

    When the full suite runs, collection imports app_shell (and thus
    advanced_panel) with the real customtkinter before any sys.modules patch
    applies. Real CTk widgets walk ``widget.master`` looking for a Tk root,
    which never terminates against MagicMock parents.
    """
    import opencohost.ui.advanced_panel as _panel_module
    with patch.object(_panel_module, "ctk", mock_ctk):
        yield


def _make_panel(mock_ctk, ui_state, dispatcher, log_queue=None, **kwargs):
    """Create an AdvancedModePanel with mocked customtkinter."""
    from opencohost.ui.advanced_panel import AdvancedModePanel

    parent = MagicMock()
    parent.grid = MagicMock()
    parent.grid_remove = MagicMock()
    parent.grid_rowconfigure = MagicMock()
    parent.configure = MagicMock()

    panel = AdvancedModePanel(
        parent_frame=parent,
        ui_state=ui_state,
        dispatcher=dispatcher,
        log_queue=log_queue or queue.Queue(),
        on_log_action=None,
        schedule_ui_update=lambda fn: fn(),
        **kwargs
    )
    return panel


# ===================================================================
# Initialization tests
# ===================================================================

class TestAdvancedModePanelInit:
    """Test AdvancedModePanel initialization."""

    def test_init_stores_ui_state(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            assert panel._ui_state is ui_state

    def test_init_stores_dispatcher(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            assert panel._dispatcher is dispatcher

    def test_init_stores_log_queue(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            assert panel._log_queue is log_queue

    def test_init_logs_panel_visible_false(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            assert panel._logs_panel_visible is False

    def test_init_widgets_are_none(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            assert panel.consola is None
            assert panel.consola_acciones is None
            assert panel.consola_youtube is None
            assert panel.text_stream_admin_log is None
            assert panel.tabview is None

    def test_init_observer_id_none(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            assert panel._observer_id is None

    def test_init_accepts_text_kira_response(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            kira_tb = MagicMock()
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue, text_kira_response=kira_tb)
            assert panel.text_kira_response is kira_tb


# ===================================================================
# Build panel tests
# ===================================================================

class TestBuildPanel:
    """Test advanced panel UI construction."""

    def test_build_creates_frame(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            assert panel._frame is not None

    def test_build_creates_consola(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            assert panel.consola is not None

    def test_build_creates_consola_acciones(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            assert panel.consola_acciones is not None

    def test_build_creates_consola_youtube(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            assert panel.consola_youtube is not None

    def test_build_creates_stream_admin_log(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            assert panel.text_stream_admin_log is not None

    def test_build_creates_tabview(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            assert panel.tabview is not None

    def test_build_subscribes_to_ui_state(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            assert panel._observer_id is not None

    def test_build_returns_frame(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            result = panel.build()
            assert result is panel._frame


# ===================================================================
# Toggle logs panel tests
# ===================================================================

class TestToggleLogsPanel:
    """Test logs panel visibility toggling."""

    def test_toggle_shows_panel_when_visible(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            panel._frame._grid_visible = False  # simulate hidden

            panel.set_logs_visible(True)

            assert panel._logs_panel_visible is True
            assert panel._frame._grid_visible is True

    def test_toggle_hides_panel_when_not_visible(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            # After build, panel is visible by default (grid() was called)
            panel._frame._grid_visible = True
            panel._logs_panel_visible = True  # simulate currently visible

            panel.set_logs_visible(False)

            assert panel._logs_panel_visible is False
            assert panel._frame._grid_visible is False

    def test_toggle_noop_when_already_visible(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            panel._frame._grid_visible = True
            panel._logs_panel_visible = True

            panel.set_logs_visible(True)

            assert panel._frame._grid_visible is True

    def test_toggle_noop_when_already_hidden(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            panel._frame._grid_visible = False
            panel._logs_panel_visible = False

            panel.set_logs_visible(False)

            assert panel._frame._grid_visible is False

    def test_toggle_noop_when_frame_not_built(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            # Don't call build()
            panel.set_logs_visible(True)
            # Should not raise

    def test_toggle_method_toggles_visibility(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            panel._frame._grid_visible = True
            panel._logs_panel_visible = True

            panel.toggle_logs()

            assert panel._logs_panel_visible is False
            assert panel._frame._grid_visible is False


# ===================================================================
# Log display tests
# ===================================================================

class TestPrintLog:
    """Test _print_log method."""

    def test_print_log_appends_to_consola(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            panel._logs_panel_visible = True  # simulate logs visible

            panel.print_log("[INFO] Test message")

            assert "[INFO] Test message" in panel.consola._content

    def test_print_log_skips_when_logs_hidden(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            panel._logs_panel_visible = False

            panel.print_log("[INFO] Hidden message")

            assert panel.consola._content == ""

    def test_print_log_skips_when_consola_none(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            # Don't call build() — consola is None
            panel._logs_panel_visible = True

            panel.print_log("[INFO] No consola")
            # Should not raise

    def test_print_log_updates_kira_response(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            kira_tb = mock_ctk.CTkTextbox()
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue, text_kira_response=kira_tb)
            panel.build()
            panel._logs_panel_visible = True

            panel.print_log("[Kira]: Hello world")

            assert "Hello world" in kira_tb._content

    def test_print_log_does_not_update_kira_for_non_kira(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            kira_tb = mock_ctk.CTkTextbox()
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue, text_kira_response=kira_tb)
            panel.build()
            panel._logs_panel_visible = True

            panel.print_log("[INFO] Not a kira message")

            # Kira response should be empty
            assert kira_tb._content == ""

    def test_print_log_handles_non_string(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            panel._logs_panel_visible = True

            panel.print_log(12345)

            assert "12345" in panel.consola._content


class TestAppendLimitedTextbox:
    """Test _append_limited_textbox method."""

    def test_append_adds_line(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()

            textbox = mock_ctk.CTkTextbox()
            panel.append_to_textbox(textbox, "line 1", max_lines=1000)

            assert "line 1" in textbox._content

    def test_append_trims_excess_lines(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()

            textbox = mock_ctk.CTkTextbox()
            for i in range(10):
                panel.append_to_textbox(textbox, f"line {i}", max_lines=5)

            # Should have trimmed to max_lines
            line_count = textbox._content.count("\n") + 1
            assert line_count <= 6  # max_lines + 1 for the new line

    def test_append_handles_disabled_state(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()

            textbox = mock_ctk.CTkTextbox(state="disabled")
            # Should not raise even if textbox is disabled
            panel.append_to_textbox(textbox, "test", max_lines=1000)


# ===================================================================
# Action log tests
# ===================================================================

class TestLogAction:
    """Test _log_accion method."""

    def test_log_action_appends_to_consola_acciones(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()

            panel.log_action("Test action")

            assert "Test action" in panel.consola_acciones._content

    def test_log_action_includes_timestamp(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()

            panel.log_action("Timestamped action")

            # Should contain a time-like pattern [HH:MM:SS]
            assert "[" in panel.consola_acciones._content
            assert "]" in panel.consola_acciones._content

    def test_log_action_saves_to_file(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            with tempfile.TemporaryDirectory() as tmpdir:
                acciones_file = os.path.join(tmpdir, "acciones.log")

                panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
                panel._acciones_file = acciones_file
                panel.build()

                panel.log_action("File action")

                assert os.path.exists(acciones_file)
                with open(acciones_file, "r", encoding="utf-8") as f:
                    content = f.read()
                assert "File action" in content

    def test_log_action_handles_file_error_gracefully(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel._acciones_file = "/nonexistent/path/acciones.log"
            panel.build()

            # Should not raise even if file write fails
            panel.log_action("Error action")

    def test_log_action_skips_when_consola_acciones_none(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            # Don't call build()
            panel.log_action("No textbox")
            # Should not raise


# ===================================================================
# Log queue processing tests
# ===================================================================

class TestProcessLogs:
    """Test _process_logs method."""

    def test_process_logs_drains_queue(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            panel._logs_panel_visible = True

            log_queue.put("[INFO] Message 1")
            log_queue.put("[INFO] Message 2")
            log_queue.put("[INFO] Message 3")

            panel.process_logs()

            assert log_queue.empty()
            assert "Message 1" in panel.consola._content
            assert "Message 2" in panel.consola._content
            assert "Message 3" in panel.consola._content

    def test_process_logs_handles_empty_queue(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            panel._logs_panel_visible = True

            # Should not raise on empty queue
            panel.process_logs()

    def test_process_logs_respects_logs_visibility(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            panel._logs_panel_visible = False

            log_queue.put("[INFO] Hidden message")
            panel.process_logs()

            assert log_queue.empty()
            # Should not appear in consola since logs are hidden
            assert panel.consola._content == ""

    def test_process_logs_bounded_per_tick(self, mock_ctk, ui_state, dispatcher, log_queue):
        """A single tick processes at most PROCESS_LOGS_CHUNK_LIMIT messages (ADR-007)."""
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            panel._logs_panel_visible = True
            # Disable the continuation so we observe a single tick only
            panel._schedule_ui_update = lambda fn: None

            limit = panel.PROCESS_LOGS_CHUNK_LIMIT
            for i in range(limit + 5):
                log_queue.put(f"[INFO] Burst {i}")

            panel.process_logs()

            assert log_queue.qsize() == 5
            assert f"Burst {limit - 1}" in panel.consola._content
            assert f"Burst {limit}" not in panel.consola._content

    def test_process_logs_reschedules_when_queue_not_empty(self, mock_ctk, ui_state, dispatcher, log_queue):
        """Leftover messages trigger a continuation via schedule_ui_update (ADR-007)."""
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            panel._logs_panel_visible = True
            scheduled = []
            panel._schedule_ui_update = lambda fn: scheduled.append(fn)

            for i in range(panel.PROCESS_LOGS_CHUNK_LIMIT + 1):
                log_queue.put(f"[INFO] Msg {i}")

            panel.process_logs()
            assert len(scheduled) == 1

            # Running the continuation drains the rest without rescheduling again
            scheduled[0]()
            assert log_queue.empty()
            assert len(scheduled) == 1

    def test_process_logs_batches_console_writes(self, mock_ctk, ui_state, dispatcher, log_queue):
        """One tick flushes the console with a single batched write (ADR-007)."""
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            panel._logs_panel_visible = True

            see_calls = []
            panel.consola.see = lambda pos: see_calls.append(pos)

            for i in range(5):
                log_queue.put(f"[INFO] Batched {i}")

            panel.process_logs()

            assert len(see_calls) == 1
            for i in range(5):
                assert f"Batched {i}" in panel.consola._content


# ===================================================================
# Kira response panel tests
# ===================================================================

class TestKiraResponsePanel:
    """Test Kira response display updates."""

    def test_update_kira_response_with_kira_message(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            kira_tb = mock_ctk.CTkTextbox()
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue, text_kira_response=kira_tb)

            panel.update_kira_response("[Kira]: Hello from Kira")

            assert "Hello from Kira" in kira_tb._content

    def test_update_kira_response_strips_emoji(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            kira_tb = mock_ctk.CTkTextbox()
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue, text_kira_response=kira_tb)

            panel.update_kira_response("[Kira]: 🧠 Brain message")

            assert "🧠" not in kira_tb._content
            assert "Brain message" in kira_tb._content

    def test_update_kira_response_ignores_non_kira(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            kira_tb = mock_ctk.CTkTextbox()
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue, text_kira_response=kira_tb)

            panel.update_kira_response("[INFO] Not a kira message")

            assert kira_tb._content == ""

    def test_update_kira_response_skips_when_textbox_none(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            # Don't pass text_kira_response — it's None
            panel.update_kira_response("[Kira]: No textbox")
            # Should not raise

    def test_update_kira_response_clears_previous(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            kira_tb = mock_ctk.CTkTextbox()
            kira_tb._content = "Old response\n"
            kira_tb._line_count = 1
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue, text_kira_response=kira_tb)

            panel.update_kira_response("[Kira]: New response")

            assert "Old response" not in kira_tb._content
            assert "New response" in kira_tb._content

    def test_update_kira_response_skips_identical_content(self, mock_ctk, ui_state, dispatcher, log_queue):
        """Identical content skips the delete/insert reflow cycle (ADR-007)."""
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            kira_tb = mock_ctk.CTkTextbox()
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue, text_kira_response=kira_tb)

            panel.update_kira_response("[Kira]: Same response")

            inserts = []
            original_insert = kira_tb.insert
            kira_tb.insert = lambda pos, text: (inserts.append(text), original_insert(pos, text))

            panel.update_kira_response("[Kira]: Same response")

            assert inserts == []
            assert "Same response" in kira_tb._content


# ===================================================================
# Cleanup tests
# ===================================================================

class TestCleanup:
    """Test cleanup and lifecycle."""

    def test_cleanup_unsubscribes_from_ui_state(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            observer_id = panel._observer_id

            panel.cleanup()

            assert panel._observer_id is None

    def test_cleanup_noop_when_not_subscribed(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            # Don't call build() — not subscribed

            panel.cleanup()
            # Should not raise


# ===================================================================
# UIState observer integration tests
# ===================================================================

class TestUIStateObserver:
    """Test UIState observer integration."""

    def test_observer_receives_advanced_mode_change(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()

            ui_state.advanced_mode = True

            # Observer callbacks are dispatched asynchronously via background thread.
            # Wait for the dispatch thread to process the notification.
            time.sleep(0.15)

            assert panel._logs_panel_visible is True

    def test_observer_receives_advanced_mode_off(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            panel._logs_panel_visible = True
            panel._frame._grid_visible = True

            ui_state.advanced_mode = False

            # Observer callbacks are dispatched asynchronously via background thread.
            time.sleep(0.15)

            assert panel._logs_panel_visible is False


# ===================================================================
# Edge case tests
# ===================================================================

class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_print_log_handles_exception_in_kira_update(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            panel._logs_panel_visible = True

            # Replace update_kira_response to raise
            original = panel.update_kira_response
            panel.update_kira_response = MagicMock(side_effect=RuntimeError("fail"))

            # Should raise since there's no try/except around update_kira_response
            # This is expected behavior — the caller should handle errors
            with pytest.raises(RuntimeError):
                panel.print_log("[Kira]: Test")

    def test_append_to_textbox_handles_non_string(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()

            textbox = mock_ctk.CTkTextbox()
            panel.append_to_textbox(textbox, 12345, max_lines=1000)

            assert "12345" in textbox._content

    def test_append_to_textbox_handles_none_line(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()

            textbox = mock_ctk.CTkTextbox()
            panel.append_to_textbox(textbox, None, max_lines=1000)

            assert "None" in textbox._content

    def test_process_logs_handles_malformed_queue_item(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()
            panel._logs_panel_visible = True

            log_queue.put(None)
            log_queue.put(123)
            log_queue.put("[INFO] Valid")

            panel.process_logs()

            assert log_queue.empty()
            # All items should have been processed (converted to strings)
            assert "None" in panel.consola._content
            assert "123" in panel.consola._content
            assert "Valid" in panel.consola._content

    def test_log_action_handles_empty_message(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()

            panel.log_action("")

            # Should not raise, empty action logged

    def test_log_action_handles_unicode(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()

            panel.log_action("🎮 Acción con unicode: áéíóú ñ")

            assert "Acción con unicode" in panel.consola_acciones._content


# ===================================================================
# CallbackDispatcher integration tests
# ===================================================================

class TestDispatcherIntegration:
    """Test CallbackDispatcher integration."""

    def test_log_action_dispatches_event(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()

            callback = MagicMock()
            dispatcher.subscribe("on_log_action", callback)

            panel.log_action("Dispatched action")

            callback.assert_called_once()

    def test_log_action_dispatches_with_message(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()

            callback = MagicMock()
            dispatcher.subscribe("on_log_action", callback)

            panel.log_action("Test dispatch")

            args = callback.call_args
            assert "Test dispatch" in str(args)

    def test_log_action_callback_error_logged(self, mock_ctk, ui_state, dispatcher, log_queue):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = _make_panel(mock_ctk, ui_state, dispatcher, log_queue)
            panel.build()

            dispatcher.subscribe("on_log_action", lambda msg: 1/0)

            # Should not raise — dispatcher handles errors
            panel.log_action("Error callback")
