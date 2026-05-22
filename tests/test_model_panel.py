"""Comprehensive tests for ui.model_panel.ModelPanel.

Covers:
- ModelPanel class initialization
- Model catalog building and display/tag mapping
- UI construction with correct widget creation
- Model selection handler (on_model_changed)
- Ollama state detection and button updates
- Download/activation flow
- UIState observer integration
- Edge cases (missing widgets, unknown states)
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from ui.state import UIState
from ui.protocols import CallbackDispatcher
from ui.model_panel import ModelPanel


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
    return CallbackDispatcher(source="ModelPanel")


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
            self.corner_radius = kwargs.get("corner_radius", 0)
            self.font = kwargs.get("font", None)

        def configure(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)
                self.kwargs[key] = value

        def pack(self, **kwargs):
            self.pack_kwargs = kwargs

        def pack_forget(self):
            self._packed = False

    class MockButton:
        def __init__(self, master=None, **kwargs):
            self.master = master
            self.kwargs = kwargs
            self.text = kwargs.get("text", "")
            self.state = kwargs.get("state", "normal")
            self.fg_color = kwargs.get("fg_color", "")
            self.hover_color = kwargs.get("hover_color", "")
            self.command = kwargs.get("command", None)

        def configure(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)
                self.kwargs[key] = value

        def pack(self, **kwargs):
            self.pack_kwargs = kwargs

        def pack_forget(self):
            self._packed = False

    class MockOptionMenu:
        def __init__(self, master=None, **kwargs):
            self.master = master
            self.kwargs = kwargs
            self.values = kwargs.get("values", [])
            self._value = self.values[0] if self.values else ""
            self.command = kwargs.get("command", None)
            self.state = kwargs.get("state", "normal")

        def set(self, value):
            self._value = value

        def get(self):
            return self._value

        def configure(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)
                self.kwargs[key] = value

        def pack(self, **kwargs):
            self.pack_kwargs = kwargs

        def pack_forget(self):
            self._packed = False

    class MockProgressBar:
        def __init__(self, master=None, **kwargs):
            self.master = master
            self.kwargs = kwargs
            self._value = 0.0
            self._packed = True

        def set(self, value):
            self._value = value

        def get(self):
            return self._value

        def pack(self, **kwargs):
            self._packed = True
            self.pack_kwargs = kwargs

        def pack_forget(self):
            self._packed = False

    class MockFont:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    mock_module.CTkLabel = MockLabel
    mock_module.CTkButton = MockButton
    mock_module.CTkOptionMenu = MockOptionMenu
    mock_module.CTkProgressBar = MockProgressBar
    mock_module.CTkFont = MockFont

    return mock_module


@pytest.fixture()
def mock_parent():
    """Mock parent frame."""
    return MagicMock()


@pytest.fixture()
def on_log():
    """Mock on_log callback."""
    return MagicMock()


@pytest.fixture()
def model_panel(mock_ctk, mock_parent, ui_state, dispatcher, on_log):
    """ModelPanel instance with mocked customtkinter, built."""
    from config.settings import DEFAULT_MODEL
    with patch("ui.model_panel.resolve_startup_model", return_value=(DEFAULT_MODEL, "default")):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = ModelPanel(
                parent_frame=mock_parent,
                ui_state=ui_state,
                dispatcher=dispatcher,
                on_log=on_log,
            )
            with patch("ui.model_panel.ctk", mock_ctk):
                panel.build()
        yield panel
        panel.cleanup()


# ---------------------------------------------------------------------------
# 1. ModelPanel Initialization
# ---------------------------------------------------------------------------


class TestModelPanelInit:
    def test_init_stores_references(self, mock_parent, ui_state, dispatcher, on_log):
        panel = ModelPanel(
            parent_frame=mock_parent,
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        assert panel._parent is mock_parent
        assert panel._ui_state is ui_state
        assert panel._dispatcher is dispatcher
        assert panel._on_log is on_log
        assert panel._observer_id is None
        assert panel._ollama_starting is False
        panel.cleanup()

    def test_init_builds_model_catalog(self, ui_state, dispatcher, on_log):
        panel = ModelPanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        assert len(panel._model_display_to_tag) > 0
        assert len(panel._model_tag_to_display) > 0
        assert len(panel._model_display_list) > 0
        panel.cleanup()

    def test_init_with_schedule_ui_update(self, ui_state, dispatcher, on_log):
        scheduler = MagicMock()
        panel = ModelPanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
            schedule_ui_update=scheduler,
        )
        assert panel._schedule_ui_update is scheduler
        panel.cleanup()


# ---------------------------------------------------------------------------
# 2. Model Catalog
# ---------------------------------------------------------------------------


class TestModelCatalog:
    def test_get_display_for_tag(self, model_panel):
        from config.settings import DEFAULT_MODEL, MODELS_CATALOG

        expected = MODELS_CATALOG[DEFAULT_MODEL]["display"]
        assert model_panel.get_display_for_tag(DEFAULT_MODEL) == expected

    def test_get_display_for_unknown_tag(self, model_panel):
        assert model_panel.get_display_for_tag("unknown_tag") == "unknown_tag"

    def test_get_tag_for_display(self, model_panel):
        from config.settings import DEFAULT_MODEL, MODELS_CATALOG

        display = MODELS_CATALOG[DEFAULT_MODEL]["display"]
        assert model_panel.get_tag_for_display(display) == DEFAULT_MODEL

    def test_get_tag_for_unknown_display(self, model_panel):
        assert model_panel.get_tag_for_display("Unknown Model") == "Unknown Model"

    def test_model_display_list_not_empty(self, model_panel):
        assert len(model_panel.model_display_list) > 0

    def test_default_display(self, model_panel):
        from config.settings import DEFAULT_MODEL, MODELS_CATALOG

        expected = MODELS_CATALOG[DEFAULT_MODEL]["display"]
        assert model_panel.default_display == expected


# ---------------------------------------------------------------------------
# 3. UI Construction
# ---------------------------------------------------------------------------


class TestUIConstruction:
    def test_build_creates_all_widgets(self, mock_ctk, mock_parent, ui_state, dispatcher, on_log):
        panel = ModelPanel(
            parent_frame=mock_parent,
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        with patch("ui.model_panel.ctk", mock_ctk):
            panel.build()

        assert panel.lbl_model_header is not None
        assert panel.combo_modelos is not None
        assert panel.btn_download is not None
        assert panel.lbl_modelo_info is not None
        assert panel.progress_download is not None
        panel.cleanup()

    def test_build_sets_default_model(self, mock_ctk, ui_state, dispatcher, on_log):
        panel = ModelPanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        with patch("ui.model_panel.ctk", mock_ctk):
            panel.build()

        assert panel.combo_modelos.get() == panel.default_display
        panel.cleanup()

    def test_build_subscribes_to_ui_state(self, mock_ctk, ui_state, dispatcher, on_log):
        panel = ModelPanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        with patch("ui.model_panel.ctk", mock_ctk):
            panel.build()

        assert panel._observer_id is not None
        assert panel._observer_id in ui_state._observers
        panel.cleanup()

    def test_progress_bar_hidden_by_default(self, mock_ctk, ui_state, dispatcher, on_log):
        panel = ModelPanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        with patch("ui.model_panel.ctk", mock_ctk):
            panel.build()

        assert panel.progress_download._packed is False
        panel.cleanup()

    def test_combo_populated_with_model_list(self, mock_ctk, ui_state, dispatcher, on_log):
        panel = ModelPanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        with patch("ui.model_panel.ctk", mock_ctk):
            panel.build()

        assert panel.combo_modelos.values == panel.model_display_list
        panel.cleanup()


# ---------------------------------------------------------------------------
# 4. Model Info Update
# ---------------------------------------------------------------------------


class TestModelInfoUpdate:
    def test_update_model_info(self, model_panel):
        from config.settings import MODELS_CATALOG

        tag = list(MODELS_CATALOG.keys())[0]
        with patch.object(model_panel, "_modelo_instalado", return_value=True):
            model_panel.update_model_info(tag)

        info = MODELS_CATALOG[tag]
        expected_text = f"{info['desc']} ({info['size_gb']}GB) \u2705"
        assert model_panel.lbl_modelo_info.text == expected_text

    def test_update_model_info_not_installed(self, model_panel):
        from config.settings import MODELS_CATALOG

        tag = list(MODELS_CATALOG.keys())[0]
        with patch.object(model_panel, "_modelo_instalado", return_value=False):
            model_panel.update_model_info(tag)

        info = MODELS_CATALOG[tag]
        expected_text = f"{info['desc']} ({info['size_gb']}GB) \u274c No instalado"
        assert model_panel.lbl_modelo_info.text == expected_text

    def test_update_model_info_unknown_tag(self, model_panel):
        model_panel.update_model_info("unknown_model")
        assert "Modelo personalizado" in model_panel.lbl_modelo_info.text

    def test_update_model_info_with_none_widget(self, model_panel):
        model_panel.lbl_modelo_info = None
        model_panel.update_model_info("some_tag")  # Should not raise


# ---------------------------------------------------------------------------
# 5. Button Update for Ollama State
# ---------------------------------------------------------------------------


class TestButtonUpdate:
    @pytest.mark.parametrize(
        "state,expected_text,expected_state",
        [
            ("checking", "Revisando Ollama...", "disabled"),
            ("package_missing", "Instalar dependencia Python", "normal"),
            ("app_missing", "Instalar Ollama", "normal"),
            ("service_stopped", "Iniciar Ollama", "normal"),
        ],
    )
    def test_button_for_ollama_states(
        self, model_panel, state, expected_text, expected_state
    ):
        model_panel._ui_state.ollama_state = state
        model_panel._update_button_for_ollama_state()
        assert model_panel.btn_download.text == expected_text
        assert model_panel.btn_download.state == expected_state

    def test_button_when_model_installed(self, model_panel):
        model_panel._ui_state.ollama_state = "ready"
        with patch.object(model_panel, "_modelo_instalado", return_value=True):
            model_panel._update_button_for_ollama_state("some_model")

        assert model_panel.btn_download.text == "Activar modelo"
        assert model_panel.btn_download.state == "normal"

    def test_button_when_model_not_installed(self, model_panel):
        model_panel._ui_state.ollama_state = "ready"
        with patch.object(model_panel, "_modelo_instalado", return_value=False):
            model_panel._update_button_for_ollama_state("some_model")

        assert model_panel.btn_download.text == "Descargar modelo"
        assert model_panel.btn_download.state == "normal"

    def test_button_when_ollama_starting(self, model_panel):
        model_panel._ollama_starting = True
        model_panel._update_button_for_ollama_state()
        assert model_panel.btn_download.text == "Iniciando Ollama..."
        assert model_panel.btn_download.state == "disabled"

    def test_button_with_none_widget(self, model_panel):
        model_panel.btn_download = None
        model_panel._update_button_for_ollama_state()  # Should not raise


# ---------------------------------------------------------------------------
# 6. Model Selection Handler
# ---------------------------------------------------------------------------


class TestModelSelection:
    def test_on_model_changed_installed_model(self, model_panel, dispatcher):
        from config.settings import MODELS_CATALOG

        tag = list(MODELS_CATALOG.keys())[0]
        display = MODELS_CATALOG[tag]["display"]
        model_panel._ui_state.ollama_state = "ready"

        received = []
        dispatcher.subscribe("on_switch_model", lambda t: received.append(t))

        with patch.object(model_panel, "_modelo_instalado", return_value=True):
            with patch.object(model_panel, "update_model_info"):
                model_panel._on_model_changed(display)

        assert tag in received
        model_panel._on_log.assert_called()

    def test_on_model_changed_not_installed(self, model_panel, dispatcher):
        from config.settings import MODELS_CATALOG

        tag = list(MODELS_CATALOG.keys())[0]
        display = MODELS_CATALOG[tag]["display"]
        model_panel._ui_state.ollama_state = "ready"

        received = []
        dispatcher.subscribe("on_switch_model", lambda t: received.append(t))

        with patch.object(model_panel, "_modelo_instalado", return_value=False):
            with patch.object(model_panel, "update_model_info"):
                model_panel._on_model_changed(display)

        assert len(received) == 0
        model_panel._on_log.assert_called()

    def test_on_model_changed_ollama_not_ready(self, model_panel, dispatcher):
        from config.settings import MODELS_CATALOG

        tag = list(MODELS_CATALOG.keys())[0]
        display = MODELS_CATALOG[tag]["display"]
        model_panel._ui_state.ollama_state = "app_missing"

        received = []
        dispatcher.subscribe("on_switch_model", lambda t: received.append(t))

        with patch.object(model_panel, "update_model_info"):
            model_panel._on_model_changed(display)

        assert len(received) == 0


# ---------------------------------------------------------------------------
# 7. Download Model Handler
# ---------------------------------------------------------------------------


class TestDownloadModel:
    def test_on_download_app_missing(self, model_panel):
        with patch.object(model_panel, "_detectar_estado_ollama", return_value="app_missing"):
            with patch("ui.model_panel.webbrowser.open") as mock_open:
                model_panel._on_download_model()

        mock_open.assert_called_once_with("https://ollama.com/download")

    def test_on_download_package_missing(self, model_panel):
        with patch.object(model_panel, "_detectar_estado_ollama", return_value="package_missing"):
            with patch("ui.model_panel.messagebox.showwarning") as mock_warn:
                model_panel._on_download_model()

        mock_warn.assert_called_once()

    def test_on_download_service_stopped(self, model_panel):
        with patch.object(model_panel, "_detectar_estado_ollama", return_value="service_stopped"):
            with patch.object(model_panel, "_iniciar_ollama") as mock_start:
                model_panel._on_download_model()

        mock_start.assert_called_once()

    def test_on_download_activates_installed_model(self, model_panel, dispatcher):
        model_panel._ui_state.ollama_state = "ready"
        from config.settings import MODELS_CATALOG

        tag = list(MODELS_CATALOG.keys())[0]
        display = MODELS_CATALOG[tag]["display"]
        model_panel.combo_modelos.set(display)

        received = []
        dispatcher.subscribe("on_switch_model", lambda t: received.append(t))

        with patch.object(model_panel, "refresh_ollama_state"):
            model_panel._ui_state.ollama_state = "ready"
            with patch.object(model_panel, "_modelo_instalado", return_value=True):
                with patch.object(model_panel, "_update_button_for_ollama_state"):
                    model_panel._on_download_model()

        assert tag in received


# ---------------------------------------------------------------------------
# 8. Ollama State Detection
# ---------------------------------------------------------------------------


class TestOllamaStateDetection:
    def test_detect_package_missing(self, model_panel):
        with patch.dict("sys.modules", {"ollama": None}):
            # Force ImportError by making the import fail
            import sys

            original = sys.modules.get("ollama")
            sys.modules["ollama"] = None
            try:
                # Reimport to trigger the check
                state = model_panel._detectar_estado_ollama()
            finally:
                if original is not None:
                    sys.modules["ollama"] = original
                else:
                    del sys.modules["ollama"]

    def test_find_ollama_in_path(self, model_panel):
        with patch("shutil.which", return_value="/usr/bin/ollama"):
            exe = model_panel._find_ollama_executable()
        assert exe == "/usr/bin/ollama"

    def test_find_ollama_not_found(self, model_panel):
        with patch("shutil.which", return_value=None):
            import os

            with patch.dict(os.environ, {"LOCALAPPDATA": "", "ProgramFiles": ""}):
                exe = model_panel._find_ollama_executable()
        assert exe is None

    def test_modelo_instalado_true(self, model_panel):
        mock_ollama = MagicMock()
        mock_model = MagicMock()
        mock_model.model = "llama3.2:latest"
        mock_ollama.list.return_value.models = [mock_model]

        with patch.dict("sys.modules", {"ollama": mock_ollama}):
            result = model_panel._modelo_instalado("llama3.2")
        assert result is True

    def test_modelo_instalado_false(self, model_panel):
        mock_ollama = MagicMock()
        mock_ollama.list.return_value.models = []

        with patch.dict("sys.modules", {"ollama": mock_ollama}):
            result = model_panel._modelo_instalado("unknown")
        assert result is False

    def test_modelo_instalado_exception(self, model_panel):
        mock_ollama = MagicMock()
        mock_ollama.list.side_effect = Exception("connection error")

        with patch.dict("sys.modules", {"ollama": mock_ollama}):
            result = model_panel._modelo_instalado("some_model")
        assert result is False


# ---------------------------------------------------------------------------
# 9. Public Methods
# ---------------------------------------------------------------------------


class TestPublicMethods:
    def test_set_model_selection(self, model_panel):
        model_panel.set_model_selection("Some Model")
        assert model_panel.combo_modelos.get() == "Some Model"

    def test_get_selected_display(self, model_panel):
        model_panel.combo_modelos.set("Test Display")
        assert model_panel.get_selected_display() == "Test Display"

    def test_get_selected_tag(self, model_panel):
        from config.settings import DEFAULT_MODEL

        display = model_panel.default_display
        model_panel.combo_modelos.set(display)
        assert model_panel.get_selected_tag() == DEFAULT_MODEL

    def test_set_download_progress_visible_show(self, model_panel):
        model_panel.set_download_progress_visible(True)
        assert model_panel.progress_download._packed is True

    def test_set_download_progress_visible_hide(self, model_panel):
        model_panel.set_download_progress_visible(False)
        assert model_panel.progress_download._packed is False

    def test_set_download_progress(self, model_panel):
        model_panel.set_download_progress(0.5)
        assert model_panel.progress_download._value == 0.5

    def test_set_model_combo_state(self, model_panel):
        model_panel.set_model_combo_state("disabled")
        assert model_panel.combo_modelos.state == "disabled"

    def test_set_download_progress_with_none_widget(self, model_panel):
        model_panel.progress_download = None
        model_panel.set_download_progress(0.5)  # Should not raise

    def test_set_model_combo_state_with_none_widget(self, model_panel):
        model_panel.combo_modelos = None
        model_panel.set_model_combo_state("disabled")  # Should not raise


# ---------------------------------------------------------------------------
# 10. UIState Observer Integration
# ---------------------------------------------------------------------------


class TestObserverIntegration:
    def test_subscribe_called_on_build(self, mock_ctk, ui_state, dispatcher, on_log):
        panel = ModelPanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        with patch("ui.model_panel.ctk", mock_ctk):
            panel.build()

        assert panel._observer_id is not None
        assert panel._observer_id in ui_state._observers
        panel.cleanup()

    def test_cleanup_removes_observer(self, mock_ctk, ui_state, dispatcher, on_log):
        panel = ModelPanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        with patch("ui.model_panel.ctk", mock_ctk):
            panel.build()

        sub_id = panel._observer_id
        panel.cleanup()
        assert panel._observer_id is None
        assert sub_id not in ui_state._observers

    def test_cleanup_is_idempotent(self, mock_ctk, ui_state, dispatcher, on_log):
        panel = ModelPanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        with patch("ui.model_panel.ctk", mock_ctk):
            panel.build()

        panel.cleanup()
        panel.cleanup()  # Should not raise
        assert panel._observer_id is None

    def test_on_state_change_ignores_unknown_keys(self, model_panel):
        original_text = model_panel.btn_download.text
        model_panel._on_state_change("unknown_key", "some_value")
        # Button text should not change
        assert model_panel.btn_download.text == original_text

    def test_ollama_state_change_triggers_button_update(self, mock_ctk, ui_state, dispatcher, on_log):
        panel = ModelPanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        with patch("ui.model_panel.ctk", mock_ctk):
            panel.build()

        # Prevent live ollama call during button update
        with patch.object(panel, "_modelo_instalado", return_value=False):
            ui_state.ollama_state = "ready"
            time.sleep(0.15)
            # Button should have been updated (text won't be "Revisando Ollama...")
            assert panel.btn_download.text != "Revisando Ollama..."
        panel.cleanup()


# ---------------------------------------------------------------------------
# 11. Refresh Ollama State
# ---------------------------------------------------------------------------


class TestRefreshOllamaState:
    def test_refresh_updates_state(self, model_panel):
        with patch.object(model_panel, "_detectar_estado_ollama", return_value="ready"):
            model_panel.refresh_ollama_state()

        assert model_panel._ui_state.ollama_state == "ready"

    def test_refresh_calls_check_callback_when_ready(self, model_panel):
        mock_check = MagicMock()
        with patch.object(model_panel, "_detectar_estado_ollama", return_value="ready"):
            model_panel.refresh_ollama_state(on_check_ollama=mock_check)

        mock_check.assert_called_once()

    def test_refresh_does_not_call_callback_when_not_ready(self, model_panel):
        mock_check = MagicMock()
        with patch.object(model_panel, "_detectar_estado_ollama", return_value="app_missing"):
            model_panel.refresh_ollama_state(on_check_ollama=mock_check)

        mock_check.assert_not_called()
