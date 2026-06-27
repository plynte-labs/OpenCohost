"""Tests for VoiceControlPanel matching the actual implementation in ui/voice_control.py."""

from __future__ import annotations

import asyncio
import json
import sys
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Constants (mirroring config/settings.py)
# ---------------------------------------------------------------------------

MOCK_WS_URI = "ws://127.0.0.1:8765"
MOCK_WS_TIMEOUT = 5.0
MOCK_WS_RECONNECT_BASE_DELAY = 1.0
MOCK_WS_RECONNECT_MAX_DELAY = 30.0
MOCK_WS_MAX_RETRIES = 10
MOCK_RECORDING_SAMPLERATE = 24000
MOCK_RECORDING_DURATION = 8
MOCK_MIN_AUDIO_RMS = 0.005


# ---------------------------------------------------------------------------
# Session-scoped mocks for native / heavy modules
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _mock_external_modules():
    """Mock soundfile, sounddevice, websockets, customtkinter before any import."""
    # --- soundfile ---
    mock_sf = MagicMock()
    mock_sf.write = MagicMock()
    mock_sf.read = MagicMock(return_value=(MagicMock(), MOCK_RECORDING_SAMPLERATE))
    sys.modules["soundfile"] = mock_sf

    # --- sounddevice ---
    mock_sd = MagicMock()

    class MockInputStream:
        """Proper context manager mock for sd.InputStream."""
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self._read_data = (np.array([[0.3]] * 1024, dtype="float32"), False)
            self.stop = MagicMock()

        def read(self, *args, **kwargs):
            time.sleep(0.01)
            return self._read_data

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    mock_sd.InputStream = MagicMock(side_effect=lambda **kw: MockInputStream(**kw))
    mock_sd.query_devices = MagicMock(return_value=[
        {"index": 0, "name": "Default Device", "max_input_channels": 2},
        {"index": 1, "name": "USB Microphone", "max_input_channels": 1},
    ])
    sys.modules["sounddevice"] = mock_sd

    # --- websockets ---
    class MockConnectionClosed(Exception):
        pass

    mock_ws_exceptions = MagicMock()
    mock_ws_exceptions.ConnectionClosed = MockConnectionClosed

    class MockWSContextManager:
        """Async context manager returned by mock websockets.connect()."""
        def __init__(self, instance):
            self._instance = instance

        async def __aenter__(self):
            return self._instance

        async def __aexit__(self, *args):
            pass

    mock_ws_module = MagicMock()
    mock_ws_module.exceptions = mock_ws_exceptions
    mock_ws_module._MockWSContextManager = MockWSContextManager

    def _make_ws_instance():
        inst = MagicMock()
        inst.recv = AsyncMock()
        inst.close = AsyncMock()
        return inst

    def _make_connect():
        inst = _make_ws_instance()
        return MagicMock(return_value=MockWSContextManager(inst))

    mock_ws_module._make_ws_instance = _make_ws_instance
    mock_ws_module.connect = _make_connect()

    sys.modules["websockets"] = mock_ws_module
    sys.modules["websockets.exceptions"] = mock_ws_exceptions

    # --- customtkinter ---
    class MockLabel:
        def __init__(self, master=None, **kwargs):
            self.master = master
            self.kwargs = kwargs
            self.text = kwargs.get("text", "")
            self.fg_color = kwargs.get("fg_color", "")
            self.text_color = kwargs.get("text_color", "")
            self.corner_radius = kwargs.get("corner_radius", 0)
            self.font = kwargs.get("font", None)
            self.configure = MagicMock(side_effect=self._configure)

        def _configure(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)
                self.kwargs[key] = value

        def pack(self, **kwargs):
            self.pack_kwargs = kwargs

        def grid(self, **kwargs):
            self.grid_kwargs = kwargs

        def grid_remove(self):
            self._grid_removed = True

        def winfo_exists(self):
            return True

    class MockFrame:
        def __init__(self, master=None, **kwargs):
            self.master = master
            self.kwargs = kwargs
            self.children = []

        def pack(self, **kwargs):
            self.pack_kwargs = kwargs

        def grid(self, **kwargs):
            self.grid_kwargs = kwargs

        def grid_remove(self):
            self._grid_removed = True

        def grid_columnconfigure(self, col, **kwargs):
            pass

        def grid_rowconfigure(self, row, **kwargs):
            pass

    class MockButton:
        def __init__(self, master=None, **kwargs):
            self.master = master
            self.kwargs = kwargs
            self.text = kwargs.get("text", "")
            self.fg_color = kwargs.get("fg_color", "")
            self.hover_color = kwargs.get("hover_color", "")
            self.state_value = kwargs.get("state", "normal")
            self.command = kwargs.get("command", None)
            self.height = kwargs.get("height", None)
            self.font = kwargs.get("font", None)
            self.width = kwargs.get("width", None)

        def configure(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)
                self.kwargs[key] = value

        def grid(self, **kwargs):
            self.grid_kwargs = kwargs

        def pack(self, **kwargs):
            self.pack_kwargs = kwargs

    class MockProgressBar:
        def __init__(self, master=None, **kwargs):
            self.master = master
            self.kwargs = kwargs
            self.value = 0.0
            self.width = kwargs.get("width", 0)
            self.height = kwargs.get("height", 0)
            self._grid_removed = False

        def set(self, value):
            self.value = value

        def get(self):
            return self.value

        def grid(self, **kwargs):
            self.grid_kwargs = kwargs
            self._grid_removed = False

        def grid_remove(self):
            self._grid_removed = True

    class MockFont:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    mock_ctk = MagicMock()
    mock_ctk.CTkLabel = MockLabel
    mock_ctk.CTkFrame = MockFrame
    mock_ctk.CTkButton = MockButton
    mock_ctk.CTkProgressBar = MockProgressBar
    mock_ctk.CTkFont = MockFont

    sys.modules["customtkinter"] = mock_ctk

    yield

    # Cleanup: remove cached voice_control module so other tests can re-import
    for mod in list(sys.modules.keys()):
        if mod == "opencohost.ui.voice_control" or mod.startswith("opencohost.ui.voice_control."):
            del sys.modules[mod]


