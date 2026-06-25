"""Tests for opencohost/ui/theme.py — design-token system.

Verifies:
1. Color token taxonomy: all required semantic names present, correct types/formats.
2. Spacing scale: named steps present with int px values.
3. Typography scale: named steps present, FONT_FAMILY string defined.
4. Radius tokens: named sizes present with int px values.
5. font() helper: returns a CTkFont instance (or mock-compatible object) when ctk is available.

Non-vacuous: each assertion targets a specific name and type so a missing or
wrong-typed token produces a clear failure message.
"""

from __future__ import annotations

import re
import sys
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HEX_RE = re.compile(r"^#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?$")


def _is_hex(value: object) -> bool:
    """True when *value* is a CSS hex color string (#RGB or #RRGGBB)."""
    return isinstance(value, str) and bool(_HEX_RE.match(value))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def theme():
    """Import theme module fresh, with ctk stubbed so it is headless-safe."""
    mock_ctk = MagicMock()

    class _MockFont:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    mock_ctk.CTkFont = _MockFont

    with patch.dict(sys.modules, {"customtkinter": mock_ctk}):
        # Force a clean reload so the patch takes effect
        if "opencohost.ui.theme" in sys.modules:
            del sys.modules["opencohost.ui.theme"]
        import opencohost.ui.theme as t

        yield t


# ---------------------------------------------------------------------------
# 1. Color tokens
# ---------------------------------------------------------------------------


REQUIRED_COLOR_TOKENS = [
    # Background family
    "BG",
    "SURFACE",
    "SURFACE_ALT",
    "SURFACE_INSET",
    # Border
    "BORDER",
    # Text
    "TEXT",
    "TEXT_MUTED",
    # Primary action (CTA blue)
    "PRIMARY",
    "PRIMARY_HOVER",
    # Status / semantic
    "DANGER",
    "DANGER_HOVER",
    "SUCCESS",
    "SUCCESS_DIM",
    "WARNING",
    "INFO",
    # Neutral
    "NEUTRAL",
    "NEUTRAL_DIM",
]


class TestColorTokens:
    def test_all_required_color_tokens_present(self, theme):
        missing = [name for name in REQUIRED_COLOR_TOKENS if not hasattr(theme, name)]
        assert not missing, f"Missing color tokens: {missing}"

    def test_all_required_color_tokens_are_hex_strings(self, theme):
        bad = [
            name
            for name in REQUIRED_COLOR_TOKENS
            if not _is_hex(getattr(theme, name, None))
        ]
        assert not bad, (
            f"Color tokens with non-hex values: "
            + ", ".join(f"{n}={getattr(theme, n, None)!r}" for n in bad)
        )

    def test_bg_is_dark(self, theme):
        # BG must be a very dark color — first two hex digits should be <= 0x1f
        hex_val = theme.BG.lstrip("#")
        r = int(hex_val[0:2], 16)
        assert r <= 0x1F, f"BG {theme.BG!r} doesn't look like a dark background (R={r})"

    def test_danger_is_red_family(self, theme):
        hex_val = theme.DANGER.lstrip("#")
        r = int(hex_val[0:2], 16)
        g = int(hex_val[2:4], 16)
        assert r > 0x80 and g < 0x80, (
            f"DANGER {theme.DANGER!r} doesn't look like red (R={r}, G={g})"
        )

    def test_success_is_green_family(self, theme):
        hex_val = theme.SUCCESS.lstrip("#")
        r = int(hex_val[0:2], 16)
        g = int(hex_val[2:4], 16)
        assert g > r, f"SUCCESS {theme.SUCCESS!r} doesn't look green (R={r}, G={g})"

    def test_warning_is_amber_family(self, theme):
        hex_val = theme.WARNING.lstrip("#")
        r = int(hex_val[0:2], 16)
        g = int(hex_val[2:4], 16)
        b = int(hex_val[4:6], 16)
        assert r > 0x80 and g > 0x50 and b < 0x50, (
            f"WARNING {theme.WARNING!r} doesn't look amber (R={r}, G={g}, B={b})"
        )

    def test_primary_is_blue_family(self, theme):
        hex_val = theme.PRIMARY.lstrip("#")
        r = int(hex_val[0:2], 16)
        b = int(hex_val[4:6], 16)
        assert b > r, f"PRIMARY {theme.PRIMARY!r} doesn't look blue (R={r}, B={b})"

    def test_single_danger_token_consolidation(self, theme):
        """Danger tokens are consolidated: only DANGER, DANGER_HOVER, and DANGER_DIM allowed.

        DANGER     — bright label / error state (the unified "danger red").
        DANGER_HOVER — hover variant.
        DANGER_DIM — dark pill-background for destructive-action buttons; a separate
                     semantic role (pill bg vs. label color), NOT a duplicated danger red.

        Any other DANGER_* name signals the 3-reds-to-1 consolidation was broken.
        """
        _allowed = {"DANGER", "DANGER_HOVER", "DANGER_DIM"}
        danger_variants = [
            attr for attr in dir(theme)
            if attr.startswith("DANGER") and attr not in _allowed
        ]
        assert not danger_variants, (
            f"Unexpected DANGER variants found (3-reds-to-1 consolidation broken): {danger_variants}"
        )


# ---------------------------------------------------------------------------
# 2. Spacing scale
# ---------------------------------------------------------------------------


