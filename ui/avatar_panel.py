"""Avatar / OBS panel — user-facing configuration for avatar states.

Provides:
- One row per state with current filename, ``Elegir imagen``, and ``Probar``
- A preview area showing the current state image
- OBS WebSocket configuration and connection controls
- File picker integration (no hardcoded user paths)
- Images are copied into the managed assets folder

This panel is designed to be embedded in the right-side product tab area.
"""

from __future__ import annotations

import os
import logging
import threading
from pathlib import Path
from tkinter import filedialog
from typing import Any, Callable, Dict, Optional

import customtkinter as ctk

from avatar.avatar_config import (
    AvatarConfig,
    VALID_STATES,
    SUPPORTED_EXTENSIONS,
    AVATAR_CONFIG_FILE,
    DEFAULT_ASSETS_FOLDER,
    load_avatar_config,
    save_avatar_config,
    assign_image_to_state,
)
from avatar.avatar_state import AvatarState, AvatarStateBridge

logger = logging.getLogger("VoiceAI")

# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------

_STATE_LABELS: Dict[str, str] = {
    "idle": "Normal (Idle)",
    "listening": "Escuchando",
    "thinking": "Pensando",
    "speaking": "Hablando",
    "speaking_alt": "Hablando (alt)",
    "sleeping": "Durmiendo",
    "angry": "Enfadada",
    "error": "Error",
}

_STATE_ORDER = ["idle", "listening", "thinking", "speaking", "speaking_alt", "sleeping", "angry", "error"]

_PREVIEW_SIZE = 160


