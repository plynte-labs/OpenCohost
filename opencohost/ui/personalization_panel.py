"""PersonalizationPanel — encapsulates the "Personalización..." launcher
button and its CTkToplevel edit form.

Design: sdd/kira-personalization-onboarding-20260705 (design.md §5 CTK, §7).

Structurally mirrors opencohost/ui/profile_panel.py's build/cleanup
lifecycle and constructor-injected-callback style, but talks to
opencohost.core.personalization directly — no engine access, no
CallbackDispatcher. Unlike a profile switch, personalization has nothing
to dispatch to the running engine: llm_engine.py reads the store itself
each turn via its own mtime cache (opencohost/core/personalization.py),
so Save/Clear here only need to write the file.
"""

from __future__ import annotations

import tkinter.messagebox as mb
from typing import Any, Callable, Optional

import customtkinter as ctk

from opencohost.config.settings import (
    PERSONALIZATION_INSTRUCTIONS_MAX,
    PERSONALIZATION_INTERESTS_MAX,
    PERSONALIZATION_NICKNAME_MAX,
    PERSONALIZATION_OCCUPATION_MAX,
)
from opencohost.core.personalization import (
    clear_personalization,
    load_personalization,
    save_personalization,
)
from opencohost.ui.window_utils import apply_app_icon


class PersonalizationPanel:
    """Manages the "Personalización..." button and its edit window.

    Call :meth:`build` once after the parent frame exists. Call
    :meth:`cleanup` before the parent window is destroyed.
    """

    def __init__(
        self,
        parent_frame: ctk.CTkFrame,
        on_log: Callable[[str], None],
    ) -> None:
        self._parent = parent_frame
        self._on_log = on_log

        self.btn_personalizacion: Optional[ctk.CTkButton] = None
        # Keeps the window alive/referenced while open; mirrors
        # ProfilePanel._active_configurador_window.
        self._active_window: Optional["_PersonalizationWindow"] = None

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def build(self) -> None:
        """Create the launcher button within parent_frame."""
        self.btn_personalizacion = ctk.CTkButton(
            self._parent,
            text="\U0001f464 Personalización...",
            command=self._on_open,
            width=130,
            fg_color="#555555",
            hover_color="#666666",
        )
        self.btn_personalizacion.pack(fill="x", padx=10, pady=(0, 10))

    def cleanup(self) -> None:
        """No observer subscriptions to release — kept for lifecycle parity
        with sibling panels (ProfilePanel, ModelPanel)."""
        return None

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_open(self) -> None:
        window = _PersonalizationWindow(self._parent, self._on_log)
        self._active_window = window
        window.grab_set()


def mount(owner: Any, parent_frame: ctk.CTkFrame, on_log: Callable[[str], None]) -> None:
    """Build the panel into `parent_frame` and attach it to `owner` as
    `owner._personalization_panel`.

    Single call-site helper — takes an explicit `on_log` rather than reaching
    into `owner` for it, so any owner (VocalAIApp, ProfilePanel, ...) can
    mount this sibling card without extra coupling
    (kira_personalization_onboarding_20260705; app_shell.py has a hard
    line-count budget — see tests/test_integration.py — so this card is
    mounted from ProfilePanel, the owner of the shared parent frame).
    """
    owner._personalization_panel = PersonalizationPanel(parent_frame=parent_frame, on_log=on_log)
    owner._personalization_panel.build()


def unmount(owner: Any) -> None:
    """Clean up the panel mounted via `mount`, if any. Mirrors the
    hasattr-guarded cleanup pattern used by sibling panel cleanup() methods."""
    if hasattr(owner, "_personalization_panel"):
        owner._personalization_panel.cleanup()


