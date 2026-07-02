"""Kira memory inspector — read-only "Memoria de Kira" window.

Product UI for streamer + operator (Configuración > Perfil), NOT a debug
tool. Mixed-lifetime honesty is the whole point of this window (Judge B
CRITICAL fix): the session conversation and the background-memory digest
live only in RAM and vanish on app close / profile switch, while the saved
agenda persists on disk. The header, lifetime badges, and agenda provenance
note below are privacy-critical copy — asserted verbatim by tests, do not
paraphrase them.

Window pattern is EXACTLY gear_popover.py's: duplicate-open guard ->
CTkToplevel -> apply_app_icon -> show_toplevel(modal=False) -> <Destroy>
ref-reset bind (same as inspector_cards.py).

Data sources:
  - Conversación de la sesión / Memoria de fondo: MotorVocalIA.memory_inspector_snapshot(),
    read on a worker thread and marshaled back via the injected
    schedule_ui_update callable — the worker never touches any Tk widget.
    Any exception fails open ("No disponible" + log).
  - Agenda guardada: the LIVE KiraAgendaController instance (kira_agenda),
    filtered to the statuses AgendaPersistence actually writes to disk
    (approved/queued/active) — read directly, no disk access here.
"""
from __future__ import annotations

import logging
import threading
import time
import tkinter.messagebox as mb
from typing import Any, Callable

import customtkinter as ctk

from opencohost.smart_aggregator.kira_agenda_controller import TopicStatus
from opencohost.ui.window_utils import apply_app_icon, raise_window, show_toplevel

logger = logging.getLogger(__name__)

HEADER_TEXT = (
    "La conversación y la memoria de fondo viven solo en RAM: se borran al "
    "cerrar la app o cambiar de perfil. La agenda guardada sí persiste en "
    "disco entre sesiones. El chat de viewers nunca se muestra."
)
BADGE_CONVERSACION = "«Solo en RAM»"
BADGE_MEMORIA_FONDO = "«Solo en RAM»"
BADGE_AGENDA_GUARDADA = "«En disco · persiste entre sesiones»"
AGENDA_PROVENANCE_NOTE = (
    "Algunos temas aprobados pueden nombrar términos que mencionó el chat: "
    "vos los aprobaste desde Sugerencias de Kira."
)

# Slice 6 (ui-core, R10) — "Memorias guardadas" management section.
# MEMORIAS_ENABLED stays False through slice 7 (design v2.1 §10), so a real
# run has nothing captured and this section renders empty — it does not
# contradict the RAM-only HEADER_TEXT above, which slice 8 rewrites once the
# flag flips. BADGE_MEMORIAS is placeholder copy until slice 8 updates it.
BADGE_MEMORIAS = "«En preparación»"
MEMORIAS_EMPTY_TEXT = "Sin memorias guardadas"
# Divergence from format_conversation_entry's ptt masking, by design: a ptt
# entry only ever reaches memorias.db after passing the R1 provenance gate
# (source in {direct, ptt}, never chat) — it is the streamer's own recorded
# speech, not viewer chat, so unlike the live conversation section there is
# no privacy reason to mask it here.
MEMORIAS_PTT_NOTE = (
    "Algunas memorias pueden venir de indicaciones dichas al aire por vos (PTT): "
    "es tu propia voz, ya filtrada por procedencia, nunca chat de viewers."
)
FREEZE_RULE_TEXT = (
    "Editar, fijar o marcar como privada una memoria la congela: Kira no la reescribirá."
)
MEMORIAS_WRITE_FAILED_TEXT = "No se pudo guardar el cambio (memoria bloqueada). Probá de nuevo."

# Statuses AgendaPersistence.save_if_changed actually writes to disk
# (opencohost/core/agenda_persistence.py _PERSISTED_STATUSES) — mirrored
# here since this module only reads the live controller, never the DB.
_SAVED_AGENDA_STATUSES = frozenset({TopicStatus.APPROVED, TopicStatus.QUEUED, TopicStatus.ACTIVE})


