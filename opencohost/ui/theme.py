"""OpenCohost UI Design Token System — Phase 1.

A CSS/Tailwind-style token module: one source of truth for colors, spacing,
typography, and radii. Panels import tokens by name; raw hex literals belong
only here.

Consolidation notes (from ADR-020):
- Three "danger" reds (#cc3333, #8f2f2f, "darkred") → ONE `DANGER` token.
- Tailwind interlopers (#4ade80, #f87171) folded into SUCCESS / DANGER.
- Five background tints (#0f151c, #10161d, #101923, #151d26, #162232) →
  three semantic levels: BG, SURFACE, SURFACE_ALT.
- Two green variants (#22cc66, #44cc66) → SUCCESS (bright label) and
  SUCCESS_DIM (dark pill background, #1f5a3a).

Usage::

    from opencohost.ui import theme

    label = ctk.CTkLabel(parent, fg_color=theme.SURFACE, text_color=theme.TEXT)
    font  = theme.font("BODY", weight="bold")
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import customtkinter as ctk

# ===========================================================================
# COLOR TOKENS
# ===========================================================================
# Dark background family — deepest to shallowest
BG: str = "#0f151c"          # Root canvas / scroll-frame background
SURFACE: str = "#151d26"     # Cards, panels (was #151d26 / #101923 / #111820)
SURFACE_ALT: str = "#162232" # Hero / elevated card (was #162232 / #10161d)
SURFACE_INSET: str = "#1b2633"  # Pill idle background (was #1b2633)
# Nested-content surface — a frame placed INSIDE a SURFACE card (profile body,
# session block, suggestion frame, bulk/topic content). It is darker than
# SURFACE so the nesting reads as recessed. The Phase-1 docstring earmarked
# #101923 for a fold into SURFACE, but in live code it is used as this distinct
# recessed level (12 sites across agenda/avatar/app_shell); collapsing it into
# SURFACE would flatten the hierarchy (a visible change), so it gets its own
# semantic token (was #101923 in agenda/avatar/app_shell nested frames).
SURFACE_NESTED: str = "#101923"  # Recessed content surface inside a SURFACE card

# Borders
BORDER: str = "#2a3a50"      # Subtle dividers

# Text
TEXT: str = "#d8e2ef"        # Primary text (bright on dark BG)
TEXT_MUTED: str = "#a9bdd3"  # Secondary / caption text
# Tertiary / state text — dimmer than TEXT_MUTED. Used for status lines, hints,
# tag captions and suggestion sub-labels. Recurs ~33x across agenda/avatar/
# app_shell/stream_admin; distinct dim blue-grey, so kept separate from the
# brighter TEXT_MUTED (was #8fa3b8).
TEXT_DIM: str = "#8fa3b8"    # Tertiary / state caption text (dim blue-grey)

# Primary action — CTA blue
PRIMARY: str = "#2f5f8f"     # Import/primary buttons (unchanged)
PRIMARY_HOVER: str = "#3a72a8"  # Slightly brighter on hover
# Subtle hover for transparent/flat toggle buttons (collapsible section headers).
# A dim surface tint applied on hover over an otherwise borderless control;
# recurs across agenda/avatar/app_shell/stream_admin collapsible toggles (was
# #1d2a38).
HOVER_SUBTLE: str = "#1d2a38"   # Hover tint for flat/transparent toggle buttons

# Status / semantic tokens
# DANGER consolidates: #cc3333 (status_bar), #8f2f2f (music_panel, delete buttons),
# and Tk "darkred" (voice_control, smart_aggregator) into one value.
DANGER: str = "#cc3333"          # Critical action / error state (bright, operator-visible)
DANGER_HOVER: str = "#e04444"    # Hover variant

SUCCESS: str = "#22cc66"         # Ready / success label (was #22cc66 / #44cc66)
SUCCESS_DIM: str = "#1f5a3a"     # Success pill background (dark, readable on BG)
# Green ACTION button background — the affirmative call-to-action fill (Activar,
# Aprobar, HIGH-confidence badge). A mid green, brighter than SUCCESS_DIM (a
# passive pill bg) and darker than SUCCESS (a bright label color). Used in
# agenda + stream_admin enable/approve buttons; consolidating into either would
# distort an interactive button into a passive indicator (was #2f7d50).
SUCCESS_ACTION: str = "#2f7d50"  # Affirmative-action button bg (enable / approve)

WARNING: str = "#cc8800"         # Amber — alert / loading / fallback (was #cc8800 / #ffaa00)

INFO: str = "#1f3f6f"            # Blue — generating / info state (pill bg)
INFO_BRIGHT: str = "#4488ff"     # Bright blue — pipeline label text

# Neutral / disabled
NEUTRAL: str = "#555555"         # Inactive mood buttons, tertiary controls
NEUTRAL_DIM: str = "#444444"     # Subtle quiet background (QUIET rollup)
NEUTRAL_HOVER: str = "#666666"   # Hover for NEUTRAL buttons (was #666666 in model/profile panels)

# Selectable / segmented control (manual LLM-tier buttons in model_panel)
# A small "tab group" palette: the active segment, idle segments, and hover.
SELECT_ACTIVE: str = "#1f4f7a"   # Active tier segment (was #1f4f7a)
SELECT_IDLE: str = "#2b3440"     # Idle tier segment (was #2b3440)
SELECT_HOVER: str = "#286391"    # Hover on a tier segment (was #286391)

# Danger dim — destructive action pill bg (was #8f2f2f — Limpiar faltantes, Eliminar)
# Kept separate from DANGER so pill-background use does not clash with bright DANGER label.
DANGER_DIM: str = "#8f2f2f"      # Dark pill bg for destructive-action buttons

# Mic-offline pill background — a dim warning-red distinct from DANGER_DIM.
# Semantically separate: DANGER_DIM is a destructive-ACTION button bg; this is a
# passive "mic disconnected" status pill. Consolidating the two would conflate a
# user action with a hardware-state indicator, so it gets its own token (was
# #4a2630 in status_bar mic palette).
MIC_OFFLINE_BG: str = "#4a2630"  # Mic disconnected status pill bg (dim warning-red)

# Warm neutral — de-emphasis action (e.g. Fade out button; not destructive, not primary)
SURFACE_WARM: str = "#7d5a2a"    # Dark amber-brown — secondary non-destructive action

# ===========================================================================
# SPACING SCALE  (px)
# ===========================================================================
# Maps ad-hoc padx/pady literals to a consistent 4-point grid.
# (2, 4, 8, 12, 16, 24) — nearest step for each literal in the codebase.
SPACE_XS: int = 2    # Minimal gap — was 2 in several padx combos
SPACE_SM: int = 4    # Small gap — was 4 in padx=4, pady=4
SPACE_MD: int = 8    # Base gap — was 8 in padx=(0, 8), pady=8
SPACE_LG: int = 12   # Large gap — was 12 in padx=12, pady=(12, 8)
SPACE_XL: int = 16   # Extra large — was 14/16 in padx=14..16
SPACE_2XL: int = 24  # Double XL — for hero section breathing room

# ===========================================================================
# TYPOGRAPHY SCALE  (font size in pt)
# ===========================================================================
# Maps 11 ad-hoc sizes (9, 10, 11, 12, 13, 14, 15, 16, 21, 22, 24) to 6 named steps.
CAPTION: int = 10    # Tiny label, note (was 9/10)
BODY: int = 12       # Default text, form helpers (was 11/12)
LABEL: int = 13      # Pill text, compact labels (was 13)
TITLE: int = 14      # Sub-panel titles, pill main labels (was 14/15)
HEADING: int = 16    # Section headings (was 16)
HERO: int = 24       # Panel hero title (was 21/22/24 — settle at 24)

FONT_FAMILY: str = "Roboto"  # Preferred font family; CTk falls back to system sans-serif

# ===========================================================================
# RADIUS TOKENS  (px)
# ===========================================================================
RADIUS_SM: int = 8   # Tight radius — compact pills, small badges
RADIUS_MD: int = 12  # Standard pill / card radius (most common)
RADIUS_LG: int = 18  # Hero card / scroll-frame radius


# ===========================================================================
# HELPERS
# ===========================================================================

def font(step: str, *, weight: str = "normal") -> "ctk.CTkFont":
    """Return a CTkFont for the given typography step.

    Args:
        step: One of CAPTION, BODY, LABEL, TITLE, HEADING, HERO (as string name).
        weight: ``"normal"`` or ``"bold"``.

    Returns:
        A ``customtkinter.CTkFont`` instance configured for the requested step.

    Raises:
        KeyError: When *step* is not a valid typography token name.

    Notes:
        CTkFont is lazily imported so this module stays importable in headless
        test environments where a Tk display is not available.
    """
    _TYPO_MAP: dict[str, int] = {
        "CAPTION": CAPTION,
        "BODY": BODY,
        "LABEL": LABEL,
        "TITLE": TITLE,
        "HEADING": HEADING,
        "HERO": HERO,
    }
    size = _TYPO_MAP[step]  # raises KeyError for unknown names — intentional
    import customtkinter as _ctk  # lazy — headless-safe
    return _ctk.CTkFont(family=FONT_FAMILY, size=size, weight=weight)