class _PersonalizationWindow(ctk.CTkToplevel):
    """CTkToplevel form for the global streamer personalization store.

    Reads/writes opencohost.core.personalization directly (no dispatcher).
    """

    def __init__(self, parent: ctk.CTkFrame, on_log: Callable[[str], None]) -> None:
        super().__init__(parent)
        apply_app_icon(self)
        self.title("Personalización")
        self.geometry("480x560")
        self.configure(fg_color="#0b0f14")
        self._on_log = on_log

        self.grid_columnconfigure(0, weight=1)

        self.check_enabled = ctk.CTkCheckBox(
            self, text="Habilitar personalización", fg_color="#2f5f8f", hover_color="#3670aa"
        )
        self.check_enabled.grid(row=0, column=0, sticky="w", padx=10, pady=(14, 10))

        ctk.CTkLabel(
            self, text=f"Apodo (máx. {PERSONALIZATION_NICKNAME_MAX})", anchor="w"
        ).grid(row=1, column=0, sticky="ew", padx=10)
        self.entry_nickname = ctk.CTkEntry(self)
        self.entry_nickname.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 8))

        ctk.CTkLabel(
            self, text=f"Ocupación (máx. {PERSONALIZATION_OCCUPATION_MAX})", anchor="w"
        ).grid(row=3, column=0, sticky="ew", padx=10)
        self.entry_occupation = ctk.CTkEntry(self)
        self.entry_occupation.grid(row=4, column=0, sticky="ew", padx=10, pady=(0, 8))

        ctk.CTkLabel(
            self, text=f"Intereses (máx. {PERSONALIZATION_INTERESTS_MAX})", anchor="w"
        ).grid(row=5, column=0, sticky="ew", padx=10)
        self.entry_interests = ctk.CTkEntry(self)
        self.entry_interests.grid(row=6, column=0, sticky="ew", padx=10, pady=(0, 8))

        ctk.CTkLabel(
            self,
            text=f"Instrucciones personalizadas (máx. {PERSONALIZATION_INSTRUCTIONS_MAX})",
            anchor="w",
        ).grid(row=7, column=0, sticky="ew", padx=10)
        self.textbox_instructions = ctk.CTkTextbox(
            self, height=140, font=ctk.CTkFont(family="Consolas", size=12)
        )
        self.textbox_instructions.grid(row=8, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self.grid_rowconfigure(8, weight=1)

        frame_acciones = ctk.CTkFrame(self, fg_color="transparent")
        frame_acciones.grid(row=9, column=0, sticky="ew", padx=10, pady=(0, 10))

        self.btn_clear = ctk.CTkButton(
            frame_acciones,
            text="\U0001f5d1️ Limpiar",
            command=self._on_clear,
            fg_color="darkred",
            hover_color="#8B0000",
        )
        self.btn_clear.pack(side="left", padx=5)

        self.btn_guardar = ctk.CTkButton(
            frame_acciones,
            text="\U0001f4be Guardar",
            command=self._on_save,
            fg_color="#2f5f8f",
            hover_color="#3670aa",
        )
        self.btn_guardar.pack(side="right", padx=5)

        self._load_into_form()

    # ------------------------------------------------------------------
    # Form <-> store
    # ------------------------------------------------------------------

    def _load_into_form(self) -> None:
        data = load_personalization()

        if data.get("enabled", True):
            self.check_enabled.select()
        else:
            self.check_enabled.deselect()

        self.entry_nickname.delete(0, "end")
        self.entry_nickname.insert(0, data.get("nickname", ""))

        self.entry_occupation.delete(0, "end")
        self.entry_occupation.insert(0, data.get("occupation", ""))

        self.entry_interests.delete(0, "end")
        self.entry_interests.insert(0, data.get("interests", ""))

        self.textbox_instructions.delete("1.0", "end")
        self.textbox_instructions.insert("1.0", data.get("custom_instructions", ""))

    def _on_save(self) -> None:
        # ponytail: save_personalization() already clamps every field to the
        # same PERSONALIZATION_*_MAX constants (opencohost/core/personalization.py
        # _clamp) — the slice here just keeps the round-tripped values from
        # visually overflowing the max before the next reload.
        data = {
            "enabled": bool(self.check_enabled.get()),
            "nickname": self.entry_nickname.get().strip()[:PERSONALIZATION_NICKNAME_MAX],
            "occupation": self.entry_occupation.get().strip()[:PERSONALIZATION_OCCUPATION_MAX],
            "interests": self.entry_interests.get().strip()[:PERSONALIZATION_INTERESTS_MAX],
            "custom_instructions": self.textbox_instructions.get("1.0", "end").strip()[
                :PERSONALIZATION_INSTRUCTIONS_MAX
            ],
        }
        if save_personalization(data):
            self._load_into_form()
            self._on_log("[Sistema] \U0001f4be Personalización guardada.")
        else:
            mb.showerror("Error", "No se pudo guardar la personalización.", parent=self)

    def _on_clear(self) -> None:
        if not mb.askyesno(
            "Confirmar",
            "¿Borrar toda la personalización guardada (apodo, ocupación, intereses e "
            "instrucciones)? Esta acción no se puede deshacer.",
            parent=self,
        ):
            return
        if clear_personalization():
            self._load_into_form()
            self._on_log("[Sistema] \U0001f5d1️ Personalización borrada.")
        else:
            mb.showerror("Error", "No se pudo borrar la personalización.", parent=self)
