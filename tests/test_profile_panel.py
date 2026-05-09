"""Comprehensive tests for ui.profile_panel.ProfilePanel.

Covers:
- ProfilePanel class initialization
- Profile data management (set, get, default)
- UI construction with correct widget creation
- Profile selection handler (on_profile_changed)
- Profile editor integration
- Profile save handler (on_perfiles_guardados)
- UIState observer integration
- Edge cases (empty profiles, missing widgets)
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from ui.state import UIState
from ui.protocols import CallbackDispatcher
from ui.profile_panel import ProfilePanel


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
    return CallbackDispatcher(source="ProfilePanel")


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
            self.font = kwargs.get("font", None)

        def configure(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)
                self.kwargs[key] = value

        def pack(self, **kwargs):
            self.pack_kwargs = kwargs

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

    class MockFont:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    mock_module.CTkLabel = MockLabel
    mock_module.CTkButton = MockButton
    mock_module.CTkOptionMenu = MockOptionMenu
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
def sample_profiles():
    """Sample profile dictionary."""
    return {
        "Kira (Default)": {"voice": "default.wav", "system": "Default system prompt"},
        "Streamer Mode": {"voice": "streamer.wav", "system": "Streamer system prompt"},
        "Casual": {"voice": "casual.wav", "system": "Casual system prompt"},
    }


@pytest.fixture()
def profile_panel(mock_ctk, mock_parent, ui_state, dispatcher, on_log, sample_profiles):
    """ProfilePanel instance with mocked customtkinter, built."""
    with patch.dict("sys.modules", {"customtkinter": mock_ctk}):
        panel = ProfilePanel(
            parent_frame=mock_parent,
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        panel.set_profiles(sample_profiles)
        with patch("ui.profile_panel.ctk", mock_ctk):
            panel.build()
    yield panel
    panel.cleanup()


# ---------------------------------------------------------------------------
# 1. ProfilePanel Initialization
# ---------------------------------------------------------------------------


class TestProfilePanelInit:
    def test_init_stores_references(self, mock_parent, ui_state, dispatcher, on_log):
        panel = ProfilePanel(
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
        assert panel._perfiles == {}
        panel.cleanup()

    def test_init_with_configurador_class(self, ui_state, dispatcher, on_log):
        mock_config = MagicMock()
        panel = ProfilePanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
            configurador_class=mock_config,
        )
        assert panel._configurador_class is mock_config
        panel.cleanup()

    def test_init_with_schedule_ui_update(self, ui_state, dispatcher, on_log):
        scheduler = MagicMock()
        panel = ProfilePanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
            schedule_ui_update=scheduler,
        )
        assert panel._schedule_ui_update is scheduler
        panel.cleanup()


# ---------------------------------------------------------------------------
# 2. Profile Data Management
# ---------------------------------------------------------------------------


class TestProfileData:
    def test_set_profiles(self, profile_panel, sample_profiles):
        assert profile_panel.get_profiles() == sample_profiles

    def test_set_profiles_updates_combo(self, profile_panel):
        new_profiles = {"New Profile": {"voice": "new.wav"}}
        profile_panel.set_profiles(new_profiles)
        assert profile_panel.combo_perfiles.values == ["New Profile"]

    def test_get_default_profile_kira(self, profile_panel):
        assert profile_panel.get_default_profile_name() == "Kira (Default)"

    def test_get_default_profile_first_available(self, ui_state, dispatcher, on_log):
        panel = ProfilePanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        panel.set_profiles({"Custom": {"voice": "custom.wav"}})
        assert panel.get_default_profile_name() == "Custom"
        panel.cleanup()

    def test_get_default_profile_empty(self, ui_state, dispatcher, on_log):
        panel = ProfilePanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        panel.set_profiles({})
        assert panel.get_default_profile_name() is None
        panel.cleanup()


# ---------------------------------------------------------------------------
# 3. UI Construction
# ---------------------------------------------------------------------------


class TestUIConstruction:
    def test_build_creates_all_widgets(self, mock_ctk, ui_state, dispatcher, on_log, sample_profiles):
        panel = ProfilePanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        panel.set_profiles(sample_profiles)
        with patch("ui.profile_panel.ctk", mock_ctk):
            panel.build()

        assert panel.lbl_profile_header is not None
        assert panel.combo_perfiles is not None
        assert panel.btn_editar_perfiles is not None
        panel.cleanup()

    def test_build_sets_default_profile(self, mock_ctk, ui_state, dispatcher, on_log, sample_profiles):
        panel = ProfilePanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        panel.set_profiles(sample_profiles)
        with patch("ui.profile_panel.ctk", mock_ctk):
            panel.build()

        assert panel.combo_perfiles.get() == "Kira (Default)"
        panel.cleanup()

    def test_build_subscribes_to_ui_state(self, mock_ctk, ui_state, dispatcher, on_log, sample_profiles):
        panel = ProfilePanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        panel.set_profiles(sample_profiles)
        with patch("ui.profile_panel.ctk", mock_ctk):
            panel.build()

        assert panel._observer_id is not None
        assert panel._observer_id in ui_state._observers
        panel.cleanup()

    def test_build_with_empty_profiles(self, mock_ctk, ui_state, dispatcher, on_log):
        panel = ProfilePanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        panel.set_profiles({})
        with patch("ui.profile_panel.ctk", mock_ctk):
            panel.build()

        assert panel.combo_perfiles.values == []
        panel.cleanup()

    def test_combo_populated_with_profile_names(self, mock_ctk, ui_state, dispatcher, on_log, sample_profiles):
        panel = ProfilePanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        panel.set_profiles(sample_profiles)
        with patch("ui.profile_panel.ctk", mock_ctk):
            panel.build()

        assert set(panel.combo_perfiles.values) == set(sample_profiles.keys())
        panel.cleanup()


# ---------------------------------------------------------------------------
# 4. Profile Selection Handler
# ---------------------------------------------------------------------------


class TestProfileSelection:
    def test_on_profile_changed_dispatches(self, profile_panel, dispatcher):
        received = []
        dispatcher.subscribe("on_set_profile", lambda p: received.append(p))

        profile_panel._on_profile_changed("Kira (Default)")

        assert len(received) == 1
        assert received[0] == profile_panel._perfiles["Kira (Default)"]

    def test_on_profile_changed_unknown_profile(self, profile_panel, dispatcher):
        received = []
        dispatcher.subscribe("on_set_profile", lambda p: received.append(p))

        profile_panel._on_profile_changed("Unknown Profile")

        assert len(received) == 0

    def test_get_selected_profile(self, profile_panel):
        profile_panel.combo_perfiles.set("Streamer Mode")
        assert profile_panel.get_selected_profile() == "Streamer Mode"

    def test_set_profile_selection(self, profile_panel):
        profile_panel.set_profile_selection("Casual")
        assert profile_panel.combo_perfiles.get() == "Casual"

    def test_get_selected_profile_with_none_widget(self, ui_state, dispatcher, on_log):
        panel = ProfilePanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        assert panel.get_selected_profile() == ""
        panel.cleanup()


# ---------------------------------------------------------------------------
# 5. Apply Current Profile
# ---------------------------------------------------------------------------


class TestApplyProfile:
    def test_apply_current_profile(self, profile_panel, dispatcher):
        profile_panel.combo_perfiles.set("Kira (Default)")

        received = []
        dispatcher.subscribe("on_set_profile", lambda p: received.append(p))

        profile_panel.apply_current_profile()

        assert len(received) == 1
        assert received[0] == profile_panel._perfiles["Kira (Default)"]

    def test_apply_current_profile_not_in_dict(self, profile_panel, dispatcher):
        profile_panel.combo_perfiles.set("Nonexistent")

        received = []
        dispatcher.subscribe("on_set_profile", lambda p: received.append(p))

        profile_panel.apply_current_profile()

        assert len(received) == 0


# ---------------------------------------------------------------------------
# 6. Profile Editor Integration
# ---------------------------------------------------------------------------


class TestProfileEditor:
    def test_on_edit_profile_opens_configurator(self, profile_panel):
        mock_configurator = MagicMock()
        profile_panel._configurador_class = mock_configurator

        profile_panel._on_edit_profile()

        mock_configurator.assert_called_once()
        mock_instance = mock_configurator.return_value
        mock_instance.grab_set.assert_called_once()

    def test_on_edit_profile_no_configurator(self, profile_panel, on_log):
        profile_panel._configurador_class = None

        profile_panel._on_edit_profile()

        on_log.assert_called_with("[Sistema] Configurador de perfiles no disponible.")


# ---------------------------------------------------------------------------
# 7. Profile Save Handler
# ---------------------------------------------------------------------------


class TestProfileSave:
    def test_on_perfiles_guardados(self, profile_panel, dispatcher):
        new_profiles = {
            "Updated Profile": {"voice": "updated.wav"},
            "Kira (Default)": {"voice": "default.wav"},
        }

        received = []
        dispatcher.subscribe("on_set_profile", lambda p: received.append(p))

        with patch("ui.profile_panel.guardar_perfiles") as mock_guardar:
            profile_panel._on_perfiles_guardados(new_profiles)

        mock_guardar.assert_called_once_with(new_profiles)
        assert profile_panel._perfiles == new_profiles
        assert profile_panel.combo_perfiles.values == list(new_profiles.keys())

    def test_on_perfiles_guardados_selects_first_if_current_missing(self, profile_panel):
        profile_panel.combo_perfiles.set("Kira (Default)")

        new_profiles = {"New Only": {"voice": "new.wav"}}

        with patch("ui.profile_panel.guardar_perfiles"):
            profile_panel._on_perfiles_guardados(new_profiles)

        assert profile_panel.combo_perfiles.get() == "New Only"

    def test_on_perfiles_guardados_keeps_current_if_still_valid(self, profile_panel):
        profile_panel.combo_perfiles.set("Kira (Default)")

        new_profiles = {
            "Kira (Default)": {"voice": "default.wav"},
            "New Profile": {"voice": "new.wav"},
        }

        with patch("ui.profile_panel.guardar_perfiles"):
            profile_panel._on_perfiles_guardados(new_profiles)

        assert profile_panel.combo_perfiles.get() == "Kira (Default)"


# ---------------------------------------------------------------------------
# 8. UIState Observer Integration
# ---------------------------------------------------------------------------


class TestObserverIntegration:
    def test_subscribe_called_on_build(self, mock_ctk, ui_state, dispatcher, on_log, sample_profiles):
        panel = ProfilePanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        panel.set_profiles(sample_profiles)
        with patch("ui.profile_panel.ctk", mock_ctk):
            panel.build()

        assert panel._observer_id is not None
        assert panel._observer_id in ui_state._observers
        panel.cleanup()

    def test_cleanup_removes_observer(self, mock_ctk, ui_state, dispatcher, on_log, sample_profiles):
        panel = ProfilePanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        panel.set_profiles(sample_profiles)
        with patch("ui.profile_panel.ctk", mock_ctk):
            panel.build()

        sub_id = panel._observer_id
        panel.cleanup()
        assert panel._observer_id is None
        assert sub_id not in ui_state._observers

    def test_cleanup_is_idempotent(self, mock_ctk, ui_state, dispatcher, on_log, sample_profiles):
        panel = ProfilePanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        panel.set_profiles(sample_profiles)
        with patch("ui.profile_panel.ctk", mock_ctk):
            panel.build()

        panel.cleanup()
        panel.cleanup()  # Should not raise
        assert panel._observer_id is None

    def test_on_state_change_ignores_unknown_keys(self, profile_panel):
        profile_panel._on_state_change("unknown_key", "some_value")
        # Should not raise or change anything

    def test_current_profile_change_triggers_selection(self, mock_ctk, ui_state, dispatcher, on_log, sample_profiles):
        panel = ProfilePanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        panel.set_profiles(sample_profiles)
        with patch("ui.profile_panel.ctk", mock_ctk):
            panel.build()

        ui_state.current_profile = "Streamer Mode"
        time.sleep(0.15)
        assert panel.combo_perfiles.get() == "Streamer Mode"
        panel.cleanup()


# ---------------------------------------------------------------------------
# 9. Edge Cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_empty_profiles_combo_values(self, mock_ctk, ui_state, dispatcher, on_log):
        panel = ProfilePanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        panel.set_profiles({})
        with patch("ui.profile_panel.ctk", mock_ctk):
            panel.build()

        assert panel.combo_perfiles.values == []
        panel.cleanup()

    def test_set_profile_selection_with_none_widget(self, ui_state, dispatcher, on_log):
        panel = ProfilePanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        panel.combo_perfiles = None
        panel.set_profile_selection("Some Profile")  # Should not raise
        panel.cleanup()

    def test_profiles_dict_isolated(self, ui_state, dispatcher, on_log):
        panel1 = ProfilePanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )
        panel2 = ProfilePanel(
            parent_frame=MagicMock(),
            ui_state=ui_state,
            dispatcher=dispatcher,
            on_log=on_log,
        )

        panel1.set_profiles({"A": {"voice": "a.wav"}})
        panel2.set_profiles({"B": {"voice": "b.wav"}})

        assert panel1.get_profiles() == {"A": {"voice": "a.wav"}}
        assert panel2.get_profiles() == {"B": {"voice": "b.wav"}}

        panel1.cleanup()
        panel2.cleanup()
