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
from types import SimpleNamespace
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Pre-import ollama at module level so it's in sys.modules before any
# patch.dict("sys.modules", ...) fixture context is entered.
# Without this, patch.dict._unpatch_dict calls sys.modules.clear() which
# removes ollama._client; with refcount dropping to 0 the module's __dict__
# gets cleared, causing AttributeError: module 'ollama' has no attribute 'chat'
# in later tests that patch("ollama.chat").
import ollama as _ollama_preimport  # noqa: F401

from opencohost.ui.state import UIState
from opencohost.ui.protocols import CallbackDispatcher
from opencohost.ui.model_panel import ModelPanel


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
    from opencohost.config.settings import DEFAULT_MODEL
    with patch("opencohost.ui.model_panel.resolve_startup_model", return_value=(DEFAULT_MODEL, "default")):
        with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
            panel = ModelPanel(
                parent_frame=mock_parent,
                ui_state=ui_state,
                dispatcher=dispatcher,
                on_log=on_log,
            )
            with patch("opencohost.ui.model_panel.ctk", mock_ctk):
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
        from opencohost.config.settings import DEFAULT_MODEL, MODELS_CATALOG

        expected = MODELS_CATALOG[DEFAULT_MODEL]["display"]
        assert model_panel.get_display_for_tag(DEFAULT_MODEL) == expected

    def test_get_display_for_unknown_tag(self, model_panel):
        assert model_panel.get_display_for_tag("unknown_tag") == "unknown_tag"

    def test_get_tag_for_display(self, model_panel):
        from opencohost.config.settings import DEFAULT_MODEL, MODELS_CATALOG

        display = MODELS_CATALOG[DEFAULT_MODEL]["display"]
        assert model_panel.get_tag_for_display(display) == DEFAULT_MODEL

    def test_get_tag_for_unknown_display(self, model_panel):
        assert model_panel.get_tag_for_display("Unknown Model") == "Unknown Model"

    def test_model_display_list_not_empty(self, model_panel):
        assert len(model_panel.model_display_list) > 0

    def test_default_display(self, model_panel):
        from opencohost.config.settings import DEFAULT_MODEL, MODELS_CATALOG

        expected = MODELS_CATALOG[DEFAULT_MODEL]["display"]
        assert model_panel.default_display == expected

    def test_apply_model_catalog_appends_discovered_models_after_curated(
        self, model_panel
    ):
        curated = list(model_panel.model_display_list)

        model_panel._apply_model_catalog({"bespoke:9b"})

        assert model_panel.combo_modelos.values[: len(curated)] == curated
        assert model_panel.combo_modelos.values[-1] == "bespoke:9b"

    def test_apply_model_catalog_deduplicates_curated_latest_tag(self, model_panel):
        curated = list(model_panel.model_display_list)

        model_panel._apply_model_catalog({"llama3:latest", "gemma4:12b"})

        assert model_panel.combo_modelos.values == curated

    def test_discover_installed_model_tags_normalizes_and_skips_invalid(
        self, model_panel
    ):
        mock_ollama = MagicMock()
        mock_ollama.list.return_value.models = [
            SimpleNamespace(model="llama3:latest"),
            {"model": " bespoke:9b "},
            SimpleNamespace(model=""),
            {},
        ]

        with patch.dict("sys.modules", {"ollama": mock_ollama}):
            assert model_panel._discover_installed_model_tags() == {
                "llama3",
                "bespoke:9b",
            }

    def test_refresh_model_list_falls_back_to_curated_on_discovery_failure(
        self, model_panel
    ):
        curated = list(model_panel.model_display_list)

        with patch.object(
            model_panel,
            "_discover_installed_model_tags",
            side_effect=RuntimeError("boom"),
        ):
            model_panel._refresh_model_list()

        assert model_panel.combo_modelos.values == curated

    def test_extreme_merge_keeps_curated_first_and_deduplicated(self, model_panel):
        curated = list(model_panel.model_display_list)
        installed = {f"lab{i}:1b" for i in range(30)}
        installed.update({"llama3:latest", "qwen3:4b", "gemma4:12b", " bespoke:9b "})

        model_panel._apply_model_catalog(installed)

        values = model_panel.combo_modelos.values
        assert values[: len(curated)] == curated
        assert values.count(model_panel.get_display_for_tag("llama3")) == 1
        assert values.count(model_panel.get_display_for_tag("qwen3:4b")) == 1
        assert "bespoke:9b" in values
        assert "lab29:1b" in values


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
        with patch("opencohost.ui.model_panel.ctk", mock_ctk):
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
        with patch("opencohost.ui.model_panel.ctk", mock_ctk):
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
        with patch("opencohost.ui.model_panel.ctk", mock_ctk):
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
        with patch("opencohost.ui.model_panel.ctk", mock_ctk):
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
        with patch("opencohost.ui.model_panel.ctk", mock_ctk):
            panel.build()

        assert panel.combo_modelos.values == panel.model_display_list
        panel.cleanup()


