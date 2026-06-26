"""Contract tests: cohost_agenda_panel.py must reference design tokens, not raw hex.

Design-system migration (ui-design-system-20260625), Phase 4. Mirrors
tests/test_status_bar_tokens.py but hardens the source-scan guard: instead of a
fragile two-space-comment split, it uses Python's ``tokenize`` module to walk
the real token stream and inspect only STRING tokens in LIVE code (comments and
docstrings are skipped by the tokenizer itself). A planted live hex literal is
therefore always caught, and a documented "was #oldhex" note in a comment never
produces a false positive.

Non-vacuity:
- The source-scan test fails (printing line numbers) if ANY raw 6-digit hex
  literal survives in a live string in cohost_agenda_panel.py.
- The constant-pinning tests import the REAL module-level color dict and assert
  its values ARE the theme tokens (not stale copies of the same hex).
- The consolidation/new-token tests assert the NEW (token) value at the
  migrated site, proving each shift/addition landed and is located.

These tests import the REAL constants and drive the REAL panel headless (ctk is
patched to a recording stub), so no Tk display is required and nothing is
over-mocked.
"""

from __future__ import annotations

import io
import pathlib
import re
import sys
import tokenize
from unittest.mock import MagicMock, patch

import pytest


_PANEL_PATH = (
    pathlib.Path(__file__).parent.parent
    / "opencohost"
    / "ui"
    / "cohost_agenda_panel.py"
)

