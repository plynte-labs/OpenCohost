"""Tests for the CTK "Idioma" language control (kira_bilingual_e2e_20260705,
P7 config surface — D6: next-boot only, no engine dispatch, no hot-swap).

Two layers, mirroring the established app_shell test conventions:
  1. Structural (source-inspection, see test_tts_local_only_switch.py) —
     confirms the widgets/wiring exist without constructing real CTk objects.
  2. Direct-method (object.__new__ + manual attrs, see
     test_app_shell_obs_resilience.py) — exercises the real, unmocked
     `_i18n_available_bundles` / `_on_idioma_change` logic against the real
     shipped es/en locale bundles, with `i18n_state`/`i18n_active` mocked at
     module level so no real disk write ever happens.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock, patch

import opencohost.ui.app_shell as app_shell

ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _source() -> str:
    path = os.path.join(ROOT_DIR, "opencohost", "ui", "app_shell.py")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


# ===========================================================================
# 1. Structural — widgets exist, wired to the D6 next-boot-only pattern
# ===========================================================================


class TestIdiomaStructural:
    def test_app_shell_has_idioma_option_menu(self):
        assert "combo_idioma" in _source()

    def test_app_shell_has_idioma_restart_banner_label(self):
        assert "lbl_idioma_restart" in _source()

    def test_app_shell_has_idioma_section_label(self):
        assert '"Idioma"' in _source()

    def test_restart_banner_never_claims_immediate_change(self):
        """D6: the banner text must say 'next start', never 'changed'."""
        assert "próximo inicio" in app_shell.VocalAIApp._IDIOMA_RESTART_BANNER
        assert "cambió" not in app_shell.VocalAIApp._IDIOMA_RESTART_BANNER.lower()
        assert "cambiado" not in app_shell.VocalAIApp._IDIOMA_RESTART_BANNER.lower()

    def test_on_idioma_change_never_dispatches_to_the_engine(self):
        """D6: no hot-swap — the handler must never touch motor_ia/command_queue."""
        import inspect

        src = inspect.getsource(app_shell.VocalAIApp._on_idioma_change)
        assert "motor_ia" not in src
        assert "command_queue" not in src


# ===========================================================================
# 2. _i18n_available_bundles — real registry, real es/en bundles
# ===========================================================================


class TestI18nAvailableBundles:
    def test_returns_es_and_en_display_names(self):
        bundles = app_shell.VocalAIApp._i18n_available_bundles()
        assert bundles == {"Español": "es", "English": "en"}


# ===========================================================================
# 3. _on_idioma_change — direct-method, mocked i18n_state/i18n_active
# ===========================================================================


def _bare_app():
    app = object.__new__(app_shell.VocalAIApp)
    app._idioma_labels = {"Español": "es", "English": "en"}
    app.lbl_idioma_restart = MagicMock()
    return app


class TestOnIdiomaChange:
    def test_unknown_label_is_a_noop(self):
        app = _bare_app()
        with patch.object(app_shell, "i18n_state") as mock_state:
            app._on_idioma_change("Klingon")
        mock_state.set_locale.assert_not_called()

    def test_selecting_a_label_persists_the_matching_code(self):
        app = _bare_app()
        with patch.object(app_shell, "i18n_state") as mock_state:
            with patch.object(app_shell, "i18n_active") as mock_active:
                mock_active.get_active_bundle.return_value = MagicMock(code="es")
                app._on_idioma_change("English")
        mock_state.set_locale.assert_called_once_with("en")

    def test_selecting_a_locale_that_differs_from_active_shows_restart_banner(self):
        app = _bare_app()
        with patch.object(app_shell, "i18n_state"):
            with patch.object(app_shell, "i18n_active") as mock_active:
                mock_active.get_active_bundle.return_value = MagicMock(code="es")
                app._on_idioma_change("English")
        app.lbl_idioma_restart.configure.assert_called_once_with(
            text=app_shell.VocalAIApp._IDIOMA_RESTART_BANNER
        )

    def test_selecting_the_already_active_locale_clears_the_banner(self):
        app = _bare_app()
        with patch.object(app_shell, "i18n_state"):
            with patch.object(app_shell, "i18n_active") as mock_active:
                mock_active.get_active_bundle.return_value = MagicMock(code="en")
                app._on_idioma_change("English")
        app.lbl_idioma_restart.configure.assert_called_once_with(text="")