class AvatarPanel:
    """Avatar/OBS configuration panel.

    Usage::

        panel = AvatarPanel(parent_frame, on_log=my_log_fn)
        panel.build()
    """

    def __init__(
        self,
        parent_frame: Any,
        on_log: Optional[Callable[[str], None]] = None,
        schedule_ui_update: Optional[Callable[[Callable[[], None]], None]] = None,
    ) -> None:
        self.parent = parent_frame
        self.on_log = on_log or (lambda m: None)
        self.schedule_ui = schedule_ui_update or (lambda fn: fn())

        # Configuration
        self.config = load_avatar_config()

        # State bridge (may be set externally for integration)
        self.state_bridge: Optional[AvatarStateBridge] = None
        self._bridge_sub_id: Optional[int] = None

        # UI widgets
        self._preview_label: Optional[ctk.CTkLabel] = None
        self._preview_image_ref: Any = None  # Keep reference for CTkImage
        self._state_rows: Dict[str, Dict[str, Any]] = {}
        self._current_state: str = "idle"
        self._frame: Optional[ctk.CTkFrame] = None
        self._content_frame: Any = None
        self._lbl_mode: Optional[ctk.CTkLabel] = None
        self._mode_spinner: Optional[ctk.CTkOptionMenu] = None
        self._toggle_all_states_btn: Optional[ctk.CTkButton] = None
        self._toggle_obs_btn: Optional[ctk.CTkButton] = None
        self._obs_frame: Optional[ctk.CTkFrame] = None
        self._main_expanded: bool = True
        self._all_states_expanded: bool = False
        self._obs_expanded: bool = False

        # OBS widgets
        self._obs_switch: Optional[ctk.CTkSwitch] = None
        self._obs_host_entry: Optional[ctk.CTkEntry] = None
        self._obs_port_entry: Optional[ctk.CTkEntry] = None
        self._obs_pass_entry: Optional[ctk.CTkEntry] = None
        self._obs_source_entry: Optional[ctk.CTkEntry] = None
        self._obs_connect_btn: Optional[ctk.CTkButton] = None
        self._obs_status_label: Optional[ctk.CTkLabel] = None
        self._obs_client: Any = None  # OBSClient instance

    # ------------------------------------------------------------------
    # Build
    # ------------------------------------------------------------------

    def build(self) -> ctk.CTkFrame:
        """Construct the Avatar/OBS panel inside the parent frame."""
        self._frame = ctk.CTkFrame(self.parent, fg_color="#151d26", corner_radius=14)
        self._frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=(0, 12))
        self._frame.grid_columnconfigure(0, weight=1)
        self._frame.grid_rowconfigure(0, weight=1)

        self._content_frame = ctk.CTkScrollableFrame(
            self._frame,
            fg_color="transparent",
            scrollbar_button_color="#2f5f8f",
            scrollbar_button_hover_color="#3670aa",
        )
        self._content_frame.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        self._content_frame.grid_columnconfigure(0, weight=1)

        # Header
        header = ctk.CTkFrame(self._content_frame, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header, text="Avatar / OBS",
            font=ctk.CTkFont(size=13, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            header, text="Próximamente: Live2D, VRM",
            font=ctk.CTkFont(size=10),
            text_color="#6b7b8d",
            anchor="e",
        ).grid(row=0, column=1, sticky="e")

        # Collapsible: Avatar / OBS main content
        card_main = ctk.CTkFrame(self._content_frame, fg_color="#151d26", corner_radius=16)
        card_main.grid(row=1, column=0, sticky="ew", padx=10, pady=(4, 8))
        card_main.grid_columnconfigure(0, weight=1)

        btn_toggle_main = ctk.CTkButton(
            card_main, text="▼ Avatar / OBS",
            anchor="w",
            fg_color="transparent", text_color="#d8e2ef",
            hover_color="#1d2a38",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        btn_toggle_main.grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))

        main_content = ctk.CTkFrame(card_main, fg_color="#101923", corner_radius=14)
        main_content.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))
        main_content.grid_columnconfigure(0, weight=1)

        btn_toggle_main.configure(
            command=lambda: self._toggle_main_section(main_content, btn_toggle_main)
        )

        # Mode / status info
        info_frame = ctk.CTkFrame(main_content, fg_color="transparent")
        info_frame.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        info_frame.grid_columnconfigure(1, weight=1)

        # Mode selector
        mode_values = ["Imagen estática"]
        if self.config.mode not in mode_values:
            mode_values.insert(0, self.config.mode)
        mode_values.extend(["Live2D (próximamente)", "VRM (próximamente)"])

        self._mode_spinner = ctk.CTkOptionMenu(
            info_frame,
            values=mode_values,
            command=self._on_mode_change,
            width=200,
        )
        self._mode_spinner.set("Imagen estática")
        self._mode_spinner.grid(row=0, column=0, sticky="w", padx=(0, 8))

        # Keep the state label but simplified
        self._lbl_mode = ctk.CTkLabel(
            info_frame, text=f"Estado: {self._current_state}",
            font=ctk.CTkFont(size=11),
            text_color="#8fa3b8",
            anchor="w",
        )
        self._lbl_mode.grid(row=0, column=1, sticky="w")

        # Preview area
        preview_frame = ctk.CTkFrame(main_content, fg_color="#0c1117", corner_radius=12)
        preview_frame.grid(row=1, column=0, sticky="ew", padx=10, pady=(4, 8))
        preview_frame.grid_columnconfigure(0, weight=1)

        self._preview_label = ctk.CTkLabel(
            preview_frame, text="Sin imagen configurada",
            text_color="#6b7b8d",
            font=ctk.CTkFont(size=12),
            height=_PREVIEW_SIZE,
            corner_radius=8,
        )
        self._preview_label.grid(row=0, column=0, sticky="ew", padx=8, pady=8)

        # State rows — show only active states by default
        DEFAULT_VISIBLE_STATES = {"idle", "listening", "speaking", "speaking_alt"}
        self._all_states_expanded = False

        self._build_parent = main_content
        row_idx = 2
        for state in _STATE_ORDER:
            self._build_state_row(state, row_idx)
            if state not in DEFAULT_VISIBLE_STATES:
                # Build but hide non-default states
                row_frame = self._state_rows[state].get("_frame")
                if row_frame:
                    row_frame.grid_remove()
            row_idx += 1
        self._build_parent = None

        # "Todos los estados" toggle button
        self._toggle_all_states_btn = ctk.CTkButton(
            self._content_frame,
            text="▶ Estados adicionales",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#151d26",
            text_color="#d8e2ef",
            hover_color="#1d2a38",
            command=self._toggle_all_states,
            anchor="w",
        )
        self._toggle_all_states_btn.grid(row=2, column=0, sticky="ew", padx=10, pady=(4, 4))

        # OBS WebSocket section — collapsible
        self._obs_expanded = False
        self._toggle_obs_btn = ctk.CTkButton(
            self._content_frame,
            text="▶ OBS WebSocket",
            font=ctk.CTkFont(size=12, weight="bold"),
            fg_color="#151d26",
            text_color="#d8e2ef",
            hover_color="#1d2a38",
            command=self._toggle_obs_section,
            anchor="w",
        )
        self._toggle_obs_btn.grid(row=3, column=0, sticky="ew", padx=10, pady=(8, 0))

        self._build_obs_section(4)
        # Hide OBS section by default
        if hasattr(self, '_obs_frame') and self._obs_frame:
            self._obs_frame.grid_remove()

        self._update_preview()

        return self._frame

    def _build_state_row(self, state: str, row: int) -> None:
        """Build a single state configuration row."""
        parent = getattr(self, '_build_parent', None) or self._content_frame
        row_frame = ctk.CTkFrame(parent, fg_color="#0f151c", corner_radius=8)
        row_frame.grid(row=row, column=0, sticky="ew", padx=10, pady=2)
        row_frame.grid_columnconfigure(1, weight=1)

        # State label
        label = _STATE_LABELS.get(state, state)
        ctk.CTkLabel(
            row_frame, text=label,
            font=ctk.CTkFont(size=11, weight="bold"),
            anchor="w", width=120,
        ).grid(row=0, column=0, sticky="w", padx=(8, 4), pady=4)

        # Current path summary
        current_path = self.config.state_images.get(state, "")
        display_name = Path(current_path).name if current_path else "Sin imagen"
        self._state_rows[state] = {}
        path_label = ctk.CTkLabel(
            row_frame, text=display_name,
            font=ctk.CTkFont(size=10),
            text_color="#8fa3b8" if current_path else "#555555",
            anchor="w",
        )
        path_label.grid(row=0, column=1, sticky="ew", padx=4, pady=4)
        self._state_rows[state]["path_label"] = path_label

        # Buttons frame
        btn_frame = ctk.CTkFrame(row_frame, fg_color="transparent")
        btn_frame.grid(row=0, column=2, sticky="e", padx=(4, 8), pady=4)

        # Elegir imagen button
        choose_btn = ctk.CTkButton(
            btn_frame, text="Elegir imagen",
            width=110, height=30,
            font=ctk.CTkFont(size=12),
            fg_color="#2f5f8f", hover_color="#3670aa",
            command=lambda s=state: self._choose_image(s),
        )
        choose_btn.pack(side="left", padx=(0, 4))
        self._state_rows[state]["choose_btn"] = choose_btn

        # Probar button
        test_btn = ctk.CTkButton(
            btn_frame, text="Probar",
            width=80, height=30,
            font=ctk.CTkFont(size=12),
            fg_color="#555555", hover_color="#666666",
            command=lambda s=state: self._test_state(s),
        )
        test_btn.pack(side="left")
        self._state_rows[state]["test_btn"] = test_btn

        # Store frame reference for collapsible toggle
        self._state_rows[state]["_frame"] = row_frame

    def _build_obs_section(self, row: int) -> None:
        """Build the OBS WebSocket configuration section."""
        obs_frame = ctk.CTkFrame(self._content_frame, fg_color="#0c1117", corner_radius=12)
        obs_frame.grid(row=row, column=0, sticky="ew", padx=10, pady=(8, 10))
        obs_frame.grid_columnconfigure(0, weight=1)

        # Header row
        header_row = ctk.CTkFrame(obs_frame, fg_color="transparent")
        header_row.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        header_row.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header_row, text="OBS WebSocket",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#d8e2ef",
            anchor="w",
        ).grid(row=0, column=0, sticky="w")

        self._obs_status_label = ctk.CTkLabel(
            header_row, text="Desconectado",
            font=ctk.CTkFont(size=10),
            text_color="#6b7b8d",
            anchor="e",
        )
        self._obs_status_label.grid(row=0, column=1, sticky="e")

        # Enable OBS switch
        enable_row = ctk.CTkFrame(obs_frame, fg_color="transparent")
        enable_row.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 4))
        self._obs_switch = ctk.CTkSwitch(
            enable_row, text="Habilitar OBS",
            onvalue=True, offvalue=False,
            command=self._on_obs_toggle,
        )
        self._obs_switch.grid(row=0, column=0, sticky="w", padx=(0, 10))
        if self.config.obs.enabled:
            self._obs_switch.select()

        # Connection fields
        fields_frame = ctk.CTkFrame(obs_frame, fg_color="transparent")
        fields_frame.grid(row=2, column=0, sticky="ew", padx=10, pady=4)
        fields_frame.grid_columnconfigure(1, weight=1)

        # Host
        ctk.CTkLabel(fields_frame, text="Host:", font=ctk.CTkFont(size=10), width=50, anchor="w").grid(row=0, column=0, sticky="w", padx=(0, 4), pady=2)
        self._obs_host_entry = ctk.CTkEntry(fields_frame, width=120, font=ctk.CTkFont(size=10))
        self._obs_host_entry.insert(0, self.config.obs.host)
        self._obs_host_entry.grid(row=0, column=1, sticky="w", padx=(0, 8), pady=2)

        # Port
        ctk.CTkLabel(fields_frame, text="Puerto:", font=ctk.CTkFont(size=10), width=50, anchor="w").grid(row=0, column=2, sticky="w", padx=(0, 4), pady=2)
        self._obs_port_entry = ctk.CTkEntry(fields_frame, width=70, font=ctk.CTkFont(size=10))
        self._obs_port_entry.insert(0, str(self.config.obs.port))
        self._obs_port_entry.grid(row=0, column=3, sticky="w", padx=(0, 8), pady=2)

        # Password
        ctk.CTkLabel(fields_frame, text="Password:", font=ctk.CTkFont(size=10), width=70, anchor="w").grid(row=1, column=0, sticky="w", padx=(0, 4), pady=2)
        self._obs_pass_entry = ctk.CTkEntry(fields_frame, width=120, font=ctk.CTkFont(size=10), show="*")
        self._obs_pass_entry.insert(0, self.config.obs.password)
        self._obs_pass_entry.grid(row=1, column=1, sticky="w", padx=(0, 8), pady=2)

        # Source name
        ctk.CTkLabel(fields_frame, text="Fuente:", font=ctk.CTkFont(size=10), width=50, anchor="w").grid(row=1, column=2, sticky="w", padx=(0, 4), pady=2)
        self._obs_source_entry = ctk.CTkEntry(fields_frame, width=120, font=ctk.CTkFont(size=10))
        self._obs_source_entry.insert(0, self.config.obs.source_name)
        self._obs_source_entry.grid(row=1, column=3, sticky="w", padx=(0, 8), pady=2)

        # Connect / Test button
        btn_row = ctk.CTkFrame(obs_frame, fg_color="transparent")
        btn_row.grid(row=3, column=0, sticky="ew", padx=10, pady=(4, 10))
        self._obs_connect_btn = ctk.CTkButton(
            btn_row, text="Probar conexión",
            width=120, height=28,
            font=ctk.CTkFont(size=11),
            fg_color="#2f5f8f", hover_color="#3670aa",
            command=self._test_obs_connection,
        )
        self._obs_connect_btn.pack(side="left", padx=(0, 8))

        ctk.CTkLabel(
            btn_row, text="Requiere obs-websocket plugin en OBS",
            font=ctk.CTkFont(size=9),
            text_color="#555555",
        ).pack(side="left")

        # Update field states
        self._update_obs_fields_state()

        # Store frame reference for collapsible toggle
        self._obs_frame = obs_frame

    def _on_obs_toggle(self) -> None:
        """Handle OBS enable/disable toggle."""
        enabled = self._obs_switch.get()
        self.config.obs.enabled = enabled
        save_avatar_config(self.config)
        self._update_obs_fields_state()
        self.on_log(f"[OBS] OBS integration {'enabled' if enabled else 'disabled'}")

    def _update_obs_fields_state(self) -> None:
        """Enable/disable OBS fields based on toggle state."""
        enabled = self._obs_switch.get() if self._obs_switch else self.config.obs.enabled
        state = "normal" if enabled else "disabled"
        for widget in (self._obs_host_entry, self._obs_port_entry, self._obs_pass_entry, self._obs_source_entry, self._obs_connect_btn):
            if widget:
                widget.configure(state=state)

    def _test_obs_connection(self) -> None:
        """Test OBS connection with current settings."""
        self._save_obs_config_from_ui()

        if self._obs_connect_btn:
            self._obs_connect_btn.configure(text="Conectando...", state="disabled")

        def do_test():
            from avatar.obs_client import OBSClient, OBSConfig
            client = OBSClient(
                config=OBSConfig(
                    enabled=True,
                    host=self.config.obs.host,
                    port=self.config.obs.port,
                    password=self.config.obs.password,
                    source_name=self.config.obs.source_name,
                ),
                assets_folder=self.config.assets_folder,
                state_images=self.config.state_images,
            )
            success, message = client.test_connection()

            def update_ui():
                if self._obs_connect_btn:
                    self._obs_connect_btn.configure(text="Probar conexión", state="normal")
                if self._obs_status_label:
                    if success:
                        self._obs_status_label.configure(text=message, text_color="#4ade80")
                    else:
                        self._obs_status_label.configure(text=message, text_color="#f87171")
                self.on_log(f"[OBS] {message}")

            self.schedule_ui(update_ui)

        threading.Thread(target=do_test, daemon=True).start()

    def _save_obs_config_from_ui(self) -> None:
        """Read OBS config from UI widgets and persist."""
        try:
            port_val = int(self._obs_port_entry.get().strip())
        except ValueError:
            port_val = 4455

        self.config.obs.host = self._obs_host_entry.get().strip() or "localhost"
        self.config.obs.port = port_val
        self.config.obs.password = self._obs_pass_entry.get()
        self.config.obs.source_name = self._obs_source_entry.get().strip() or "KiraAvatar"
        save_avatar_config(self.config)

    def set_obs_client(self, client: Any) -> None:
        """Set the OBSClient instance for this panel."""
        self._obs_client = client
        if self._obs_status_label:
            if client and client.is_connected:
                self._obs_status_label.configure(text="Conectado", text_color="#4ade80")
            else:
                self._obs_status_label.configure(text="Desconectado", text_color="#6b7b8d")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def _choose_image(self, state: str) -> None:
        """Open a file picker and assign the selected image to a state."""
        filetypes = [
            ("Im\u00e1genes", "*.png *.jpg *.jpeg *.webp"),
            ("PNG", "*.png"),
            ("JPEG", "*.jpg *.jpeg"),
            ("WebP", "*.webp"),
            ("Todos", "*.*"),
        ]
        path = filedialog.askopenfilename(
            title=f"Seleccionar imagen para '{_STATE_LABELS.get(state, state)}'",
            filetypes=filetypes,
        )
        if not path:
            return

        try:
            self.config = assign_image_to_state(self.config, state, path)
            save_avatar_config(self.config)
        except Exception as e:
            self.on_log(f"[Avatar] Error al asignar imagen: {e}")
            return

        # Update UI
        self._update_row_display(state)
        self._update_preview()
        self.on_log(f"[Avatar] Imagen asignada a '{_STATE_LABELS.get(state, state)}': {Path(path).name}")

    def _test_state(self, state: str) -> None:
        """Preview a state image without changing the runtime state."""
        self._current_state = state
        self._update_preview()
        self._update_info_label()

    def _update_row_display(self, state: str) -> None:
        """Update the path label for a state row."""
        row = self._state_rows.get(state)
        if not row:
            return

        current_path = self.config.state_images.get(state, "")
        display_name = Path(current_path).name if current_path else "Sin imagen"
        row["path_label"].configure(
            text=display_name,
            text_color="#8fa3b8" if current_path else "#555555",
        )

    def _update_preview(self) -> None:
        """Update the preview image based on the current state."""
        if self._preview_label is None or not self._preview_label.winfo_exists():
            return

        image_path = self.config.get_image_for_state(self._current_state)

        if image_path and os.path.isfile(image_path):
            try:
                from PIL import Image
                img = Image.open(image_path)
                # Resize maintaining aspect ratio
                img.thumbnail((_PREVIEW_SIZE, _PREVIEW_SIZE), Image.Resampling.LANCZOS)
                # Keep BOTH references alive to prevent Tkinter image GC.
                self._preview_image_pil = img
                ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
                self._preview_label.configure(image=ctk_img, text="")
                self._preview_image_ref = ctk_img
                if self._current_state == "idle" or image_path == self.config.state_images.get("idle"):
                    self._preview_idle_image_pil = img
                    self._preview_idle_image_ref = ctk_img
            except Exception as e:
                logger.warning(f"[Avatar] No se pudo cargar preview: {e}")
                if self._show_cached_idle_preview():
                    return
                self._preview_image_pil = None
                self._preview_image_ref = None
                self._preview_label.configure(image=None, text="Error al cargar imagen", text_color="#aa5555")
        else:
            state_label = _STATE_LABELS.get(self._current_state, self._current_state)
            if self._show_cached_idle_preview():
                return
            self._preview_image_pil = None
            self._preview_image_ref = None
            self._preview_label.configure(image=None, text=f"Sin imagen para: {state_label}", text_color="#6b7b8d")

    def _show_cached_idle_preview(self) -> bool:
        """Keep a known idle image visible when a transient state cannot load."""
        idle_ref = getattr(self, "_preview_idle_image_ref", None)
        if not idle_ref or self._preview_label is None:
            return False
        self._preview_image_pil = getattr(self, "_preview_idle_image_pil", None)
        self._preview_image_ref = idle_ref
        self._preview_label.configure(image=idle_ref, text="")
        return True

    def _on_mode_change(self, value: str) -> None:
        self.config.mode = value
        save_avatar_config(self.config)
        self._update_preview()
        self.on_log(f"[Avatar] Modo cambiado a: {value}")

    def _toggle_all_states(self) -> None:
        self._all_states_expanded = not self._all_states_expanded
        DEFAULT_VISIBLE_STATES = {"idle", "listening", "speaking", "speaking_alt"}
        for state in _STATE_ORDER:
            if state not in DEFAULT_VISIBLE_STATES:
                row_frame = self._state_rows.get(state, {}).get("_frame")
                if row_frame:
                    if self._all_states_expanded:
                        row_frame.grid()
                    else:
                        row_frame.grid_remove()
        self._toggle_all_states_btn.configure(
            text=f"{'▼' if self._all_states_expanded else '▶'} Estados adicionales"
        )

    def _toggle_obs_section(self) -> None:
        self._obs_expanded = not self._obs_expanded
        if hasattr(self, '_obs_frame') and self._obs_frame:
            if self._obs_expanded:
                self._obs_frame.grid()
            else:
                self._obs_frame.grid_remove()
        arrow = "▼" if self._obs_expanded else "▶"
        if self._toggle_obs_btn:
            self._toggle_obs_btn.configure(text=f"{arrow} OBS WebSocket")

    def _toggle_main_section(self, content: Any, button: Any) -> None:
        self._main_expanded = not self._main_expanded
        if self._main_expanded:
            content.grid()
            button.configure(text="▼ Avatar / OBS")
        else:
            content.grid_remove()
            button.configure(text="▶ Avatar / OBS")

    def _update_info_label(self) -> None:
        """Update the state info label."""
        if self._lbl_mode:
            self._lbl_mode.configure(
                text=f"Estado: {self._current_state}"
            )

    # ------------------------------------------------------------------
    # Integration with state bridge
    # ------------------------------------------------------------------

    def set_state_bridge(self, bridge: AvatarStateBridge) -> None:
        """Connect this panel to the runtime state bridge."""
        self.state_bridge = bridge
        self._bridge_sub_id = bridge.subscribe(self._on_state_change)

    def _on_state_change(self, state: AvatarState) -> None:
        """Called when the state bridge changes state."""
        def update():
            self._current_state = state.value
            self._update_preview()
            self._update_info_label()

        self.schedule_ui(update)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """Unsubscribe from state bridge."""
        if self.state_bridge and self._bridge_sub_id is not None:
            self.state_bridge.unsubscribe(self._bridge_sub_id)
            self._bridge_sub_id = None

    # ------------------------------------------------------------------
    # Public accessors
    # ------------------------------------------------------------------

    def get_config(self) -> AvatarConfig:
        """Return the current avatar configuration."""
        return self.config

    def get_current_state(self) -> str:
        """Return the currently previewed state key."""
        return self._current_state