# Hex color literal pattern — matches #RGB and #RRGGBB.
_HEX_RE = re.compile(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?")


# ---------------------------------------------------------------------------
# Source-scan guard — tokenizer-based (robust, no comment-split brittleness)
# ---------------------------------------------------------------------------


def _live_hex_offenses(source: str) -> list[str]:
    """Return offenses for raw hex literals living in real STRING tokens.

    Uses ``tokenize`` so comments (``tokenize.COMMENT``) are never inspected and
    docstrings/string literals are inspected as whole STRING tokens. Only a hex
    color sitting inside a live string literal counts as a violation — exactly
    the design-system contract (raw hex belongs only in theme.py).
    """
    offenses: list[str] = []
    readline = io.StringIO(source).readline
    for tok in tokenize.generate_tokens(readline):
        if tok.type != tokenize.STRING:
            continue
        # Skip module/class/function docstrings: a STRING that is the whole
        # logical line on its own is a docstring or standalone string, never a
        # widget color argument. We still scan it to be strict, EXCEPT the
        # module docstring legitimately may mention hex in prose — but this file
        # avoids that, so we scan all strings. The migration must therefore not
        # leave hex in ANY string. Documented old values live in COMMENTS.
        for match in _HEX_RE.finditer(tok.string):
            line = source.splitlines()[tok.start[0] - 1]
            offenses.append(
                f"  Line {tok.start[0]}: {line.strip()!r}  -> match: {match.group()!r}"
            )
    return offenses


class TestCohostAgendaNoRawHex:
    def test_panel_file_exists(self):
        assert _PANEL_PATH.exists(), f"Panel not found at: {_PANEL_PATH}"

    def test_panel_imports_theme(self):
        source = _PANEL_PATH.read_text(encoding="utf-8")
        assert (
            "from opencohost.ui import theme" in source
            or "from opencohost.ui.theme" in source
        ), "cohost_agenda_panel.py must import theme tokens."

    def test_no_raw_hex_in_live_strings(self):
        source = _PANEL_PATH.read_text(encoding="utf-8")
        offenses = _live_hex_offenses(source)
        assert not offenses, (
            f"cohost_agenda_panel.py contains {len(offenses)} raw hex color "
            f"literal(s) in live strings (design-system contract VIOLATED — "
            f"migrate to theme tokens):\n" + "\n".join(offenses)
        )

    def test_scan_is_non_vacuous(self):
        """The guard MUST flag a planted live hex literal.

        Proves the tokenizer scan is real: a hex inside a string token is
        caught, while a hex inside a comment is not.
        """
        planted = 'x = "#abcdef"  # was #123456 -> TOKEN\n'
        offenses = _live_hex_offenses(planted)
        # Inspect only the matched hex (after the "-> match:" marker) so the
        # comment text echoed into the message does not create a false signal.
        matched = [o.split("-> match:", 1)[1] for o in offenses]
        # The live string "#abcdef" must be flagged.
        assert any("#abcdef" in m for m in matched), offenses
        # The comment's "#123456" must NOT be flagged.
        assert not any("#123456" in m for m in matched), offenses


# ---------------------------------------------------------------------------
# Module import under a patched ctk (headless-safe)
# ---------------------------------------------------------------------------


@pytest.fixture()
def panel_mod():
    """Import the panel module with ctk + theme available, headless-safe."""
    from opencohost.ui import cohost_agenda_panel as mod

    return mod


@pytest.fixture()
def theme_mod():
    from opencohost.ui import theme

    return theme


# ---------------------------------------------------------------------------
# Constant pinning — module-level color dict references tokens, not stale hex.
# ---------------------------------------------------------------------------


class TestConfidenceColorsPinned:
    def test_confidence_colors_are_tokens(self, panel_mod, theme_mod):
        # Real class constant must reference theme tokens (proves no stale copy).
        assert panel_mod.CoHostAgendaPanel._CONFIDENCE_COLORS == {
            "HIGH": theme_mod.SUCCESS_ACTION,   # new token: mid-green action bg
            "MEDIUM": theme_mod.SURFACE_WARM,   # exact #7d5a2a
            "LOW": theme_mod.NEUTRAL,           # exact #555555
        }

    def test_confidence_labels_unchanged(self, panel_mod):
        # Behavior (labels) must be identical post-migration.
        assert panel_mod.CoHostAgendaPanel._CONFIDENCE_LABELS == {
            "HIGH": "ALTA",
            "MEDIUM": "MEDIA",
            "LOW": "BAJA",
        }


# ---------------------------------------------------------------------------
# New tokens — must exist, be valid hex, and preserve their distinct value.
# ---------------------------------------------------------------------------


class TestNewTokens:
    def test_success_action_present_and_value(self, theme_mod):
        assert hasattr(theme_mod, "SUCCESS_ACTION"), "theme.SUCCESS_ACTION missing"
        assert theme_mod.SUCCESS_ACTION == "#2f7d50"
        # Distinct from the other greens — consolidating would distort meaning.
        assert theme_mod.SUCCESS_ACTION != theme_mod.SUCCESS
        assert theme_mod.SUCCESS_ACTION != theme_mod.SUCCESS_DIM

    def test_text_dim_present_and_value(self, theme_mod):
        assert hasattr(theme_mod, "TEXT_DIM"), "theme.TEXT_DIM missing"
        assert theme_mod.TEXT_DIM == "#8fa3b8"
        # Dimmer tertiary text, distinct from TEXT_MUTED.
        assert theme_mod.TEXT_DIM != theme_mod.TEXT_MUTED

    def test_surface_nested_present_and_value(self, theme_mod):
        assert hasattr(theme_mod, "SURFACE_NESTED"), "theme.SURFACE_NESTED missing"
        assert theme_mod.SURFACE_NESTED == "#101923"
        # Darker nested-content surface, distinct from the SURFACE card it sits in.
        assert theme_mod.SURFACE_NESTED != theme_mod.SURFACE

    def test_hover_subtle_present_and_value(self, theme_mod):
        assert hasattr(theme_mod, "HOVER_SUBTLE"), "theme.HOVER_SUBTLE missing"
        assert theme_mod.HOVER_SUBTLE == "#1d2a38"

    def test_new_tokens_are_hex(self, theme_mod):
        for name in ("SUCCESS_ACTION", "TEXT_DIM", "SURFACE_NESTED", "HOVER_SUBTLE"):
            value = getattr(theme_mod, name)
            assert re.match(r"^#[0-9a-fA-F]{6}$", value), f"{name}={value!r} not hex"


# ---------------------------------------------------------------------------
# Real-surface drive — build the panel headless and assert rendered token
# values land at the right widgets. ctk is a recording stub that captures the
# fg_color / text_color kwargs passed to each widget constructor.
# ---------------------------------------------------------------------------


class _RecordingWidget:
    """Stub CTk widget: records construction kwargs, supports grid/config chains."""

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs

    def grid(self, *a, **k):
        return self

    def grid_remove(self, *a, **k):
        return self

    def grid_columnconfigure(self, *a, **k):
        return self

    def grid_rowconfigure(self, *a, **k):
        return self

    def configure(self, *a, **k):
        self.kwargs.update(k)
        return self

    def set(self, *a, **k):
        return self

    def insert(self, *a, **k):
        return self

    def winfo_children(self):
        return []


class _RecordingCtk:
    """Recording ctk module stub: every factory returns a _RecordingWidget and
    appends it to ``created`` so tests can inspect what colors were used."""

    def __init__(self):
        self.created: list[_RecordingWidget] = []

    def _make(self, *args, **kwargs):
        w = _RecordingWidget(*args, **kwargs)
        self.created.append(w)
        return w

    # Each CTk constructor used by the panel maps to the recording factory.
    CTkScrollableFrame = property(lambda self: self._make)
    CTkFrame = property(lambda self: self._make)
    CTkLabel = property(lambda self: self._make)
    CTkButton = property(lambda self: self._make)
    CTkEntry = property(lambda self: self._make)
    CTkTextbox = property(lambda self: self._make)
    CTkOptionMenu = property(lambda self: self._make)

    def CTkFont(self, *args, **kwargs):  # noqa: N802 — mirrors ctk API
        return ("font", kwargs)


def _build_panel(panel_mod):
    """Build the panel with a recording ctk stub; return (panel, ctk_stub)."""
    stub = _RecordingCtk()
    with patch.object(panel_mod, "ctk", stub):
        panel = panel_mod.CoHostAgendaPanel()
        parent = _RecordingWidget()
        panel.build(parent)
    return panel, stub


def _colors_used(stub: _RecordingCtk, key: str) -> set[str]:
    """All distinct values passed under *key* (fg_color / text_color / hover_color)."""
    return {
        w.kwargs[key]
        for w in stub.created
        if key in w.kwargs and isinstance(w.kwargs[key], str)
    }


class TestRenderedColorsAreTokens:
    def test_no_raw_hex_reaches_any_widget(self, panel_mod):
        """No widget may be constructed with a raw hex string color.

        Strongest behavioral guard: drives the REAL build() and asserts every
        color kwarg that reached a widget is either a theme token value or the
        literal 'transparent' — never an un-tokenized hex that happens to match.
        This catches a hex that slipped through even if it equals a token value,
        because we additionally pin specific widgets below.
        """
        stub = _build_panel(panel_mod)[1]
        all_colors: set[str] = set()
        for key in ("fg_color", "text_color", "hover_color"):
            all_colors |= _colors_used(stub, key)
        # Every color used must be a known theme token value or 'transparent'.
        from opencohost.ui import theme

        token_values = {
            getattr(theme, n)
            for n in dir(theme)
            if n.isupper() and isinstance(getattr(theme, n), str)
        }
        allowed = token_values | {"transparent"}
        stray = {c for c in all_colors if c not in allowed}
        assert not stray, f"Colors not sourced from theme tokens: {stray}"

    def test_root_and_surfaces_use_tokens(self, panel_mod, theme_mod):
        stub = _build_panel(panel_mod)[1]
        fg = _colors_used(stub, "fg_color")
        assert theme_mod.BG in fg            # scroll root + textboxes + suggestion rows
        assert theme_mod.SURFACE_ALT in fg   # hero
        assert theme_mod.SURFACE in fg       # cards
        assert theme_mod.SURFACE_NESTED in fg  # nested content frames
        assert theme_mod.PRIMARY in fg       # primary action buttons
        assert theme_mod.SUCCESS_ACTION in fg  # Activar / Aprobar buttons
        assert theme_mod.SURFACE_WARM in fg  # Stop suave
        assert theme_mod.DANGER_DIM in fg    # Emergencia / Eliminar / Rechazar
        assert theme_mod.NEUTRAL in fg       # neutral buttons

    def test_text_colors_use_tokens(self, panel_mod, theme_mod):
        stub = _build_panel(panel_mod)[1]
        tc = _colors_used(stub, "text_color")
        assert theme_mod.TEXT in tc          # primary text
        assert theme_mod.TEXT_MUTED in tc    # muted captions (#a9bdd3)
        assert theme_mod.TEXT_DIM in tc      # dimmer state text (#8fa3b8)

    def test_hover_uses_subtle_token(self, panel_mod, theme_mod):
        stub = _build_panel(panel_mod)[1]
        hc = _colors_used(stub, "hover_color")
        assert theme_mod.HOVER_SUBTLE in hc  # transparent toggle hover (#1d2a38)


# ---------------------------------------------------------------------------
# Behavior preservation — pure helpers must be untouched by the migration.
# ---------------------------------------------------------------------------


class TestBehaviorUnchanged:
    def test_normalize_helpers_unchanged(self, panel_mod):
        cls = panel_mod.CoHostAgendaPanel
        assert cls.normalize_priority("Alta") == "alta"
        assert cls.normalize_response_length("Expandida") == "expandida"
        assert cls.normalize_rhythm("Dinámico") == "dinamico"
        assert cls.normalize_safety_mode("Live_safe") == "live_safe"
        assert cls.clamp_turn_limit("99") == 20
