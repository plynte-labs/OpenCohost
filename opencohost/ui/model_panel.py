"""ModelPanel — encapsulates model selection, Ollama status, and download UI.

Manages the model combobox, Ollama action button, model info label, and
download progress bar inside the "Modelo/Perfil" config tab.  Subscribes
to UIState observer for automatic updates when ``ollama_state`` changes,
and exposes explicit methods for model selection and Ollama actions.
"""

from __future__ import annotations

import shutil
import threading
import webbrowser
from typing import Any, Callable, Iterable, Optional

import customtkinter as ctk
import tkinter.messagebox as messagebox

from opencohost.config.settings import DEFAULT_MODEL, MODELS_CATALOG, resolve_llm_tiers, resolve_startup_model
from opencohost.core.ollama_startup import OllamaStartupManager
from opencohost.core.llm_tiers import LLM_TIER_LABELS, LLM_TIERS
from opencohost.ui.state import UIState
from opencohost.ui.protocols import CallbackDispatcher


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
        on_check_ollama: Optional[Callable[[], None]] = None,
    ) -> None:
        self._parent = parent_frame
        self._ui_state = ui_state
        self._dispatcher = dispatcher
        self._on_log = on_log
        self._schedule_ui_update = schedule_ui_update or (lambda fn: fn())
        self._on_check_ollama = on_check_ollama

        # Model catalog mappings
        self._model_display_to_tag: dict[str, str] = {}
        self._model_tag_to_display: dict[str, str] = {}
        self._model_display_list: list[str] = []
        self._build_model_catalog()
        self._model_refresh_inflight: bool = False

        # Widget references
        self.lbl_model_header: Optional[ctk.CTkLabel] = None
        self.combo_modelos: Optional[ctk.CTkOptionMenu] = None
        self.btn_download: Optional[ctk.CTkButton] = None
        self.lbl_modelo_info: Optional[ctk.CTkLabel] = None
        self.progress_download: Optional[ctk.CTkProgressBar] = None
        self.lbl_tier_header: Optional[ctk.CTkLabel] = None
        self.lbl_tier_info: Optional[ctk.CTkLabel] = None
        self._tier_buttons: dict[str, ctk.CTkButton] = {}

        # Observer
        self._observer_id: Optional[int] = None

        # Ollama starting flag
        self._ollama_starting: bool = False
        self._ollama_process: Optional[Any] = None
        
        # Active model tracking
        self._active_model_tag: Optional[str] = None
        self._active_llm_tier: str = "balanced"
        self._llm_tiers: dict[str, Optional[str]] = dict(resolve_llm_tiers())

    # ------------------------------------------------------------------
    # Model catalog
    # ------------------------------------------------------------------

    def _canonical_model_tag(self, tag: str) -> str:
        """Normalize discovered model tags for display and matching."""
        normalized = str(tag or "").strip()
        if normalized.endswith(":latest"):
            return normalized[:-7]
        return normalized

    def _build_model_catalog(
        self,
        installed_model_tags: Optional[Iterable[str]] = None,
    ) -> None:
        """Build display↔tag mappings from curated and discovered models."""
        self._model_display_to_tag.clear()
        self._model_tag_to_display.clear()
        self._model_display_list.clear()

        for tag, info in MODELS_CATALOG.items():
            display = info["display"]
            self._model_display_to_tag[display] = tag
            self._model_tag_to_display[tag] = display
            self._model_display_list.append(display)

        discovered = sorted(
            {
                self._canonical_model_tag(tag)
                for tag in (installed_model_tags or [])
                if self._canonical_model_tag(tag)
            }
        )
        for tag in discovered:
            if tag in self._model_tag_to_display:
                continue
            self._model_tag_to_display[tag] = tag
            self._model_display_to_tag[tag] = tag
            self._model_display_list.append(tag)

    def _extract_model_tag(self, model: Any) -> str:
        """Extract a model tag from Ollama discovery payloads."""
        if isinstance(model, dict):
            return str(model.get("model") or model.get("name") or "").strip()
        return str(
            getattr(model, "model", None) or getattr(model, "name", "") or ""
        ).strip()

    def _discover_installed_model_tags(self) -> set[str]:
        """Return canonical installed model tags from Ollama."""
        import ollama

        discovered: set[str] = set()
        response = ollama.list()
        models = getattr(response, "models", None)
        if models is None and isinstance(response, dict):
            models = response.get("models", [])
        for model in models or []:
            tag = self._canonical_model_tag(self._extract_model_tag(model))
            if tag:
                discovered.add(tag)
        return discovered

    def _apply_model_catalog(
        self,
        installed_model_tags: Optional[Iterable[str]] = None,
    ) -> None:
        """Apply curated + discovered catalog state to the visible widgets."""
        selected_tag = self.get_selected_tag()
        active_tag = self._active_model_tag or selected_tag or DEFAULT_MODEL
        self._build_model_catalog(installed_model_tags)
        self._llm_tiers = dict(resolve_llm_tiers(installed_model_tags))

        if self.combo_modelos is not None:
            self.combo_modelos.configure(values=self._model_display_list)
            next_tag = active_tag if active_tag in self._model_tag_to_display else DEFAULT_MODEL
            self.combo_modelos.set(self.get_display_for_tag(next_tag))

        self.update_model_info(self.get_selected_tag())
        self._update_button_for_ollama_state(self.get_selected_tag())

    def _refresh_model_list(self) -> None:
        """Discover installed models and update the catalog with fallback."""
        try:
            installed_model_tags = self._discover_installed_model_tags()
        except Exception:
            installed_model_tags = None
        self._schedule_ui_update(
            lambda: self._apply_model_catalog(installed_model_tags)
        )

    def _refresh_model_list_async(self) -> None:
        """Refresh visible model choices without blocking the UI thread."""
        if self._ui_state.ollama_state != "ready" or self._model_refresh_inflight:
            return

        self._model_refresh_inflight = True

        def worker() -> None:
            try:
                self._refresh_model_list()
            finally:
                self._model_refresh_inflight = False

        threading.Thread(
            target=worker,
            name="ModelPanelRefresh",
            daemon=True,
        ).start()

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
        """Return the startup model display name (saved or default)."""
        startup_model, _ = resolve_startup_model()
        return self.get_display_for_tag(startup_model)

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

        # Guard: disable combo at startup when Ollama is not ready
        if self._ui_state.ollama_state != "ready":
            self.combo_modelos.configure(state="disabled")

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

        self.lbl_tier_header = ctk.CTkLabel(
            self._parent,
            text="Tier LLM manual",
            font=ctk.CTkFont(size=12, weight="bold"),
            anchor="w",
        )
        self.lbl_tier_header.pack(fill="x", padx=10, pady=(8, 2))

        for tier in LLM_TIERS:
            button = ctk.CTkButton(
                self._parent,
                text=self._format_tier_button_text(tier),
                command=lambda selected=tier: self._on_llm_tier_selected(selected),
                width=110,
                fg_color="#1f4f7a" if tier == self._active_llm_tier else "#2b3440",
                hover_color="#286391",
            )
            button.pack(fill="x", padx=10, pady=2)
            self._tier_buttons[tier] = button

        self.lbl_tier_info = ctk.CTkLabel(
            self._parent,
            text="El tier cambia solo el modelo de futuros pedidos; perfil y memoria se conservan.",
            font=ctk.CTkFont(size=10),
            text_color="#8fa3b8",
            anchor="w",
            justify="left",
            wraplength=300,
        )
        self.lbl_tier_info.pack(fill="x", padx=10, pady=(2, 6))

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

        check_callback = on_check_ollama or self._on_check_ollama
        if state == "ready" and check_callback is not None:
            check_callback()
        if state == "ready":
            self._refresh_model_list_async()

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

    def set_active_model(self, tag: str) -> None:
        """Set the currently active model to update button state."""
        self._active_model_tag = tag
        self._update_button_for_ollama_state()
        self._update_tier_buttons()

    def restore_to_active_model(self, model_tag: str) -> None:
        """Restore combobox to the actual active model after a failed switch."""
        display = self.get_display_for_tag(model_tag)
        if self.combo_modelos is not None:
            self.combo_modelos.set(display)
        self._active_model_tag = model_tag
        self._update_button_for_ollama_state(model_tag)
        self._update_tier_buttons()

    def set_llm_tier_state(self, tiers: dict[str, Optional[str]], active_tier: str) -> None:
        """Update visible tier mapping and active tier."""
        self._llm_tiers = dict(tiers)
        self._active_llm_tier = active_tier
        self._update_tier_buttons()

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
                f"[Sistema] Ollama no esta listo. "
                f"Selecciona un modelo cuando Ollama este activo."
            )

    def _on_llm_tier_selected(self, tier: str) -> None:
        """Handle explicit manual Quality/Balanced/Fast tier selection."""
        model = self._llm_tiers.get(tier)
        label = LLM_TIER_LABELS.get(tier, tier)
        if not model:
            self._on_log(f"[Sistema] Tier LLM {label} no configurado.")
            self._update_tier_buttons()
            return
        if self._ui_state.ollama_state != "ready":
            self._on_log("[Sistema] Ollama no esta listo para cambiar tier LLM.")
            self._update_tier_buttons()
            return
        if not self._modelo_instalado(model):
            self._on_log(f"[Sistema] Tier LLM {label} usa '{model}', pero no esta instalado.")
            self._update_tier_buttons()
            return

        self._dispatcher.dispatch("on_switch_llm_tier", tier)
        self._on_log(f"[Sistema] Cambiando tier LLM a {label}: {model}")

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
            if self.combo_modelos is not None:
                self.combo_modelos.configure(state="disabled")
            return

        state = self._ui_state.ollama_state

        # Combo state guard: disable when Ollama is not ready
        if self.combo_modelos is not None:
            if state != "ready":
                self.combo_modelos.configure(state="disabled")
            else:
                self.combo_modelos.configure(state="normal")

        if state in _OLLAMA_BUTTON_CONFIG:
            config = _OLLAMA_BUTTON_CONFIG[state]
            self.btn_download.configure(state=config["state"], text=config["text"])
        elif model_tag and self._active_model_tag == model_tag:
            self.btn_download.configure(state="disabled", text="Modelo Activo")
        elif model_tag and self._modelo_instalado(model_tag):
            self.btn_download.configure(state="normal", text="Activar modelo")
        else:
            self.btn_download.configure(state="normal", text="Descargar modelo")

        self._update_tier_buttons()

    def _format_tier_button_text(self, tier: str) -> str:
        label = LLM_TIER_LABELS.get(tier, tier)
        model = self._llm_tiers.get(tier)
        detail = self.get_display_for_tag(model) if model else "sin configurar"
        active = "Activo: " if tier == self._active_llm_tier else ""
        return f"{active}{label} - {detail}"

    def _update_tier_buttons(self) -> None:
        if not self._tier_buttons:
            return
        for tier, button in self._tier_buttons.items():
            model = self._llm_tiers.get(tier)
            is_active = tier == self._active_llm_tier
            state = "normal" if model and self._ui_state.ollama_state == "ready" else "disabled"
            button.configure(
                text=self._format_tier_button_text(tier),
                state="disabled" if is_active else state,
                fg_color="#1f4f7a" if is_active else "#2b3440",
            )

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
        import threading
        from opencohost.config.settings import LOG_DIR, OLLAMA_MODELS_DIR

        def worker() -> None:
            manager = OllamaStartupManager(
                is_ready=lambda: self._detectar_estado_ollama() == "ready",
                timeout_seconds=60.0,
                poll_interval_seconds=0.5,
                stdout_log_path=os.path.join(LOG_DIR, "ollama_startup_stdout.log"),
                stderr_log_path=os.path.join(LOG_DIR, "ollama_startup_stderr.log"),
            )
            result = manager.start_and_wait(ollama_exe, OLLAMA_MODELS_DIR)
            self._ollama_process = result.process
            self._ollama_starting = False

            if result.status in {"ready", "already_running"}:
                if result.status == "already_running":
                    self._on_log("[Sistema] Ollama ya estaba iniciado.")
                else:
                    self._on_log("[Sistema] Ollama iniciado correctamente.")
                self._schedule_ui_update(lambda: self.refresh_ollama_state())
                return

            if result.status == "process_exited_early":
                self._on_log(f"[Sistema] Ollama se cerro durante el inicio: {result.diagnostic}")
            elif result.status == "timeout_waiting_ready":
                self._on_log(
                    "[Sistema] Ollama no respondio despues de iniciar. "
                    f"{result.diagnostic}"
                )
            else:
                self._on_log(f"[Sistema] No se pudo iniciar Ollama: {result.diagnostic}")

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
            self.btn_download.configure(state="disabled", text="Activando...")
            self._dispatcher.dispatch("on_switch_model", tag)
            self._on_log(f"[Sistema] Activando '{tag}'...")
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
            if value == "ready":
                self._refresh_model_list_async()
        elif key == "model_status":
            self._schedule_ui_update(lambda: self._handle_model_status_change(value))

    # ------------------------------------------------------------------
    # Model status handler
    # ------------------------------------------------------------------

    def _handle_model_status_change(self, value: str) -> None:
        """Handle model_status state changes: loading indicator + combo guard."""
        if value == "loading":
            if self.lbl_modelo_info is not None:
                self.lbl_modelo_info.configure(text="Cargando modelo en memoria...")
            if self.combo_modelos is not None:
                self.combo_modelos.configure(state="disabled")
        else:
            # Terminal state (ready, idle, error): re-enable combo if Ollama is ready
            if self.lbl_modelo_info is not None:
                self.update_model_info(self.get_selected_tag())
            if self.combo_modelos is not None and self._ui_state.ollama_state == "ready":
                self.combo_modelos.configure(state="normal")

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