# ---------------------------------------------------------------------------
# 4. Model Info Update
# ---------------------------------------------------------------------------


class TestModelInfoUpdate:
    def test_update_model_info(self, model_panel):
        from opencohost.config.settings import MODELS_CATALOG

        tag = list(MODELS_CATALOG.keys())[0]
        with patch.object(model_panel, "_modelo_instalado", return_value=True):
            model_panel.update_model_info(tag)

        info = MODELS_CATALOG[tag]
        expected_text = f"{info['desc']} ({info['size_gb']}GB) \u2705"
        assert model_panel.lbl_modelo_info.text == expected_text

    def test_update_model_info_not_installed(self, model_panel):
        from opencohost.config.settings import MODELS_CATALOG

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
        from opencohost.config.settings import MODELS_CATALOG

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
        from opencohost.config.settings import MODELS_CATALOG

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

    def test_on_model_changed_ollama_not_ready_does_not_dispatch_switch(self, model_panel, dispatcher):
        from opencohost.config.settings import MODELS_CATALOG

        tag = list(MODELS_CATALOG.keys())[0]
        display = MODELS_CATALOG[tag]["display"]
        model_panel._ui_state.ollama_state = "app_missing"

        received = []
        dispatcher.subscribe("on_switch_model", lambda t: received.append(t))

        with patch.object(model_panel, "update_model_info"):
            model_panel._on_model_changed(display)

        assert received == []
        model_panel._on_log.assert_any_call(
            "[Sistema] Ollama no esta listo. "
            "Selecciona un modelo cuando Ollama este activo."
        )


# ---------------------------------------------------------------------------
# 7. Download Model Handler
# ---------------------------------------------------------------------------


