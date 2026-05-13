"""StreamAdminUI — encapsulates Stream Admin (RF4) behavior and UI updates.

Manages OAuth connection, metadata editing, moderation, chat integration,
analytics display, and runtime settings for the YouTube/Twitch stream admin
tab.  All widget references are injected at construction time so the module
can update the UI without knowing about app.py internals.

Thread safety is provided via the injected ``UIState``.  Callbacks are
invoked through ``CallbackDispatcher`` for safe error handling.

Usage::

    sa_ui = StreamAdminUI(
        ui_state=state,
        dispatcher=dispatcher,
        stream_admin=admin_manager,
        smart_agg=aggregator,
        motor_ia=motor,
        on_log=lambda msg: print(msg),
        schedule_ui_update=lambda fn: root.after(0, fn),
    )
    frame = sa_ui.build(parent_frame)
    sa_ui.populate_oauth_fields()
"""

from __future__ import annotations

import time
import threading
from typing import Any, Callable, Optional

import customtkinter as ctk
import tkinter.messagebox as messagebox

from ui.state import UIState
from ui.protocols import CallbackDispatcher


class StreamAdminUI:
    """Manages Stream Admin (RF4) behavior and UI state synchronization.

    The class does NOT own top-level application state.  It reads/writes
    through the injected ``ui_state`` object and calls injected callbacks
    for logging.  Widget references are injected via :meth:`set_widgets`
    so the UI can be built independently.
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        ui_state: UIState,
        dispatcher: CallbackDispatcher,
        stream_admin: Any = None,
        smart_agg: Any = None,
        motor_ia: Any = None,
        on_log: Callable[[str], None] = None,
        schedule_ui_update: Optional[Callable[[Callable[[], None]], None]] = None,
    ) -> None:
        self._ui_state = ui_state
        self._dispatcher = dispatcher
        self._stream_admin = stream_admin
        self._smart_agg = smart_agg
        self._motor_ia = motor_ia
        self._on_log = on_log or (lambda msg: None)
        self._schedule_ui_update = schedule_ui_update or (lambda fn: fn())

        # Internal state
        self._last_metadata: dict[str, Any] = {}
        self._chat_connected: bool = False
        self._chat_stop: Any = None
        self._chat_thread: Any = None
        self._seen_chat_ids: set[str] = set()
        self._sim_round: int = 0
        self._chat_users: dict[str, dict[str, Any]] = {}
        self._smart_agg_default_activity: Optional[dict[str, float]] = None

        # Widget references (set via set_widgets or build)
        self._widgets: dict[str, Any] = {}

        # Dispatch callbacks (set via set_*_callback)
        self._connect_cb: Optional[Callable] = None
        self._disconnect_cb: Optional[Callable] = None
        self._save_oauth_cb: Optional[Callable] = None
        self._refresh_metadata_cb: Optional[Callable] = None
        self._suggest_metadata_cb: Optional[Callable] = None
        self._apply_metadata_cb: Optional[Callable] = None
        self._reject_pending_cb: Optional[Callable] = None
        self._apply_runtime_settings_cb: Optional[Callable] = None
        self._propose_high_risk_cb: Optional[Callable] = None
        self._connect_current_chat_cb: Optional[Callable] = None
        self._send_chat_cb: Optional[Callable] = None
        self._toggle_small_stream_cb: Optional[Callable] = None
        self._simulate_chat_cb: Optional[Callable] = None
        self._force_kira_cb: Optional[Callable] = None
        self._refresh_user_list_cb: Optional[Callable] = None

    # ------------------------------------------------------------------
    # Widget injection
    # ------------------------------------------------------------------

    def set_widgets(self, widgets: dict[str, Any]) -> None:
        """Inject widget references for UI updates.

        Args:
            widgets: Dict mapping widget names to their instances.
                Expected keys include: ``lbl_stream_admin_status``,
                ``btn_stream_youtube_read``, ``btn_stream_youtube_write``,
                ``btn_stream_revoke_write``, ``btn_stream_disconnect``,
                ``btn_stream_twitch``, ``entry_stream_client_id``,
                ``entry_stream_client_secret``, ``lbl_stream_metadata_state``,
                ``btn_stream_read_metadata``, ``btn_stream_suggest_metadata``,
                ``btn_stream_apply_metadata``, ``btn_stream_reject_pending``,
                ``entry_stream_title``, ``entry_stream_category``,
                ``entry_stream_tags``, ``text_stream_description``,
                ``switch_stream_mod_enabled``, ``combo_stream_mod_mode``,
                ``switch_stream_announce``, ``entry_stream_mod_user``,
                ``entry_stream_mod_reason``, ``btn_stream_propose_timeout``,
                ``btn_stream_propose_ban``, ``frame_stream_users``,
                ``btn_stream_connect_chat``, ``switch_stream_chat_enabled``,
                ``switch_stream_small``, ``entry_stream_chat_message``,
                ``btn_stream_send_chat``, ``btn_stream_simulate_chat``,
                ``btn_stream_force_kira``,
                ``lbl_stream_analytics``, ``lbl_stream_pending``,
                ``text_stream_admin_log``, ``lbl_oauth_status_pill``,
                ``lbl_moderation_status_pill``, ``lbl_oauth_side_status``,
                ``lbl_moderation_side_status``, ``lbl_kira_chat_state``.
        """
        self._widgets.update(widgets)

    def _widget(self, name: str) -> Any:
        """Return a widget by name, or None if not set."""
        return self._widgets.get(name)

    # ------------------------------------------------------------------
    # UI Building
    # ------------------------------------------------------------------

    def build(self, parent: Any) -> Any:
        """Build the Stream Admin tab UI within the given parent frame.

        Creates the streamlined Stream workspace tabs and their widgets.
        Controls are grouped by operator intent instead of low-level feature
        area: ``Emisión`` for setup/metadata, ``Acciones`` for chat and
        moderation, and ``Estado`` for read-only stream feedback.  Widget
        references are stored in ``_widgets`` for later UI updates.

        Args:
            parent: CTkFrame to build the UI inside.

        Returns:
            The parent frame (for chaining).
        """
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)

        stream_tabs = ctk.CTkTabview(
            parent,
            fg_color="#0f151c",
            segmented_button_fg_color="#0c1117",
            segmented_button_selected_color="#2f5f8f",
            segmented_button_selected_hover_color="#3670aa",
            segmented_button_unselected_color="#151d26",
            segmented_button_unselected_hover_color="#1d2a38",
            text_color="#d8e2ef",
        )
        stream_tabs.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        tab_stream_live = stream_tabs.add("Emisión")
        tab_stream_actions = stream_tabs.add("Acciones")
        tab_stream_status = stream_tabs.add("Estado")
        for tab in (tab_stream_live, tab_stream_actions, tab_stream_status):
            tab.grid_columnconfigure(0, weight=1)
            tab.grid_rowconfigure(0, weight=1)

        # Stream uses vertical scrollable containment: cards stack instead of
        # building cockpit-wide rows that can be clipped by the product shell.
        live_sections = ctk.CTkScrollableFrame(tab_stream_live, fg_color="transparent")
        live_sections.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        live_sections.grid_columnconfigure(0, weight=1)

        actions_sections = ctk.CTkScrollableFrame(tab_stream_actions, fg_color="transparent")
        actions_sections.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        actions_sections.grid_columnconfigure(0, weight=1)

        connection_section = ctk.CTkFrame(live_sections, fg_color="transparent")
        connection_section.grid(row=0, column=0, sticky="ew", padx=0, pady=0)
        connection_section.grid_columnconfigure(0, weight=1)
        self._build_connection_tab(connection_section)

        metadata_section = ctk.CTkFrame(live_sections, fg_color="transparent")
        metadata_section.grid(row=1, column=0, sticky="ew", padx=0, pady=0)
        metadata_section.grid_columnconfigure(0, weight=1)
        self._build_metadata_tab(metadata_section)

        chat_section = ctk.CTkFrame(actions_sections, fg_color="transparent")
        chat_section.grid(row=0, column=0, sticky="ew", padx=0, pady=0)
        chat_section.grid_columnconfigure(0, weight=1)
        self._build_chat_tab(chat_section)

        moderation_section = ctk.CTkFrame(actions_sections, fg_color="transparent")
        moderation_section.grid(row=1, column=0, sticky="ew", padx=0, pady=0)
        moderation_section.grid_columnconfigure(0, weight=1)
        self._build_moderation_tab(moderation_section)

        self._build_status_tab(tab_stream_status)

        return parent

    def _build_connection_tab(self, tab: Any) -> None:
        frame_auth = ctk.CTkFrame(tab, fg_color="#151d26", corner_radius=14)
        frame_auth.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        frame_auth.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(frame_auth, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(header, text="OAuth / Proveedor", font=ctk.CTkFont(size=14, weight="bold"), anchor="w").grid(row=0, column=0, sticky="ew")
        lbl_status = ctk.CTkLabel(header, text="RF4 iniciando...", text_color="#aaaaaa", anchor="w", justify="left", wraplength=520)
        lbl_status.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self._widgets["lbl_stream_admin_status"] = lbl_status

        button_stack = ctk.CTkFrame(frame_auth, fg_color="transparent")
        button_stack.grid(row=1, column=0, sticky="ew", padx=10, pady=4)
        button_stack.grid_columnconfigure(0, weight=1)
        button_stack.grid_columnconfigure(1, weight=1)
        btn_read = ctk.CTkButton(button_stack, text="Conectar YouTube Lectura", command=lambda: self._dispatch_connect(False), width=170, fg_color="#2f5f8f", hover_color="#3670aa")
        btn_read.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        self._widgets["btn_stream_youtube_read"] = btn_read

        btn_write = ctk.CTkButton(button_stack, text="Reconectar Escritura", command=lambda: self._dispatch_connect(True), width=150, fg_color="#555555", hover_color="#666666")
        btn_write.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        self._widgets["btn_stream_youtube_write"] = btn_write

        btn_revoke = ctk.CTkButton(button_stack, text="Revocar Escritura", command=self.revoke_write, width=130, fg_color="#7d5a2a", hover_color="#a07030", state="disabled")
        btn_revoke.grid(row=1, column=0, padx=4, pady=4, sticky="ew")
        self._widgets["btn_stream_revoke_write"] = btn_revoke

        btn_disconnect = ctk.CTkButton(button_stack, text="Desconectar", command=lambda: self._dispatch_disconnect(), width=105, fg_color="#555555")
        btn_disconnect.grid(row=1, column=1, padx=4, pady=4, sticky="ew")
        self._widgets["btn_stream_disconnect"] = btn_disconnect

        btn_twitch = ctk.CTkButton(button_stack, text="Twitch Próximamente", state="disabled", width=145, fg_color="#444444")
        btn_twitch.grid(row=2, column=0, columnspan=2, padx=4, pady=4, sticky="ew")
        self._widgets["btn_stream_twitch"] = btn_twitch

        ctk.CTkLabel(frame_auth, text="Client ID:", anchor="w").grid(row=2, column=0, padx=10, pady=(8, 2), sticky="ew")
        entry_id = ctk.CTkEntry(frame_auth, placeholder_text="tu_client_id.apps.googleusercontent.com")
        entry_id.grid(row=3, column=0, padx=10, pady=2, sticky="ew")
        self._widgets["entry_stream_client_id"] = entry_id

        ctk.CTkLabel(frame_auth, text="Secret:", anchor="w").grid(row=4, column=0, padx=10, pady=(8, 2), sticky="ew")
        entry_secret = ctk.CTkEntry(frame_auth, placeholder_text="OAuth client secret", show="*")
        entry_secret.grid(row=5, column=0, padx=10, pady=2, sticky="ew")
        self._widgets["entry_stream_client_secret"] = entry_secret

        ctk.CTkButton(frame_auth, text="Guardar OAuth", command=self._dispatch_save_oauth, width=125, fg_color="#555555", hover_color="#666666").grid(row=6, column=0, padx=10, pady=(8, 10), sticky="ew")

    def _build_metadata_tab(self, tab: Any) -> None:
        frame_meta = ctk.CTkFrame(tab, fg_color="#151d26", corner_radius=14)
        frame_meta.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        frame_meta.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(frame_meta, text="Metadata", font=ctk.CTkFont(size=14, weight="bold"), anchor="w").grid(row=0, column=0, padx=10, pady=(10, 2), sticky="ew")
        lbl_meta = ctk.CTkLabel(frame_meta, text="Sin metadata", text_color="#aaaaaa", anchor="w", justify="left", wraplength=520)
        lbl_meta.grid(row=1, column=0, padx=10, pady=(0, 6), sticky="ew")
        self._widgets["lbl_stream_metadata_state"] = lbl_meta

        meta_actions = ctk.CTkFrame(frame_meta, fg_color="transparent")
        meta_actions.grid(row=2, column=0, sticky="ew", padx=10, pady=4)
        meta_actions.grid_columnconfigure(0, weight=1)
        meta_actions.grid_columnconfigure(1, weight=1)
        btn_read = ctk.CTkButton(meta_actions, text="Leer", command=lambda: self._dispatch_refresh_metadata(), width=80, fg_color="#555555", hover_color="#666666")
        btn_read.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        self._widgets["btn_stream_read_metadata"] = btn_read

        btn_suggest = ctk.CTkButton(meta_actions, text="Sugerir", command=lambda: self._dispatch_suggest_metadata(), width=85, fg_color="#2f5f8f", hover_color="#3670aa")
        btn_suggest.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        self._widgets["btn_stream_suggest_metadata"] = btn_suggest

        btn_apply = ctk.CTkButton(meta_actions, text="Aplicar", command=lambda: self._dispatch_apply_metadata(), width=85, fg_color="#2a7d3f")
        btn_apply.grid(row=1, column=0, padx=4, pady=4, sticky="ew")
        self._widgets["btn_stream_apply_metadata"] = btn_apply

        btn_reject = ctk.CTkButton(meta_actions, text="Rechazar", command=lambda: self._dispatch_reject_pending(), width=85, fg_color="#7d2a2a")
        btn_reject.grid(row=1, column=1, padx=4, pady=4, sticky="ew")
        self._widgets["btn_stream_reject_pending"] = btn_reject

        ctk.CTkLabel(frame_meta, text="Título:", anchor="w").grid(row=3, column=0, padx=10, pady=(8, 2), sticky="ew")
        entry_title = ctk.CTkEntry(frame_meta, placeholder_text="Título del stream")
        entry_title.grid(row=4, column=0, padx=10, pady=2, sticky="ew")
        self._widgets["entry_stream_title"] = entry_title

        ctk.CTkLabel(frame_meta, text="Categoría:", anchor="w").grid(row=5, column=0, padx=10, pady=(8, 2), sticky="ew")
        entry_cat = ctk.CTkEntry(frame_meta, placeholder_text="ID categoría YouTube, ej. 20")
        entry_cat.grid(row=6, column=0, padx=10, pady=2, sticky="ew")
        self._widgets["entry_stream_category"] = entry_cat

        ctk.CTkLabel(frame_meta, text="Tags:", anchor="w").grid(row=7, column=0, padx=10, pady=(8, 2), sticky="ew")
        entry_tags = ctk.CTkEntry(frame_meta, placeholder_text="tag1, tag2, tag3")
        entry_tags.grid(row=8, column=0, padx=10, pady=2, sticky="ew")
        self._widgets["entry_stream_tags"] = entry_tags

        ctk.CTkLabel(frame_meta, text="Descripción:", anchor="w").grid(row=9, column=0, padx=10, pady=(8, 2), sticky="ew")
        text_desc = ctk.CTkTextbox(frame_meta, height=70)
        text_desc.grid(row=10, column=0, padx=10, pady=(2, 10), sticky="ew")
        self._widgets["text_stream_description"] = text_desc

    def _build_moderation_tab(self, tab: Any) -> None:
        frame_mod = ctk.CTkFrame(tab, fg_color="#151d26", corner_radius=14)
        frame_mod.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        frame_mod.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(frame_mod, text="Moderación", font=ctk.CTkFont(size=14, weight="bold"), anchor="w").grid(row=0, column=0, padx=10, pady=(10, 4), sticky="ew")
        controls = ctk.CTkFrame(frame_mod, fg_color="transparent")
        controls.grid(row=1, column=0, sticky="ew", padx=10, pady=4)
        controls.grid_columnconfigure(0, weight=1)
        switch_mod = ctk.CTkSwitch(controls, text="AutoMod", command=lambda: self._dispatch_apply_runtime_settings())
        switch_mod.grid(row=0, column=0, padx=4, pady=4, sticky="w")
        self._widgets["switch_stream_mod_enabled"] = switch_mod

        combo_mode = ctk.CTkOptionMenu(controls, values=["alerts_only", "confirm_required", "automatic"], command=lambda _: self._dispatch_apply_runtime_settings(), width=150)
        combo_mode.set("alerts_only")
        combo_mode.grid(row=1, column=0, padx=4, pady=4, sticky="ew")
        self._widgets["combo_stream_mod_mode"] = combo_mode

        switch_announce = ctk.CTkSwitch(controls, text="Anunciar al chat", command=lambda: self._dispatch_apply_runtime_settings())
        switch_announce.grid(row=2, column=0, padx=4, pady=4, sticky="w")
        self._widgets["switch_stream_announce"] = switch_announce

        ctk.CTkLabel(frame_mod, text="User Channel ID:", anchor="w").grid(row=2, column=0, padx=10, pady=(8, 2), sticky="ew")
        entry_user = ctk.CTkEntry(frame_mod, width=180, placeholder_text="channelId del usuario")
        entry_user.grid(row=3, column=0, padx=10, pady=2, sticky="ew")
        self._widgets["entry_stream_mod_user"] = entry_user

        ctk.CTkLabel(frame_mod, text="Razón:", anchor="w").grid(row=4, column=0, padx=10, pady=(8, 2), sticky="ew")
        entry_reason = ctk.CTkEntry(frame_mod, placeholder_text="spam/toxicidad/etc.")
        entry_reason.grid(row=5, column=0, padx=10, pady=2, sticky="ew")
        self._widgets["entry_stream_mod_reason"] = entry_reason

        mod_actions = ctk.CTkFrame(frame_mod, fg_color="transparent")
        mod_actions.grid(row=6, column=0, sticky="ew", padx=10, pady=4)
        mod_actions.grid_columnconfigure(0, weight=1)
        mod_actions.grid_columnconfigure(1, weight=1)
        btn_timeout = ctk.CTkButton(mod_actions, text="Proponer Timeout", command=lambda: self._dispatch_propose_high_risk("timeout"), width=135, fg_color="#555555", hover_color="#666666")
        btn_timeout.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        self._widgets["btn_stream_propose_timeout"] = btn_timeout

        btn_ban = ctk.CTkButton(mod_actions, text="Proponer Ban", command=lambda: self._dispatch_propose_high_risk("ban"), width=115, fg_color="#7d2a2a")
        btn_ban.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        self._widgets["btn_stream_propose_ban"] = btn_ban

        ctk.CTkLabel(frame_mod, text="Usuarios recientes", font=ctk.CTkFont(size=12, weight="bold"), anchor="w").grid(row=7, column=0, padx=10, pady=(8, 4), sticky="ew")
        ctk.CTkButton(frame_mod, text="Actualizar lista", command=self.refresh_user_list, width=115, fg_color="#555555").grid(row=8, column=0, padx=10, pady=(0, 4), sticky="ew")

        frame_users = ctk.CTkScrollableFrame(frame_mod, height=115)
        frame_users.grid(row=9, column=0, padx=10, pady=(0, 10), sticky="ew")
        frame_users.grid_columnconfigure(2, weight=1)
        self._widgets["frame_stream_users"] = frame_users

    def _build_chat_tab(self, tab: Any) -> None:
        frame_chat = ctk.CTkFrame(tab, fg_color="#151d26", corner_radius=14)
        frame_chat.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        frame_chat.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(frame_chat, text="Kira Chat", font=ctk.CTkFont(size=14, weight="bold"), anchor="w").grid(row=0, column=0, padx=10, pady=(10, 4), sticky="ew")
        chat_actions = ctk.CTkFrame(frame_chat, fg_color="transparent")
        chat_actions.grid(row=1, column=0, sticky="ew", padx=10, pady=4)
        chat_actions.grid_columnconfigure(0, weight=1)
        chat_actions.grid_columnconfigure(1, weight=1)
        btn_connect_chat = ctk.CTkButton(chat_actions, text="Conectar Chat", command=self._dispatch_connect_current_chat, width=125, fg_color="#2f5f8f")
        btn_connect_chat.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        self._widgets["btn_stream_connect_chat"] = btn_connect_chat

        switch_chat = ctk.CTkSwitch(chat_actions, text="Permitir mensajes", command=lambda: self._dispatch_apply_runtime_settings())
        switch_chat.grid(row=0, column=1, padx=4, pady=4, sticky="w")
        self._widgets["switch_stream_chat_enabled"] = switch_chat

        switch_small = ctk.CTkSwitch(chat_actions, text="Stream Chico", command=self._dispatch_toggle_small_stream)
        switch_small.grid(row=1, column=0, padx=4, pady=4, sticky="w")
        self._widgets["switch_stream_small"] = switch_small

        btn_simulate = ctk.CTkButton(chat_actions, text="Simular Chat", command=self._dispatch_simulate_chat, width=115, fg_color="#555555", hover_color="#666666")
        btn_simulate.grid(row=1, column=1, padx=4, pady=4, sticky="ew")
        self._widgets["btn_stream_simulate_chat"] = btn_simulate

        entry_msg = ctk.CTkEntry(frame_chat, placeholder_text="Mensaje breve de Kira para el chat")
        entry_msg.grid(row=2, column=0, padx=10, pady=(8, 4), sticky="ew")
        self._widgets["entry_stream_chat_message"] = entry_msg

        send_actions = ctk.CTkFrame(frame_chat, fg_color="transparent")
        send_actions.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 10))
        send_actions.grid_columnconfigure(0, weight=1)
        send_actions.grid_columnconfigure(1, weight=1)
        btn_send = ctk.CTkButton(send_actions, text="Enviar al chat", command=lambda: self._dispatch_send_chat(), width=120)
        btn_send.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        self._widgets["btn_stream_send_chat"] = btn_send

        btn_force = ctk.CTkButton(send_actions, text="Forzar Kira", command=self._dispatch_force_kira, width=115, fg_color="#555555", hover_color="#666666")
        btn_force.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        self._widgets["btn_stream_force_kira"] = btn_force

    def _build_status_tab(self, tab: Any) -> None:
        frame_bottom = ctk.CTkScrollableFrame(tab, fg_color="#151d26", corner_radius=14)
        frame_bottom.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        frame_bottom.grid_columnconfigure(0, weight=1)

        lbl_analytics = ctk.CTkLabel(frame_bottom, text="Analíticas: esperando RF3", anchor="w", justify="left", wraplength=520)
        lbl_analytics.grid(row=0, column=0, padx=10, pady=(10, 6), sticky="ew")
        self._widgets["lbl_stream_analytics"] = lbl_analytics

        lbl_pending = ctk.CTkLabel(frame_bottom, text="Acción pendiente: ninguna", anchor="w", justify="left", text_color="#aaaaaa", wraplength=520)
        lbl_pending.grid(row=1, column=0, padx=10, pady=6, sticky="ew")
        self._widgets["lbl_stream_pending"] = lbl_pending

        ctk.CTkLabel(
            frame_bottom,
            text="El log detallado de Stream Admin está abajo en Logs / Terminales > Stream Log.",
            text_color="#8fa3b8",
            anchor="w",
            justify="left",
            wraplength=520,
        ).grid(row=2, column=0, padx=10, pady=(0, 10), sticky="ew")

    # ------------------------------------------------------------------
    # Dispatch callbacks (wired by AppShell)
    # ------------------------------------------------------------------

    def set_connect_callback(self, cb: Callable) -> None:
        self._connect_cb = cb

    def set_disconnect_callback(self, cb: Callable) -> None:
        self._disconnect_cb = cb

    def set_save_oauth_callback(self, cb: Callable) -> None:
        self._save_oauth_cb = cb

    def set_refresh_metadata_callback(self, cb: Callable) -> None:
        self._refresh_metadata_cb = cb

    def set_suggest_metadata_callback(self, cb: Callable) -> None:
        self._suggest_metadata_cb = cb

    def set_apply_metadata_callback(self, cb: Callable) -> None:
        self._apply_metadata_cb = cb

    def set_reject_pending_callback(self, cb: Callable) -> None:
        self._reject_pending_cb = cb

    def set_apply_runtime_settings_callback(self, cb: Callable) -> None:
        self._apply_runtime_settings_cb = cb

    def set_propose_high_risk_callback(self, cb: Callable) -> None:
        self._propose_high_risk_cb = cb

    def set_connect_current_chat_callback(self, cb: Callable) -> None:
        self._connect_current_chat_cb = cb

    def set_send_chat_callback(self, cb: Callable) -> None:
        self._send_chat_cb = cb

    def set_toggle_small_stream_callback(self, cb: Callable) -> None:
        self._toggle_small_stream_cb = cb

    def set_simulate_chat_callback(self, cb: Callable) -> None:
        self._simulate_chat_cb = cb

    def set_force_kira_callback(self, cb: Callable) -> None:
        self._force_kira_cb = cb

    def set_refresh_user_list_callback(self, cb: Callable) -> None:
        self._refresh_user_list_cb = cb

    def _dispatch_connect(self, request_write: bool) -> None:
        if self._connect_cb is not None:
            self._connect_cb(request_write)

    def _dispatch_disconnect(self) -> None:
        if self._disconnect_cb is not None:
            self._disconnect_cb()

    def _dispatch_save_oauth(self) -> None:
        if self._save_oauth_cb is not None:
            self._save_oauth_cb()

    def _dispatch_refresh_metadata(self) -> None:
        if self._refresh_metadata_cb is not None:
            self._refresh_metadata_cb()

    def _dispatch_suggest_metadata(self) -> None:
        if self._suggest_metadata_cb is not None:
            self._suggest_metadata_cb()

    def _dispatch_apply_metadata(self) -> None:
        if self._apply_metadata_cb is not None:
            self._apply_metadata_cb()

    def _dispatch_reject_pending(self) -> None:
        if self._reject_pending_cb is not None:
            self._reject_pending_cb()

    def _dispatch_apply_runtime_settings(self) -> None:
        if self._apply_runtime_settings_cb is not None:
            self._apply_runtime_settings_cb()

    def _dispatch_propose_high_risk(self, action: str) -> None:
        if self._propose_high_risk_cb is not None:
            self._propose_high_risk_cb(action)

    def _dispatch_connect_current_chat(self) -> None:
        if self._connect_current_chat_cb is not None:
            self._connect_current_chat_cb()

    def _dispatch_send_chat(self) -> None:
        if self._send_chat_cb is not None:
            self._send_chat_cb()

    def _dispatch_toggle_small_stream(self) -> None:
        if self._toggle_small_stream_cb is not None:
            self._toggle_small_stream_cb()

    def _dispatch_simulate_chat(self) -> None:
        if self._simulate_chat_cb is not None:
            self._simulate_chat_cb()

    def _dispatch_force_kira(self) -> None:
        if self._force_kira_cb is not None:
            self._force_kira_cb()

    def refresh_user_list(self) -> None:
        if self._refresh_user_list_cb is not None:
            self._refresh_user_list_cb()

    # ------------------------------------------------------------------
    # Stream admin reference management
    # ------------------------------------------------------------------

    def set_stream_admin(self, admin: Any) -> None:
        """Set the AdminManager instance after initialization."""
        self._stream_admin = admin

    def set_smart_agg(self, agg: Any) -> None:
        """Set the Aggregator instance after initialization."""
        self._smart_agg = agg

    def set_motor_ia(self, motor: Any) -> None:
        """Set the MotorVocalIA instance after initialization."""
        self._motor_ia = motor

    def set_smart_agg_defaults(self, defaults: dict[str, float]) -> None:
        """Set default activity limits for the smart aggregator."""
        self._smart_agg_default_activity = defaults

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def chat_connected(self) -> bool:
        """Whether the stream admin chat is currently connected."""
        return self._chat_connected

    @chat_connected.setter
    def chat_connected(self, value: bool) -> None:
        self._chat_connected = value

    @property
    def last_metadata(self) -> dict[str, Any]:
        """Return the last received metadata snapshot."""
        return self._last_metadata

    @property
    def sim_round(self) -> int:
        """Return the current simulation round counter."""
        return self._sim_round

    @sim_round.setter
    def sim_round(self, value: int) -> None:
        self._sim_round = value

    @property
    def chat_users(self) -> dict[str, dict[str, Any]]:
        """Return the tracked chat users dictionary."""
        return self._chat_users

    # ------------------------------------------------------------------
    # OAuth / Connection
    # ------------------------------------------------------------------

    def connect(self, request_write: bool, connect_func: Callable) -> None:
        """Connect to YouTube with optional write scope.

        Args:
            request_write: Whether to request write scopes.
            connect_func: Callable that performs the actual authentication.
        """
        self._run_task(
            "OAuth YouTube",
            lambda: connect_func(request_write),
        )

    def save_oauth_client(self, client_id: str, client_secret: str, save_func: Callable) -> None:
        """Save OAuth client credentials.

        Args:
            client_id: Google OAuth client ID.
            client_secret: Google OAuth client secret.
            save_func: Callable that saves the config.
        """
        self._run_task(
            "Guardar OAuth client",
            lambda: save_func(client_id, client_secret),
        )

    def populate_oauth_fields(self) -> None:
        """Populate OAuth entry fields from current config."""
        if not self._stream_admin:
            return
        entry_id = self._widget("entry_stream_client_id")
        if entry_id is None:
            return
        cfg = self._stream_admin.get_oauth_client_config()
        cid = cfg.get("client_id", "")
        if cid and not cid.startswith("${"):
            entry_id.delete(0, "end")
            entry_id.insert(0, cid)
        if cfg.get("has_client_secret"):
            entry_secret = self._widget("entry_stream_client_secret")
            if entry_secret:
                entry_secret.configure(placeholder_text="Secret guardado localmente; escribe uno nuevo para reemplazar")

    def disconnect(self, disconnect_func: Callable) -> None:
        """Disconnect from the current provider."""
        self._run_task("Desconectar proveedor", disconnect_func)

    def revoke_write(self) -> None:
        """Revoke write mode, returning to read-only."""
        if not self._stream_admin:
            return
        self._stream_admin.revoke_write_mode()
        self._log("[StreamAdmin] Escritura revocada manualmente. Solo lectura activo.")

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    def refresh_metadata(self, refresh_func: Callable) -> None:
        """Fetch current stream metadata."""
        self._run_task("Leer metadata", refresh_func)

    def suggest_metadata(self, context: str, suggest_func: Callable) -> None:
        """Request LLM-suggested metadata updates.

        Args:
            context: Recent chat context for the suggestion.
            suggest_func: Callable that performs the suggestion.
        """
        self._run_task("Sugerir metadata", lambda: suggest_func(context))

    def apply_metadata(self, payload: dict, force: bool = False) -> None:
        """Apply metadata changes to the stream.

        Args:
            payload: Metadata fields to apply.
            force: Whether to force-apply even with pending actions.
        """
        if not self._can_write():
            return
        if self._stream_admin and self._stream_admin.pending_action:
            action = lambda: self._stream_admin.apply_pending_action(payload, force=True)
        else:
            action = lambda: self._stream_admin.apply_metadata(payload)
        self._run_task("Aplicar metadata", action)

    def reject_pending(self, reject_func: Callable) -> None:
        """Reject the current pending action."""
        self._run_task("Rechazar acción", reject_func)

    def metadata_payload_from_ui(self) -> dict[str, Any]:
        """Build a metadata payload from widget values.

        Returns:
            Dict with ``title``, ``category_id``, ``description``, ``tags``.
        """
        tags_entry = self._widget("entry_stream_tags")
        desc_text = self._widget("text_stream_description")
        title_entry = self._widget("entry_stream_title")
        category_entry = self._widget("entry_stream_category")

        tags_raw = tags_entry.get().split(",") if tags_entry else ""
        tags = [t.strip() for t in tags_raw if t.strip()] if isinstance(tags_raw, list) else []
        description = desc_text.get("1.0", "end").strip() if desc_text else ""
        title = title_entry.get().strip() if title_entry else ""
        category = category_entry.get().strip() if category_entry else ""

        return {
            "title": title,
            "category_id": category,
            "description": description,
            "tags": tags,
        }

    def populate_metadata(self, metadata: dict[str, Any]) -> None:
        """Populate metadata fields from a metadata dict.

        Args:
            metadata: Dict with ``title``, ``category_id``, ``tags``,
                ``description``, ``video_id``, ``status``.
        """
        self._schedule_ui_update(lambda: self._populate_metadata_now(metadata))

    def _populate_metadata_now(self, metadata: dict[str, Any]) -> None:
        """Apply metadata values to widgets. Must run on the UI thread."""
        metadata = metadata or {}
        lbl = self._widget("lbl_stream_metadata_state")
        if lbl:
            vid = metadata.get("video_id", "") or "N/A"
            status = metadata.get("status", "unknown")
            color = "#44cc66" if metadata.get("video_id") else "#ffaa00"
            lbl.configure(
                text=f"Estado: {status} · Video: {vid}",
                text_color=color,
            )

        title_entry = self._widget("entry_stream_title")
        if title_entry:
            title_entry.delete(0, "end")
            title_entry.insert(0, metadata.get("title", ""))

        category_entry = self._widget("entry_stream_category")
        if category_entry:
            category_entry.delete(0, "end")
            category_entry.insert(0, metadata.get("category_id", ""))

        tags_entry = self._widget("entry_stream_tags")
        if tags_entry:
            tags_entry.delete(0, "end")
            tags_entry.insert(0, ", ".join(metadata.get("tags", []) or []))

        desc_text = self._widget("text_stream_description")
        if desc_text:
            desc_text.delete("1.0", "end")
            desc_text.insert("1.0", metadata.get("description", ""))

    # ------------------------------------------------------------------
    # Moderation
    # ------------------------------------------------------------------

    def propose_high_risk(self, action: str, user: str, reason: str, propose_func: Callable) -> None:
        """Propose a high-risk moderation action (timeout/ban).

        Args:
            action: ``timeout`` or ``ban``.
            user: Channel ID of the user.
            reason: Reason for the moderation.
            propose_func: Callable that performs the proposal.
        """
        self._run_task(
            f"Proponer {action}",
            lambda: propose_func(action, user, reason, 300),
        )

    def track_chat_user(self, message: dict[str, Any]) -> None:
        """Track a chat user for the moderation user list.

        Args:
            message: Chat message dict with user info.
        """
        channel_id = message.get("author_channel_id") or message.get("channel_id") or ""
        if not channel_id:
            return
        user = message.get("user", "YouTube")
        current = self._chat_users.get(channel_id, {})
        current.update({
            "user": user,
            "channel_id": channel_id,
            "last_message": message.get("text", ""),
            "last_seen": time.time(),
            "count": int(current.get("count", 0)) + 1,
            "is_owner": bool(message.get("is_owner", False)),
            "is_moderator": bool(message.get("is_moderator", False)),
            "is_member": bool(message.get("is_member", False)),
        })
        self._chat_users[channel_id] = current

    def default_mod_reason(self, item: dict[str, Any]) -> str:
        """Generate a default moderation reason from a user item.

        Args:
            item: User dict with ``last_message``.

        Returns:
            Default reason string.
        """
        text = (item.get("last_message") or "").strip()
        if len(text) > 80:
            text = text[:77] + "..."
        return f"Revisado desde Stream Admin: {text}" if text else "Moderación manual desde Stream Admin"

    def moderate_user_from_list(
        self,
        action: str,
        channel_id: str,
        user: str,
        reason: str,
        moderate_func: Callable,
    ) -> None:
        """Apply moderation action from the user list.

        Args:
            action: ``timeout`` or ``ban``.
            channel_id: User's channel ID.
            user: Display name.
            reason: Moderation reason.
            moderate_func: Callable that applies the moderation.
        """
        if not self._can_write():
            return
        if not channel_id:
            return
        self._run_task(
            f"Moderación {action}",
            lambda: moderate_func(action, channel_id, reason, 300),
        )

    def apply_runtime_settings(self, log: bool = True) -> None:
        """Apply current moderation/chat toggle settings to the admin.

        Args:
            log: Whether to log the settings change.
        """
        if not self._stream_admin:
            return
        mod_cfg = self._stream_admin.config.setdefault("moderation", {})
        chat_cfg = self._stream_admin.config.setdefault("chat", {})

        switch_mod = self._widget("switch_stream_mod_enabled")
        combo_mode = self._widget("combo_stream_mod_mode")
        switch_announce = self._widget("switch_stream_announce")
        switch_chat = self._widget("switch_stream_chat_enabled")

        mod_cfg["enabled"] = bool(switch_mod.get()) if switch_mod else False
        mod_cfg["mode"] = combo_mode.get() if combo_mode else "alerts_only"
        mod_cfg["announce_actions_to_chat"] = bool(switch_announce.get()) if switch_announce else False
        chat_cfg["allow_kira_chat_messages"] = bool(switch_chat.get()) if switch_chat else False

        self._stream_admin.moderation.enabled = mod_cfg["enabled"]
        self._stream_admin.moderation.mode = mod_cfg["mode"]

        if log:
            self._log(
                f"[StreamAdmin] Runtime: moderación={mod_cfg['enabled']} "
                f"modo={mod_cfg['mode']} chat={chat_cfg['allow_kira_chat_messages']}"
            )

    # ------------------------------------------------------------------
    # Chat
    # ------------------------------------------------------------------

    def send_chat(self, message: str, send_func: Callable) -> None:
        """Send a message to the stream chat.

        Args:
            message: Chat message text.
            send_func: Callable that sends the message.
        """
        if not self._can_write():
            return
        if not message:
            return
        self._run_task("Enviar mensaje al chat", lambda: send_func(message))

    def toggle_small_stream(self) -> None:
        """Toggle small stream mode (reduced Kira activity)."""
        if not self._smart_agg:
            return
        switch = self._widget("switch_stream_small")
        if switch and switch.get():
            self._smart_agg.set_activity_limits(threshold_per_second=0.2, cooldown_seconds=20.0, reset=True)
            self._smart_agg.set_spam_limits(max_messages_per_user=30)
            self._log("[StreamAdmin] Modo Stream Chico ON: Kira reaccionará con menos mensajes.")
        else:
            defaults = self._smart_agg_default_activity or {"threshold": 1.0, "cooldown": 45.0}
            self._smart_agg.set_activity_limits(
                threshold_per_second=defaults.get("threshold", 1.0),
                cooldown_seconds=defaults.get("cooldown", 45.0),
                reset=True,
            )
            self._log("[StreamAdmin] Modo Stream Chico OFF: umbrales RF3 restaurados.")

    def connect_chat(self, video_id: str, live_chat_id: str, connect_func: Callable) -> None:
        """Connect to the stream's live chat.

        Args:
            video_id: YouTube video ID.
            live_chat_id: Live chat ID.
            connect_func: Callable that starts the chat polling loop.
        """
        self._chat_connected = True
        self._seen_chat_ids = set()
        if self._smart_agg:
            self._smart_agg.start_session("youtube", video_id)
        connect_func(video_id, live_chat_id)

    def disconnect_chat(self, disconnect_func: Callable) -> None:
        """Disconnect from the live chat."""
        self._chat_connected = False
        if self._smart_agg:
            try:
                self._smart_agg.end_session()
            except Exception:
                pass
        disconnect_func()

    # ------------------------------------------------------------------
    # Analytics / State / Events
    # ------------------------------------------------------------------

    def ingest_rf3_event(self, event_type: str, payload: dict[str, Any]) -> None:
        """Ingest an RF3 event into the stream admin.

        Args:
            event_type: Event type string.
            payload: Event payload dict.
        """
        if not self._stream_admin:
            return
        try:
            self._stream_admin.ingest_rf3_event(event_type, payload)
            context = self._stream_admin.analytics_context_if_due()
            if context:
                self._inject_silent_context(context)
        except Exception as e:
            self._log(f"[StreamAdmin] No pudo consumir evento RF3 {event_type}: {e}")

    def inject_silent_context(self, context: Any) -> None:
        """Inject silent context into Motor IA history.

        Args:
            context: Context data to inject.
        """
        self._inject_silent_context(context)

    def _inject_silent_context(self, context: Any) -> None:
        """Internal: inject silent context into Motor IA."""
        try:
            if self._motor_ia and hasattr(self._motor_ia, "historial"):
                self._motor_ia.historial.append({
                    "role": "user",
                    "content": f"[Contexto administrativo silencioso RF4, no responder directamente]: {context}"
                })
                self._log("[StreamAdmin] Analíticas inyectadas como contexto silencioso para Kira.")
        except Exception as e:
            self._log(f"[StreamAdmin] No se pudo inyectar contexto RF4: {e}")

    # ------------------------------------------------------------------
    # UI state handlers
    # ------------------------------------------------------------------

    def on_state(self, state: dict[str, Any]) -> None:
        """Handle stream admin state change.

        Updates status label, OAuth pill, side status, and syncs controls.

        Args:
            state: Stream admin status dict.
        """
        def update() -> None:
            account = state.get("account") or {}
            name = account.get("title") or "sin cuenta"
            mode = state.get("mode", "read_only")
            connected = "conectado" if state.get("connected") else "desconectado"
            mode_display = mode
            if mode == "write" and self._stream_admin and getattr(self._stream_admin, "_write_activated_at", None):
                elapsed = time.time() - self._stream_admin._write_activated_at
                remaining = max(0, int((900 - elapsed) / 60))
                mode_display = f"write (~{remaining}min)"

            oauth_text = "OAuth: desconectado"
            if state.get("connected"):
                oauth_text = f"OAuth: {mode_display}" if mode == "write" else "OAuth: lectura"

            lbl = self._widget("lbl_stream_admin_status")
            if lbl:
                lbl.configure(
                    text=(
                        f"YouTube {connected} ({name}) - modo {mode_display} - "
                        f"OAuth client {'OK' if state.get('oauth_client_configured') else 'pendiente'} · Twitch placeholder"
                    ),
                    text_color="#44cc66" if state.get("connected") else "#aaaaaa",
                )

            oauth_pill = self._widget("lbl_oauth_status_pill")
            if oauth_pill:
                if state.get("connected") and mode == "write":
                    color = "#5f3a1f"
                elif state.get("connected"):
                    color = "#1f3f6f"
                else:
                    color = "#1b2633"
                oauth_pill.configure(text=oauth_text, fg_color=color)

            side_status = self._widget("lbl_oauth_side_status")
            if side_status:
                side_status.configure(
                    text=f"YouTube {connected} ({name}) - modo {mode_display}. Controles en Stream Admin.",
                    text_color="#44cc66" if state.get("connected") else "#8fa3b8",
                )

            self._sync_controls(state)

        self._schedule_ui_update(update)

    def on_metadata(self, metadata: dict[str, Any]) -> None:
        """Handle metadata update from stream admin.

        Args:
            metadata: Metadata dict from the provider.
        """
        self._last_metadata = metadata or {}
        self._schedule_ui_update(lambda: self._populate_metadata_now(metadata))

    def on_pending(self, pending: dict[str, Any]) -> None:
        """Handle pending action update.

        Args:
            pending: Pending action dict or None.
        """
        def update() -> None:
            if not pending:
                lbl = self._widget("lbl_stream_pending")
                if lbl:
                    lbl.configure(text="Acción pendiente: ninguna", text_color="#aaaaaa")
                mod_pill = self._widget("lbl_moderation_status_pill")
                if mod_pill:
                    mod_pill.configure(text="Moderación: sin pendientes", fg_color="#1b2633")
                mod_side = self._widget("lbl_moderation_side_status")
                if mod_side:
                    mod_side.configure(text="Sin acciones pendientes. Detalles en Stream Admin.", text_color="#8fa3b8")
                if self._stream_admin:
                    self._sync_controls(self._stream_admin.status())
                return

            payload = pending.get("payload", {})
            label = payload.get("title") or payload.get("action") or pending.get("type")

            lbl = self._widget("lbl_stream_pending")
            if lbl:
                lbl.configure(
                    text=f"Acción pendiente: {pending.get('type')} · {label}",
                    text_color="#ffaa00",
                )

            mod_pill = self._widget("lbl_moderation_status_pill")
            if mod_pill:
                mod_pill.configure(text="Moderación: acción pendiente", fg_color="#5f461b")

            mod_side = self._widget("lbl_moderation_side_status")
            if mod_side:
                mod_side.configure(
                    text=f"Pendiente: {pending.get('type')} · {label}",
                    text_color="#ffaa00",
                )

            if pending.get("type") == "metadata_update":
                self._populate_metadata_now({
                    "status": "suggested",
                    "title": payload.get("title", ""),
                    "category_id": payload.get("category_id", ""),
                    "description": payload.get("description", ""),
                    "tags": payload.get("tags", []),
                    "video_id": self._last_metadata.get("video_id", ""),
                })

            if self._stream_admin:
                self._sync_controls(self._stream_admin.status())

        self._schedule_ui_update(update)

    def on_analytics(self, snapshot: dict[str, Any]) -> None:
        """Handle analytics snapshot update.

        Args:
            snapshot: Analytics dict with ``viewers``, ``uptime_seconds``,
                ``chat_rate_avg``, ``last_vibe_dominant``, ``last_vibe_temperature``.
        """
        def update() -> None:
            viewers = snapshot.get("viewers")
            viewers_display = "N/A" if viewers is None else viewers
            uptime_min = snapshot.get("uptime_seconds", 0) // 60
            chat_rate = snapshot.get("chat_rate_avg", 0.0)
            vibe = snapshot.get("last_vibe_dominant", "neutral")
            temp = snapshot.get("last_vibe_temperature", 0)

            lbl = self._widget("lbl_stream_analytics")
            if lbl:
                lbl.configure(
                    text=(
                        f"Analíticas: viewers={viewers_display} · uptime={uptime_min}m · "
                        f"chat={chat_rate:.2f} msg/s · "
                        f"vibe={vibe} {temp:.0f}/100"
                    )
                )

        self._schedule_ui_update(update)

    def on_log(self, msg: str) -> None:
        """Handle log message from stream admin.

        Args:
            msg: Log message string.
        """
        clean = msg.replace("[StreamAdmin] ", "")
        self._log(clean)
        text_widget = self._widget("text_stream_admin_log")
        if text_widget:
            self._append_log(text_widget, msg)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _can_write(self) -> bool:
        """Check if write mode is active.

        Returns:
            True if stream admin has write enabled and scope active.
        """
        if not self._stream_admin:
            return False
        try:
            status = self._stream_admin.status()
        except Exception:
            return False
        return bool(status.get("write_enabled") and status.get("write_scope_active"))

    def _sync_controls(self, state: dict[str, Any]) -> None:
        """Enable/disable widgets based on current state.

        Args:
            state: Stream admin status dict.
        """
        connected = bool(state.get("connected"))
        write_ready = bool(state.get("write_enabled") and state.get("write_scope_active"))
        pending = bool(state.get("pending_action"))

        widget_states = {
            "btn_stream_disconnect": connected,
            "btn_stream_read_metadata": connected,
            "btn_stream_suggest_metadata": connected,
            "btn_stream_connect_chat": connected,
            "btn_stream_apply_metadata": write_ready,
            "btn_stream_send_chat": write_ready,
            "switch_stream_chat_enabled": write_ready,
            "switch_stream_announce": write_ready,
            "btn_stream_reject_pending": pending,
            "btn_stream_revoke_write": write_ready,
            "btn_stream_propose_timeout": write_ready,
            "btn_stream_propose_ban": write_ready,
        }

        for name, enabled in widget_states.items():
            widget = self._widget(name)
            if widget is not None:
                try:
                    widget.configure(state="normal" if enabled else "disabled")
                except Exception:
                    pass

        if not write_ready:
            switch_chat = self._widget("switch_stream_chat_enabled")
            if switch_chat:
                try:
                    switch_chat.deselect()
                except Exception:
                    pass
            switch_announce = self._widget("switch_stream_announce")
            if switch_announce:
                try:
                    switch_announce.deselect()
                except Exception:
                    pass

    def _run_task(self, action_name: str, func: Callable) -> None:
        """Run a stream admin task in a background thread.

        Args:
            action_name: Human-readable action name for logging.
            func: Callable to execute.
        """
        if not self._stream_admin:
            return

        def worker() -> None:
            try:
                result = func()
                if result is not None:
                    pass  # Debug logging available via dispatcher
            except Exception as e:
                hint = ""
                if "Falta scope de escritura" in str(e):
                    hint = " Usa 'Reconectar Escritura' y vuelve a autorizar YouTube para aplicar cambios."
                self._log(f"[StreamAdmin] {action_name} falló: {e}{hint}")

        import threading
        threading.Thread(target=worker, daemon=True).start()

    def _log(self, msg: str) -> None:
        """Send a log message via the injected callback."""
        self._on_log(msg)

    def _append_log(self, text_widget: Any, msg: str, max_lines: int = 1000) -> None:
        """Append a message to a text widget with line limiting.

        Args:
            text_widget: CTkTextbox instance.
            msg: Message to append.
            max_lines: Maximum lines to keep.
        """
        try:
            text_widget.insert("end", msg + "\n")
            lines = int(text_widget.index("end-1c").split(".")[0])
            if lines > max_lines:
                text_widget.delete("1.0", f"{lines - max_lines}.0")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """Disconnect chat and release resources."""
        self._chat_connected = False
        if self._chat_stop:
            self._chat_stop.set()
            self._chat_stop = None
