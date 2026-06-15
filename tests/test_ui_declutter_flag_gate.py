"""TDD tests for STREAM_ADMIN_ENABLED flag gate — Part A of ui_declutter_20260614.

RED phase: these tests should FAIL before implementation, then GREEN after.

Covers:
- STREAM_ADMIN_ENABLED=False hides _build_metadata_tab widgets
- STREAM_ADMIN_ENABLED=False hides _build_moderation_tab widgets
- STREAM_ADMIN_ENABLED=False hides _build_chat_tab widgets
- _build_chat_live_tab always builds regardless of flag value (RF3 carve-out)
- STREAM_ADMIN_ENABLED=True builds all four sections
- Flag is defined in settings.py with the expected default value
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def ui_state():
    from opencohost.ui.state import UIState
    s = UIState()
    yield s
    s.shutdown(timeout=2.0)


@pytest.fixture()
def dispatcher():
    from opencohost.ui.protocols import CallbackDispatcher
    return CallbackDispatcher(source="StreamAdminUITest")


def _make_mock_parent():
    """Create a lightweight mock parent frame that supports grid geometry calls."""

    class _MockChild:
        def __init__(self, *args, **kwargs):
            self._kwargs = kwargs
            self._removed = False
            self._packed = False
            self._gridded = False

        def configure(self, **kw):
            self._kwargs.update(kw)

        def grid(self, **kw):
            self._gridded = True
            self._removed = False

        def grid_remove(self):
            self._removed = True

        def pack(self, **kw):
            self._packed = True

        def bind(self, *a, **kw):
            pass

        def get(self):
            return ""

        def delete(self, *a):
            pass

        def insert(self, *a):
            pass

        def grid_columnconfigure(self, *a, **kw):
            pass

        def grid_rowconfigure(self, *a, **kw):
            pass

        # Allow CTkScrollableFrame, CTkTextbox etc. to call these
        def pack_propagate(self, *a):
            pass

    class _MockFrame(_MockChild):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)

        # CTkFrame can be used as parent; return new _MockChild for children
        def __call__(self, *a, **kw):
            return _MockFrame()

    parent = _MockFrame()
    return parent


def _build_stream_admin_with_flag(flag_value: bool, ui_state, dispatcher):
    """Build a StreamAdminUI against a mock parent with the given flag value.

    Returns (sa_ui, widgets_dict).
    """
    import importlib
    import opencohost.config.settings as settings_mod

    original = settings_mod.STREAM_ADMIN_ENABLED
    settings_mod.STREAM_ADMIN_ENABLED = flag_value

    try:
        # Re-import stream_admin_ui to pick up the monkeypatched constant
        import opencohost.ui.stream_admin_ui as sa_mod
        importlib.reload(sa_mod)
        StreamAdminUI = sa_mod.StreamAdminUI

        import customtkinter as ctk
        with _mock_ctk_context():
            sa = StreamAdminUI(
                ui_state=ui_state,
                dispatcher=dispatcher,
            )
            parent = _make_mock_parent()
            sa.build(parent)
            return sa, sa._widgets
    finally:
        settings_mod.STREAM_ADMIN_ENABLED = original
        # Reload to restore original behavior
        importlib.reload(sa_mod)


from contextlib import contextmanager
from unittest.mock import patch, MagicMock


@contextmanager
def _mock_ctk_context():
    """Patch customtkinter so no real Tk window is needed."""
    mock_ctk = MagicMock()

    class _Widget:
        def __init__(self, *args, **kwargs):
            self._kwargs = kwargs
            self._grid_removed = False
            self._callbacks = {}

        def configure(self, **kw):
            self._kwargs.update(kw)

        def cget(self, key):
            return self._kwargs.get(key, "")

        def grid(self, **kw):
            self._grid_removed = False

        def grid_remove(self):
            self._grid_removed = True

        def pack(self, **kw):
            pass

        def bind(self, *a, **kw):
            pass

        def get(self):
            return ""

        def delete(self, *a):
            pass

        def insert(self, *a):
            pass

        def set(self, *a):
            pass

        def select(self):
            pass

        def deselect(self):
            pass

        def grid_columnconfigure(self, *a, **kw):
            pass

        def grid_rowconfigure(self, *a, **kw):
            pass

        def pack_propagate(self, *a):
            pass

    mock_ctk.CTkFrame.return_value = _Widget()
    mock_ctk.CTkFrame.side_effect = lambda *a, **kw: _Widget()
    mock_ctk.CTkScrollableFrame.side_effect = lambda *a, **kw: _Widget()
    mock_ctk.CTkLabel.side_effect = lambda *a, **kw: _Widget()
    mock_ctk.CTkButton.side_effect = lambda *a, **kw: _Widget()
    mock_ctk.CTkEntry.side_effect = lambda *a, **kw: _Widget()
    mock_ctk.CTkTextbox.side_effect = lambda *a, **kw: _Widget()
    mock_ctk.CTkSwitch.side_effect = lambda *a, **kw: _Widget()
    mock_ctk.CTkOptionMenu.side_effect = lambda *a, **kw: _Widget()
    mock_ctk.CTkFont.side_effect = lambda *a, **kw: MagicMock()

    with patch("opencohost.ui.stream_admin_ui.ctk", mock_ctk):
        yield mock_ctk


# ---------------------------------------------------------------------------
# 1. Flag defined in settings.py
# ---------------------------------------------------------------------------


class TestStreamAdminFlagDefined:
    def test_flag_exists_in_settings(self):
        """STREAM_ADMIN_ENABLED must be defined in settings.py."""
        import opencohost.config.settings as settings
        assert hasattr(settings, "STREAM_ADMIN_ENABLED")

    def test_flag_is_bool(self):
        """STREAM_ADMIN_ENABLED must be a boolean constant."""
        import opencohost.config.settings as settings
        assert isinstance(settings.STREAM_ADMIN_ENABLED, bool)

    def test_flag_default_is_false(self):
        """STREAM_ADMIN_ENABLED default for launch must be False."""
        import opencohost.config.settings as settings
        assert settings.STREAM_ADMIN_ENABLED is False

    def test_settings_source_has_justification_comment(self):
        """settings.py must document WHY the flag is False (4-place mandate)."""
        source = (ROOT / "opencohost" / "config" / "settings.py").read_text(encoding="utf-8")
        # Any of these signals the 4-place comment is present
        assert "STREAM_ADMIN_ENABLED" in source
        assert "RF4" in source or "HANDOFF_RF4" in source or "legacy" in source.lower()


# ---------------------------------------------------------------------------
# 2. Flag=False hides retired sections (RED until Part A implemented)
# ---------------------------------------------------------------------------


class TestFlagFalseHidesSections:
    def test_metadata_widgets_not_registered_when_flag_false(self, ui_state, dispatcher):
        """flag=False → _build_metadata_tab must not register any widgets."""
        _metadata_keys = [
            "lbl_stream_metadata_state",
            "btn_stream_read_metadata",
            "btn_stream_suggest_metadata",
            "btn_stream_apply_metadata",
            "btn_stream_reject_pending",
            "entry_stream_title",
            "entry_stream_category",
            "entry_stream_tags",
            "text_stream_description",
        ]
        import opencohost.config.settings as settings_mod
        import importlib
        import opencohost.ui.stream_admin_ui as sa_mod

        original = settings_mod.STREAM_ADMIN_ENABLED
        settings_mod.STREAM_ADMIN_ENABLED = False
        importlib.reload(sa_mod)
        try:
            StreamAdminUI = sa_mod.StreamAdminUI
            with _mock_ctk_context():
                sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)
                parent = MagicMock()
                # Stub parent to return mock frames for children
                parent.grid_columnconfigure = MagicMock()
                parent.grid_rowconfigure = MagicMock()

                class _Frame:
                    def __init__(self):
                        self._grc = 0
                        self._gcc = 0
                    def grid(self, **kw): pass
                    def grid_remove(self): pass
                    def grid_columnconfigure(self, *a, **kw): pass
                    def grid_rowconfigure(self, *a, **kw): pass

                # Direct call to _build_metadata_tab with a fresh frame
                section = _Frame()
                sa._build_metadata_tab(section)

                for key in _metadata_keys:
                    assert key not in sa._widgets, (
                        f"Expected {key!r} NOT to be registered when STREAM_ADMIN_ENABLED=False"
                    )
        finally:
            settings_mod.STREAM_ADMIN_ENABLED = original
            importlib.reload(sa_mod)

    def test_moderation_widgets_not_registered_when_flag_false(self, ui_state, dispatcher):
        """flag=False → _build_moderation_tab must not register any widgets."""
        _mod_keys = [
            "switch_stream_mod_enabled",
            "combo_stream_mod_mode",
            "switch_stream_announce",
            "entry_stream_mod_user",
            "entry_stream_mod_reason",
            "btn_stream_propose_timeout",
            "btn_stream_propose_ban",
            "frame_stream_users",
        ]
        import opencohost.config.settings as settings_mod
        import importlib
        import opencohost.ui.stream_admin_ui as sa_mod

        original = settings_mod.STREAM_ADMIN_ENABLED
        settings_mod.STREAM_ADMIN_ENABLED = False
        importlib.reload(sa_mod)
        try:
            StreamAdminUI = sa_mod.StreamAdminUI
            with _mock_ctk_context():
                sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)

                class _Frame:
                    def grid(self, **kw): pass
                    def grid_remove(self): pass
                    def grid_columnconfigure(self, *a, **kw): pass
                    def grid_rowconfigure(self, *a, **kw): pass

                sa._build_moderation_tab(_Frame())

                for key in _mod_keys:
                    assert key not in sa._widgets, (
                        f"Expected {key!r} NOT to be registered when STREAM_ADMIN_ENABLED=False"
                    )
        finally:
            settings_mod.STREAM_ADMIN_ENABLED = original
            importlib.reload(sa_mod)

    def test_chat_kira_widgets_not_registered_when_flag_false(self, ui_state, dispatcher):
        """flag=False → _build_chat_tab (Kira Chat RF4) must not register any widgets."""
        _chat_keys = [
            "btn_stream_connect_chat",
            "switch_stream_chat_enabled",
            "switch_stream_small",
            "btn_stream_simulate_chat",
            "entry_stream_chat_message",
            "btn_stream_send_chat",
            "btn_stream_force_kira",
        ]
        import opencohost.config.settings as settings_mod
        import importlib
        import opencohost.ui.stream_admin_ui as sa_mod

        original = settings_mod.STREAM_ADMIN_ENABLED
        settings_mod.STREAM_ADMIN_ENABLED = False
        importlib.reload(sa_mod)
        try:
            StreamAdminUI = sa_mod.StreamAdminUI
            with _mock_ctk_context():
                sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)

                class _Frame:
                    def grid(self, **kw): pass
                    def grid_remove(self): pass
                    def grid_columnconfigure(self, *a, **kw): pass
                    def grid_rowconfigure(self, *a, **kw): pass

                sa._build_chat_tab(_Frame())

                for key in _chat_keys:
                    assert key not in sa._widgets, (
                        f"Expected {key!r} NOT to be registered when STREAM_ADMIN_ENABLED=False"
                    )
        finally:
            settings_mod.STREAM_ADMIN_ENABLED = original
            importlib.reload(sa_mod)


# ---------------------------------------------------------------------------
# 3. RF3 carve-out — _build_chat_live_tab always builds
# ---------------------------------------------------------------------------


class TestChatLiveCarveOut:
    def test_chat_live_widgets_registered_when_flag_false(self, ui_state, dispatcher):
        """_build_chat_live_tab must register widgets regardless of flag value."""
        import opencohost.config.settings as settings_mod
        import importlib
        import opencohost.ui.stream_admin_ui as sa_mod

        original = settings_mod.STREAM_ADMIN_ENABLED
        settings_mod.STREAM_ADMIN_ENABLED = False
        importlib.reload(sa_mod)
        try:
            StreamAdminUI = sa_mod.StreamAdminUI
            with _mock_ctk_context():
                sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)

                class _Frame:
                    def grid(self, **kw): pass
                    def grid_remove(self): pass
                    def grid_columnconfigure(self, *a, **kw): pass
                    def grid_rowconfigure(self, *a, **kw): pass

                sa._build_chat_live_tab(_Frame())

                # RF3 Chat Live must be built regardless of flag
                assert "btn_stream_chat_live_connect" in sa._widgets, (
                    "Chat Live (RF3) button must be registered even when STREAM_ADMIN_ENABLED=False"
                )
                assert "entry_stream_chat_url" in sa._widgets, (
                    "Chat Live URL entry must be registered even when STREAM_ADMIN_ENABLED=False"
                )
        finally:
            settings_mod.STREAM_ADMIN_ENABLED = original
            importlib.reload(sa_mod)

    def test_chat_live_widgets_registered_when_flag_true(self, ui_state, dispatcher):
        """_build_chat_live_tab must register widgets when flag is True too."""
        import opencohost.config.settings as settings_mod
        import importlib
        import opencohost.ui.stream_admin_ui as sa_mod

        original = settings_mod.STREAM_ADMIN_ENABLED
        settings_mod.STREAM_ADMIN_ENABLED = True
        importlib.reload(sa_mod)
        try:
            StreamAdminUI = sa_mod.StreamAdminUI
            with _mock_ctk_context():
                sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)

                class _Frame:
                    def grid(self, **kw): pass
                    def grid_remove(self): pass
                    def grid_columnconfigure(self, *a, **kw): pass
                    def grid_rowconfigure(self, *a, **kw): pass

                sa._build_chat_live_tab(_Frame())
                assert "btn_stream_chat_live_connect" in sa._widgets
        finally:
            settings_mod.STREAM_ADMIN_ENABLED = original
            importlib.reload(sa_mod)


# ---------------------------------------------------------------------------
# 4. Flag=True builds all four sections
# ---------------------------------------------------------------------------


class TestFlagTrueBuildsSections:
    def test_metadata_widgets_registered_when_flag_true(self, ui_state, dispatcher):
        """flag=True → _build_metadata_tab must register its widgets."""
        import opencohost.config.settings as settings_mod
        import importlib
        import opencohost.ui.stream_admin_ui as sa_mod

        original = settings_mod.STREAM_ADMIN_ENABLED
        settings_mod.STREAM_ADMIN_ENABLED = True
        importlib.reload(sa_mod)
        try:
            StreamAdminUI = sa_mod.StreamAdminUI
            with _mock_ctk_context():
                sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)

                class _Frame:
                    def grid(self, **kw): pass
                    def grid_remove(self): pass
                    def grid_columnconfigure(self, *a, **kw): pass
                    def grid_rowconfigure(self, *a, **kw): pass

                sa._build_metadata_tab(_Frame())
                assert "lbl_stream_metadata_state" in sa._widgets
        finally:
            settings_mod.STREAM_ADMIN_ENABLED = original
            importlib.reload(sa_mod)

    def test_moderation_widgets_registered_when_flag_true(self, ui_state, dispatcher):
        """flag=True → _build_moderation_tab must register its widgets."""
        import opencohost.config.settings as settings_mod
        import importlib
        import opencohost.ui.stream_admin_ui as sa_mod

        original = settings_mod.STREAM_ADMIN_ENABLED
        settings_mod.STREAM_ADMIN_ENABLED = True
        importlib.reload(sa_mod)
        try:
            StreamAdminUI = sa_mod.StreamAdminUI
            with _mock_ctk_context():
                sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)

                class _Frame:
                    def grid(self, **kw): pass
                    def grid_remove(self): pass
                    def grid_columnconfigure(self, *a, **kw): pass
                    def grid_rowconfigure(self, *a, **kw): pass

                sa._build_moderation_tab(_Frame())
                assert "switch_stream_mod_enabled" in sa._widgets
        finally:
            settings_mod.STREAM_ADMIN_ENABLED = original
            importlib.reload(sa_mod)

    def test_chat_kira_widgets_registered_when_flag_true(self, ui_state, dispatcher):
        """flag=True → _build_chat_tab (Kira Chat RF4) must register its widgets."""
        import opencohost.config.settings as settings_mod
        import importlib
        import opencohost.ui.stream_admin_ui as sa_mod

        original = settings_mod.STREAM_ADMIN_ENABLED
        settings_mod.STREAM_ADMIN_ENABLED = True
        importlib.reload(sa_mod)
        try:
            StreamAdminUI = sa_mod.StreamAdminUI
            with _mock_ctk_context():
                sa = StreamAdminUI(ui_state=ui_state, dispatcher=dispatcher)

                class _Frame:
                    def grid(self, **kw): pass
                    def grid_remove(self): pass
                    def grid_columnconfigure(self, *a, **kw): pass
                    def grid_rowconfigure(self, *a, **kw): pass

                sa._build_chat_tab(_Frame())
                assert "btn_stream_force_kira" in sa._widgets
        finally:
            settings_mod.STREAM_ADMIN_ENABLED = original
            importlib.reload(sa_mod)


# ---------------------------------------------------------------------------
# 5. Source-level structural checks
# ---------------------------------------------------------------------------


class TestSourceStructure:
    def test_stream_admin_ui_import_still_in_app_shell(self):
        """app_shell.py must still import StreamAdminUI (safety test)."""
        source = (ROOT / "opencohost" / "ui" / "app_shell.py").read_text(encoding="utf-8")
        assert "from opencohost.ui.stream_admin_ui import StreamAdminUI" in source

    def test_stream_admin_build_call_still_in_app_shell(self):
        """app_shell.py must still call .build() on stream_admin_ui."""
        source = (ROOT / "opencohost" / "ui" / "app_shell.py").read_text(encoding="utf-8")
        assert "self.stream_admin_ui.build(" in source

    def test_wire_stream_admin_callbacks_still_in_app_shell(self):
        """app_shell.py must still call _wire_stream_admin_callbacks()."""
        source = (ROOT / "opencohost" / "ui" / "app_shell.py").read_text(encoding="utf-8")
        assert "self._wire_stream_admin_callbacks()" in source

    def test_build_chat_live_tab_has_no_flag_guard_in_source(self):
        """_build_chat_live_tab must not contain a STREAM_ADMIN_ENABLED guard."""
        source = (ROOT / "opencohost" / "ui" / "stream_admin_ui.py").read_text(encoding="utf-8")
        # Find _build_chat_live_tab method and ensure no flag guard inside it
        idx = source.find("def _build_chat_live_tab(")
        assert idx >= 0, "_build_chat_live_tab method must exist"
        # The next method definition after _build_chat_live_tab
        next_def = source.find("\n    def ", idx + 1)
        method_body = source[idx:next_def] if next_def > idx else source[idx:]
        assert "STREAM_ADMIN_ENABLED" not in method_body, (
            "_build_chat_live_tab must not contain a STREAM_ADMIN_ENABLED guard"
        )
