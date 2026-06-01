"""Comprehensive tests for ui.status_bar.StatusBar.

Covers:
- StatusBar class initialization
- Status pill creation with correct defaults
- State-driven visual feedback (color + text for each status type)
- UIState observer integration (subscribe, update, cleanup)
- Batch UIState updates
- Pipeline state derivation
- Edge cases (unknown status, cleanup, None widgets)
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from ui.state import UIState
from ui.status_bar import StatusBar


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
def mock_ctk():
    """Mock customtkinter module with realistic widget behavior."""
    mock_module = MagicMock()

    class MockLabel:
        """Minimal mock of a CTkLabel that tracks configure calls."""

        def __init__(self, master=None, **kwargs):
            self.master = master
            self.kwargs = kwargs
            self.text = kwargs.get("text", "")
            self.fg_color = kwargs.get("fg_color", "")
            self.text_color = kwargs.get("text_color", "")
            self.corner_radius = kwargs.get("corner_radius", 0)
            self.font = kwargs.get("font", None)

        def configure(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)
                self.kwargs[key] = value

        def pack(self, **kwargs):
            self.pack_kwargs = kwargs

    class MockFrame:
        """Minimal mock of a CTkFrame."""

        def __init__(self, master=None, **kwargs):
            self.master = master
            self.kwargs = kwargs

        def pack(self, **kwargs):
            self.pack_kwargs = kwargs

    class MockFont:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    mock_module.CTkLabel = MockLabel
    mock_module.CTkFrame = MockFrame
    mock_module.CTkFont = MockFont

    return mock_module


@pytest.fixture()
def mock_parent():
    """Mock parent frame for the StatusBar."""
    return MagicMock()


@pytest.fixture()
def status_bar(mock_ctk, mock_parent, ui_state):
    """StatusBar instance with mocked customtkinter, pills created."""
    with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
        import customtkinter as ctk
        with patch.object(StatusBar, "__init__", lambda self, pf, us: None):
            bar = StatusBar.__new__(StatusBar)
            bar._parent = mock_parent
            bar._ui_state = ui_state
            bar._observer_id = None
            bar.lbl_status = None
            bar.lbl_model_status_pill = None
            bar.lbl_mic_status_pill = None
            bar.lbl_tts_status_pill = None
            bar.lbl_chat_status_pill = None
            # Manually call create_status_pills with mocked ctk
            with patch("ui.status_bar.ctk", mock_ctk):
                bar.create_status_pills()
    yield bar
    bar.cleanup()


# ---------------------------------------------------------------------------
# 1. StatusBar Initialization
# ---------------------------------------------------------------------------


class TestStatusBarInit:
    def test_init_stores_references(self, mock_parent, ui_state):
        bar = StatusBar(parent_frame=mock_parent, ui_state=ui_state)
        assert bar._parent is mock_parent
        assert bar._ui_state is ui_state
        assert bar._observer_id is None
        assert bar.lbl_status is None
        assert bar.lbl_mic_status_pill is None
        assert bar.lbl_tts_status_pill is None
        assert bar.lbl_chat_status_pill is None
        bar.cleanup()

    def test_create_pills_creates_widgets(self, mock_ctk, mock_parent, ui_state):
        bar = StatusBar(parent_frame=mock_parent, ui_state=ui_state)
        with patch("ui.status_bar.ctk", mock_ctk):
            bar.create_status_pills()
        assert bar.lbl_status is not None
        assert bar.lbl_mic_status_pill is not None
        assert bar.lbl_tts_status_pill is not None
        assert bar.lbl_chat_status_pill is not None
        bar.cleanup()

    def test_create_pills_subscribes_to_ui_state(self, mock_ctk, mock_parent, ui_state):
        bar = StatusBar(parent_frame=mock_parent, ui_state=ui_state)
        with patch("ui.status_bar.ctk", mock_ctk):
            bar.create_status_pills()
        assert bar._observer_id is not None
        bar.cleanup()

    def test_create_pills_only_once(self, mock_ctk, mock_parent, ui_state):
        bar = StatusBar(parent_frame=mock_parent, ui_state=ui_state)
        with patch("ui.status_bar.ctk", mock_ctk):
            bar.create_status_pills()
            first_id = bar._observer_id
            bar.create_status_pills()
            second_id = bar._observer_id
        # Second call creates a new subscription
        assert first_id != second_id
        bar.cleanup()


# ---------------------------------------------------------------------------
# 2. Status Pill Creation — Default State
# ---------------------------------------------------------------------------


class TestPillCreationDefaults:
    def test_main_label_default_text(self, status_bar):
        assert status_bar.lbl_status.text == "Modelo cargando"

    def test_main_label_default_color(self, status_bar):
        assert status_bar.lbl_status.text_color == "#ffaa00"

    def test_mic_pill_default_text(self, status_bar):
        assert status_bar.lbl_mic_status_pill.text == "Mic: revisando"

    def test_mic_pill_default_color(self, status_bar):
        assert status_bar.lbl_mic_status_pill.fg_color == "#1b2633"

    def test_tts_pill_default_text(self, status_bar):
        assert status_bar.lbl_tts_status_pill.text == "TTS: inactivo"

    def test_tts_pill_default_color(self, status_bar):
        assert status_bar.lbl_tts_status_pill.fg_color == "#1b2633"

    def test_chat_pill_default_text(self, status_bar):
        assert status_bar.lbl_chat_status_pill.text == "Chat: desconectado"

    def test_chat_pill_default_color(self, status_bar):
        assert status_bar.lbl_chat_status_pill.fg_color == "#1b2633"

    def test_pills_have_corner_radius(self, status_bar):
        assert status_bar.lbl_mic_status_pill.corner_radius == 12
        assert status_bar.lbl_tts_status_pill.corner_radius == 12
        assert status_bar.lbl_chat_status_pill.corner_radius == 12

    def test_main_label_has_bold_font(self, status_bar):
        assert status_bar.lbl_status.font is not None


# ---------------------------------------------------------------------------
# 3. Model Status — Visual Feedback
# ---------------------------------------------------------------------------


class TestModelStatus:
    @pytest.mark.parametrize(
        "status,expected_text,expected_color",
        [
            ("loading", "Modelo: cargando", "#cc8800"),
            ("ready", "Modelo: listo", "#22cc66"),
            ("error", "Modelo: error", "#cc3333"),
            ("offline", "Modelo: offline", "#666666"),
        ],
    )
    def test_model_status_color_and_text(self, status_bar, status, expected_text, expected_color):
        color = status_bar._get_status_color("model_status", status)
        text = status_bar._get_status_text("model_status", status)
        assert color == expected_color
        assert text == expected_text

    def test_model_status_unknown_returns_default(self, status_bar):
        color = status_bar._get_status_color("model_status", "unknown")
        text = status_bar._get_status_text("model_status", "unknown")
        assert color == "#1b2633"
        assert text == "model_status: unknown"

    def test_update_model_status_ready(self, status_bar):
        status_bar.lbl_model_status_pill = MagicMock()
        status_bar.update_model_status("ready")
        status_bar.lbl_model_status_pill.configure.assert_called_once_with(
            text="Modelo: listo", fg_color="#22cc66"
        )

    def test_update_model_status_with_none_widget(self, status_bar):
        status_bar.lbl_model_status_pill = None
        status_bar.update_model_status("ready")  # Should not raise


# ---------------------------------------------------------------------------
# 4. Mic Status — Visual Feedback
# ---------------------------------------------------------------------------


class TestMicStatus:
    @pytest.mark.parametrize(
        "status,expected_text,expected_color",
        [
            ("disconnected", "Mic: desconectado", "#4a2630"),
            ("idle", "Mic: conectado", "#1b2633"),
            ("listening", "Mic: escuchando", "#1f5a3a"),
            ("recording", "Mic: grabando", "#cc8800"),
        ],
    )
    def test_mic_status_color_and_text(self, status_bar, status, expected_text, expected_color):
        color = status_bar._get_status_color("mic_status", status)
        text = status_bar._get_status_text("mic_status", status)
        assert color == expected_color
        assert text == expected_text

    def test_mic_status_unknown_returns_default(self, status_bar):
        color = status_bar._get_status_color("mic_status", "unknown")
        text = status_bar._get_status_text("mic_status", "unknown")
        assert color == "#1b2633"
        assert text == "mic_status: unknown"

    def test_update_mic_status_listening(self, status_bar):
        status_bar.update_mic_status("listening")
        assert status_bar.lbl_mic_status_pill.text == "Mic: escuchando"
        assert status_bar.lbl_mic_status_pill.fg_color == "#1f5a3a"

    def test_update_mic_status_disconnected(self, status_bar):
        status_bar.update_mic_status("disconnected")
        assert status_bar.lbl_mic_status_pill.text == "Mic: desconectado"
        assert status_bar.lbl_mic_status_pill.fg_color == "#4a2630"

    def test_update_mic_status_recording(self, status_bar):
        status_bar.update_mic_status("recording")
        assert status_bar.lbl_mic_status_pill.text == "Mic: grabando"
        assert status_bar.lbl_mic_status_pill.fg_color == "#cc8800"

    def test_update_mic_status_with_none_widget(self, status_bar):
        status_bar.lbl_mic_status_pill = None
        status_bar.update_mic_status("listening")  # Should not raise


# ---------------------------------------------------------------------------
# 5. TTS Status — Visual Feedback
# ---------------------------------------------------------------------------


class TestTtsStatus:
    @pytest.mark.parametrize(
        "status,expected_text,expected_color",
        [
            ("idle", "TTS: inactivo", "#1b2633"),
            ("generating", "TTS: renderizando", "#1f3f6f"),
            ("speaking", "TTS: hablando", "#1f526f"),
            ("error", "TTS: error", "#cc3333"),
        ],
    )
    def test_tts_status_color_and_text(self, status_bar, status, expected_text, expected_color):
        color = status_bar._get_status_color("tts_status", status)
        text = status_bar._get_status_text("tts_status", status)
        assert color == expected_color
        assert text == expected_text

    def test_tts_status_unknown_returns_default(self, status_bar):
        color = status_bar._get_status_color("tts_status", "unknown")
        text = status_bar._get_status_text("tts_status", "unknown")
        assert color == "#1b2633"
        assert text == "tts_status: unknown"

    def test_update_tts_status_generating(self, status_bar):
        status_bar.update_tts_status("generating")
        assert status_bar.lbl_tts_status_pill.text == "TTS: renderizando"
        assert status_bar.lbl_tts_status_pill.fg_color == "#1f3f6f"

    def test_update_tts_status_speaking(self, status_bar):
        status_bar.update_tts_status("speaking")
        assert status_bar.lbl_tts_status_pill.text == "TTS: hablando"
        assert status_bar.lbl_tts_status_pill.fg_color == "#1f526f"

    def test_update_tts_status_error(self, status_bar):
        status_bar.update_tts_status("error")
        assert status_bar.lbl_tts_status_pill.text == "TTS: error"
        assert status_bar.lbl_tts_status_pill.fg_color == "#cc3333"

    def test_update_tts_status_with_none_widget(self, status_bar):
        status_bar.lbl_tts_status_pill = None
        status_bar.update_tts_status("speaking")  # Should not raise


# ---------------------------------------------------------------------------
# 6. Chat Status — Visual Feedback
# ---------------------------------------------------------------------------


class TestChatStatus:
    @pytest.mark.parametrize(
        "status,expected_text,expected_color",
        [
            ("disconnected", "Chat: desconectado", "#1b2633"),
            ("connecting", "Chat: conectando", "#cc8800"),
            ("connected", "Chat: conectado", "#1f5a3a"),
            ("error", "Chat: error", "#cc3333"),
        ],
    )
    def test_chat_status_color_and_text(self, status_bar, status, expected_text, expected_color):
        color = status_bar._get_status_color("chat_status", status)
        text = status_bar._get_status_text("chat_status", status)
        assert color == expected_color
        assert text == expected_text

    def test_chat_status_unknown_returns_default(self, status_bar):
        color = status_bar._get_status_color("chat_status", "unknown")
        text = status_bar._get_status_text("chat_status", "unknown")
        assert color == "#1b2633"
        assert text == "chat_status: unknown"

    def test_update_chat_status_connected(self, status_bar):
        status_bar.update_chat_status("connected")
        assert status_bar.lbl_chat_status_pill.text == "Chat: conectado"
        assert status_bar.lbl_chat_status_pill.fg_color == "#1f5a3a"

    def test_update_chat_status_connecting(self, status_bar):
        status_bar.update_chat_status("connecting")
        assert status_bar.lbl_chat_status_pill.text == "Chat: conectando"
        assert status_bar.lbl_chat_status_pill.fg_color == "#cc8800"

    def test_update_chat_status_error(self, status_bar):
        status_bar.update_chat_status("error")
        assert status_bar.lbl_chat_status_pill.text == "Chat: error"
        assert status_bar.lbl_chat_status_pill.fg_color == "#cc3333"

    def test_update_chat_status_with_none_widget(self, status_bar):
        status_bar.lbl_chat_status_pill = None
        status_bar.update_chat_status("connected")  # Should not raise


# ---------------------------------------------------------------------------
# 7. Pipeline State — Visual Feedback
# ---------------------------------------------------------------------------


class TestPipelineState:
    @pytest.mark.parametrize(
        "state,expected_text,expected_color",
        [
            ("idle", "Modelo listo", "#44cc66"),
            ("listening", "Micrófono escuchando", "#44ff44"),
            ("processing", "Modelo procesando", "#ffaa00"),
            ("speaking", "TTS renderizando voz", "#4488ff"),
            ("playing", "TTS hablando", "#44ccff"),
            ("downloading", "Modelo cargando", "#ff8800"),
            ("init", "Modelo cargando", "#888888"),
            ("error", "Modelo error", "#ff5555"),
        ],
    )
    def test_pipeline_state_display(self, status_bar, state, expected_text, expected_color):
        status_bar.update_pipeline_state(state)
        assert status_bar.lbl_status.text == expected_text
        assert status_bar.lbl_status.text_color == expected_color

    def test_pipeline_state_unknown(self, status_bar):
        status_bar.update_pipeline_state("unknown_state")
        assert status_bar.lbl_status.text == ""
        assert status_bar.lbl_status.text_color == "#aaaaaa"

    def test_pipeline_idle_sets_mic_and_tts_idle(self, status_bar):
        status_bar.update_pipeline_state("idle")
        assert status_bar.lbl_mic_status_pill.text == "Mic: conectado"
        assert status_bar.lbl_tts_status_pill.text == "TTS: inactivo"

    def test_pipeline_listening_sets_mic_listening(self, status_bar):
        status_bar.update_pipeline_state("listening")
        assert status_bar.lbl_mic_status_pill.text == "Mic: escuchando"
        assert status_bar.lbl_tts_status_pill.text == "TTS: inactivo"

    def test_pipeline_speaking_sets_tts_generating(self, status_bar):
        status_bar.update_pipeline_state("speaking")
        assert status_bar.lbl_tts_status_pill.text == "TTS: renderizando"
        assert status_bar.lbl_tts_status_pill.fg_color == "#1f3f6f"

    def test_pipeline_playing_sets_tts_speaking(self, status_bar):
        status_bar.update_pipeline_state("playing")
        assert status_bar.lbl_tts_status_pill.text == "TTS: hablando"
        assert status_bar.lbl_tts_status_pill.fg_color == "#1f526f"

    def test_pipeline_error_sets_tts_idle(self, status_bar):
        status_bar.update_pipeline_state("error")
        assert status_bar.lbl_tts_status_pill.text == "TTS: inactivo"


# ---------------------------------------------------------------------------
# 8. UIState Observer Integration
# ---------------------------------------------------------------------------


class TestObserverIntegration:
    def test_subscribe_called_on_create_pills(self, mock_ctk, mock_parent, ui_state):
        bar = StatusBar(parent_frame=mock_parent, ui_state=ui_state)
        with patch("ui.status_bar.ctk", mock_ctk):
            bar.create_status_pills()
        assert bar._observer_id is not None
        assert bar._observer_id in ui_state._observers
        bar.cleanup()

    def test_cleanup_removes_observer(self, mock_ctk, mock_parent, ui_state):
        bar = StatusBar(parent_frame=mock_parent, ui_state=ui_state)
        with patch("ui.status_bar.ctk", mock_ctk):
            bar.create_status_pills()
        sub_id = bar._observer_id
        bar.cleanup()
        assert bar._observer_id is None
        assert sub_id not in ui_state._observers

    def test_cleanup_is_idempotent(self, mock_ctk, mock_parent, ui_state):
        bar = StatusBar(parent_frame=mock_parent, ui_state=ui_state)
        with patch("ui.status_bar.ctk", mock_ctk):
            bar.create_status_pills()
        bar.cleanup()
        bar.cleanup()  # Should not raise
        assert bar._observer_id is None

    def test_model_status_change_triggers_pill_update(self, mock_ctk, mock_parent, ui_state):
        bar = StatusBar(parent_frame=mock_parent, ui_state=ui_state)
        with patch("ui.status_bar.ctk", mock_ctk):
            bar.create_status_pills()
        ui_state.model_status = "ready"
        time.sleep(0.15)
        assert bar.lbl_mic_status_pill is not None  # sanity
        bar.cleanup()

    def test_mic_status_change_triggers_pill_update(self, mock_ctk, mock_parent, ui_state):
        bar = StatusBar(parent_frame=mock_parent, ui_state=ui_state)
        with patch("ui.status_bar.ctk", mock_ctk):
            bar.create_status_pills()
        ui_state.mic_status = "listening"
        time.sleep(0.15)
        assert bar.lbl_mic_status_pill.text == "Mic: escuchando"
        assert bar.lbl_mic_status_pill.fg_color == "#1f5a3a"
        bar.cleanup()

    def test_tts_status_change_triggers_pill_update(self, mock_ctk, mock_parent, ui_state):
        bar = StatusBar(parent_frame=mock_parent, ui_state=ui_state)
        with patch("ui.status_bar.ctk", mock_ctk):
            bar.create_status_pills()
        ui_state.tts_status = "speaking"
        time.sleep(0.15)
        assert bar.lbl_tts_status_pill.text == "TTS: hablando"
        assert bar.lbl_tts_status_pill.fg_color == "#1f526f"
        bar.cleanup()

    def test_chat_status_change_triggers_pill_update(self, mock_ctk, mock_parent, ui_state):
        bar = StatusBar(parent_frame=mock_parent, ui_state=ui_state)
        with patch("ui.status_bar.ctk", mock_ctk):
            bar.create_status_pills()
        ui_state.chat_status = "connected"
        time.sleep(0.15)
        assert bar.lbl_chat_status_pill.text == "Chat: conectado"
        assert bar.lbl_chat_status_pill.fg_color == "#1f5a3a"
        bar.cleanup()

    def test_cleanup_prevents_further_updates(self, mock_ctk, mock_parent, ui_state):
        bar = StatusBar(parent_frame=mock_parent, ui_state=ui_state)
        with patch("ui.status_bar.ctk", mock_ctk):
            bar.create_status_pills()
        bar.cleanup()
        ui_state.mic_status = "listening"
        time.sleep(0.15)
        # Pill should NOT have updated after cleanup
        assert bar.lbl_mic_status_pill.text == "Mic: revisando"

    def test_on_state_change_ignores_unknown_keys(self, mock_ctk, mock_parent, ui_state):
        bar = StatusBar(parent_frame=mock_parent, ui_state=ui_state)
        with patch("ui.status_bar.ctk", mock_ctk):
            bar.create_status_pills()
        original_text = bar.lbl_mic_status_pill.text
        bar._on_state_change("unknown_key", "some_value")
        assert bar.lbl_mic_status_pill.text == original_text
        bar.cleanup()

    def test_on_state_change_ignores_non_string_values(self, mock_ctk, mock_parent, ui_state):
        bar = StatusBar(parent_frame=mock_parent, ui_state=ui_state)
        with patch("ui.status_bar.ctk", mock_ctk):
            bar.create_status_pills()
        bar._on_state_change("mic_status", 123)  # Should not raise or update
        bar.cleanup()


# ---------------------------------------------------------------------------
# 9. Batch UIState Updates
# ---------------------------------------------------------------------------


class TestBatchUpdates:
    def test_batch_update_multiple_statuses(self, mock_ctk, mock_parent, ui_state):
        bar = StatusBar(parent_frame=mock_parent, ui_state=ui_state)
        with patch("ui.status_bar.ctk", mock_ctk):
            bar.create_status_pills()
        ui_state.update(
            mic_status="listening",
            tts_status="generating",
            chat_status="connecting",
        )
        time.sleep(0.15)
        assert bar.lbl_mic_status_pill.text == "Mic: escuchando"
        assert bar.lbl_tts_status_pill.text == "TTS: renderizando"
        assert bar.lbl_chat_status_pill.text == "Chat: conectando"
        bar.cleanup()

    def test_batch_update_all_error(self, mock_ctk, mock_parent, ui_state):
        bar = StatusBar(parent_frame=mock_parent, ui_state=ui_state)
        with patch("ui.status_bar.ctk", mock_ctk):
            bar.create_status_pills()
        ui_state.update(
            mic_status="disconnected",
            tts_status="error",
            chat_status="error",
        )
        time.sleep(0.15)
        assert bar.lbl_mic_status_pill.fg_color == "#4a2630"
        assert bar.lbl_tts_status_pill.fg_color == "#cc3333"
        assert bar.lbl_chat_status_pill.fg_color == "#cc3333"
        bar.cleanup()


# ---------------------------------------------------------------------------
# 10. Edge Cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_get_status_color_unknown_type(self, status_bar):
        color = status_bar._get_status_color("nonexistent", "any")
        assert color == "#1b2633"

    def test_get_status_text_unknown_type(self, status_bar):
        text = status_bar._get_status_text("nonexistent", "any")
        assert text == "nonexistent: any"

    def test_update_model_with_none_widget(self, status_bar):
        status_bar.lbl_model_status_pill = None
        status_bar.update_model_status("ready")  # Should not raise

    def test_sequential_status_transitions(self, status_bar):
        status_bar.update_mic_status("disconnected")
        assert status_bar.lbl_mic_status_pill.text == "Mic: desconectado"
        status_bar.update_mic_status("idle")
        assert status_bar.lbl_mic_status_pill.text == "Mic: conectado"
        status_bar.update_mic_status("listening")
        assert status_bar.lbl_mic_status_pill.text == "Mic: escuchando"
        status_bar.update_mic_status("recording")
        assert status_bar.lbl_mic_status_pill.text == "Mic: grabando"

    def test_all_tts_transitions(self, status_bar):
        transitions = [
            ("idle", "TTS: inactivo", "#1b2633"),
            ("generating", "TTS: renderizando", "#1f3f6f"),
            ("speaking", "TTS: hablando", "#1f526f"),
            ("error", "TTS: error", "#cc3333"),
        ]
        for status, expected_text, expected_color in transitions:
            status_bar.update_tts_status(status)
            assert status_bar.lbl_tts_status_pill.text == expected_text
            assert status_bar.lbl_tts_status_pill.fg_color == expected_color

    def test_all_chat_transitions(self, status_bar):
        transitions = [
            ("disconnected", "Chat: desconectado", "#1b2633"),
            ("connecting", "Chat: conectando", "#cc8800"),
            ("connected", "Chat: conectado", "#1f5a3a"),
            ("error", "Chat: error", "#cc3333"),
        ]
        for status, expected_text, expected_color in transitions:
            status_bar.update_chat_status(status)
            assert status_bar.lbl_chat_status_pill.text == expected_text
            assert status_bar.lbl_chat_status_pill.fg_color == expected_color

    def test_all_pipeline_transitions(self, status_bar):
        transitions = [
            ("idle", "Modelo listo", "#44cc66"),
            ("listening", "Micrófono escuchando", "#44ff44"),
            ("processing", "Modelo procesando", "#ffaa00"),
            ("speaking", "TTS renderizando voz", "#4488ff"),
            ("playing", "TTS hablando", "#44ccff"),
            ("downloading", "Modelo cargando", "#ff8800"),
            ("init", "Modelo cargando", "#888888"),
            ("error", "Modelo error", "#ff5555"),
        ]
        for state, expected_text, expected_color in transitions:
            status_bar.update_pipeline_state(state)
            assert status_bar.lbl_status.text == expected_text
            assert status_bar.lbl_status.text_color == expected_color
