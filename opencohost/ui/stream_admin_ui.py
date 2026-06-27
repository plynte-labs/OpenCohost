"""StreamAdminUI — encapsulates Stream Admin (RF4) behavior and UI updates.

Manages OAuth connection, metadata editing, moderation, chat integration,
analytics display, and runtime settings for the YouTube/Twitch stream admin
tab.  All widget references are injected at construction time so the module
can update the UI without knowing about app.py internals.

Thread safety is provided via the injected ``UIState``.  Callbacks are
invoked through ``CallbackDispatcher`` for safe error handling.
NOTE_DEVELOPER: Only RF3 is in usage the rest is not longer in usage
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

from opencohost.ui.state import UIState
from opencohost.ui.protocols import CallbackDispatcher
from opencohost.config.settings import STREAM_ADMIN_ENABLED


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
        # Fix: audit/ui-security-perf-2026-05-17 — _chat_users, _seen_chat_ids, _chat_connected
        # are accessed from chat daemon threads AND UI main thread with zero sync.
        # Single lock prevents RuntimeError on dict iteration and torn reads.
        self._chat_lock = threading.Lock()
        self._smart_agg_default_activity: Optional[dict[str, float]] = None
        self._oauth_expanded: bool = True
        self._metadata_expanded: bool = False
        self._client_id_visible: bool = False

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
        self._connect_chat_live_cb: Optional[Callable] = None
        self._connect_chat_twitch_cb: Optional[Callable] = None
        self._threshold_preset_cb: Optional[Callable] = None
        self._cooldown_preset_cb: Optional[Callable] = None
        self._refresh_user_list_cb: Optional[Callable] = None
        self._agenda_add_topic_cb: Optional[Callable] = None
        self._agenda_enable_cb: Optional[Callable] = None
        self._agenda_soft_stop_cb: Optional[Callable] = None
        self._agenda_emergency_stop_cb: Optional[Callable] = None

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

        Creates two content frames (Emisión, Acciones) directly — sub-tab
        switching is managed at the side_panel level by app_shell.py.
        """
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)

        # Emisión content
        self.tab_stream_live = ctk.CTkFrame(parent, fg_color="transparent")
        self.tab_stream_live.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        self.tab_stream_live.grid_columnconfigure(0, weight=1)
        self.tab_stream_live.grid_rowconfigure(0, weight=1)

        # Acciones content (initially hidden)
        self.tab_stream_actions = ctk.CTkFrame(parent, fg_color="transparent")
        self.tab_stream_actions.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        self.tab_stream_actions.grid_columnconfigure(0, weight=1)
        self.tab_stream_actions.grid_rowconfigure(0, weight=1)
        self.tab_stream_actions.grid_remove()

        # Stream uses vertical scrollable containment
        live_sections = ctk.CTkScrollableFrame(self.tab_stream_live, fg_color="transparent")
        live_sections.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        live_sections.grid_columnconfigure(0, weight=1)

        actions_sections = ctk.CTkScrollableFrame(self.tab_stream_actions, fg_color="transparent")
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

        # Chat Live (RF3) is the only Acciones section exposed for the launch UI;
        # it renders directly (no collapsible). Kira Chat (RF4) + Moderación are
        # flag-gated: rendered only when STREAM_ADMIN_ENABLED, so flipping the flag
        # restores the full Acciones surface (and the widget-contract tests pass),
        # while the launch default (flag False) stays decluttered.
        chat_live_section = ctk.CTkFrame(actions_sections, fg_color="transparent")
        chat_live_section.grid(row=0, column=0, sticky="ew", padx=0, pady=0)
        chat_live_section.grid_columnconfigure(0, weight=1)
        self._build_chat_live_tab(chat_live_section)

        if STREAM_ADMIN_ENABLED:
            chat_section = ctk.CTkFrame(actions_sections, fg_color="transparent")
            chat_section.grid(row=1, column=0, sticky="ew", padx=0, pady=0)
            chat_section.grid_columnconfigure(0, weight=1)
            self._build_chat_tab(chat_section)

            moderation_section = ctk.CTkFrame(actions_sections, fg_color="transparent")
            moderation_section.grid(row=2, column=0, sticky="ew", padx=0, pady=0)
            moderation_section.grid_columnconfigure(0, weight=1)
            self._build_moderation_tab(moderation_section)

        return parent

    def _toggle_oauth_section(self, content: Any, button: Any) -> None:
        self._oauth_expanded = not self._oauth_expanded
        if self._oauth_expanded:
            content.grid()
            button.configure(text="▼ OAuth / Proveedor")
        else:
            content.grid_remove()
            button.configure(text="▶ OAuth / Proveedor")

    def _toggle_metadata_section(self, content: Any, button: Any) -> None:
        self._metadata_expanded = not self._metadata_expanded
        if self._metadata_expanded:
            content.grid()
            button.configure(text="▼ Metadata")
        else:
            content.grid_remove()
            button.configure(text="▶ Metadata")

    def _toggle_client_id_visibility(self, entry: Any, button: Any) -> None:
        self._client_id_visible = not self._client_id_visible
        entry.configure(show="" if self._client_id_visible else "*")
        button.configure(text="👁" if not self._client_id_visible else "🔒")

    def _build_connection_tab(self, tab: Any) -> None:
        frame_auth = ctk.CTkFrame(tab, fg_color="#151d26", corner_radius=14)
        frame_auth.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        frame_auth.grid_columnconfigure(0, weight=1)

        # Toggle header
        header = ctk.CTkFrame(frame_auth, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 4))
        header.grid_columnconfigure(0, weight=1)

        self._oauth_expanded = True
        btn_toggle_oauth = ctk.CTkButton(
            header, text="▼ OAuth / Proveedor",
            anchor="w",
            fg_color="transparent", text_color="#d8e2ef",
            hover_color="#1d2a38",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        btn_toggle_oauth.grid(row=0, column=0, sticky="w")

        lbl_status = ctk.CTkLabel(header, text="RF4 iniciando...", text_color="#aaaaaa", anchor="w", justify="left", wraplength=520)
        lbl_status.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self._widgets["lbl_stream_admin_status"] = lbl_status

        # Content frame (collapsible)
        oauth_content = ctk.CTkFrame(frame_auth, fg_color="#101923", corner_radius=12)
        oauth_content.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))
        oauth_content.grid_columnconfigure(0, weight=1)

        # Wire toggle — collapse content on click
        btn_toggle_oauth.configure(
            command=lambda: self._toggle_oauth_section(oauth_content, btn_toggle_oauth)
        )

        # BUTTON STACK goes inside oauth_content
        button_stack = ctk.CTkFrame(oauth_content, fg_color="transparent")
        button_stack.grid(row=0, column=0, sticky="ew", padx=4, pady=4)
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

        btn_twitch = ctk.CTkButton(button_stack, text="Twitch", command=lambda: self._dispatch_connect_chat_twitch(), width=145, fg_color="#2f5f8f", hover_color="#3670aa")
        btn_twitch.grid(row=2, column=0, columnspan=2, padx=4, pady=4, sticky="ew")
        self._widgets["btn_stream_twitch"] = btn_twitch

        # Client ID row with show/hide toggle
        client_id_header = ctk.CTkFrame(oauth_content, fg_color="transparent")
        client_id_header.grid(row=1, column=0, sticky="ew", padx=10, pady=(8, 2))
        client_id_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(client_id_header, text="Client ID:", anchor="w").grid(row=0, column=0, sticky="w")

        entry_id = ctk.CTkEntry(oauth_content, placeholder_text="tu_client_id.apps.googleusercontent.com", show="*")
        entry_id.grid(row=2, column=0, padx=10, pady=2, sticky="ew")
        self._widgets["entry_stream_client_id"] = entry_id

        self._client_id_visible = False
        btn_toggle_id = ctk.CTkButton(
            client_id_header, text="👁",
            width=30, height=24,
            fg_color="transparent", text_color="#8fa3b8",
            hover_color="#1d2a38", font=ctk.CTkFont(size=12),
            command=lambda: self._toggle_client_id_visibility(entry_id, btn_toggle_id),
        )
        btn_toggle_id.grid(row=0, column=1, sticky="e")

        ctk.CTkLabel(oauth_content, text="Secret:", anchor="w").grid(row=3, column=0, padx=10, pady=(8, 2), sticky="ew")
        entry_secret = ctk.CTkEntry(oauth_content, placeholder_text="OAuth client secret", show="*")
        entry_secret.grid(row=4, column=0, padx=10, pady=2, sticky="ew")
        self._widgets["entry_stream_client_secret"] = entry_secret

        ctk.CTkButton(oauth_content, text="Guardar OAuth", command=self._dispatch_save_oauth, width=125, fg_color="#555555", hover_color="#666666").grid(row=5, column=0, padx=10, pady=(8, 6), sticky="ew")

        lbl_analytics = ctk.CTkLabel(oauth_content, text="Analíticas: esperando datos...", anchor="w", justify="left", text_color="#8fa3b8", wraplength=520)
        lbl_analytics.grid(row=6, column=0, padx=10, pady=(2, 2), sticky="ew")
        self._widgets["lbl_stream_analytics"] = lbl_analytics

        lbl_pending = ctk.CTkLabel(oauth_content, text="Acción pendiente: ninguna", anchor="w", justify="left", text_color="#8fa3b8", wraplength=520)
        lbl_pending.grid(row=7, column=0, padx=10, pady=(2, 10), sticky="ew")
        self._widgets["lbl_stream_pending"] = lbl_pending

    def _build_metadata_tab(self, tab: Any) -> None:
        # RF4 LEGACY: hidden when STREAM_ADMIN_ENABLED=False (see settings.py comment).
        if not STREAM_ADMIN_ENABLED:
            return
        frame_meta = ctk.CTkFrame(tab, fg_color="#151d26", corner_radius=14)
        frame_meta.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        frame_meta.grid_columnconfigure(0, weight=1)

        # Toggle header
        self._metadata_expanded = False
        btn_toggle_meta = ctk.CTkButton(
            frame_meta, text="▶ Metadata",
            anchor="w",
            fg_color="transparent", text_color="#d8e2ef",
            hover_color="#1d2a38",
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        btn_toggle_meta.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 2))

        lbl_meta = ctk.CTkLabel(frame_meta, text="Sin metadata", text_color="#aaaaaa", anchor="w", justify="left", wraplength=520)
        lbl_meta.grid(row=1, column=0, padx=10, pady=(0, 6), sticky="ew")
        self._widgets["lbl_stream_metadata_state"] = lbl_meta

        # Content frame (initially hidden)
        meta_content = ctk.CTkFrame(frame_meta, fg_color="#101923", corner_radius=12)
        meta_content.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
        meta_content.grid_remove()
        meta_content.grid_columnconfigure(0, weight=1)

        btn_toggle_meta.configure(
            command=lambda: self._toggle_metadata_section(meta_content, btn_toggle_meta)
        )

        meta_actions = ctk.CTkFrame(meta_content, fg_color="transparent")
        meta_actions.grid(row=0, column=0, sticky="ew", padx=10, pady=4)
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

        ctk.CTkLabel(meta_content, text="Título:", anchor="w").grid(row=1, column=0, padx=10, pady=(8, 2), sticky="ew")
        entry_title = ctk.CTkEntry(meta_content, placeholder_text="Título del stream")
        entry_title.grid(row=2, column=0, padx=10, pady=2, sticky="ew")
        self._widgets["entry_stream_title"] = entry_title

        ctk.CTkLabel(meta_content, text="Categoría:", anchor="w").grid(row=3, column=0, padx=10, pady=(8, 2), sticky="ew")
        entry_cat = ctk.CTkEntry(meta_content, placeholder_text="ID categoría YouTube, ej. 20")
        entry_cat.grid(row=4, column=0, padx=10, pady=2, sticky="ew")
        self._widgets["entry_stream_category"] = entry_cat

        ctk.CTkLabel(meta_content, text="Tags:", anchor="w").grid(row=5, column=0, padx=10, pady=(8, 2), sticky="ew")
        entry_tags = ctk.CTkEntry(meta_content, placeholder_text="tag1, tag2, tag3")
        entry_tags.grid(row=6, column=0, padx=10, pady=2, sticky="ew")
        self._widgets["entry_stream_tags"] = entry_tags

        ctk.CTkLabel(meta_content, text="Descripción:", anchor="w").grid(row=7, column=0, padx=10, pady=(8, 2), sticky="ew")
        text_desc = ctk.CTkTextbox(meta_content, height=70)
        text_desc.grid(row=8, column=0, padx=10, pady=(2, 10), sticky="ew")
        self._widgets["text_stream_description"] = text_desc

    def _build_moderation_tab(self, tab: Any) -> None:
        # RF4 LEGACY: hidden when STREAM_ADMIN_ENABLED=False (see settings.py comment).
        if not STREAM_ADMIN_ENABLED:
            return
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
        # RF4 LEGACY: Kira Chat panel hidden when STREAM_ADMIN_ENABLED=False.
        # RF3 Chat Live (_build_chat_live_tab) is NOT gated — it remains reachable.
        # See settings.py STREAM_ADMIN_ENABLED comment for full rationale.
        if not STREAM_ADMIN_ENABLED:
            return
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

    def _build_chat_live_tab(self, tab: Any) -> None:
        frame_live = ctk.CTkFrame(tab, fg_color="#151d26", corner_radius=14)
        frame_live.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        frame_live.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(frame_live, text="Chat Live", font=ctk.CTkFont(size=14, weight="bold"), anchor="w").grid(row=0, column=0, padx=10, pady=(10, 4), sticky="ew")

        entry_url = ctk.CTkEntry(frame_live, placeholder_text="URL del directo (YouTube o Twitch)")
        entry_url.grid(row=1, column=0, padx=10, pady=4, sticky="ew")
        self._widgets["entry_stream_chat_url"] = entry_url

        btn_row = ctk.CTkFrame(frame_live, fg_color="transparent")
        btn_row.grid(row=2, column=0, sticky="ew", padx=10, pady=4)
        btn_row.grid_columnconfigure(0, weight=1)
        btn_connect = ctk.CTkButton(btn_row, text="Conectar Chat Live", command=self._dispatch_connect_chat_live, fg_color="#2f5f8f")
        btn_connect.grid(row=0, column=0, padx=(0, 4), sticky="ew")
        self._widgets["btn_stream_chat_live_connect"] = btn_connect

        lbl_status = ctk.CTkLabel(btn_row, text="", font=ctk.CTkFont(size=12), text_color="#aaaaaa", anchor="w")
        lbl_status.grid(row=0, column=1, padx=(4, 0), sticky="w")
        self._widgets["lbl_stream_chat_live_status"] = lbl_status

        tune_row = ctk.CTkFrame(frame_live, fg_color="transparent")
        tune_row.grid(row=3, column=0, sticky="ew", padx=10, pady=(8, 2))
        ctk.CTkLabel(tune_row, text="Reacciona si el chat supera", font=ctk.CTkFont(size=11), text_color="#8fa3b8").pack(side="left", padx=(0, 4))
        entry_threshold = ctk.CTkEntry(tune_row, width=50)
        entry_threshold.insert(0, "1.0")
        entry_threshold.pack(side="left", padx=2)
        self._widgets["entry_stream_chat_threshold"] = entry_threshold
        # Auto-apply on focus loss or Enter
        entry_threshold.bind("<FocusOut>", lambda e: self._apply_threshold_from_entry())
        entry_threshold.bind("<Return>", lambda e: self._apply_threshold_from_entry())
        ctk.CTkLabel(tune_row, text="msg/s", font=ctk.CTkFont(size=11), text_color="#8fa3b8").pack(side="left", padx=(3, 4))
        for label, value in (("0.5", "0.5"), ("1", "1.0"), ("3", "3.0")):
            btn = ctk.CTkButton(
                tune_row, text=label, width=36, height=24,
                fg_color="#3a5f3f", hover_color="#4a7f4f",
                command=lambda v=value: self._dispatch_threshold_preset(v),
            )
            btn.pack(side="left", padx=1)

        cooldown_row = ctk.CTkFrame(frame_live, fg_color="transparent")
        cooldown_row.grid(row=4, column=0, sticky="ew", padx=10, pady=2)
        ctk.CTkLabel(cooldown_row, text="Esperar al menos", font=ctk.CTkFont(size=11), text_color="#8fa3b8").pack(side="left", padx=(0, 4))
        entry_cooldown = ctk.CTkEntry(cooldown_row, width=50)
        entry_cooldown.insert(0, "45")
        entry_cooldown.pack(side="left", padx=2)
        self._widgets["entry_stream_chat_cooldown"] = entry_cooldown
        # Auto-apply on focus loss or Enter
        entry_cooldown.bind("<FocusOut>", lambda e: self._apply_cooldown_from_entry())
        entry_cooldown.bind("<Return>", lambda e: self._apply_cooldown_from_entry())
        ctk.CTkLabel(cooldown_row, text="s entre reacciones", font=ctk.CTkFont(size=11), text_color="#8fa3b8").pack(side="left", padx=(3, 4))
        for label, value in (("30", "30"), ("60", "60"), ("120", "120")):
            btn = ctk.CTkButton(
                cooldown_row, text=label, width=36, height=24,
                fg_color="#5f4a3a", hover_color="#7f6a4a",
                command=lambda v=value: self._dispatch_cooldown_preset(v),
            )
            btn.pack(side="left", padx=1)

        spam_row = ctk.CTkFrame(frame_live, fg_color="transparent")
        spam_row.grid(row=5, column=0, sticky="ew", padx=10, pady=(2, 10))
        ctk.CTkLabel(spam_row, text="Máximo", font=ctk.CTkFont(size=11), text_color="#8fa3b8").pack(side="left", padx=(0, 4))
        entry_spam = ctk.CTkEntry(spam_row, width=50)
        entry_spam.insert(0, "10")
        entry_spam.pack(side="left", padx=2)
        self._widgets["entry_stream_chat_spam"] = entry_spam
        ctk.CTkLabel(spam_row, text="msgs/usuario en 30s", font=ctk.CTkFont(size=11), text_color="#8fa3b8").pack(side="left", padx=(3, 4))

        # ── Input Contract toggle ──────────────────────────────────────
        input_row = ctk.CTkFrame(frame_live, fg_color="transparent")
        input_row.grid(row=6, column=0, sticky="ew", padx=10, pady=(2, 10))
        switch_input = ctk.CTkSwitch(
            input_row,
            text="🧠 Input Contract (contexto real)",
            command=self._toggle_input_contract,
            onvalue=True,
            offvalue=False,
        )
        switch_input.pack(side="left", padx=(0, 4))
        self._widgets["switch_input_contract"] = switch_input
        # Sync with current flag state
        try:
            from opencohost.smart_aggregator.chat_input_contract import USE_INPUT_CONTRACT_PROMPT
            if USE_INPUT_CONTRACT_PROMPT:
                switch_input.select()
            else:
                switch_input.deselect()
        except Exception:
            switch_input.deselect()

    def _toggle_input_contract(self) -> None:
        """Toggle the USE_INPUT_CONTRACT_PROMPT flag from UI."""
        import opencohost.smart_aggregator.chat_input_contract as ic
        switch = self._widgets.get("switch_input_contract")
        if switch is None:
            return
        enabled = switch.get()
        ic.USE_INPUT_CONTRACT_PROMPT = bool(enabled)
        state = "ON" if enabled else "OFF"
        self._log(f"[StreamAdmin] Input Contract: {state} (ChatContextPacket como fuente de contexto)")

    @staticmethod
    def sanitize_live_url(value: str) -> str:
        """Extract a video ID from YouTube/Twitch URLs; reject invalid input."""
        import re
        value = value.strip()
        if not value:
            return ""
        lowered = value.lower()
        for domain in ("facebook.com", "dlive.tv", "vk.com", "tiktok.com", "kick.com", "rumble.com"):
            if domain in lowered:
                return ""
        yt_patterns = [
            r"(?:https?://)?(?:www\.)?youtube\.com/watch\?v=([\w-]{11})",
            r"(?:https?://)?(?:www\.)?youtube\.com/live/([\w-]{11})",
            r"(?:https?://)?youtu\.be/([\w-]{11})",
        ]
        for pattern in yt_patterns:
            match = re.search(pattern, value, re.IGNORECASE)
            if match:
                return match.group(1)
        if re.match(r"^[\w-]{11}$", value):
            return value
        twitch_match = re.match(r"(?:https?://)?(?:www\.)?twitch\.tv/(\w+)", value, re.IGNORECASE)
        if twitch_match:
            return twitch_match.group(1)
        return ""

    def _build_agenda_tab(self, tab: Any) -> None:
        frame_agenda = ctk.CTkFrame(tab, fg_color="#151d26", corner_radius=14)
        frame_agenda.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        frame_agenda.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            frame_agenda,
            text="Kira Co-host Agenda",
            font=ctk.CTkFont(size=14, weight="bold"),
            anchor="w",
        ).grid(row=0, column=0, padx=10, pady=(10, 2), sticky="ew")

        lbl_state = ctk.CTkLabel(
            frame_agenda,
            text="Agenda apagada. Kira espera temas aprobados.",
            text_color="#8fa3b8",
            anchor="w",
            justify="left",
            wraplength=520,
        )
        lbl_state.grid(row=1, column=0, padx=10, pady=(0, 6), sticky="ew")
        self._widgets["lbl_kira_agenda_state"] = lbl_state

        entry_title = ctk.CTkEntry(frame_agenda, placeholder_text="Tema aprobado para Kira")
        entry_title.grid(row=2, column=0, padx=10, pady=(4, 4), sticky="ew")
        self._widgets["entry_kira_agenda_title"] = entry_title

        entry_angle = ctk.CTkEntry(frame_agenda, placeholder_text="Ángulo breve: cómo debe tratarlo")
        entry_angle.grid(row=3, column=0, padx=10, pady=4, sticky="ew")
        self._widgets["entry_kira_agenda_angle"] = entry_angle

        entry_constraints = ctk.CTkEntry(frame_agenda, placeholder_text="Restricciones separadas por ;")
        entry_constraints.grid(row=4, column=0, padx=10, pady=4, sticky="ew")
        self._widgets["entry_kira_agenda_constraints"] = entry_constraints

        actions = ctk.CTkFrame(frame_agenda, fg_color="transparent")
        actions.grid(row=5, column=0, sticky="ew", padx=10, pady=(4, 10))
        for col in range(4):
            actions.grid_columnconfigure(col, weight=1)

        btn_add = ctk.CTkButton(actions, text="Agregar tema", command=self._dispatch_agenda_add_topic, width=110, fg_color="#2f5f8f")
        btn_add.grid(row=0, column=0, padx=4, pady=4, sticky="ew")
        self._widgets["btn_kira_agenda_add_topic"] = btn_add

        btn_enable = ctk.CTkButton(actions, text="Activar agenda", command=self._dispatch_agenda_enable, width=110, fg_color="#2f7d50")
        btn_enable.grid(row=0, column=1, padx=4, pady=4, sticky="ew")
        self._widgets["btn_kira_agenda_enable"] = btn_enable

        btn_soft_stop = ctk.CTkButton(actions, text="Stop suave", command=self._dispatch_agenda_soft_stop, width=100, fg_color="#7d5a2a")
        btn_soft_stop.grid(row=0, column=2, padx=4, pady=4, sticky="ew")
        self._widgets["btn_kira_agenda_soft_stop"] = btn_soft_stop

        btn_emergency = ctk.CTkButton(actions, text="Emergencia", command=self._dispatch_agenda_emergency_stop, width=100, fg_color="#8f2f2f")
        btn_emergency.grid(row=0, column=3, padx=4, pady=4, sticky="ew")
        self._widgets["btn_kira_agenda_emergency_stop"] = btn_emergency

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

    def set_connect_chat_live_callback(self, cb: Callable) -> None:
        self._connect_chat_live_cb = cb

    def set_connect_chat_twitch_callback(self, cb: Callable) -> None:
        self._connect_chat_twitch_cb = cb

    def set_threshold_preset_callback(self, cb: Callable) -> None:
        self._threshold_preset_cb = cb

    def set_cooldown_preset_callback(self, cb: Callable) -> None:
        self._cooldown_preset_cb = cb

    def set_refresh_user_list_callback(self, cb: Callable) -> None:
        self._refresh_user_list_cb = cb

    def set_agenda_add_topic_callback(self, cb: Callable) -> None:
        self._agenda_add_topic_cb = cb

    def set_agenda_enable_callback(self, cb: Callable) -> None:
        self._agenda_enable_cb = cb

    def set_agenda_soft_stop_callback(self, cb: Callable) -> None:
        self._agenda_soft_stop_cb = cb

    def set_agenda_emergency_stop_callback(self, cb: Callable) -> None:
        self._agenda_emergency_stop_cb = cb

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

    def _dispatch_connect_chat_live(self) -> None:
        if self._connect_chat_live_cb is not None:
            self._connect_chat_live_cb()

    def _dispatch_connect_chat_twitch(self) -> None:
        if self._connect_chat_twitch_cb is not None:
            self._connect_chat_twitch_cb()

    def _dispatch_threshold_preset(self, value: str) -> None:
        entry = self._widget("entry_stream_chat_threshold")
        if entry:
            entry.delete(0, "end")
            entry.insert(0, value)
        if self._threshold_preset_cb is not None:
            self._threshold_preset_cb(value)

    def _dispatch_cooldown_preset(self, value: str) -> None:
        entry = self._widget("entry_stream_chat_cooldown")
        if entry:
            entry.delete(0, "end")
            entry.insert(0, value)
        if self._cooldown_preset_cb is not None:
            self._cooldown_preset_cb(value)

    def _apply_threshold_from_entry(self) -> None:
        """Apply the current threshold text value immediately."""
        entry = self._widget("entry_stream_chat_threshold")
        if entry is None:
            return
        value = entry.get().strip()
        if value:
            self._dispatch_threshold_preset(value)

    def _apply_cooldown_from_entry(self) -> None:
        """Apply the current cooldown text value immediately."""
        entry = self._widget("entry_stream_chat_cooldown")
        if entry is None:
            return
        value = entry.get().strip()
        if value:
            self._dispatch_cooldown_preset(value)

    def refresh_user_list(self) -> None:
        if self._refresh_user_list_cb is not None:
            self._refresh_user_list_cb()

    def _dispatch_agenda_add_topic(self) -> None:
        if self._agenda_add_topic_cb is None:
            return
        title = (self._widget("entry_kira_agenda_title").get() if self._widget("entry_kira_agenda_title") else "").strip()
        angle = (self._widget("entry_kira_agenda_angle").get() if self._widget("entry_kira_agenda_angle") else "").strip()
        constraints_raw = (self._widget("entry_kira_agenda_constraints").get() if self._widget("entry_kira_agenda_constraints") else "").strip()
        constraints = [part.strip() for part in constraints_raw.split(";") if part.strip()]
        self._agenda_add_topic_cb(title, angle, constraints)

    def _dispatch_agenda_enable(self) -> None:
        if self._agenda_enable_cb is not None:
            self._agenda_enable_cb()

    def _dispatch_agenda_soft_stop(self) -> None:
        if self._agenda_soft_stop_cb is not None:
            self._agenda_soft_stop_cb()

    def _dispatch_agenda_emergency_stop(self) -> None:
        if self._agenda_emergency_stop_cb is not None:
            self._agenda_emergency_stop_cb()

    def set_agenda_status(self, text: str, color: str = "#8fa3b8") -> None:
        label = self._widget("lbl_kira_agenda_state")
        if label is not None:
            label.configure(text=text, text_color=color)

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
        # Fix: audit/ui-security-perf-2026-05-17 — lock prevents torn read
        # when chat daemon thread mutates _chat_connected concurrently.
        with self._chat_lock:
            return self._chat_connected

    @chat_connected.setter
    def chat_connected(self, value: bool) -> None:
        # Fix: audit/ui-security-perf-2026-05-17 — lock protects write
        # against concurrent reads from polling thread.
        with self._chat_lock:
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
        # Fix: audit/ui-security-perf-2026-05-17 — return shallow copy.
        # Callers must NOT mutate the original dict directly; use track_chat_user()
        # (which also locks) for mutations. Copy prevents RuntimeError if the
        # chat daemon modifies _chat_users during iteration.
        with self._chat_lock:
            return dict(self._chat_users)

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
        # Fix: audit/ui-security-perf-2026-05-17 — lock protects _chat_users
        # against concurrent reads from UI thread (chat_users property, refresh).
        with self._chat_lock:
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

            # Fix: audit/ui-security-perf-2026-05-17 — cap _chat_users to 1000 entries
            # to prevent unbounded memory growth over long streaming sessions.
            # Python 3.7+ dict preserves insertion order — evict oldest first.
            if len(self._chat_users) > 1000:
                excess = len(self._chat_users) - 1000
                for key in list(self._chat_users.keys())[:excess]:
                    del self._chat_users[key]

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
        # Fix: audit/ui-security-perf-2026-05-17 — lock shared state mutation
        # so polling daemon thread sees consistent _seen_chat_ids + _chat_connected.
        with self._chat_lock:
            self._chat_connected = True
            self._seen_chat_ids = set()
        if self._smart_agg:
            self._smart_agg.start_session("youtube", video_id)
        connect_func(video_id, live_chat_id)

    def disconnect_chat(self, disconnect_func: Callable) -> None:
        """Disconnect from the live chat."""
        # Fix: audit/ui-security-perf-2026-05-17 — lock shared state mutation
        # so polling thread doesn't see inconsistent _chat_connected.
        with self._chat_lock:
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
        # Fix: audit/ui-security-perf-2026-05-17 — lock prevents torn read
        # if polling daemon thread is still accessing _chat_connected during shutdown.
        with self._chat_lock:
            self._chat_connected = False
        if self._chat_stop:
            self._chat_stop.set()
            self._chat_stop = None
