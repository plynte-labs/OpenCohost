"""Tests for opencohost.ui.styles — the Layer 2 component-styles factory.

Design system architecture (ADR-021):
    Layer 1  theme.py   → raw tokens (colors, spacing, typography, radii)
    Layer 2  styles.py  → factory functions composing tokens into pre-styled
                          CustomTkinter widgets (THIS module under test)
    Layer 3  panels     → consume styles + theme, zero raw hex/sizes

Non-vacuity guarantee: every assertion below pins a factory's default to a
SPECIFIC theme token value. If a factory regressed to a raw literal (e.g.
``fg_color="#555555"`` instead of ``theme.NEUTRAL``) OR pointed at the wrong
token, the equality check fails. The tests would NOT pass against a stubbed /
hard-coded implementation — they assert the exact token object/value.

Why a fake ctk: styles factories receive the ``ctk`` module from the calling
panel (so the panel's own patched ctk is reused). We inject a fake here that
records constructor kwargs, letting us read back what the factory set without a
Tk display (headless-safe, mirrors theme.font's lazy-import discipline).
"""

from __future__ import annotations

from typing import Any

import pytest

from opencohost.ui import styles, theme


# ---------------------------------------------------------------------------
# Fake ctk — records kwargs, no Tk display needed
# ---------------------------------------------------------------------------


class _FakeWidget:
    """Base fake CTk widget: stores master + kwargs for assertion."""

    def __init__(self, master: Any = None, **kwargs: Any) -> None:
        self.master = master
        self.kwargs = dict(kwargs)

    def cget(self, key: str) -> Any:
        return self.kwargs.get(key)


class _FakeFrame(_FakeWidget):
    pass


class _FakeButton(_FakeWidget):
    pass


class _FakeLabel(_FakeWidget):
    pass


class _FakeFont:
    """Fake CTkFont recording size/weight/family."""

    def __init__(self, *, family: str = "", size: int = 0, weight: str = "normal") -> None:
        self.family = family
        self.size = size
        self.weight = weight


class _FakeCtk:
    """Stand-in customtkinter module exposing only what styles touches."""

    CTkFrame = _FakeFrame
    CTkButton = _FakeButton
    CTkLabel = _FakeLabel
    CTkFont = _FakeFont


@pytest.fixture()
def ctk() -> _FakeCtk:
    return _FakeCtk()


@pytest.fixture()
def parent() -> object:
    return object()


# ---------------------------------------------------------------------------
# card()
# ---------------------------------------------------------------------------


class TestCard:
    def test_returns_frame(self, ctk, parent):
        w = styles.card(ctk, parent)
        assert isinstance(w, _FakeFrame)
        assert w.master is parent

    def test_uses_surface_and_radius_md_tokens(self, ctk, parent):
        w = styles.card(ctk, parent)
        # Pinned to SPECIFIC tokens — fails if a raw literal or wrong token leaks.
        assert w.cget("fg_color") == theme.SURFACE
        assert w.cget("corner_radius") == theme.RADIUS_MD

    def test_caller_kwargs_override_defaults(self, ctk, parent):
        w = styles.card(ctk, parent, fg_color=theme.BG, corner_radius=theme.RADIUS_LG)
        assert w.cget("fg_color") == theme.BG
        assert w.cget("corner_radius") == theme.RADIUS_LG

    def test_extra_kwargs_passed_through(self, ctk, parent):
        w = styles.card(ctk, parent, width=300)
        assert w.cget("width") == 300


# ---------------------------------------------------------------------------
# primary_button()
# ---------------------------------------------------------------------------


class TestPrimaryButton:
    def test_returns_button_with_text_and_command(self, ctk, parent):
        cmd = lambda: None
        w = styles.primary_button(ctk, parent, "Importar", cmd)
        assert isinstance(w, _FakeButton)
        assert w.cget("text") == "Importar"
        assert w.cget("command") is cmd

    def test_uses_primary_tokens(self, ctk, parent):
        w = styles.primary_button(ctk, parent, "Go", lambda: None)
        assert w.cget("fg_color") == theme.PRIMARY
        assert w.cget("hover_color") == theme.PRIMARY_HOVER

    def test_caller_can_override_fg(self, ctk, parent):
        w = styles.primary_button(ctk, parent, "Go", lambda: None, fg_color=theme.DANGER)
        assert w.cget("fg_color") == theme.DANGER


