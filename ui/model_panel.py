"""ModelPanel — encapsulates model selection, Ollama status, and download UI.

Manages the model combobox, Ollama action button, model info label, and
download progress bar inside the "Modelo/Perfil" config tab.  Subscribes
to UIState observer for automatic updates when ``ollama_state`` changes,
and exposes explicit methods for model selection and Ollama actions.
"""

from __future__ import annotations

import shutil
import webbrowser
from typing import Any, Callable, Optional

import customtkinter as ctk
import tkinter.messagebox as messagebox

from config.settings import DEFAULT_MODEL, MODELS_CATALOG
from ui.state import UIState
from ui.protocols import CallbackDispatcher


# ---------------------------------------------------------------------------
# Ollama state → button text + state
# ---------------------------------------------------------------------------

_OLLAMA_BUTTON_CONFIG: dict[str, dict[str, Any]] = {
    "checking": {"text": "Revisando Ollama...", "state": "disabled"},
    "package_missing": {"text": "Instalar dependencia Python", "state": "normal"},
    "app_missing": {"text": "Instalar Ollama", "state": "normal"},
    "service_stopped": {"text": "Iniciar Ollama", "state": "normal"},
}


class ModelPanel:
    """Manages model selection UI, Ollama status detection, and model download.

    Builds the model combobox, Ollama action button, model info label, and
    download progress bar.  Subscribes to a :class:`UIState` observer so
    that changes to ``ollama_state`` automatically update the button.

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
        schedule_ui_update: Optional[Callable[[Callable[[], None]], None]] = None,
    ) -> None:
        self._parent = parent_frame
        self._ui_state = ui_state
        self._dispatcher = dispatcher
        self._on_log = on_log
        self._schedule_ui_update = schedule_ui_update or (lambda fn: fn())

        # Model catalog mappings
        self._model_display_to_tag: dict[str, str] = {}
        self._model_tag_to_display: dict[str, str] = {}
        self._model_display_list: list[str] = []
        self._build_model_catalog()

        # Widget references
        self.lbl_model_header: Optional[ctk.CTkLabel] = None
        self.combo_modelos: Optional[ctk.CTkOptionMenu] = None
        self.btn_download: Optional[ctk.CTkButton] = None
        self.lbl_modelo_info: Optional[ctk.CTkLabel] = None
        self.progress_download: Optional[ctk.CTkProgressBar] = None

        # Observer
        self._observer_id: Optional[int] = None

        # Ollama starting flag
        self._ollama_starting: bool = False

    # ------------------------------------------------------------------
    # Model catalog
    # ------------------------------------------------------------------

    def _build_model_catalog(self) -> None:
        """Build display↔tag mappings from MODELS_CATALOG."""
        for tag, info in MODELS_CATALOG.items():
            display = info["display"]
            self._model_display_to_tag[display] = tag
            self._model_tag_to_display[tag] = display
            self._model_display_list.append(display)

    def get_display_for_tag(self, tag: str) -> str:
        """Return the display name for a model tag."""
        return self._model_tag_to_display.get(tag, tag)

    def get_tag_for_display(self, display: str) -> str:
        """Return the model tag for a display name."""
        return self._model_display_to_tag.get(display, display)

    @property
    def model_display_list(self) -> list[str]:
        """Return the list of display names for the combobox."""
        return list(self._model_display_list)

    @property
    def default_display(self) -> str:
        """Return the default model display name."""
        return self.get_display_for_tag(DEFAULT_MODEL)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def build(self) -> None:
        """Create and layout all model panel widgets within parent_frame.

        Must be called once after the parent frame exists.
        """
        self.lbl_model_header = ctk.CTkLabel(
            self._parent,
            text="Modelo",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        )
        self.lbl_model_header.pack(fill="x", padx=10, pady=(10, 4))

        self.combo_modelos = ctk.CTkOptionMenu(
            self._parent,
            values=self._model_display_list,
            command=self._on_model_changed,
            width=300,
        )
        self.combo_modelos.set(self.default_display)
        self.combo_modelos.pack(fill="x", padx=10, pady=4)

        self.btn_download = ctk.CTkButton(
            self._parent,
            text="Revisando Ollama...",
            command=self._on_download_model,
            width=110,
            fg_color="#555555",
            hover_color="#666666",
        )
        self.btn_download.pack(fill="x", padx=10, pady=4)

        self.lbl_modelo_info = ctk.CTkLabel(
            self._parent,
            text="",
            font=ctk.CTkFont(size=11),
            text_color="#aaaaaa",
            anchor="w",
            justify="left",
            wraplength=300,
        )
        self.lbl_modelo_info.pack(fill="x", padx=10, pady=4)

        self.progress_download = ctk.CTkProgressBar(self._parent, width=150)
        self.progress_download.pack(fill="x", padx=10, pady=(4, 10))
        self.progress_download.set(0)
        self.progress_download.pack_forget()

        # Subscribe to UIState observer for automatic button updates
        self._observer_id = self._ui_state.subscribe(self._on_state_change)

        # Update info for default model
        self.update_model_info(DEFAULT_MODEL)
        self._update_button_for_ollama_state()

    # ------------------------------------------------------------------
    # Public methods
    # ------------------------------------------------------------------

    def update_model_info(self, model_tag: str) -> None:
        """Update the model info label with description, size, and install status."""
        if self.lbl_modelo_info is None:
            return
        info = MODELS_CATALOG.get(model_tag, {})
        desc = info.get("desc", "Modelo personalizado")
        size = info.get("size_gb", "?")
        installed = "\u2705" if self._modelo_instalado(model_tag) else "\u274c No instalado"
        self.lbl_modelo_info.configure(text=f"{desc} ({size}GB) {installed}")

    def update_button_for_ollama_state(self, model_tag: Optional[str] = None) -> None:
        """Public wrapper to update the button based on Ollama state."""
        self._update_button_for_ollama_state(model_tag)

    def refresh_ollama_state(self, on_check_ollama: Optional[Callable[[], None]] = None) -> None:
        """Detect current Ollama state and update UI.

        Args:
            on_check_ollama: Optional callback to trigger an Ollama check
                when the state is ``ready``.
        """
        state = self._detectar_estado_ollama()
        self._ui_state.ollama_state = state
        self._update_button_for_ollama_state()

        if state == "ready" and on_check_ollama is not None:
            on_check_ollama()

    def set_model_selection(self, display_name: str) -> None:
        """Set the model combobox to a specific display name."""
        if self.combo_modelos is not None:
            self.combo_modelos.set(display_name)

    def get_selected_display(self) -> str:
        """Return the currently selected model display name."""
        if self.combo_modelos is not None:
            return self.combo_modelos.get()
        return self.default_display

    def get_selected_tag(self) -> str:
        """Return the model tag for the currently selected display."""
        return self.get_tag_for_display(self.get_selected_display())

    def set_download_progress_visible(self, visible: bool) -> None:
        """Show or hide the download progress bar."""
        if self.progress_download is None:
            return
        if visible:
            self.progress_download.pack(fill="x", padx=10, pady=(4, 10))
        else:
            self.progress_download.pack_forget()

    def set_download_progress(self, value: float) -> None:
        """Set the download progress bar value (0.0 to 1.0)."""
        if self.progress_download is not None:
            self.progress_download.set(value)

    def set_model_combo_state(self, state: str) -> None:
        """Enable or disable the model combobox."""
        if self.combo_modelos is not None:
            self.combo_modelos.configure(state=state)

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def _on_model_changed(self, display_name: str) -> None:
        """Handle model combobox selection change."""
        tag = self.get_tag_for_display(display_name)
        self.update_model_info(tag)
        self._update_button_for_ollama_state(tag)

        ollama_state = self._ui_state.ollama_state
        if ollama_state == "ready" and self._modelo_instalado(tag):
            self._dispatcher.dispatch("on_switch_model", tag)
            self._on_log(f"[Sistema] Cambiando a modelo: {tag}")
        elif ollama_state == "ready":
            self._on_log(
                f"[Sistema] Modelo '{tag}' no instalado. "
                "Usa el boton de Ollama/modelo para obtenerlo."
            )
        else:
            self._on_log(
                "[Sistema] Ollama no esta listo. "
                "Usa el boton de Ollama/modelo para prepararlo."
            )

    def _on_download_model(self) -> None:
        """Handle Ollama/model button click."""
        self.refresh_ollama_state()

        state = self._ui_state.ollama_state

        if state == "app_missing":
            webbrowser.open("https://ollama.com/download")
            self._on_log("[Sistema] Abriendo pagina de descarga de Ollama.")
            return

        if state == "package_missing":
            messagebox.showwarning(
                "Dependencia faltante",
                "Falta el paquete Python 'ollama' en este entorno. "
                "Instala las dependencias del proyecto y vuelve a abrir la app.",
            )
            return

        if state == "service_stopped":
            self._iniciar_ollama()
            return

        self._descargar_o_activar_modelo()

    # ------------------------------------------------------------------
    # Ollama state detection
    # ------------------------------------------------------------------

    def _detectar_estado_ollama(self) -> str:
        """Detect the current Ollama state.

        Returns one of: ``checking``, ``ready``, ``package_missing``,
        ``app_missing``, ``service_stopped``.
        """
        try:
            import ollama  # noqa: F401
        except ImportError:
            return "package_missing"

        try:
            import requests

            requests.get("http://127.0.0.1:11434/api/tags", timeout=1.0).raise_for_status()
            return "ready"
        except Exception:
            pass

        return "service_stopped" if self._find_ollama_executable() else "app_missing"

    def _find_ollama_executable(self) -> Optional[str]:
        """Find the Ollama executable on the system."""
        ollama_exe = shutil.which("ollama")
        if ollama_exe:
            return ollama_exe

        candidates = []
        local_appdata = __import__("os").environ.get("LOCALAPPDATA")
        if local_appdata:
            candidates.append(
                __import__("os").path.join(local_appdata, "Programs", "Ollama", "ollama.exe")
            )

        program_files = __import__("os").environ.get("ProgramFiles")
        if program_files:
            candidates.append(
                __import__("os").path.join(program_files, "Ollama", "ollama.exe")
            )

        for candidate in candidates:
            if __import__("os").path.exists(candidate):
                return candidate

        return None

    def _modelo_instalado(self, model_tag: str) -> bool:
        """Check if an Ollama model is installed."""
        try:
            import ollama

            for mod in ollama.list().models:
                if mod.model == model_tag or mod.model == f"{model_tag}:latest":
                    return True
                if ":" not in model_tag and mod.model.startswith(model_tag + ":"):
                    return True
            return False
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Button update logic
    # ------------------------------------------------------------------

    def _update_button_for_ollama_state(self, model_tag: Optional[str] = None) -> None:
        """Update the download/ollama button text and state."""
        if self.btn_download is None:
            return

        if model_tag is None and self.combo_modelos is not None:
            display_name = self.combo_modelos.get()
            model_tag = self.get_tag_for_display(display_name)

        if self._ollama_starting:
            self.btn_download.configure(state="disabled", text="Iniciando Ollama...")
            return

        state = self._ui_state.ollama_state

        if state in _OLLAMA_BUTTON_CONFIG:
            config = _OLLAMA_BUTTON_CONFIG[state]
            self.btn_download.configure(state=config["state"], text=config["text"])
        elif model_tag and self._modelo_instalado(model_tag):
            self.btn_download.configure(state="normal", text="Activar modelo")
        else:
            self.btn_download.configure(state="normal", text="Descargar modelo")

    # ------------------------------------------------------------------
    # Ollama start
    # ------------------------------------------------------------------

    def _iniciar_ollama(self) -> None:
        """Start the Ollama service in a background thread."""
        if self._ollama_starting:
            return

        ollama_exe = self._find_ollama_executable()
        if not ollama_exe:
            self._ui_state.ollama_state = "app_missing"
            self._update_button_for_ollama_state()
            return

        self._ollama_starting = True
        self._update_button_for_ollama_state()
        self._on_log("[Sistema] Iniciando Ollama...")

        import os
        import subprocess
        import threading
        import time

        def worker() -> None:
            try:
                creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
                subprocess.Popen(
                    [ollama_exe, "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=creationflags,
                )
            except Exception as e:
                self._on_log(f"[Sistema] No se pudo iniciar Ollama: {e}")
                self._ollama_starting = False
                self._schedule_ui_update(lambda: self.refresh_ollama_state())
                return

            for _ in range(20):
                time.sleep(0.5)
                if self._detectar_estado_ollama() == "ready":
                    self._ollama_starting = False
                    self._on_log("[Sistema] Ollama iniciado correctamente.")
                    self._schedule_ui_update(lambda: self.refresh_ollama_state())
                    return

            self._ollama_starting = False
            self._on_log(
                "[Sistema] Ollama no respondio despues de iniciar. "
                "Revisa la instalacion."
            )
            self._schedule_ui_update(lambda: self.refresh_ollama_state())

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------------
    # Model download / activation
    # ------------------------------------------------------------------

    def _descargar_o_activar_modelo(self) -> None:
        """Download or activate the currently selected model."""
        tag = self.get_selected_tag()

        if self._ui_state.ollama_state != "ready":
            self._on_log("[Sistema] Ollama no esta listo para gestionar modelos.")
            return

        if self._modelo_instalado(tag):
            self._dispatcher.dispatch("on_switch_model", tag)
            self._on_log(f"[Sistema] '{tag}' ya está instalado. Activado.")
            self._update_button_for_ollama_state(tag)
            return

        info = MODELS_CATALOG.get(tag, {})
        size = info.get("size_gb", "?")
        confirmar = messagebox.askyesno(
            "Descargar Modelo",
            f"Descargar '{tag}'?\n\n"
            f"Tamaño aprox: {size} GB\n"
            f"{info.get('desc', '')}\n\n"
            "Esto puede tardar varios minutos.",
        )
        if confirmar:
            self._dispatcher.dispatch("on_download_model", tag)

    # ------------------------------------------------------------------
    # UIState observer
    # ------------------------------------------------------------------

    def _on_state_change(self, key: str, value: Any) -> None:
        """Handle UIState changes that affect the model panel."""
        if key == "ollama_state":
            self._schedule_ui_update(lambda: self._update_button_for_ollama_state())

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
