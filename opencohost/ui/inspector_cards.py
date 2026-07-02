"""Editorial cards inspector — read-only "Tarjetas editoriales" window.

Product UI for streamer + operator (Configuración > Perfil), NOT a debug
tool. v1 is strictly read-only; list+detail layout reserves structure for a
future edit affordance (cards_memory_readonly_panels_20260701).

Window pattern is EXACTLY gear_popover.py's: duplicate-open guard ->
CTkToplevel -> apply_app_icon -> show_toplevel(modal=False) -> <Destroy>
ref-reset bind.

Data comes from an injected EditorialCardStore.list_all(), read on a worker
thread and marshaled back to the caller's main thread via the injected
schedule_ui_update callable (mirrors app_shell._safe_after) — the worker
thread never touches any Tk widget. Any store exception fails open: the
list renders "No disponible" and the error is logged, never raised.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

import customtkinter as ctk

from opencohost.core.editorial_cards import EditorialCardStatus
from opencohost.ui.window_utils import apply_app_icon, raise_window, show_toplevel

logger = logging.getLogger(__name__)

_STATUS_ORDER = (
    EditorialCardStatus.ACTIVE,
    EditorialCardStatus.ARMED,
    EditorialCardStatus.DRAFT,
    EditorialCardStatus.USED,
    EditorialCardStatus.EXPIRED,
)
_STATUS_LABELS = {
    EditorialCardStatus.ACTIVE: "Activa",
    EditorialCardStatus.ARMED: "Armada",
    EditorialCardStatus.DRAFT: "Borrador",
    EditorialCardStatus.USED: "Usada",
    EditorialCardStatus.EXPIRED: "Vencida",
}


def format_launcher_label(count: int | None) -> str:
    """Return the "Tarjetas editoriales (N)" launcher button text.

    Fail-open: count=None (unknown, e.g. the store errored) omits the
    parenthetical instead of showing a misleading "(0)".
    """
    if count is None:
        return "Tarjetas editoriales"
    return f"Tarjetas editoriales ({count})"


def group_cards_by_status(cards: list) -> list[tuple[Any, str, bool]]:
    """Group cards by STORED status, ACTIVE -> ARMED -> DRAFT -> USED -> EXPIRED.

    Returns a flat list of (card, status_label, vencida) tuples in render
    order. ``vencida`` is a computed chip: True when card.is_expired() but
    the stored status is not already EXPIRED (stored-vs-computed nuance —
    expiry is checked at render time, the DB row may not have caught up).
    """
    by_status: dict[EditorialCardStatus, list] = {status: [] for status in _STATUS_ORDER}
    for card in cards:
        by_status.setdefault(card.status, []).append(card)
    ordered: list[tuple[Any, str, bool]] = []
    for status in _STATUS_ORDER:
        for card in by_status.get(status, []):
            vencida = card.is_expired() and status is not EditorialCardStatus.EXPIRED
            ordered.append((card, _STATUS_LABELS[status], vencida))
    return ordered


def open_inspector_cards(
    parent: Any,
    *,
    ref_getter: Callable[[], Any],
    ref_setter: Callable[[Any], None],
    card_store: Any,
    schedule_ui_update: Callable[[Callable[[], None]], None],
) -> Any:
    """Open (or focus) the "Tarjetas editoriales" read-only inspector.

    Guard: if a window is already open, focus it instead of opening a
    second one (see gear_popover.open_gear_popover). Returns the new
    CTkToplevel, or None if an existing one was focused instead.
    """
    existing = ref_getter()
    if existing is not None:
        try:
            if existing.winfo_exists():
                raise_window(existing)
                return None
        except Exception:
            pass

    win = ctk.CTkToplevel(parent)
    apply_app_icon(win)
    ref_setter(win)
    win.title("Tarjetas editoriales")
    win.geometry("760x540")
    win.configure(fg_color="#111820")

    ctk.CTkLabel(
        win,
        text="Tarjetas editoriales",
        font=ctk.CTkFont(size=15, weight="bold"),
        text_color="#d8e2ef",
    ).pack(pady=(14, 4), padx=16, anchor="w")

    stamp_label = ctk.CTkLabel(win, text="", font=ctk.CTkFont(size=10), text_color="#6b7b8d")
    stamp_label.pack(padx=16, anchor="w")

    body = ctk.CTkFrame(win, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=16, pady=(8, 8))

    list_frame = ctk.CTkScrollableFrame(body, fg_color="#151d26", corner_radius=10, width=260)
    list_frame.pack(side="left", fill="y", padx=(0, 8))

    detail_frame = ctk.CTkFrame(body, fg_color="#151d26", corner_radius=10)
    detail_frame.pack(side="left", fill="both", expand=True)

    def _clear(frame: Any) -> None:
        for child in list(frame.winfo_children()):
            child.destroy()

    def _copy_to_clipboard(text: str) -> None:
        try:
            win.clipboard_clear()
            win.clipboard_append(text)
        except Exception:
            logger.exception("No se pudo copiar al portapapeles")

    def _field_row(label_text: str, value: str) -> None:
        row = ctk.CTkFrame(detail_frame, fg_color="transparent")
        row.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(row, text=label_text, font=ctk.CTkFont(size=11, weight="bold")).pack(anchor="w")
        ctk.CTkLabel(row, text=value or "—", wraplength=380, justify="left").pack(anchor="w")
        ctk.CTkButton(row, text="Copiar", width=70, command=lambda v=value: _copy_to_clipboard(v)).pack(anchor="w", pady=(2, 0))

    def _render_detail(card: Any) -> None:
        _clear(detail_frame)
        if card is None:
            ctk.CTkLabel(detail_frame, text="Seleccioná una tarjeta", text_color="#6b7b8d").pack(padx=12, pady=12, anchor="w")
            return
        ctk.CTkLabel(detail_frame, text=card.topic, font=ctk.CTkFont(size=14, weight="bold")).pack(padx=12, pady=(12, 4), anchor="w")
        ctk.CTkLabel(detail_frame, text=card.summary, wraplength=380, justify="left").pack(padx=12, pady=4, anchor="w")
        _field_row("Tu postura", card.streamer_take)
        _field_row("Contrapuntos", "\n".join(card.counterpoints))
        _field_row("Ganchos de discusión", "\n".join(card.discussion_hooks))

    def _select_card(card: Any) -> None:
        _render_detail(card)

    def _render_cards(cards: list) -> None:
        if not win.winfo_exists():
            return
        stamp_label.configure(text="Actualizado " + time.strftime("%H:%M:%S"))
        _clear(list_frame)
        grouped = group_cards_by_status(cards)
        for card, status_label, vencida in grouped:
            badge = status_label + (" · vencida" if vencida else "")
            ctk.CTkButton(
                list_frame,
                text=f"{card.topic}\n{badge}",
                anchor="w",
                command=lambda c=card: _select_card(c),
            ).pack(fill="x", pady=2)
        _select_card(grouped[0][0] if grouped else None)

    def _render_error() -> None:
        if not win.winfo_exists():
            return
        stamp_label.configure(text="No disponible")
        _clear(list_frame)
        ctk.CTkLabel(list_frame, text="No disponible", text_color="#e06c6c").pack(padx=8, pady=8)
        _render_detail(None)

    def _load_worker() -> None:
        try:
            cards = card_store.list_all()
        except Exception:
            logger.exception("Fallo leyendo Tarjetas editoriales")
            schedule_ui_update(_render_error)
            return
        schedule_ui_update(lambda: _render_cards(cards))

    def _refresh() -> None:
        threading.Thread(target=_load_worker, daemon=True).start()

    ctk.CTkButton(win, text="Actualizar", command=_refresh, width=100).pack(padx=16, pady=(0, 12), anchor="w")

    def _on_close() -> None:
        ref_setter(None)
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", _on_close)
    win.bind("<Destroy>", lambda e: ref_setter(None) if e.widget is win else None)

    _refresh()
    show_toplevel(win, parent, modal=False)

    return win