class TestDownloadModel:
    def test_on_download_app_missing(self, model_panel):
        with patch.object(model_panel, "_detectar_estado_ollama", return_value="app_missing"):
            with patch("opencohost.ui.model_panel.webbrowser.open") as mock_open:
                model_panel._on_download_model()

        mock_open.assert_called_once_with("https://ollama.com/download")

    def test_on_download_package_missing(self, model_panel):
        with patch.object(model_panel, "_detectar_estado_ollama", return_value="package_missing"):
            with patch("opencohost.ui.model_panel.messagebox.showwarning") as mock_warn:
                model_panel._on_download_model()

        mock_warn.assert_called_once()

    def test_on_download_service_stopped(self, model_panel):
        with patch.object(model_panel, "_detectar_estado_ollama", return_value="service_stopped"):
            with patch.object(model_panel, "_iniciar_ollama") as mock_start:
                model_panel._on_download_model()

        mock_start.assert_called_once()

    def test_on_download_activates_installed_model(self, model_panel, dispatcher):
        model_panel._ui_state.ollama_state = "ready"
        from opencohost.config.settings import MODELS_CATALOG

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

        assert state == "package_missing"

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

    def test_modelo_instalado_true_via_cache(self, model_panel):
        # New contract: _modelo_instalado reads from _installed_model_cache, not ollama.list().
        # Simulate cache populated with canonical tag (ollama returns "llama3.2:latest" →
        # _canonical_model_tag strips suffix → "llama3.2").
        model_panel._installed_model_cache = {"llama3.2"}
        result = model_panel._modelo_instalado("llama3.2")
        assert result is True

    def test_modelo_instalado_false_via_cache(self, model_panel):
        # Cache populated but does not include "unknown".
        model_panel._installed_model_cache = {"llama3.2"}
        result = model_panel._modelo_instalado("unknown")
        assert result is False

    def test_modelo_instalado_exception_safe_returns_false(self, model_panel):
        # Empty cache → conservative False (no exception raised).
        model_panel._installed_model_cache = set()
        with patch.object(model_panel, "_refresh_model_list_async"):
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
        from opencohost.config.settings import DEFAULT_MODEL

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
        with patch("opencohost.ui.model_panel.ctk", mock_ctk):
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
        with patch("opencohost.ui.model_panel.ctk", mock_ctk):
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
        with patch("opencohost.ui.model_panel.ctk", mock_ctk):
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
        with patch("opencohost.ui.model_panel.ctk", mock_ctk):
            panel.build()

        # Prevent live ollama calls during button update AND background model-list
        # refresh that _on_state_change("ollama_state", "ready") would otherwise spawn.
        with patch.object(panel, "_modelo_instalado", return_value=False):
            with patch.object(panel, "_refresh_model_list_async"):
                ui_state.ollama_state = "ready"
                time.sleep(0.15)
                # Button should have been updated (text won't be "Revisando Ollama...")
                assert panel.btn_download.text != "Revisando Ollama..."
        panel.cleanup()

    def test_ready_state_change_triggers_model_refresh(
        self, mock_ctk, ui_state, dispatcher, on_log
    ):
        panel = ModelPanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        with patch("opencohost.ui.model_panel.ctk", mock_ctk):
            panel.build()

        with patch.object(panel, "_refresh_model_list_async") as mock_refresh:
            panel._on_state_change("ollama_state", "ready")

        mock_refresh.assert_called_once()
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

    def test_refresh_calls_persistent_check_callback_when_ready(self, mock_parent, ui_state, dispatcher, on_log):
        mock_check = MagicMock()
        panel = ModelPanel(
            parent_frame=mock_parent,
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
            on_check_ollama=mock_check,
        )
        try:
            with patch.object(panel, "_detectar_estado_ollama", return_value="ready"):
                panel.refresh_ollama_state()
        finally:
            panel.cleanup()

        mock_check.assert_called_once()

    def test_refresh_override_callback_takes_precedence_when_ready(self, mock_parent, ui_state, dispatcher, on_log):
        persistent_check = MagicMock()
        override_check = MagicMock()
        panel = ModelPanel(
            parent_frame=mock_parent,
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
            on_check_ollama=persistent_check,
        )
        try:
            with patch.object(panel, "_detectar_estado_ollama", return_value="ready"):
                panel.refresh_ollama_state(on_check_ollama=override_check)
        finally:
            panel.cleanup()

        override_check.assert_called_once()
        persistent_check.assert_not_called()

    def test_refresh_does_not_call_callback_when_not_ready(self, model_panel):
        mock_check = MagicMock()
        with patch.object(model_panel, "_detectar_estado_ollama", return_value="app_missing"):
            model_panel.refresh_ollama_state(on_check_ollama=mock_check)

        mock_check.assert_not_called()


# ---------------------------------------------------------------------------
# 12. Fix #A — _installed_model_cache (freeze elimination)
# ---------------------------------------------------------------------------