def format_memory_launcher_label(turn_count: int | None) -> str:
    """Return the "Memoria de Kira (N turnos)" launcher button text.

    Fail-open: turn_count=None (unknown, e.g. before the engine exists yet)
    omits the parenthetical instead of showing a misleading "(0)".
    """
    if turn_count is None:
        return "Memoria de Kira"
    return f"Memoria de Kira ({turn_count} turnos)"


def format_conversation_entry(entry: dict) -> str:
    """Render one historial entry for the "Conversación de la sesión" list.

    Entries without a 'content' key (the privacy gate in
    memory_inspector_snapshot) render as a masked metadata row instead of
    being silently dropped, so the turn sequence stays honest.
    """
    speaker = "Vos" if entry.get("role") == "user" else "Kira"
    if "content" in entry:
        return f"{speaker}: {entry['content']}"
    if entry.get("role") == "user" and entry.get("source") == "ptt":
        return "[turno de voz / indicación al aire]"
    # Masked/uncredited row (e.g. chat-derived): do not attribute to "Vos" —
    # the content may have originated from a viewer, not the operator.
    return f"[turno oculto — {entry.get('content_chars', 0)} caracteres]"


def format_background_memory(digest: dict) -> str:
    """Render the Memoria de fondo stats-only line (never digest line text)."""
    return f"{digest['line_count']} línea(s) · {digest['total_chars']}/{digest['max_chars']} caracteres"


def format_memoria_row_metadata(row: Any) -> str:
    """Render a memoria row's "created_at · updated_at · rev N" metadata line."""
    return f"{row['created_at']} · {row['updated_at']} · rev {row['revision']}"


def format_saved_agenda_topics(topics: list) -> list[str]:
    """Return one display line per topic AgendaPersistence would save."""
    return [
        f"{topic.title} · prioridad {topic.priority} · {topic.status.value}"
        for topic in topics
        if topic.status in _SAVED_AGENDA_STATUSES
    ]


