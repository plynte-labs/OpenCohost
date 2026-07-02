"""ProfilePanel — encapsulates profile selection and management UI.

Manages the profile combobox and edit button inside the "Modelo/Perfil"
config tab.  Provides methods for profile selection, editing via the
profile configurator window, and handling saved profile updates.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

import customtkinter as ctk

from opencohost.core.profiles import guardar_perfiles
from opencohost.ui.state import UIState
from opencohost.ui.protocols import CallbackDispatcher


class ProfilePanel:
    """Manages profile selection UI and profile editor integration.

    Builds the profile combobox and edit button.  Subscribes to a
    :class:`UIState` observer so that changes to ``current_profile``
    automatically update the combobox selection.

    Call :meth:`build` once after the parent frame exists.  Call
    :meth:`cleanup` before the parent window is destroyed to unsubscribe
    from the UIState observer.
    """

    def __init__(
        self,
        parent_frame: ctk.CTkFrame,
        ui_state: UIState,
        dispatcher: CallbackDispatcher,
        on_log: Callable[[str], None],
        configurador_class: Optional[type] = None,
        schedule_ui_update: Optional[Callable[[Callable[[], None]], None]] = None,
    ) -> None:
        self._parent = parent_frame
        self._ui_state = ui_state
        self._dispatcher = dispatcher
        self._on_log = on_log
        self._configurador_class = configurador_class
        self._schedule_ui_update = schedule_ui_update or (lambda fn: fn())

        # Profile data
        self._perfiles: dict[str, Any] = {}
        # (name, id) of the last profile explicitly deleted via the
        # configurator window; purge wiring lands in a later slice.
        self.last_deleted_profile: Optional[tuple[str, Optional[str]]] = None

        # Widget references
        self.lbl_profile_header: Optional[ctk.CTkLabel] = None
        self.combo_perfiles: Optional[ctk.CTkOptionMenu] = None
        self.btn_editar_perfiles: Optional[ctk.CTkButton] = None

        # Observer
        self._observer_id: Optional[int] = None

    # ------------------------------------------------------------------
    # Profile data management
    # ------------------------------------------------------------------

    def set_profiles(self, perfiles: dict[str, Any]) -> None:
        """Set the profile dictionary and update the combobox.

        Args:
            perfiles: Dict mapping profile names to profile data.
        """
        self._perfiles = perfiles
        if self.combo_perfiles is not None:
            nombres = list(self._perfiles.keys())
            self.combo_perfiles.configure(values=nombres)

    def get_profiles(self) -> dict[str, Any]:
        """Return the current profile dictionary."""
        return self._perfiles

    def get_default_profile_name(self) -> Optional[str]:
        """Return the default profile name, or first available."""
        if not self._perfiles:
            return None
        if "Kira (Default)" in self._perfiles:
            return "Kira (Default)"
        return list(self._perfiles.keys())[0]

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def build(self) -> None:
        """Create and layout all profile panel widgets within parent_frame.

        Must be called once after the parent frame exists.
        """
        self.lbl_profile_header = ctk.CTkLabel(
            self._parent,
            text="Perfil",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        )
        self.lbl_profile_header.pack(fill="x", padx=10, pady=(10, 4))

        self.combo_perfiles = ctk.CTkOptionMenu(
            self._parent,
            values=list(self._perfiles.keys()),
            command=self._on_profile_changed,
            width=300,
        )
        default_perfil = self.get_default_profile_name()
        if default_perfil is not None:
            self.combo_perfiles.set(default_perfil)
        self.combo_perfiles.pack(fill="x", padx=10, pady=4)

        self.btn_editar_perfiles = ctk.CTkButton(
            self._parent,
            text="\u270f\ufe0f Editar Perfiles",
            command=self._on_edit_profile,
            width=130,
            fg_color="#555555",
            hover_color="#666666",
        )
        self.btn_editar_perfiles.pack(fill="x", padx=10, pady=(4, 10))

        # Subscribe to UIState observer
        self._observer_id = self._ui_state.subscribe(self._on_state_change)

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def get_selected_profile(self) -> str:
        """Return the currently selected profile name."""
        if self.combo_perfiles is not None:
            return self.combo_perfiles.get()
        return ""

    def set_profile_selection(self, nombre: str) -> None:
        """Set the profile combobox to a specific profile name."""
        if self.combo_perfiles is not None:
            self.combo_perfiles.set(nombre)

    def apply_current_profile(self) -> None:
        """Apply the currently selected profile to the motor."""
        nombre = self.get_selected_profile()
        if nombre in self._perfiles:
            data = dict(self._perfiles[nombre])
            data["_profile_name"] = nombre
            self._dispatcher.dispatch("on_set_profile", data)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_profile_changed(self, nombre: str) -> None:
        """Handle profile combobox selection change."""
        if nombre in self._perfiles:
            data = dict(self._perfiles[nombre])
            data["_profile_name"] = nombre
            self._dispatcher.dispatch("on_set_profile", data)

    def _on_edit_profile(self) -> None:
        """Open the profile configurator window."""
        if self._configurador_class is None:
            self._on_log("[Sistema] Configurador de perfiles no disponible.")
            return

        ventana = self._configurador_class(
            self._parent,
            self._perfiles,
            self._on_perfiles_guardados,
        )
        ventana.grab_set()

    def _on_perfiles_guardados(
        self,
        nuevos_perfiles: dict[str, Any],
        deleted: Optional[tuple[str, Optional[str]]] = None,
    ) -> None:
        """Handle profiles saved from the configurator window.

        Args:
            nuevos_perfiles: Updated profile dictionary.
            deleted: (name, id) of a profile explicitly deleted in this save,
                if any. Purge wiring lands in a later slice — for now this is
                just recorded for future consumption.
        """
        self._perfiles = nuevos_perfiles
        guardar_perfiles(self._perfiles)
        self.last_deleted_profile = deleted

        nombres = list(self._perfiles.keys())
        if self.combo_perfiles is not None:
            self.combo_perfiles.configure(values=nombres)

            actual = self.combo_perfiles.get()
            if actual not in nombres:
                actual = nombres[0] if nombres else ""
                self.combo_perfiles.set(actual)

        if actual := self.get_selected_profile():
            self._on_profile_changed(actual)
        self._on_log("[Sistema] \U0001f4be Perfiles actualizados y guardados.")

    # ------------------------------------------------------------------
    # UIState observer
    # ------------------------------------------------------------------

    def _on_state_change(self, key: str, value: Any) -> None:
        """Handle UIState changes that affect the profile panel."""
        if key == "current_profile":
            self._schedule_ui_update(lambda: self.set_profile_selection(value))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """Unsubscribe from UIState observer.

        Call this before the parent window is destroyed to prevent
        stale callbacks.
        """
        if self._observer_id is not None:
            self._ui_state.unsubscribe(self._observer_id)
            self._observer_id = None