# ---------------------------------------------------------------------------
# Per-test fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def ui_state():
    """Fresh UIState instance, shut down after each test."""
    from opencohost.ui.state import UIState
    s = UIState()
    yield s
    s.shutdown(timeout=2.0)


@pytest.fixture()
def mock_parent():
    """Mock parent frame for the VoiceControlPanel."""
    parent = MagicMock()
    parent.grid = MagicMock()
    parent.grid_columnconfigure = MagicMock()
    parent.grid_rowconfigure = MagicMock()
    return parent


@pytest.fixture()
def mock_logger():
    """Mock logger to avoid file I/O during tests."""
    logger = MagicMock()
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    logger.debug = MagicMock()
    logger.exception = MagicMock()
    return logger


@pytest.fixture()
def mock_motor_ia():
    """Mock MotorVocalIA with command_queue."""
    motor = MagicMock()
    motor.is_speaking = False
    motor.is_processing = False
    motor.command_queue = MagicMock()
    return motor


@pytest.fixture()
def voice_panel_class():
    """Import VoiceControlPanel with all external modules already mocked."""
    # Clear any cached import
    for mod in list(sys.modules.keys()):
        if mod == "opencohost.ui.voice_control" or mod.startswith("opencohost.ui.voice_control."):
            del sys.modules[mod]

    from opencohost.ui.voice_control import VoiceControlPanel

    return VoiceControlPanel


@pytest.fixture()
def voice_panel(voice_panel_class, mock_parent, ui_state, mock_logger, mock_motor_ia):
    """VoiceControlPanel instance with mocked dependencies."""
    panel = voice_panel_class(
        parent_frame=mock_parent,
        ui_state=ui_state,
        logger=mock_logger,
        on_log=mock_logger.info,
        on_motor_event=MagicMock(),
        on_pipeline_change=MagicMock(),
    )
    panel._motor_ia = mock_motor_ia
    yield panel
    panel.cleanup()


@pytest.fixture()
def mock_sd():
    """Return the session-scoped mock sounddevice module."""
    return sys.modules["sounddevice"]


@pytest.fixture()
def mock_sf():
    """Return the session-scoped mock soundfile module."""
    return sys.modules["soundfile"]


@pytest.fixture()
def mock_ws():
    """Return the session-scoped mock websockets module with a fresh instance."""
    ws_module = sys.modules["websockets"]
    MockWSContextManager = ws_module._MockWSContextManager
    mock_ws_instance = ws_module._make_ws_instance()
    ws_module.connect = MagicMock(return_value=MockWSContextManager(mock_ws_instance))
    return ws_module, mock_ws_instance


# ===================================================================
# 1. VoiceControlPanel Class Initialization
# ===================================================================