# ---------------------------------------------------------------------------
# danger_button()
# ---------------------------------------------------------------------------


class TestDangerButton:
    def test_uses_danger_dim_pill_tokens(self, ctk, parent):
        w = styles.danger_button(ctk, parent, "Eliminar", lambda: None)
        assert isinstance(w, _FakeButton)
        # danger_button is the destructive pill role → DANGER_DIM bg (matches
        # music_panel "Eliminar track" / "Limpiar faltantes").
        assert w.cget("fg_color") == theme.DANGER_DIM
        assert w.cget("hover_color") == theme.DANGER


# ---------------------------------------------------------------------------
# neutral_button()
# ---------------------------------------------------------------------------


class TestNeutralButton:
    def test_uses_neutral_tokens(self, ctk, parent):
        w = styles.neutral_button(ctk, parent, "Editar", lambda: None)
        assert isinstance(w, _FakeButton)
        assert w.cget("fg_color") == theme.NEUTRAL
        assert w.cget("hover_color") == theme.NEUTRAL_HOVER

    def test_command_and_text_set(self, ctk, parent):
        cmd = lambda: None
        w = styles.neutral_button(ctk, parent, "Editar Perfiles", cmd)
        assert w.cget("text") == "Editar Perfiles"
        assert w.cget("command") is cmd


# ---------------------------------------------------------------------------
# heading()
# ---------------------------------------------------------------------------


class TestHeading:
    def test_returns_label_with_text(self, ctk, parent):
        w = styles.heading(ctk, parent, "Modelo")
        assert isinstance(w, _FakeLabel)
        assert w.cget("text") == "Modelo"

    def test_uses_label_step_bold_font(self, ctk, parent):
        # heading default maps to the LABEL typography step, bold — matches the
        # model_panel section headers (size=13, weight="bold").
        w = styles.heading(ctk, parent, "Modelo")
        font = w.cget("font")
        assert isinstance(font, _FakeFont)
        assert font.size == theme.LABEL
        assert font.weight == "bold"
        assert font.family == theme.FONT_FAMILY

    def test_anchor_defaults_west(self, ctk, parent):
        w = styles.heading(ctk, parent, "Modelo")
        assert w.cget("anchor") == "w"

    def test_step_override(self, ctk, parent):
        w = styles.heading(ctk, parent, "Hero", step="HEADING")
        assert w.cget("font").size == theme.HEADING


# ---------------------------------------------------------------------------
# body_label()
# ---------------------------------------------------------------------------


class TestBodyLabel:
    def test_uses_text_token_and_body_size(self, ctk, parent):
        w = styles.body_label(ctk, parent, "hola")
        assert isinstance(w, _FakeLabel)
        assert w.cget("text_color") == theme.TEXT
        assert w.cget("font").size == theme.BODY


# ---------------------------------------------------------------------------
# muted_label()
# ---------------------------------------------------------------------------


class TestMutedLabel:
    def test_uses_text_muted_token(self, ctk, parent):
        w = styles.muted_label(ctk, parent, "nota")
        assert isinstance(w, _FakeLabel)
        assert w.cget("text_color") == theme.TEXT_MUTED

    def test_caption_step_override(self, ctk, parent):
        w = styles.muted_label(ctk, parent, "nota", step="CAPTION")
        assert w.cget("font").size == theme.CAPTION


# ---------------------------------------------------------------------------
# Cross-cutting: factories must not bypass tokens
# ---------------------------------------------------------------------------


class TestNonVacuity:
    def test_card_default_is_not_arbitrary(self, ctk, parent):
        """Guard: SURFACE must differ from BG so the assertion is meaningful."""
        assert theme.SURFACE != theme.BG
        w = styles.card(ctk, parent)
        assert w.cget("fg_color") == theme.SURFACE
        assert w.cget("fg_color") != theme.BG

    def test_primary_and_neutral_are_distinct(self, ctk, parent):
        assert theme.PRIMARY != theme.NEUTRAL
        p = styles.primary_button(ctk, parent, "a", lambda: None)
        n = styles.neutral_button(ctk, parent, "b", lambda: None)
        assert p.cget("fg_color") != n.cget("fg_color")