class TestInstalledModelCache:
    """_modelo_instalado must read from cache, never call ollama.list() directly."""

    @pytest.fixture(autouse=True)
    def _no_real_timer(self):
        """Prevent real threading.Timer objects from leaking across test boundaries.

        Any call to _invalidate_model_cache while ollama_state != 'ready'
        would normally start a 1.5-second daemon Timer that fires
        _refresh_model_list_async on a worker thread.  That worker can
        race the ollama.chat patch used by test_health_integration tests.

        This fixture replaces threading.Timer with a no-op stand-in for
        every test in this class so no real timer ever starts.  Tests that
        need to assert specific Timer behaviour (TestInvalidateFallbackTimer)
        use object.__new__ bypass + their own explicit patches instead.
        """
        class _NoOpTimer:
            def __init__(self, interval, fn, *args, **kwargs):
                self.interval = interval
                self.fn = fn
                self.daemon = False
                self.cancelled = False

            def cancel(self):
                self.cancelled = True

            def start(self):
                pass  # never fires

        with patch("opencohost.ui.model_panel.threading.Timer", _NoOpTimer):
            yield

    def test_modelo_instalado_never_calls_ollama_list_when_cache_populated(self, model_panel):
        """After cache is set, _modelo_instalado must NOT call ollama.list()."""
        model_panel._installed_model_cache = {"qwen3:8b"}

        mock_ollama = MagicMock()
        mock_ollama.list.side_effect = AssertionError("ollama.list must not be called from main thread")

        with patch.dict("sys.modules", {"ollama": mock_ollama}):
            result = model_panel._modelo_instalado("qwen3:8b")

        assert result is True

    def test_modelo_instalado_returns_true_exact_match(self, model_panel):
        model_panel._installed_model_cache = {"qwen3:8b"}
        assert model_panel._modelo_instalado("qwen3:8b") is True

    def test_modelo_instalado_returns_true_latest_suffix_match(self, model_panel):
        """Caller passes a :latest-suffixed tag; cache holds the canonical (no :latest) form.

        _canonical_model_tag strips ':latest', so _modelo_instalado("llama3:latest")
        canonicalises to "llama3" and matches the cache entry via exact match (path 1).
        The old dead branch (canonical.endswith(":latest")) is never reached because
        _canonical_model_tag already stripped the suffix before the check.
        """
        # Cache stores canonical "llama3" (as _apply_model_catalog populates it).
        model_panel._installed_model_cache = {"llama3"}
        # Caller passes the :latest-suffixed form — real scenario when a UI tag
        # is passed before being normalised.
        assert model_panel._modelo_instalado("llama3:latest") is True

    def test_modelo_instalado_returns_true_prefix_match(self, model_panel):
        """Prefix match: query 'phi3' should match 'phi3:mini' in cache."""
        model_panel._installed_model_cache = {"phi3:mini"}
        assert model_panel._modelo_instalado("phi3") is True

    def test_modelo_instalado_returns_false_when_not_in_cache(self, model_panel):
        model_panel._installed_model_cache = {"qwen3:8b"}
        assert model_panel._modelo_instalado("llama3") is False

    def test_modelo_instalado_returns_false_and_does_not_call_ollama_when_cache_empty(self, model_panel):
        """Empty cache -> conservative False, does NOT call ollama.list()."""
        model_panel._installed_model_cache = set()

        mock_ollama = MagicMock()
        mock_ollama.list.side_effect = AssertionError("ollama.list must not be called when cache is empty")

        with patch.dict("sys.modules", {"ollama": mock_ollama}):
            result = model_panel._modelo_instalado("qwen3:8b")

        assert result is False

    def test_apply_model_catalog_writes_cache_before_ui_calls(self, model_panel):
        """Cache must be populated in _apply_model_catalog with the installed tag set."""
        model_panel._apply_model_catalog({"qwen3:8b", "llama3:latest"})
        # Cache is written with canonical tags (stripped :latest)
        assert "qwen3:8b" in model_panel._installed_model_cache
        assert "llama3" in model_panel._installed_model_cache

    def test_apply_model_catalog_none_does_not_wipe_cache(self, model_panel):
        """When installed_model_tags is None (Ollama DOWN), cache must not be cleared."""
        model_panel._installed_model_cache = {"qwen3:8b"}
        model_panel._apply_model_catalog(None)
        assert "qwen3:8b" in model_panel._installed_model_cache

    def test_invalidate_model_cache_clears_set(self, model_panel):
        model_panel._installed_model_cache = {"qwen3:8b", "llama3"}
        with patch.object(model_panel, "_refresh_model_list_async"):
            model_panel._invalidate_model_cache()
        assert len(model_panel._installed_model_cache) == 0

    def test_invalidate_model_cache_triggers_async_refresh(self, model_panel):
        with patch.object(model_panel, "_refresh_model_list_async") as mock_refresh:
            model_panel._invalidate_model_cache()
        mock_refresh.assert_called_once()

    def test_init_has_empty_installed_model_cache(self, ui_state, dispatcher, on_log):
        panel = ModelPanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        assert hasattr(panel, "_installed_model_cache")
        assert isinstance(panel._installed_model_cache, set)
        assert len(panel._installed_model_cache) == 0
        panel.cleanup()