REQUIRED_SPACING_TOKENS = ["SPACE_XS", "SPACE_SM", "SPACE_MD", "SPACE_LG", "SPACE_XL", "SPACE_2XL"]


class TestSpacingScale:
    def test_all_spacing_tokens_present(self, theme):
        missing = [name for name in REQUIRED_SPACING_TOKENS if not hasattr(theme, name)]
        assert not missing, f"Missing spacing tokens: {missing}"

    def test_all_spacing_tokens_are_positive_ints(self, theme):
        bad = [
            name
            for name in REQUIRED_SPACING_TOKENS
            if not (isinstance(getattr(theme, name, None), int) and getattr(theme, name) > 0)
        ]
        assert not bad, (
            f"Spacing tokens with non-positive-int values: "
            + ", ".join(f"{n}={getattr(theme, n, None)!r}" for n in bad)
        )

    def test_spacing_scale_is_monotonically_increasing(self, theme):
        values = [getattr(theme, name) for name in REQUIRED_SPACING_TOKENS]
        for i in range(len(values) - 1):
            assert values[i] < values[i + 1], (
                f"Spacing scale not monotonically increasing at position {i}: "
                f"{REQUIRED_SPACING_TOKENS[i]}={values[i]} >= "
                f"{REQUIRED_SPACING_TOKENS[i+1]}={values[i+1]}"
            )


# ---------------------------------------------------------------------------
# 3. Typography scale
# ---------------------------------------------------------------------------


REQUIRED_TYPO_TOKENS = ["CAPTION", "BODY", "LABEL", "TITLE", "HEADING", "HERO"]


class TestTypographyScale:
    def test_all_typo_tokens_present(self, theme):
        missing = [name for name in REQUIRED_TYPO_TOKENS if not hasattr(theme, name)]
        assert not missing, f"Missing typography tokens: {missing}"

    def test_all_typo_tokens_are_positive_ints(self, theme):
        bad = [
            name
            for name in REQUIRED_TYPO_TOKENS
            if not (isinstance(getattr(theme, name, None), int) and getattr(theme, name) > 0)
        ]
        assert not bad, (
            f"Typography tokens with non-positive-int values: "
            + ", ".join(f"{n}={getattr(theme, n, None)!r}" for n in bad)
        )

    def test_typo_scale_is_monotonically_increasing(self, theme):
        values = [getattr(theme, name) for name in REQUIRED_TYPO_TOKENS]
        for i in range(len(values) - 1):
            assert values[i] < values[i + 1], (
                f"Typography scale not monotonically increasing at position {i}: "
                f"{REQUIRED_TYPO_TOKENS[i]}={values[i]} >= "
                f"{REQUIRED_TYPO_TOKENS[i+1]}={values[i+1]}"
            )

    def test_font_family_token_present(self, theme):
        assert hasattr(theme, "FONT_FAMILY"), "FONT_FAMILY token missing from theme"
        assert isinstance(theme.FONT_FAMILY, str), "FONT_FAMILY must be a string"
        assert len(theme.FONT_FAMILY) > 0, "FONT_FAMILY must not be empty"

    def test_font_helper_exists(self, theme):
        assert hasattr(theme, "font"), "font() helper function missing from theme"
        assert callable(theme.font), "theme.font must be callable"

    def test_font_helper_returns_ctkfont(self, theme):
        result = theme.font("BODY")
        # Must be the mock CTkFont we injected, or a real CTkFont
        assert result is not None
        # The mock CTkFont class stores kwargs
        assert hasattr(result, "kwargs"), f"Expected mock CTkFont with kwargs, got: {type(result)}"

    def test_font_helper_accepts_weight(self, theme):
        result = theme.font("HEADING", weight="bold")
        assert result.kwargs.get("weight") == "bold"

    def test_font_helper_unknown_step_raises(self, theme):
        with pytest.raises((KeyError, ValueError)):
            theme.font("NONEXISTENT_STEP")


# ---------------------------------------------------------------------------
# 4. Radius tokens
# ---------------------------------------------------------------------------


REQUIRED_RADIUS_TOKENS = ["RADIUS_SM", "RADIUS_MD", "RADIUS_LG"]


class TestRadiusTokens:
    def test_all_radius_tokens_present(self, theme):
        missing = [name for name in REQUIRED_RADIUS_TOKENS if not hasattr(theme, name)]
        assert not missing, f"Missing radius tokens: {missing}"

    def test_all_radius_tokens_are_positive_ints(self, theme):
        bad = [
            name
            for name in REQUIRED_RADIUS_TOKENS
            if not (isinstance(getattr(theme, name, None), int) and getattr(theme, name) > 0)
        ]
        assert not bad, (
            f"Radius tokens with non-positive-int values: "
            + ", ".join(f"{n}={getattr(theme, n, None)!r}" for n in bad)
        )

    def test_radius_scale_is_monotonically_increasing(self, theme):
        values = [getattr(theme, name) for name in REQUIRED_RADIUS_TOKENS]
        for i in range(len(values) - 1):
            assert values[i] < values[i + 1], (
                f"Radius scale not monotonically increasing: "
                f"{REQUIRED_RADIUS_TOKENS[i]}={values[i]} >= "
                f"{REQUIRED_RADIUS_TOKENS[i+1]}={values[i+1]}"
            )
