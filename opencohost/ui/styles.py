"""OpenCohost UI Component Styles — Phase 2 (Layer 2 of the design system).

The atomic-design middle layer:

    Layer 1  ``theme``   → raw tokens (colors, spacing, typography, radii)
    Layer 2  ``styles``  → factory functions composing tokens into pre-styled
                           CustomTkinter widgets (THIS module)
    Layer 3  panels      → consume ``styles`` + ``theme``; zero raw hex/sizes

Think of this as the "CSS component classes / Tailwind components" layer: panels
stop repeating ``fg_color=...  corner_radius=...  font=...`` and instead call a
named factory that already knows the right tokens.

Design rules (ADR-021):
- A factory sets DEFAULTS from tokens. Callers may override any of them by
  passing the same keyword — caller ``**kw`` always wins (defaults merged first,
  caller kwargs merged last).
- Factories contain NO business logic — pure presentation.
- ``ctk`` (the customtkinter module) is passed in by the calling panel rather
  than imported here. This keeps the factory headless-safe AND reuses whatever
  ``ctk`` the panel already holds, so a panel test that patches its own module's
  ``ctk`` transparently flows through to these factories (no separate patch
  target, no real-Tk construction under test).

Usage::

    from opencohost.ui import styles, theme
    import customtkinter as ctk

    box = styles.card(ctk, parent)
    styles.heading(ctk, box, "Modelo").pack(fill="x")
    styles.neutral_button(ctk, box, "Editar", on_edit).pack(fill="x")
"""

from __future__ import annotations

from typing import Any, Callable

from opencohost.ui import theme


# ---------------------------------------------------------------------------
# Internal helper
# ---------------------------------------------------------------------------


def _merged(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Return token defaults updated with caller overrides (caller wins)."""
    merged = dict(defaults)
    merged.update(overrides)
    return merged


def _font(ctk: Any, step: str, *, weight: str = "normal") -> Any:
    """Build a CTkFont from a typography token step using the panel's ctk.

    Mirrors ``theme.font`` but uses the injected ``ctk`` so the panel's patched
    module is honored under test.
    """
    _TYPO_MAP: dict[str, int] = {
        "CAPTION": theme.CAPTION,
        "BODY": theme.BODY,
        "LABEL": theme.LABEL,
        "TITLE": theme.TITLE,
        "HEADING": theme.HEADING,
        "HERO": theme.HERO,
    }
    size = _TYPO_MAP[step]  # KeyError on unknown step — intentional
    return ctk.CTkFont(family=theme.FONT_FAMILY, size=size, weight=weight)


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------


def card(ctk: Any, parent: Any, **kw: Any) -> Any:
    """A surface container: ``SURFACE`` background, ``RADIUS_MD`` corners."""
    return ctk.CTkFrame(
        parent,
        **_merged(
            {"fg_color": theme.SURFACE, "corner_radius": theme.RADIUS_MD},
            kw,
        ),
    )


# ---------------------------------------------------------------------------
# Buttons
# ---------------------------------------------------------------------------


def primary_button(
    ctk: Any, parent: Any, text: str, command: Callable[[], None], **kw: Any
) -> Any:
    """Primary call-to-action button (PRIMARY / PRIMARY_HOVER)."""
    return ctk.CTkButton(
        parent,
        text=text,
        command=command,
        **_merged(
            {"fg_color": theme.PRIMARY, "hover_color": theme.PRIMARY_HOVER},
            kw,
        ),
    )


def danger_button(
    ctk: Any, parent: Any, text: str, command: Callable[[], None], **kw: Any
) -> Any:
    """Destructive-action pill button (DANGER_DIM bg, DANGER hover)."""
    return ctk.CTkButton(
        parent,
        text=text,
        command=command,
        **_merged(
            {"fg_color": theme.DANGER_DIM, "hover_color": theme.DANGER},
            kw,
        ),
    )


def neutral_button(
    ctk: Any, parent: Any, text: str, command: Callable[[], None], **kw: Any
) -> Any:
    """Tertiary / neutral button (NEUTRAL bg, NEUTRAL_HOVER hover)."""
    return ctk.CTkButton(
        parent,
        text=text,
        command=command,
        **_merged(
            {"fg_color": theme.NEUTRAL, "hover_color": theme.NEUTRAL_HOVER},
            kw,
        ),
    )


def select_button(
    ctk: Any,
    parent: Any,
    text: str,
    command: Callable[[], None],
    *,
    active: bool = False,
    **kw: Any,
) -> Any:
    """Segmented-control button (manual tier selector).

    When ``active`` the segment uses ``SELECT_ACTIVE``; otherwise ``SELECT_IDLE``.
    Hover is always ``SELECT_HOVER``. Caller ``**kw`` still wins over all.
    """
    return ctk.CTkButton(
        parent,
        text=text,
        command=command,
        **_merged(
            {
                "fg_color": theme.SELECT_ACTIVE if active else theme.SELECT_IDLE,
                "hover_color": theme.SELECT_HOVER,
            },
            kw,
        ),
    )


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------


def heading(ctk: Any, parent: Any, text: str, *, step: str = "LABEL", **kw: Any) -> Any:
    """Bold section heading label (default LABEL step, west-anchored)."""
    return ctk.CTkLabel(
        parent,
        text=text,
        **_merged(
            {"font": _font(ctk, step, weight="bold"), "anchor": "w"},
            kw,
        ),
    )


def body_label(ctk: Any, parent: Any, text: str, *, step: str = "BODY", **kw: Any) -> Any:
    """Primary-text body label (TEXT color, BODY step)."""
    return ctk.CTkLabel(
        parent,
        text=text,
        **_merged(
            {"text_color": theme.TEXT, "font": _font(ctk, step), "anchor": "w"},
            kw,
        ),
    )


def muted_label(ctk: Any, parent: Any, text: str, *, step: str = "BODY", **kw: Any) -> Any:
    """Secondary / caption label (TEXT_MUTED color)."""
    return ctk.CTkLabel(
        parent,
        text=text,
        **_merged(
            {"text_color": theme.TEXT_MUTED, "font": _font(ctk, step), "anchor": "w"},
            kw,
        ),
    )