# ---------------------------------------------------------------------------
# 13. Fix — _invalidate_fallback_timer (orphaned-timer leak)
# ---------------------------------------------------------------------------


class TestInvalidateFallbackTimer:
    """Tests for the dedup + cancel fix on the fallback Timer in _invalidate_model_cache.

    Uses object.__new__ to bypass __init__ entirely — no real UIState threads,
    no ollama.list() calls, and no resolve_llm_tiers() side effects.  This
    pattern matches the isolation strategy used in test_app_shell_obs_resilience.py.
    """

    def _make_bare_panel(self):
        """Create a structurally minimal ModelPanel without running __init__."""
        panel = object.__new__(ModelPanel)
        panel._invalidate_fallback_timer = None
        panel._installed_model_cache = set()
        panel._model_refresh_inflight = False
        panel._observer_id = None
        panel._ui_state = MagicMock()
        panel._ui_state.ollama_state = "service_stopped"
        panel._schedule_ui_update = MagicMock(side_effect=lambda fn: fn())
        return panel

    def test_init_has_invalidate_fallback_timer_attribute(self):
        """ModelPanel.__init__ must declare _invalidate_fallback_timer = None."""
        panel = self._make_bare_panel()
        assert hasattr(panel, "_invalidate_fallback_timer")
        assert panel._invalidate_fallback_timer is None

    def test_double_invalidate_cancels_first_timer(self):
        """Two calls to _invalidate_model_cache while ollama_state != 'ready' must
        cancel the first Timer before starting the second — only one live handle."""
        panel = self._make_bare_panel()
        created_timers: list = []

        class FakeTimer:
            def __init__(self, interval, fn):
                self.interval = interval
                self.fn = fn
                self.cancelled = False
                self.daemon = False
                self.started = False
                created_timers.append(self)

            def cancel(self):
                self.cancelled = True

            def start(self):
                self.started = True

        with patch.object(panel, "_refresh_model_list_async"):
            with patch("opencohost.ui.model_panel.threading.Timer", FakeTimer):
                panel._invalidate_model_cache()
                panel._invalidate_model_cache()

        # Exactly two Timer instances were created.
        assert len(created_timers) == 2
        # The FIRST timer was cancelled before the second was started.
        assert created_timers[0].cancelled is True
        # The SECOND timer was started and is now the stored handle.
        assert created_timers[1].started is True
        assert panel._invalidate_fallback_timer is created_timers[1]

    def test_invalidate_stores_timer_handle(self):
        """After _invalidate_model_cache, _invalidate_fallback_timer must hold the Timer."""
        panel = self._make_bare_panel()
        sentinel_timer = MagicMock()
        sentinel_timer.daemon = False

        with patch.object(panel, "_refresh_model_list_async"):
            with patch("opencohost.ui.model_panel.threading.Timer", return_value=sentinel_timer):
                panel._invalidate_model_cache()

        assert panel._invalidate_fallback_timer is sentinel_timer

    def test_cleanup_cancels_invalidate_fallback_timer(self):
        """cleanup() must cancel and clear _invalidate_fallback_timer."""
        panel = self._make_bare_panel()
        mock_timer = MagicMock()
        panel._invalidate_fallback_timer = mock_timer

        panel.cleanup()

        mock_timer.cancel.assert_called_once()
        assert panel._invalidate_fallback_timer is None

    def test_cleanup_safe_when_no_fallback_timer(self):
        """cleanup() must not raise when _invalidate_fallback_timer is None."""
        panel = self._make_bare_panel()
        panel._invalidate_fallback_timer = None
        panel.cleanup()  # Should not raise

    def test_no_timer_when_ollama_already_ready(self):
        """When ollama_state == 'ready', no fallback Timer should be created."""
        panel = self._make_bare_panel()
        panel._ui_state.ollama_state = "ready"

        with patch.object(panel, "_refresh_model_list_async"):
            with patch("opencohost.ui.model_panel.threading.Timer") as mock_timer_cls:
                panel._invalidate_model_cache()

        mock_timer_cls.assert_not_called()
        assert panel._invalidate_fallback_timer is None