def open_inspector_memory(
    parent: Any,
    *,
    ref_getter: Callable[[], Any],
    ref_setter: Callable[[Any], None],
    motor_ia: Any,
    kira_agenda: Any,
    schedule_ui_update: Callable[[Callable[[], None]], None],
    memoria_store: Any = None,
    profile_id_getter: Callable[[], str | None] | None = None,
) -> Any:
    """Open (or focus) the "Memoria de Kira" inspector (read-only sections
    plus the slice-6 "Memorias guardadas" management section).

    Guard: if a window is already open, focus it instead of opening a
    second one (see gear_popover.open_gear_popover). Returns the new
    CTkToplevel, or None if an existing one was focused instead.

    memoria_store/profile_id_getter are optional (default None) — the
    production call site does not wire them yet this slice (MEMORIAS_ENABLED
    stays False through slice 7), so the section renders MEMORIAS_EMPTY_TEXT.
    Tests pass a real or mocked MemoriaStore to exercise the section.
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
    win.title("Memoria de Kira")
    win.geometry("640x560")
    win.configure(fg_color="#111820")

    ctk.CTkLabel(
        win,
        text="Memoria de Kira",
        font=ctk.CTkFont(size=15, weight="bold"),
        text_color="#d8e2ef",
    ).pack(pady=(14, 4), padx=16, anchor="w")
    ctk.CTkLabel(win, text=HEADER_TEXT, wraplength=560, justify="left", text_color="#8fa3b8").pack(padx=16, pady=(0, 8), anchor="w")

    body = ctk.CTkScrollableFrame(win, fg_color="transparent")
    body.pack(fill="both", expand=True, padx=16, pady=(0, 8))

    def _clear(frame: Any) -> None:
        for child in list(frame.winfo_children()):
            child.destroy()

    def _section(title: str, badge: str) -> Any:
        frame = ctk.CTkFrame(body, fg_color="#151d26", corner_radius=10)
        frame.pack(fill="x", pady=(0, 8))
        header_row = ctk.CTkFrame(frame, fg_color="transparent")
        header_row.pack(fill="x", padx=10, pady=(8, 2))
        ctk.CTkLabel(header_row, text=title, font=ctk.CTkFont(size=13, weight="bold")).pack(side="left")
        ctk.CTkLabel(header_row, text=badge, text_color="#6b7b8d").pack(side="left", padx=(8, 0))
        content = ctk.CTkFrame(frame, fg_color="transparent")
        content.pack(fill="x", padx=10, pady=(0, 8))
        return content

    conversation_content = _section("Conversación de la sesión", BADGE_CONVERSACION)
    background_content = _section("Memoria de fondo", BADGE_MEMORIA_FONDO)
    agenda_content = _section("Agenda guardada", BADGE_AGENDA_GUARDADA)
    ctk.CTkLabel(agenda_content, text=AGENDA_PROVENANCE_NOTE, wraplength=520, justify="left", text_color="#6b7b8d").pack(anchor="w", pady=(0, 4))
    # Dedicated child frame for topic rows, packed below the provenance note.
    # _render_agenda only clears/redraws THIS sub-frame, never agenda_content
    # itself, so repeated refreshes never wipe out the provenance note above.
    agenda_topics_content = ctk.CTkFrame(agenda_content, fg_color="transparent")
    agenda_topics_content.pack(fill="x")

    memorias_content = _section("Memorias guardadas", BADGE_MEMORIAS)
    ctk.CTkLabel(memorias_content, text=MEMORIAS_PTT_NOTE, wraplength=520, justify="left", text_color="#6b7b8d").pack(anchor="w", pady=(0, 2))
    ctk.CTkLabel(memorias_content, text=FREEZE_RULE_TEXT, wraplength=520, justify="left", text_color="#6b7b8d").pack(anchor="w", pady=(0, 4))
    memorias_error_label = ctk.CTkLabel(memorias_content, text="", text_color="#e06c6c")
    memorias_error_label.pack(anchor="w", pady=(0, 4))
    # Same idempotent-refresh pattern as agenda_topics_content: only THIS
    # sub-frame is cleared/redrawn on refresh, so the notes above survive.
    memorias_rows_content = ctk.CTkFrame(memorias_content, fg_color="transparent")
    memorias_rows_content.pack(fill="x")

    def _render_agenda() -> None:
        _clear(agenda_topics_content)
        try:
            lines = format_saved_agenda_topics(list(kira_agenda.topics))
        except Exception:
            logger.exception("Fallo leyendo la agenda guardada")
            lines = []
        if not lines:
            ctk.CTkLabel(agenda_topics_content, text="Sin temas guardados", text_color="#6b7b8d").pack(anchor="w")
            return
        for line in lines:
            ctk.CTkLabel(agenda_topics_content, text=line, wraplength=520, justify="left").pack(anchor="w")

    # ------------------------------------------------------------------
    # Slice 6 (ui-core, R10) — Memorias guardadas: load / render / mutate
    # ------------------------------------------------------------------

    def _load_memorias_rows() -> list:
        if memoria_store is None or profile_id_getter is None:
            return []
        try:
            profile_id = profile_id_getter()
        except Exception:
            return []
        if not profile_id:
            return []
        try:
            return list(memoria_store.list_for_profile(profile_id))
        except Exception:
            logger.exception("Fallo leyendo Memorias guardadas")
            return []

    def _show_memorias_error(message: str) -> None:
        if not win.winfo_exists():
            return
        memorias_error_label.configure(text=message)

    def _run_memoria_mutation(worker_fn: Callable[[], Any]) -> None:
        """Run *worker_fn* (a store write) off the Tk thread. On success,
        marshal a full _refresh() (same pattern as the Actualizar button).
        On failure (worker_fn returns False, or raises), surface
        MEMORIAS_WRITE_FAILED_TEXT instead of _refresh() — A-SF1
        (engram obs #2782): update_row/set_flags/delete_row return False on
        write contention and this UI must not silently claim success.
        """
        def _worker() -> None:
            try:
                ok = worker_fn()
            except Exception:
                logger.exception("Fallo mutando una memoria")
                schedule_ui_update(lambda: _show_memorias_error(MEMORIAS_WRITE_FAILED_TEXT))
                return
            if ok is False:
                schedule_ui_update(lambda: _show_memorias_error(MEMORIAS_WRITE_FAILED_TEXT))
                return
            schedule_ui_update(_refresh)

        threading.Thread(target=_worker, daemon=True).start()

    def _toggle_flag(row: Any, flag_name: str, new_value: bool) -> None:
        _run_memoria_mutation(lambda: memoria_store.set_flags(row["id"], **{flag_name: new_value}))

    def _confirm_delete(row: Any) -> None:
        if not mb.askyesno(
            "Eliminar memoria",
            f"¿Eliminar la memoria \"{row['title']}\"? Esta acción no se puede deshacer.",
            parent=win,
        ):
            return
        _run_memoria_mutation(lambda: memoria_store.delete_row(row["id"]))

    def _open_edit_modal(row: Any) -> None:
        modal = ctk.CTkToplevel(win)
        apply_app_icon(modal)
        modal.title("Editar memoria")
        modal.geometry("420x360")

        ctk.CTkLabel(modal, text="Título").pack(anchor="w", padx=12, pady=(12, 0))
        title_entry = ctk.CTkEntry(modal)
        title_entry.insert(0, row["title"])
        title_entry.pack(fill="x", padx=12)

        ctk.CTkLabel(modal, text="Contenido").pack(anchor="w", padx=12, pady=(8, 0))
        content_box = ctk.CTkTextbox(modal, height=140)
        content_box.insert("1.0", row["content"])
        content_box.pack(fill="both", expand=True, padx=12)

        ctk.CTkLabel(
            modal, text=FREEZE_RULE_TEXT, wraplength=380, justify="left", text_color="#6b7b8d",
        ).pack(padx=12, pady=(8, 0), anchor="w")

        def _save() -> None:
            new_title = title_entry.get().strip()
            new_content = content_box.get("1.0", "end").strip()
            _run_memoria_mutation(
                lambda: memoria_store.update_row(row["id"], title=new_title, content=new_content)
            )
            try:
                modal.destroy()
            except Exception:
                pass

        button_row = ctk.CTkFrame(modal, fg_color="transparent")
        button_row.pack(fill="x", padx=12, pady=12)
        ctk.CTkButton(button_row, text="Guardar", width=90, command=_save).pack(side="left", padx=(0, 8))
        ctk.CTkButton(button_row, text="Cancelar", width=90, command=modal.destroy).pack(side="left")

        show_toplevel(modal, win, modal=True)

    def _render_memoria_row(row: Any) -> None:
        row_frame = ctk.CTkFrame(memorias_rows_content, fg_color="#101820", corner_radius=8)
        row_frame.pack(fill="x", pady=(0, 6))
        ctk.CTkLabel(
            row_frame, text=row["title"] or "(sin título)", font=ctk.CTkFont(size=12, weight="bold"),
        ).pack(anchor="w", padx=8, pady=(6, 0))
        ctk.CTkLabel(row_frame, text=format_memoria_row_metadata(row), text_color="#6b7b8d").pack(anchor="w", padx=8)
        ctk.CTkLabel(row_frame, text=row["content"], wraplength=500, justify="left").pack(anchor="w", padx=8, pady=(2, 4))

        flags = [
            label
            for flag_key, label in (("pinned", "Fijada"), ("private", "Privada"), ("inactive", "Inactiva"))
            if row[flag_key]
        ]
        if flags:
            ctk.CTkLabel(row_frame, text=" · ".join(flags), text_color="#8fa3b8").pack(anchor="w", padx=8)

        actions_row = ctk.CTkFrame(row_frame, fg_color="transparent")
        actions_row.pack(fill="x", padx=8, pady=(0, 6))
        ctk.CTkButton(actions_row, text="Editar", width=70, command=lambda r=row: _open_edit_modal(r)).pack(side="left", padx=(0, 4))
        ctk.CTkButton(
            actions_row,
            text="Desfijar" if row["pinned"] else "Fijar",
            width=70,
            command=lambda r=row: _toggle_flag(r, "pinned", not r["pinned"]),
        ).pack(side="left", padx=(0, 4))
        ctk.CTkButton(
            actions_row,
            text="Quitar privada" if row["private"] else "Marcar privada",
            width=110,
            command=lambda r=row: _toggle_flag(r, "private", not r["private"]),
        ).pack(side="left", padx=(0, 4))
        ctk.CTkButton(
            actions_row,
            text="Reactivar" if row["inactive"] else "Desactivar",
            width=80,
            command=lambda r=row: _toggle_flag(r, "inactive", not r["inactive"]),
        ).pack(side="left", padx=(0, 4))
        ctk.CTkButton(actions_row, text="Eliminar", width=70, command=lambda r=row: _confirm_delete(r)).pack(side="left")

    def _render_memorias(memorias_rows: list) -> None:
        if not win.winfo_exists():
            return
        memorias_error_label.configure(text="")
        _clear(memorias_rows_content)
        if not memorias_rows:
            ctk.CTkLabel(memorias_rows_content, text=MEMORIAS_EMPTY_TEXT, text_color="#6b7b8d").pack(anchor="w")
            return
        for row in memorias_rows:
            _render_memoria_row(row)

    def _render_snapshot(snapshot: dict, memorias_rows: list) -> None:
        if not win.winfo_exists():
            return
        stamp_label.configure(text="Actualizado " + time.strftime("%H:%M:%S"))
        _clear(conversation_content)
        entries = snapshot["entries"]
        if not entries:
            ctk.CTkLabel(conversation_content, text="Sin turnos todavía", text_color="#6b7b8d").pack(anchor="w")
        for entry in entries:
            ctk.CTkLabel(conversation_content, text=format_conversation_entry(entry), wraplength=520, justify="left").pack(anchor="w", pady=(0, 2))

        _clear(background_content)
        ctk.CTkLabel(background_content, text=format_background_memory(snapshot["digest"])).pack(anchor="w")

        _render_agenda()
        _render_memorias(memorias_rows)

    def _render_error(memorias_rows: list) -> None:
        if not win.winfo_exists():
            return
        stamp_label.configure(text="No disponible")
        _clear(conversation_content)
        ctk.CTkLabel(conversation_content, text="No disponible", text_color="#e06c6c").pack(anchor="w")
        _clear(background_content)
        ctk.CTkLabel(background_content, text="No disponible", text_color="#e06c6c").pack(anchor="w")
        _render_agenda()
        _render_memorias(memorias_rows)

    def _load_worker() -> None:
        memorias_rows = _load_memorias_rows()
        try:
            snapshot = motor_ia.memory_inspector_snapshot()
        except Exception:
            logger.exception("Fallo leyendo Memoria de Kira")
            schedule_ui_update(lambda: _render_error(memorias_rows))
            return
        schedule_ui_update(lambda: _render_snapshot(snapshot, memorias_rows))

    def _refresh() -> None:
        threading.Thread(target=_load_worker, daemon=True).start()

    footer_row = ctk.CTkFrame(win, fg_color="transparent")
    footer_row.pack(fill="x", padx=16, pady=(0, 12))
    ctk.CTkButton(footer_row, text="Actualizar", command=_refresh, width=100).pack(side="left")
    stamp_label = ctk.CTkLabel(footer_row, text="", font=ctk.CTkFont(size=10), text_color="#6b7b8d")
    stamp_label.pack(side="left", padx=(8, 0))

    def _on_close() -> None:
        ref_setter(None)
        win.destroy()

    win.protocol("WM_DELETE_WINDOW", _on_close)
    win.bind("<Destroy>", lambda e: ref_setter(None) if e.widget is win else None)

    _refresh()
    show_toplevel(win, parent, modal=False)

    return win