class TestVoiceControlPanelInit:
    """Test VoiceControlPanel constructor and initial state."""

    def test_init_stores_references(self, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = voice_panel_class(
            parent_frame=mock_parent,
            ui_state=ui_state,
            logger=mock_logger,
            on_log=mock_logger.info,
            on_motor_event=MagicMock(),
            on_pipeline_change=MagicMock(),
        )
        assert panel._parent is mock_parent
        assert panel._ui_state is ui_state
        assert panel._logger is mock_logger
        panel.cleanup()

    def test_init_pipeline_state_is_idle(self, voice_panel):
        assert voice_panel._pipeline_state == "idle"

    def test_init_ws_not_connected(self, voice_panel):
        assert voice_panel._ws_connected is False

    def test_init_ws_should_not_reconnect(self, voice_panel):
        assert voice_panel._ws_should_reconnect is False

    def test_init_state_sub_id_is_int(self, voice_panel):
        assert isinstance(voice_panel._state_sub_id, int)
        assert voice_panel._state_sub_id >= 0


# ===================================================================
# 2. Voice Panel Creation
# ===================================================================


class TestCreateVoicePanel:
    """Test voice panel UI creation."""

    def _make_panel(self, voice_panel_class, mock_parent, ui_state, mock_logger):
        return voice_panel_class(
            parent_frame=mock_parent,
            ui_state=ui_state,
            logger=mock_logger,
            on_log=mock_logger.info,
            on_motor_event=MagicMock(),
            on_pipeline_change=MagicMock(),
        )

    def test_create_panel_creates_button(self, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = self._make_panel(voice_panel_class, mock_parent, ui_state, mock_logger)
        panel.create_voice_panel()
        assert panel.btn_primary_voice is not None
        panel.cleanup()

    def test_create_panel_button_default_text(self, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = self._make_panel(voice_panel_class, mock_parent, ui_state, mock_logger)
        panel.create_voice_panel()
        assert panel.btn_primary_voice.text == "Hablar"
        panel.cleanup()

    def test_create_panel_button_default_color(self, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = self._make_panel(voice_panel_class, mock_parent, ui_state, mock_logger)
        panel.create_voice_panel()
        assert panel.btn_primary_voice.fg_color == "#1f7a5a"
        panel.cleanup()

    def test_create_panel_subscribes_to_ui_state(self, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = self._make_panel(voice_panel_class, mock_parent, ui_state, mock_logger)
        # Subscription happens in __init__, before create_voice_panel
        assert panel._state_sub_id is not None
        assert panel._state_sub_id in ui_state._observers
        panel.cleanup()

    def test_create_panel_is_idempotent(self, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = self._make_panel(voice_panel_class, mock_parent, ui_state, mock_logger)
        panel.create_voice_panel()
        first_sub = panel._state_sub_id
        panel.create_voice_panel()
        second_sub = panel._state_sub_id
        # Subscription happens in __init__, not in create_voice_panel
        assert first_sub == second_sub
        panel.cleanup()

    def test_create_panel_button_has_command(self, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = self._make_panel(voice_panel_class, mock_parent, ui_state, mock_logger)
        panel.create_voice_panel()
        assert panel.btn_primary_voice.command is not None
        assert callable(panel.btn_primary_voice.command)
        panel.cleanup()


# ===================================================================
# 3. WebSocket Connection Lifecycle
# ===================================================================


class TestWebSocketConnect:
    """Test WebSocket connection initiation."""

    def test_connect_sets_ws_flags(self, voice_panel):
        voice_panel.connect_ws()
        assert voice_panel._ws_should_reconnect is True
        assert voice_panel._ws_connected is True

    def test_connect_starts_ws_thread(self, voice_panel):
        with patch("opencohost.ui.voice_control.threading.Thread") as mock_thread:
            voice_panel.connect_ws()
            mock_thread.assert_called_once()

    def test_connect_updates_ui_state(self, voice_panel):
        voice_panel.connect_ws()
        assert voice_panel._ui_state.ws_should_reconnect is True
        assert voice_panel._ui_state.ws_connected is True

    def test_connect_when_already_connected_is_noop(self, voice_panel):
        voice_panel._ws_connected = True
        with patch("opencohost.ui.voice_control.threading.Thread") as mock_thread:
            voice_panel.connect_ws()
            mock_thread.assert_not_called()


class TestWebSocketDisconnect:
    """Test WebSocket disconnection."""

    def test_disconnect_clears_ws_flags(self, voice_panel):
        voice_panel._ws_connected = True
        voice_panel._ws_should_reconnect = True
        voice_panel.disconnect_ws()
        assert voice_panel._ws_connected is False
        assert voice_panel._ws_should_reconnect is False

    def test_disconnect_updates_ui_state(self, voice_panel):
        voice_panel._ws_connected = True
        voice_panel._ws_should_reconnect = True
        voice_panel.disconnect_ws()
        assert voice_panel._ui_state.ws_connected is False
        assert voice_panel._ui_state.ws_should_reconnect is False

    def test_disconnect_logs_message(self, voice_panel, mock_logger):
        voice_panel._on_log = mock_logger.info
        voice_panel._ws_connected = True
        voice_panel.disconnect_ws()
        mock_logger.info.assert_called()


class TestWebSocketReconnection:
    """Test WebSocket reconnection with exponential backoff."""

    def test_run_ws_client_logs_unexpected_exception_and_marks_idle(self, voice_panel, mock_logger):
        voice_panel.create_voice_panel()
        voice_panel._ws_connected = True
        voice_panel._ws_should_reconnect = True
        voice_panel._ui_state.ws_connected = True
        voice_panel._ui_state.ws_should_reconnect = True

        with patch.object(
            voice_panel,
            "_ws_reconnect_loop",
            new_callable=AsyncMock,
        ) as mock_loop:
            mock_loop.side_effect = RuntimeError("event loop crashed")
            voice_panel._run_ws_client()

        mock_logger.exception.assert_called_once_with(
            "Unexpected error in WebSocket client thread"
        )
        mock_logger.info.assert_any_call(
            "[Red] Error inesperado en LiveAudio. Desconectado."
        )
        assert voice_panel._ws_connected is False
        assert voice_panel._ws_should_reconnect is False
        assert voice_panel._ui_state.ws_connected is False
        assert voice_panel._ui_state.ws_should_reconnect is False
        assert voice_panel.get_state() == "error"
        assert voice_panel.btn_primary_voice.text == "Hablar"

    def test_reconnect_uses_exponential_backoff(self, voice_panel):
        voice_panel._ws_should_reconnect = True

        connect_call_count = 0

        def failing_connect(*args, **kwargs):
            nonlocal connect_call_count
            connect_call_count += 1
            raise Exception("Connection refused")

        with patch("opencohost.ui.voice_control.WS_RECONNECT_BASE_DELAY", 1.0):
            with patch("opencohost.ui.voice_control.WS_RECONNECT_MAX_DELAY", 8.0):
                with patch("opencohost.ui.voice_control.WS_MAX_RETRIES", 3):
                    with patch("opencohost.ui.voice_control.websockets.connect", failing_connect):
                        with patch("opencohost.ui.voice_control.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
                            asyncio.run(voice_panel._ws_reconnect_loop())

        assert mock_sleep.call_count >= 2
        delays = [c.args[0] for c in mock_sleep.call_args_list]
        assert delays[0] == 1.0
        assert delays[1] == 2.0

    def test_reconnect_stops_after_max_retries(self, voice_panel):
        voice_panel._ws_should_reconnect = True

        def failing_connect(*args, **kwargs):
            raise Exception("Connection refused")

        with patch("opencohost.ui.voice_control.WS_RECONNECT_BASE_DELAY", 0.01):
            with patch("opencohost.ui.voice_control.WS_RECONNECT_MAX_DELAY", 0.02):
                with patch("opencohost.ui.voice_control.WS_MAX_RETRIES", 2):
                    with patch("opencohost.ui.voice_control.websockets.connect", failing_connect):
                        with patch("opencohost.ui.voice_control.asyncio.sleep", new_callable=AsyncMock):
                            asyncio.run(voice_panel._ws_reconnect_loop())

        assert voice_panel._ws_connected is False
        assert voice_panel._ws_should_reconnect is False

    def test_reconnect_stops_when_flag_cleared(self, voice_panel):
        voice_panel._ws_should_reconnect = True
        attempt = 0

        def failing_connect(*args, **kwargs):
            nonlocal attempt
            attempt += 1
            if attempt >= 2:
                voice_panel._ws_should_reconnect = False
            raise Exception("Connection refused")

        with patch("opencohost.ui.voice_control.WS_RECONNECT_BASE_DELAY", 0.01):
            with patch("opencohost.ui.voice_control.WS_RECONNECT_MAX_DELAY", 0.02):
                with patch("opencohost.ui.voice_control.WS_MAX_RETRIES", 10):
                    with patch("opencohost.ui.voice_control.websockets.connect", failing_connect):
                        with patch("opencohost.ui.voice_control.asyncio.sleep", new_callable=AsyncMock):
                            asyncio.run(voice_panel._ws_reconnect_loop())

        assert voice_panel._ws_should_reconnect is False


class TestPTTFlushWatcher:
    """Test PTT flush watcher resilience."""

    def test_flush_watcher_logs_exception_and_continues(self, voice_panel, mock_logger):
        class FakeStop:
            def __init__(self):
                self.stopped = False
                self.wait_calls = []

            def is_set(self):
                return self.stopped

            def wait(self, timeout):
                self.wait_calls.append(timeout)

            def set(self):
                self.stopped = True

        fake_stop = FakeStop()
        voice_panel._ptt_flush_stop = fake_stop
        voice_panel._ptt_buffer = "buffered transcript"
        voice_panel._ptt_grace_deadline = time.time() - 1

        flush_calls = 0

        def flush_side_effect():
            nonlocal flush_calls
            flush_calls += 1
            if flush_calls == 1:
                raise RuntimeError("flush failed")
            fake_stop.set()

        with patch.object(voice_panel, "_flush_ptt_buffer", side_effect=flush_side_effect):
            voice_panel._ptt_flush_watcher()

        mock_logger.exception.assert_called_once_with(
            "Unexpected error in PTT flush watcher"
        )
        assert flush_calls == 2
        assert fake_stop.wait_calls == [0.5, 0.5]


class TestWebSocketMessageHandling:
    """Test WebSocket message reception and processing."""

    def test_transcription_received_processes_message(self, voice_panel, mock_motor_ia):
        ws_module = sys.modules["websockets"]
        MockWSContextManager = ws_module._MockWSContextManager

        mock_ws_instance = MagicMock()
        transcript_msg = json.dumps({"text": "hello world this is a test"})
        call_count = 0

        def recv_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return transcript_msg
            voice_panel._ws_connected = False
            raise asyncio.TimeoutError()

        mock_ws_instance.recv = AsyncMock(side_effect=recv_side_effect)
        ws_module.connect = MagicMock(return_value=MockWSContextManager(mock_ws_instance))

        voice_panel._ws_connected = True
        voice_panel._ws_should_reconnect = False

        asyncio.run(voice_panel._ws_listener())
        mock_motor_ia.command_queue.put.assert_called()

    def test_short_transcription_is_ignored(self, voice_panel, mock_motor_ia):
        ws_module = sys.modules["websockets"]
        MockWSContextManager = ws_module._MockWSContextManager

        mock_ws_instance = MagicMock()
        short_msg = json.dumps({"text": "hi there"})
        call_count = 0

        def recv_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return short_msg
            voice_panel._ws_connected = False
            raise asyncio.TimeoutError()

        mock_ws_instance.recv = AsyncMock(side_effect=recv_side_effect)
        ws_module.connect = MagicMock(return_value=MockWSContextManager(mock_ws_instance))

        voice_panel._ws_connected = True
        voice_panel._ws_should_reconnect = False

        asyncio.run(voice_panel._ws_listener())
        mock_motor_ia.command_queue.put.assert_not_called()

    def test_empty_transcription_is_ignored(self, voice_panel, mock_motor_ia):
        ws_module = sys.modules["websockets"]
        MockWSContextManager = ws_module._MockWSContextManager

        mock_ws_instance = MagicMock()
        empty_msg = json.dumps({"text": ""})
        call_count = 0

        def recv_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return empty_msg
            voice_panel._ws_connected = False
            raise asyncio.TimeoutError()

        mock_ws_instance.recv = AsyncMock(side_effect=recv_side_effect)
        ws_module.connect = MagicMock(return_value=MockWSContextManager(mock_ws_instance))

        voice_panel._ws_connected = True
        voice_panel._ws_should_reconnect = False

        asyncio.run(voice_panel._ws_listener())
        mock_motor_ia.command_queue.put.assert_not_called()

    def test_invalid_json_is_logged(self, voice_panel, mock_logger):
        ws_module = sys.modules["websockets"]
        MockWSContextManager = ws_module._MockWSContextManager

        mock_ws_instance = MagicMock()
        call_count = 0

        def recv_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return "not valid json"
            voice_panel._ws_connected = False
            raise asyncio.TimeoutError()

        mock_ws_instance.recv = AsyncMock(side_effect=recv_side_effect)
        ws_module.connect = MagicMock(return_value=MockWSContextManager(mock_ws_instance))

        voice_panel._ws_connected = True
        voice_panel._ws_should_reconnect = False
        voice_panel._logger = mock_logger

        asyncio.run(voice_panel._ws_listener())
        mock_logger.warning.assert_called()

    def test_connection_closed_raises(self, voice_panel):
        ws_module = sys.modules["websockets"]
        MockWSContextManager = ws_module._MockWSContextManager

        mock_ws_instance = MagicMock()
        mock_ws_instance.recv = AsyncMock(
            side_effect=ws_module.exceptions.ConnectionClosed(None, None)
        )
        ws_module.connect = MagicMock(return_value=MockWSContextManager(mock_ws_instance))

        voice_panel._ws_connected = True
        voice_panel._ws_should_reconnect = False

        async def run_test():
            with pytest.raises(ws_module.exceptions.ConnectionClosed):
                await voice_panel._ws_listener()

        asyncio.run(run_test())

    def test_websocket_timeout_is_handled(self, voice_panel):
        ws_module = sys.modules["websockets"]
        MockWSContextManager = ws_module._MockWSContextManager

        mock_ws_instance = MagicMock()
        mock_ws_instance.recv = AsyncMock(side_effect=asyncio.TimeoutError())
        ws_module.connect = MagicMock(return_value=MockWSContextManager(mock_ws_instance))

        voice_panel._ws_connected = True
        voice_panel._ws_should_reconnect = False

        async def run_test():
            voice_panel._ws_connected = False
            await voice_panel._ws_listener()

        asyncio.run(run_test())


# ===================================================================
# 4. Audio Recording
# ===================================================================


class TestRecordingDuration:
    """Test recording duration constants."""

    def test_recording_duration_constant_is_eight_seconds(self):
        assert MOCK_RECORDING_DURATION == 8


# ===================================================================
# 6. Voice Button State Machine
# ===================================================================


class TestVoiceButtonStateTransitions:
    """Test all valid state transitions via set_state()."""

    def test_set_state_idle_to_listening(self, voice_panel, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = voice_panel_class(
            parent_frame=mock_parent, ui_state=ui_state, logger=mock_logger,
            on_log=mock_logger.info, on_motor_event=MagicMock(), on_pipeline_change=MagicMock(),
        )
        panel.create_voice_panel()
        # Patch _schedule_rms_frame to avoid tkinter dependency
        panel._schedule_rms_frame = MagicMock()
        panel.set_state("listening")
        assert panel._pipeline_state == "listening"
        assert panel.btn_primary_voice.text == "Detener"
        assert panel.btn_primary_voice.fg_color == "darkred"
        panel.cleanup()

    def test_set_state_listening_to_processing(self, voice_panel, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = voice_panel_class(
            parent_frame=mock_parent, ui_state=ui_state, logger=mock_logger,
            on_log=mock_logger.info, on_motor_event=MagicMock(), on_pipeline_change=MagicMock(),
        )
        panel.create_voice_panel()
        panel._schedule_rms_frame = MagicMock()
        panel.set_state("listening")
        panel.set_state("processing")
        assert panel._pipeline_state == "processing"
        assert panel.btn_primary_voice.text == "Pensando..."
        assert panel.btn_primary_voice.fg_color == "#8a6400"
        panel.cleanup()

    def test_set_state_processing_to_speaking(self, voice_panel, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = voice_panel_class(
            parent_frame=mock_parent, ui_state=ui_state, logger=mock_logger,
            on_log=mock_logger.info, on_motor_event=MagicMock(), on_pipeline_change=MagicMock(),
        )
        panel.create_voice_panel()
        panel._schedule_rms_frame = MagicMock()
        panel.set_state("processing")
        panel.set_state("speaking")
        assert panel._pipeline_state == "speaking"
        assert panel.btn_primary_voice.text == "Detener voz"
        assert panel.btn_primary_voice.fg_color == "#2f5f8f"
        panel.cleanup()

    def test_set_state_speaking_to_idle(self, voice_panel, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = voice_panel_class(
            parent_frame=mock_parent, ui_state=ui_state, logger=mock_logger,
            on_log=mock_logger.info, on_motor_event=MagicMock(), on_pipeline_change=MagicMock(),
        )
        panel.create_voice_panel()
        panel._schedule_rms_frame = MagicMock()
        panel.set_state("speaking")
        panel.set_state("idle")
        assert panel._pipeline_state == "idle"
        assert panel.btn_primary_voice.text == "Hablar"
        assert panel.btn_primary_voice.fg_color == "#1f7a5a"
        panel.cleanup()

    def test_set_state_error_falls_to_default(self, voice_panel, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = voice_panel_class(
            parent_frame=mock_parent, ui_state=ui_state, logger=mock_logger,
            on_log=mock_logger.info, on_motor_event=MagicMock(), on_pipeline_change=MagicMock(),
        )
        panel.create_voice_panel()
        panel._schedule_rms_frame = MagicMock()
        panel.set_state("error")
        assert panel._pipeline_state == "error"
        assert panel.btn_primary_voice.text == "Hablar"
        assert panel.btn_primary_voice.fg_color == "#1f7a5a"
        panel.cleanup()

    def test_set_state_downloading(self, voice_panel, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = voice_panel_class(
            parent_frame=mock_parent, ui_state=ui_state, logger=mock_logger,
            on_log=mock_logger.info, on_motor_event=MagicMock(), on_pipeline_change=MagicMock(),
        )
        panel.create_voice_panel()
        panel._schedule_rms_frame = MagicMock()
        panel.set_state("downloading")
        assert panel._pipeline_state == "downloading"
        assert panel.btn_primary_voice.text == "Modelo cargando"
        assert panel.btn_primary_voice.fg_color == "#555555"
        panel.cleanup()

    def test_set_state_playing(self, voice_panel, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = voice_panel_class(
            parent_frame=mock_parent, ui_state=ui_state, logger=mock_logger,
            on_log=mock_logger.info, on_motor_event=MagicMock(), on_pipeline_change=MagicMock(),
        )
        panel.create_voice_panel()
        panel._schedule_rms_frame = MagicMock()
        panel.set_state("playing")
        assert panel._pipeline_state == "playing"
        assert panel.btn_primary_voice.text == "Detener voz"
        assert panel.btn_primary_voice.fg_color == "#2f5f8f"
        panel.cleanup()


class TestVoiceButtonVisualStates:
    """Test button text and color for each state."""

    @pytest.mark.parametrize(
        "state,expected_text,expected_color,expected_hover",
        [
            ("idle", "Hablar", "#1f7a5a", "#24946c"),
            ("listening", "Detener", "darkred", "#8b1a1a"),
            ("processing", "Pensando...", "#8a6400", "#a67800"),
            ("speaking", "Detener voz", "#2f5f8f", "#3670aa"),
            ("playing", "Detener voz", "#2f5f8f", "#3670aa"),
            ("downloading", "Modelo cargando", "#555555", "#666666"),
        ],
    )
    def test_button_appearance_per_state(
        self, mock_parent, ui_state, mock_logger, voice_panel_class,
        state, expected_text, expected_color, expected_hover
    ):
        panel = voice_panel_class(
            parent_frame=mock_parent, ui_state=ui_state, logger=mock_logger,
            on_log=mock_logger.info, on_motor_event=MagicMock(), on_pipeline_change=MagicMock(),
        )
        panel.create_voice_panel()
        panel._schedule_rms_frame = MagicMock()
        panel.set_state(state)
        assert panel.btn_primary_voice.text == expected_text
        assert panel.btn_primary_voice.fg_color == expected_color
        assert panel.btn_primary_voice.hover_color == expected_hover
        panel.cleanup()


# ===================================================================
# 7. UIState Observer Integration
# ===================================================================


class TestUIStateObserverIntegration:
    """Test VoiceControlPanel integration with UIState observer pattern."""

    def test_subscribe_on_init(self, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = voice_panel_class(
            parent_frame=mock_parent, ui_state=ui_state, logger=mock_logger,
            on_log=mock_logger.info, on_motor_event=MagicMock(), on_pipeline_change=MagicMock(),
        )
        assert panel._state_sub_id is not None
        assert panel._state_sub_id in ui_state._observers
        panel.cleanup()

    def test_cleanup_removes_observer(self, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = voice_panel_class(
            parent_frame=mock_parent, ui_state=ui_state, logger=mock_logger,
            on_log=mock_logger.info, on_motor_event=MagicMock(), on_pipeline_change=MagicMock(),
        )
        sub_id = panel._state_sub_id
        panel.cleanup()
        assert sub_id not in ui_state._observers

    def test_cleanup_is_idempotent(self, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = voice_panel_class(
            parent_frame=mock_parent, ui_state=ui_state, logger=mock_logger,
            on_log=mock_logger.info, on_motor_event=MagicMock(), on_pipeline_change=MagicMock(),
        )
        panel.cleanup()
        panel.cleanup()

    def test_set_state_updates_ui_state(self, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = voice_panel_class(
            parent_frame=mock_parent, ui_state=ui_state, logger=mock_logger,
            on_log=mock_logger.info, on_motor_event=MagicMock(), on_pipeline_change=MagicMock(),
        )
        panel.create_voice_panel()
        panel._schedule_rms_frame = MagicMock()
        panel.set_state("listening")
        assert panel._ui_state.pipeline_state == "listening"
        panel.cleanup()

    def test_get_state_returns_pipeline_state(self, voice_panel):
        voice_panel._pipeline_state = "processing"
        assert voice_panel.get_state() == "processing"

    def test_on_state_change_ignores_unknown_keys(self, voice_panel, mock_logger):
        voice_panel._logger = mock_logger
        original_state = voice_panel._pipeline_state
        voice_panel._on_state_change("unknown_key", "some_value")
        assert voice_panel._pipeline_state == original_state

    def test_on_state_change_handles_ptt_active(self, voice_panel, mock_logger):
        voice_panel._logger = mock_logger
        voice_panel._on_state_change("ptt_active", True)
        mock_logger.debug.assert_called()


# ===================================================================
# 8. Cleanup and Lifecycle
# ===================================================================


class TestCleanup:
    """Test cleanup and resource release."""

    def test_cleanup_disconnects_websocket(self, voice_panel):
        voice_panel._ws_connected = True
        voice_panel._ws_should_reconnect = True
        voice_panel.cleanup()
        assert voice_panel._ws_should_reconnect is False

    def test_cleanup_removes_observer(self, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = voice_panel_class(
            parent_frame=mock_parent, ui_state=ui_state, logger=mock_logger,
            on_log=mock_logger.info, on_motor_event=MagicMock(), on_pipeline_change=MagicMock(),
        )
        sub_id = panel._state_sub_id
        panel.cleanup()
        assert sub_id not in ui_state._observers


# ===================================================================
# 9. Edge Cases and Error Handling
# ===================================================================


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_sequential_state_transitions(self, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = voice_panel_class(
            parent_frame=mock_parent, ui_state=ui_state, logger=mock_logger,
            on_log=mock_logger.info, on_motor_event=MagicMock(), on_pipeline_change=MagicMock(),
        )
        panel.create_voice_panel()
        panel._schedule_rms_frame = MagicMock()

        transitions = [
            ("idle", "Hablar", "#1f7a5a"),
            ("listening", "Detener", "darkred"),
            ("processing", "Pensando...", "#8a6400"),
            ("speaking", "Detener voz", "#2f5f8f"),
            ("idle", "Hablar", "#1f7a5a"),
        ]

        for state, expected_text, expected_color in transitions:
            panel.set_state(state)
            assert panel.btn_primary_voice.text == expected_text
            assert panel.btn_primary_voice.fg_color == expected_color
        panel.cleanup()

    def test_toggle_websocket_connects_when_disconnected(self, voice_panel):
        with patch.object(voice_panel, "connect_ws") as mock_connect:
            voice_panel._ws_connected = False
            voice_panel._toggle_websocket()
            mock_connect.assert_called_once()

    def test_toggle_websocket_disconnects_when_connected(self, voice_panel):
        with patch.object(voice_panel, "disconnect_ws") as mock_disconnect:
            voice_panel._ws_connected = True
            voice_panel._toggle_websocket()
            mock_disconnect.assert_called_once()

    def test_is_ws_connected_returns_flag(self, voice_panel):
        voice_panel._ws_connected = True
        assert voice_panel.is_ws_connected() is True
        voice_panel._ws_connected = False
        assert voice_panel.is_ws_connected() is False

    def test_set_motor_ia(self, voice_panel):
        new_motor = MagicMock()
        voice_panel.set_motor_ia(new_motor)
        assert voice_panel._motor_ia is new_motor

    def test_update_tts_label_speaking(self, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = voice_panel_class(
            parent_frame=mock_parent, ui_state=ui_state, logger=mock_logger,
            on_log=mock_logger.info, on_motor_event=MagicMock(), on_pipeline_change=MagicMock(),
        )
        panel.create_voice_panel()
        panel.update_tts_label("speaking")
        assert panel.lbl_kira_tts_state.text == "TTS: generando"
        assert panel.lbl_kira_tts_state.fg_color == "#1f3f6f"
        panel.cleanup()

    def test_update_tts_label_playing(self, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = voice_panel_class(
            parent_frame=mock_parent, ui_state=ui_state, logger=mock_logger,
            on_log=mock_logger.info, on_motor_event=MagicMock(), on_pipeline_change=MagicMock(),
        )
        panel.create_voice_panel()
        panel.update_tts_label("playing")
        assert panel.lbl_kira_tts_state.text == "TTS: hablando"
        assert panel.lbl_kira_tts_state.fg_color == "#1f526f"
        panel.cleanup()

    def test_update_tts_label_idle(self, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = voice_panel_class(
            parent_frame=mock_parent, ui_state=ui_state, logger=mock_logger,
            on_log=mock_logger.info, on_motor_event=MagicMock(), on_pipeline_change=MagicMock(),
        )
        panel.create_voice_panel()
        panel.update_tts_label("idle")
        assert panel.lbl_kira_tts_state.text == "TTS: idle"
        assert panel.lbl_kira_tts_state.fg_color == "#1b2633"
        panel.cleanup()

    def test_update_voice_state_label_listening(self, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = voice_panel_class(
            parent_frame=mock_parent, ui_state=ui_state, logger=mock_logger,
            on_log=mock_logger.info, on_motor_event=MagicMock(), on_pipeline_change=MagicMock(),
        )
        panel.create_voice_panel()
        panel._dispositivo_seleccionado = 0
        panel._update_voice_state_label("listening")
        assert panel.lbl_kira_voice_state.text == "Voz/PTT: escuchando"
        panel.cleanup()

    def test_update_voice_state_label_no_mic(self, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = voice_panel_class(
            parent_frame=mock_parent, ui_state=ui_state, logger=mock_logger,
            on_log=mock_logger.info, on_motor_event=MagicMock(), on_pipeline_change=MagicMock(),
        )
        panel.create_voice_panel()
        panel._dispositivo_seleccionado = None
        panel._update_voice_state_label("idle")
        assert panel.lbl_kira_voice_state.text == "Voz/PTT: sin mic"
        panel.cleanup()

    def test_update_voice_state_label_ready(self, mock_parent, ui_state, mock_logger, voice_panel_class):
        panel = voice_panel_class(
            parent_frame=mock_parent, ui_state=ui_state, logger=mock_logger,
            on_log=mock_logger.info, on_motor_event=MagicMock(), on_pipeline_change=MagicMock(),
        )
        panel.create_voice_panel()
        panel._dispositivo_seleccionado = 0
        panel._update_voice_state_label("idle")
        assert panel.lbl_kira_voice_state.text == "Voz/PTT: listo"
        panel.cleanup()

    def test_update_button_with_no_button(self, voice_panel):
        voice_panel.btn_primary_voice = None
        voice_panel._update_button_for_state("listening")

    def test_update_voice_state_label_with_no_label(self, voice_panel):
        voice_panel.lbl_kira_voice_state = None
        voice_panel._update_voice_state_label("listening")

    def test_update_tts_label_with_no_label(self, voice_panel):
        voice_panel.lbl_kira_tts_state = None
        voice_panel.update_tts_label("speaking")

    def test_run_ws_client_creates_event_loop(self, voice_panel):
        with patch.object(voice_panel, "_ws_reconnect_loop", new_callable=AsyncMock) as mock_loop:
            voice_panel._ws_should_reconnect = False
            voice_panel._run_ws_client()
            mock_loop.assert_called_once()

    def test_ws_listener_sets_listening_state(self, mock_parent, ui_state, mock_logger, voice_panel_class):
        ws_module = sys.modules["websockets"]
        MockWSContextManager = ws_module._MockWSContextManager

        mock_ws_instance = MagicMock()
        mock_ws_instance.recv = AsyncMock(side_effect=asyncio.TimeoutError())
        ws_module.connect = MagicMock(return_value=MockWSContextManager(mock_ws_instance))

        panel = voice_panel_class(
            parent_frame=mock_parent, ui_state=ui_state, logger=mock_logger,
            on_log=mock_logger.info, on_motor_event=MagicMock(), on_pipeline_change=MagicMock(),
        )
        panel._ws_connected = True
        panel._ws_should_reconnect = False

        async def run_test():
            panel._ws_connected = False
            await panel._ws_listener()

        asyncio.run(run_test())
        assert panel._pipeline_state == "listening"
        panel.cleanup()

    def test_ws_listener_skips_when_motor_busy(self, voice_panel, mock_motor_ia):
        ws_module = sys.modules["websockets"]
        MockWSContextManager = ws_module._MockWSContextManager

        mock_ws_instance = MagicMock()
        transcript_msg = json.dumps({"text": "hello world this is a test"})
        call_count = 0

        def recv_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return transcript_msg
            voice_panel._ws_connected = False
            raise asyncio.TimeoutError()

        mock_ws_instance.recv = AsyncMock(side_effect=recv_side_effect)
        ws_module.connect = MagicMock(return_value=MockWSContextManager(mock_ws_instance))

        mock_motor_ia.is_speaking = True
        voice_panel._ws_connected = True
        voice_panel._ws_should_reconnect = False

        asyncio.run(voice_panel._ws_listener())
        mock_motor_ia.command_queue.put.assert_not_called()

    def test_ws_listener_skips_short_text(self, voice_panel, mock_motor_ia):
        ws_module = sys.modules["websockets"]
        MockWSContextManager = ws_module._MockWSContextManager

        mock_ws_instance = MagicMock()
        short_msg = json.dumps({"text": "hi"})
        call_count = 0

        def recv_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return short_msg
            voice_panel._ws_connected = False
            raise asyncio.TimeoutError()

        mock_ws_instance.recv = AsyncMock(side_effect=recv_side_effect)
        ws_module.connect = MagicMock(return_value=MockWSContextManager(mock_ws_instance))

        voice_panel._ws_connected = True
        voice_panel._ws_should_reconnect = False

        asyncio.run(voice_panel._ws_listener())
        mock_motor_ia.command_queue.put.assert_not_called()


# ===================================================================
# 10. WU 1.3 — Deletion absence guards (qwen_tts_extirpation_20260627)
# ===================================================================


class TestWU13DeletionGuards:
    """RED-first absence assertions for WU 1.3 removals.

    These replace the deleted fake-RMS-bar and unwired-recorder behavioral
    tests: they go RED while the members still exist and GREEN once removed.
    """

    def test_voice_control_has_no_unwired_recorder(self, voice_panel_class):
        VoiceControlPanel = voice_panel_class
        assert not hasattr(VoiceControlPanel, "start_recording")
        assert not hasattr(VoiceControlPanel, "set_dispositivo")
        # Siblings removed with the unwired duplicate recorder (L1).
        assert not hasattr(VoiceControlPanel, "stop_recording")
        assert not hasattr(VoiceControlPanel, "is_recording")
        assert not hasattr(VoiceControlPanel, "_hilo_grabacion")

    def test_voice_control_has_no_fake_rms_bar(
        self, voice_panel_class, mock_parent, ui_state, mock_logger
    ):
        VoiceControlPanel = voice_panel_class
        # Class-level: the fake (random-painted) RMS animation API is gone.
        assert not hasattr(VoiceControlPanel, "start_rms_animation")
        assert not hasattr(VoiceControlPanel, "stop_rms_animation")
        assert not hasattr(VoiceControlPanel, "_animar_rms")
        assert not hasattr(VoiceControlPanel, "update_rms")
        # Instance-level: building the panel must not create a barra_rms widget.
        panel = VoiceControlPanel(
            parent_frame=mock_parent, ui_state=ui_state, logger=mock_logger,
            on_log=mock_logger.info, on_motor_event=MagicMock(), on_pipeline_change=MagicMock(),
        )
        panel.create_voice_panel()
        assert not hasattr(panel, "barra_rms")
        panel.cleanup()