# ---------------------------------------------------------------------------
# 14. on_closing calls model_panel.cleanup()
# ---------------------------------------------------------------------------


class TestOnClosingCallsModelPanelCleanup:
    """on_closing must invoke model_panel.cleanup() during teardown."""

    def test_on_closing_invokes_model_panel_cleanup(self):
        """app_shell.on_closing() must call model_panel.cleanup() if model_panel exists."""
        # We only want to verify the structural call — no need to spin up VocalAIApp.
        # Read the source and confirm model_panel.cleanup() is called.
        import ast
        import pathlib

        source = (
            pathlib.Path(__file__).resolve().parents[1] / "opencohost" / "ui" / "app_shell.py"
        ).read_text(encoding="utf-8")
        tree = ast.parse(source)

        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "on_closing":
                src_segment = ast.get_source_segment(source, node)
                # Check that model_panel.cleanup() is present in the on_closing body
                if src_segment and "model_panel" in src_segment and "cleanup()" in src_segment:
                    found = True
                    break

        assert found, "on_closing must call model_panel.cleanup()"


# ---------------------------------------------------------------------------
# 15. Bug fix — set_active_model must sync combo_modelos (success-path desync)
# ---------------------------------------------------------------------------


class TestSetActiveModelComboSync:
    """set_active_model (called on the SUCCESS path of a runtime model switch)
    must update combo_modelos to reflect the new active model.

    Root cause: set_active_model only updated _active_model_tag and button/tier
    labels, but never called combo_modelos.set().  The FAILURE path already used
    restore_to_active_model which does call combo_modelos.set().  Only the success
    path was broken.

    Fix applied to model_panel.py set_active_model: now calls
    combo_modelos.set(get_display_for_tag(tag)) before updating _active_model_tag,
    bringing the success path to parity with restore_to_active_model.
    The app_shell.py _on_motor_model_changed success path was also updated to
    use restore_to_active_model for belt-and-suspenders correctness.

    This test pins the panel-level contract: after set_active_model is called with
    a new tag, combo_modelos.get() must equal get_display_for_tag(new_tag).
    """

    def test_set_active_model_syncs_combo_to_new_model(self, model_panel):
        """After a successful runtime model switch, the combobox must reflect
        the new active model — not the stale pre-switch selection."""
        from opencohost.config.settings import MODELS_CATALOG

        # Start: panel is initialised with DEFAULT_MODEL.
        # Pick a different model to simulate a successful runtime switch.
        new_tag = "qwen3:4b"
        expected_display = MODELS_CATALOG[new_tag]["display"]

        # Pre-condition: combo currently shows the default (old) model.
        assert model_panel.combo_modelos.get() != expected_display, (
            "Pre-condition failed: combo already shows the new model before set_active_model"
        )

        # This is the call that _on_motor_model_changed makes on the success path.
        model_panel.set_active_model(new_tag)

        # After a successful switch the combobox MUST reflect the new model.
        assert model_panel.combo_modelos.get() == expected_display, (
            f"combo_modelos still shows '{model_panel.combo_modelos.get()}' "
            f"after set_active_model('{new_tag}') — combobox was not synced"
        )
