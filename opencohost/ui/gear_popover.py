"""Gear-settings popover — extracted from app_shell.py (ui_declutter_20260614).

A non-modal CTkToplevel window that contains:
  - Mostrar-logs toggle (default OFF)
  - Compacto toggle (default OFF)
  - OpenCohost brand/About link

Usage (from app_shell.py)::

    self._gear_popover: Any = None
    self.btn_gear = ctk.CTkButton(..., command=self._open_gear_popover)

    def _open_gear_popover(self) -> None:
        from opencohost.ui.gear_popover import open_gear_popover
        self._gear_popover = open_gear_popover(
            parent=self,
            popover_ref_getter=lambda: self._gear_popover,
            popover_ref_setter=lambda p: setattr(self, "_gear_popover", p),
            compacto_active=getattr(self, "_compacto_active", True),
            logs_visible=getattr(self, "_logs_visible_active", False),
            on_compacto_toggle=self._toggle_modo_compacto,
            on_logs_toggle=self._toggle_logs_panel,
            on_compacto_state_write=lambda v: setattr(self, "_compacto_active", v),
            on_logs_state_write=lambda v: setattr(self, "_logs_visible_active", v),
        )
"""
from __future__ import annotations

import webbrowser
from typing import Any, Callable

import customtkinter as ctk

from opencohost.ui.window_utils import raise_window, show_toplevel


def open_gear_popover(
    parent: Any,
    *,
    popover_ref_getter: Callable[[], Any],
    popover_ref_setter: Callable[[Any], None],
    compacto_active: bool,
    logs_visible: bool,
    on_compacto_toggle: Callable[[], None],
    on_logs_toggle: Callable[[], None],
    on_compacto_state_write: Callable[[bool], None],
    on_logs_state_write: Callable[[bool], None],
) -> Any:
    """Open (or focus) the gear-settings popover.

    Returns the CTkToplevel instance (or None if an existing one was focused).

    Guard: if a popover is already open, focus it instead of opening a second one.
    Teardown: the Toplevel is destroyed when the user closes it; a ``<Destroy>``
    bind clears the caller's reference so the guard resets correctly.  State is
    persisted immediately on each toggle change (no Apply button needed).

    Args:
        parent: The parent Tk window (usually VocalAIApp / ``self``).
        popover_ref_getter: Zero-arg callable that returns the current popover
            reference (``None`` when closed).
        popover_ref_setter: One-arg callable to update the caller's reference.
        compacto_active: Current compact-mode state to initialise the toggle.
        logs_visible: Current logs-visible state to initialise the toggle.
        on_compacto_toggle: Called after compact state is written to apply the change.
        on_logs_toggle: Called after logs-visible state is written to apply the change.
        on_compacto_state_write: Called with the new bool to persist compact state.
        on_logs_state_write: Called with the new bool to persist logs-visible state.

    Returns:
        The new CTkToplevel instance, or ``None`` if an existing popover was focused.
    """
    # Guard: prevent duplicate popovers
    existing = popover_ref_getter()
    if existing is not None:
        try:
            if existing.winfo_exists():
                raise_window(existing)
                return None
        except Exception:
            pass

    popover = ctk.CTkToplevel(parent)
    popover_ref_setter(popover)
    popover.title("Configuración")
    popover.geometry("260x200")
    popover.resizable(False, False)
    popover.configure(fg_color="#111820")

    ctk.CTkLabel(
        popover,
        text="Configuración",
        font=ctk.CTkFont(size=14, weight="bold"),
        text_color="#d8e2ef",
    ).pack(pady=(14, 8), padx=16, anchor="w")

    # Mostrar-logs toggle
    def _on_logs_toggle():
        on_logs_state_write(bool(switch_logs.get()))
        on_logs_toggle()

    switch_logs = ctk.CTkSwitch(
        popover,
        text="Mostrar logs",
        command=_on_logs_toggle,
        onvalue=True,
        offvalue=False,
    )
    switch_logs.pack(padx=20, pady=4, anchor="w")
    if logs_visible:
        switch_logs.select()
    else:
        switch_logs.deselect()

    # Compacto toggle
    def _on_compacto_toggle():
        on_compacto_state_write(bool(switch_compacto_pop.get()))
        on_compacto_toggle()

    switch_compacto_pop = ctk.CTkSwitch(
        popover,
        text="Compacto",
        command=_on_compacto_toggle,
        onvalue=True,
        offvalue=False,
    )
    switch_compacto_pop.pack(padx=20, pady=4, anchor="w")
    if compacto_active:
        switch_compacto_pop.select()
    else:
        switch_compacto_pop.deselect()

    # Brand link
    lbl_brand = ctk.CTkLabel(
        popover,
        text="OpenCohost",
        font=ctk.CTkFont(size=12, weight="bold"),
        text_color="#3a86ff",
        cursor="hand2",
    )
    lbl_brand.pack(pady=(12, 4), padx=16, anchor="w")
    lbl_brand.bind(
        "<Button-1>",
        lambda e: webbrowser.open_new_tab("https://github.com/plynte-labs/opencohost"),
    )
    lbl_brand.bind("<Enter>", lambda e: lbl_brand.configure(text_color="#5390ff"))
    lbl_brand.bind("<Leave>", lambda e: lbl_brand.configure(text_color="#3a86ff"))

    def _on_close():
        popover_ref_setter(None)
        popover.destroy()

    popover.protocol("WM_DELETE_WINDOW", _on_close)
    # <Destroy> bind clears the reference when the window is closed by any
    # means (e.g. parent window teardown), not just the WM_DELETE_WINDOW protocol.
    popover.bind("<Destroy>", lambda e: popover_ref_setter(None) if e.widget is popover else None)

    # Raise the popover above the parent after all widgets are built.
    # Uses a deferred raise to land after CTkToplevel's own after(5, deiconify)
    # dance on Windows. modal=False — the gear popover is non-modal by design.
    show_toplevel(popover, parent, modal=False)

    return popover
