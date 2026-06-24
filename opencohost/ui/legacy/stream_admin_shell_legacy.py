"""Preserved RF4 stream-admin shell callbacks — disabled product surface
(STREAM_ADMIN_ENABLED=False). Parked for revival; owned by
stream_admin_legacy_removal_20260614. Not wired, not inherited, never instantiated.

Moved from opencohost/ui/app_shell.py during ui_rendering_optimization_20260609
Phase 6 Stage 2a (2026-06-24). Contains the RF4-dead VocalAIApp methods verbatim
(self.X references intact — never executed). Must pass compileall.
"""
from __future__ import annotations

import os
import threading
import time
import tkinter.messagebox as messagebox
from typing import Any

import customtkinter as ctk

from opencohost.config.logger import get_logger
from opencohost.config.settings import PACKAGE_CONFIG_DIR
from opencohost.stream_admin import AdminManager  # noqa: F401 — needed for compile-clean
from opencohost.ui.smart_aggregator_ui import SmartAggregatorUI

logger = get_logger()


def _stream_admin_should_process_chat_message(stream_admin_ui: Any, msg_id: Any) -> bool:
    if not msg_id:
        return True
    with stream_admin_ui._chat_lock:
        if msg_id in stream_admin_ui._seen_chat_ids:
            return False
        stream_admin_ui._seen_chat_ids.add(msg_id)
        if len(stream_admin_ui._seen_chat_ids) > 2000:
            stream_admin_ui._seen_chat_ids = set(list(stream_admin_ui._seen_chat_ids)[-1000:])
    return True


class StreamAdminShellLegacy:
    """Parked RF4 shell methods from VocalAIApp.

    This class is NEVER instantiated and NEVER inherited by VocalAIApp.
    It exists solely for Feature Preservation Rule compliance (revivable surface).
    All self.X references are intact and verbatim — never executed.

    Owner: stream_admin_legacy_removal_20260614
    Moved: 2026-06-24 (ui_rendering_optimization_20260609 Phase 6 Stage 2a)
    """

    def _init_stream_admin(self) -> None:
        try:
            config_path = os.path.join(PACKAGE_CONFIG_DIR, "stream_admin.yaml")
            self.stream_admin = AdminManager(config_path=config_path, llm_interface=self.smart_agg_ui.llm_interface)
            self.stream_admin.on_log = self._on_stream_admin_log
            self.stream_admin.on_state = self._on_stream_admin_state
            self.stream_admin.on_metadata = self._on_stream_admin_metadata
            self.stream_admin.on_pending_action = self._on_stream_admin_pending
            self.stream_admin.on_analytics = self._on_stream_admin_analytics
            self.stream_admin_ui.set_stream_admin(self.stream_admin)
            self._stream_admin_apply_runtime_settings(log=False)
            self._populate_stream_oauth_client_fields()
            self._on_stream_admin_state(self.stream_admin.status())
            self._on_stream_admin_log("[StreamAdmin] RF4 listo. YouTube read-only disponible; Twitch en placeholder.")
        except Exception as e:
            self.stream_admin = None
            logger.exception("No se pudo inicializar Stream Admin")
            self.log_queue.put(f"[StreamAdmin] No disponible: {e}")

    def _populate_stream_oauth_client_fields(self) -> None:
        if not self.stream_admin:
            return
        cfg = self.stream_admin.get_oauth_client_config()
        client_id = cfg.get("client_id", "")
        entry_id = self.stream_admin_ui._widget("entry_stream_client_id")
        entry_secret = self.stream_admin_ui._widget("entry_stream_client_secret")
        if entry_id and client_id and not client_id.startswith("${"):
            entry_id.delete(0, "end")
            entry_id.insert(0, client_id)
        if entry_secret and cfg.get("has_client_secret"):
            entry_secret.configure(placeholder_text="Secret guardado localmente; escribe uno nuevo para reemplazar")

    def _run_stream_admin_task(self, action_name: str, func) -> None:
        if not self.stream_admin:
            self._notify_operator("Stream Admin", "RF4 no inicializado. Revisa config/stream_admin.yaml.")
            return

        def worker():
            try:
                result = func()
                if result is not None:
                    logger.debug(f"StreamAdmin {action_name}: {result}")
            except Exception as e:
                logger.exception(f"StreamAdmin fallo en {action_name}")
                hint = ""
                if "Falta scope de escritura" in str(e):
                    hint = " Usa 'Reconectar Escritura' y vuelve a autorizar YouTube para aplicar cambios."
                try:
                    self._safe_after(lambda err=e: self._notify_operator("Stream Admin", str(err), level="error"))
                except Exception:
                    self._notify_operator("Stream Admin", str(e), level="error")
                self._on_stream_admin_log(f"[StreamAdmin] {action_name} falló: {e}{hint}")

        threading.Thread(target=worker, daemon=True).start()

    def _stream_admin_connect(self, request_write: bool) -> None:
        self._run_stream_admin_task("OAuth YouTube", lambda: self.stream_admin.authenticate("youtube", request_write_scopes=request_write))

    def _stream_admin_save_oauth_client(self) -> None:
        entry_id = self.stream_admin_ui._widget("entry_stream_client_id")
        entry_secret = self.stream_admin_ui._widget("entry_stream_client_secret")
        client_id = entry_id.get().strip() if entry_id else ""
        client_secret = entry_secret.get().strip() if entry_secret else ""
        self._run_stream_admin_task("Guardar OAuth client", lambda: self.stream_admin.save_oauth_client_config(client_id, client_secret))
        if entry_secret:
            entry_secret.delete(0, "end")

    def _stream_admin_disconnect(self) -> None:
        self._run_stream_admin_task("Desconectar proveedor", self.stream_admin.disconnect)

    def _stream_admin_revoke_write(self) -> None:
        if not self.stream_admin:
            return
        self.stream_admin.revoke_write_mode()
        self._on_stream_admin_log("[StreamAdmin] Escritura revocada manualmente. Solo lectura activo.")

    def _stream_admin_refresh_metadata(self) -> None:
        self._run_stream_admin_task("Leer metadata", self.stream_admin.refresh_metadata)

    def _stream_admin_suggest_metadata(self) -> None:
        context = ""
        if self.smart_agg and getattr(self.smart_agg, "_session_id", None):
            try:
                intent_summary = self.smart_agg.intent_aggregator.summarize()
                prompt = intent_summary.get("prompt", "")
                if prompt and prompt != "No hay un tema dominante claro en el chat filtrado.":
                    context = prompt
                if not context:
                    snapshots = self.smart_agg.history.get_recent_context_snapshots(self.smart_agg._session_id, max_items=3)
                    context = "\n\n".join(s.get("summary", "") for s in snapshots)
            except Exception:
                context = ""
        self._run_stream_admin_task("Sugerir metadata", lambda: self.stream_admin.suggest_metadata(context))

    def _stream_admin_apply_metadata(self) -> None:
        if not self._stream_admin_can_write():
            self._notify_operator("Stream Admin", "Modo solo lectura activo. Usa 'Reconectar Escritura' antes de aplicar cambios.")
            return
        payload = self.stream_admin_ui.metadata_payload_from_ui()
        if self.stream_admin and self.stream_admin.pending_action:
            action = lambda: self.stream_admin.apply_pending_action(payload, force=True)
        else:
            action = lambda: self.stream_admin.apply_metadata(payload)
        self._run_stream_admin_task("Aplicar metadata", action)

    def _stream_admin_reject_pending(self) -> None:
        self._run_stream_admin_task("Rechazar acción", self.stream_admin.reject_pending_action)

    def _stream_admin_connect_current_chat(self) -> None:
        metadata = self.stream_admin_ui.last_metadata or {}
        video_id = metadata.get("video_id")
        live_chat_id = metadata.get("live_chat_id")
        if not video_id and self.stream_admin:
            video_id = getattr(getattr(self.stream_admin, "metadata", None), "video_id", "")
            live_chat_id = getattr(getattr(self.stream_admin, "metadata", None), "live_chat_id", "")
        if not video_id:
            self._notify_operator("Stream Admin", "Primero usa 'Leer' para detectar el live activo.")
            return

        if self.stream_admin_ui.chat_connected:
            self._stream_admin_disconnect_api_chat()
            return

        if live_chat_id and self.stream_admin:
            self._stream_admin_connect_api_chat(video_id, live_chat_id)
            return

        if self._ui_state.smart_agg_connected or self._ui_state.smart_agg_connecting:
            current = SmartAggregatorUI.extract_youtube_video_id(self.entry_youtube_video.get())
            if current == video_id:
                self._on_stream_admin_log(f"[StreamAdmin] Chat ya conectado al live {video_id}.")
                return
            self._notify_operator("Stream Admin", "Ya hay un chat conectado. Desconéctalo antes de cambiar de live.")
            return

        entry_video = self.stream_admin_ui._widget("entry_stream_chat_message")
        self.entry_youtube_video.delete(0, "end")
        self.entry_youtube_video.insert(0, video_id)
        self._on_stream_admin_log(f"[StreamAdmin] Conectando RF3 al chat del live {video_id}.")
        self.smart_agg_ui.toggle_connection()

    def _stream_admin_connect_api_chat(self, video_id: str, live_chat_id: str) -> None:
        if not self.smart_agg:
            self._notify_operator("Stream Admin", "Smart Aggregator no inicializado.")
            return
        if self._ui_state.smart_agg_connected or self._ui_state.smart_agg_connecting:
            self._notify_operator("Stream Admin", "Ya hay un chat RF3 conectado. Desconéctalo antes de usar chat autenticado.")
            return

        self.stream_admin_ui.chat_connected = True
        self.stream_admin_ui._chat_stop = threading.Event()
        self.stream_admin_ui._seen_chat_ids = set()
        self.smart_agg.start_session("youtube", video_id)
        self.entry_youtube_video.delete(0, "end")
        self.entry_youtube_video.insert(0, video_id)

        btn = self.stream_admin_ui._widget("btn_stream_connect_chat")
        if btn:
            btn.configure(text="Desconectar Chat", fg_color="darkred")
        if hasattr(self, "btn_youtube_chat"):
            self.btn_youtube_chat.configure(text="Desconectar Chat", fg_color="darkred")
        if self.status_bar:
            self.status_bar.update_chat_status("connected")
        if hasattr(self, "lbl_kira_chat_state"):
            self.lbl_kira_chat_state.configure(text="Chat: conectado", fg_color="#1f5a3a")
        self._on_stream_admin_log(f"[StreamAdmin] Chat autenticado conectado al live {video_id}.")

        def worker():
            page_token = None
            failures = 0
            max_failures = 6
            while self.stream_admin_ui.chat_connected and self.stream_admin_ui._chat_stop and not self.stream_admin_ui._chat_stop.is_set():
                try:
                    result = self.stream_admin.provider.list_live_chat_messages(live_chat_id, page_token=page_token)
                    failures = 0
                    page_token = result.get("next_page_token") or page_token
                    for message in result.get("messages", []):
                        msg_id = message.get("id")
                        if not _stream_admin_should_process_chat_message(self.stream_admin_ui, msg_id):
                            continue
                        self.smart_agg.process_message(message)
                    delay = max(1.0, float(result.get("polling_interval_millis", 5000)) / 1000.0)
                except Exception as e:
                    failures += 1
                    logger.warning(f"Chat autenticado YouTube fallo: {e}")
                    self._on_stream_admin_log(f"[StreamAdmin] Chat autenticado aviso: {e}")
                    if failures >= max_failures or "Token" in str(e) or "Permisos" in str(e):
                        self._on_stream_admin_log("[StreamAdmin] Chat autenticado detenido por fallos consecutivos. Reconecta cuando el proveedor esté estable.")
                        self._safe_after(self._stream_admin_disconnect_api_chat)
                        break
                    delay = min(60.0, 5.0 * (2 ** (failures - 1)))
                if self.stream_admin_ui._chat_stop:
                    self.stream_admin_ui._chat_stop.wait(delay)

        self.stream_admin_ui._chat_thread = threading.Thread(target=worker, daemon=True)
        self.stream_admin_ui._chat_thread.start()

    def _stream_admin_disconnect_api_chat(self) -> None:
        self.stream_admin_ui.chat_connected = False
        if self.stream_admin_ui._chat_stop:
            self.stream_admin_ui._chat_stop.set()
        self.stream_admin_ui._chat_stop = None
        try:
            if self.smart_agg:
                self.smart_agg.end_session()
        except Exception:
            pass
        btn = self.stream_admin_ui._widget("btn_stream_connect_chat")
        if btn:
            btn.configure(text="Conectar Chat", fg_color="#2f5f8f")
        if hasattr(self, "btn_youtube_chat"):
            self.btn_youtube_chat.configure(text="Conectar Chat", fg_color="#2f5f8f")
        if self.status_bar:
            self.status_bar.update_chat_status("disconnected")
        if hasattr(self, "lbl_kira_chat_state"):
            self.lbl_kira_chat_state.configure(text="Chat: desconectado", fg_color="#1b2633")
        self._on_stream_admin_log("[StreamAdmin] Chat autenticado desconectado.")

    def _stream_admin_send_chat(self) -> None:
        if not self._stream_admin_can_write():
            self._notify_operator("Stream Admin", "Modo solo lectura activo. Reconecta escritura antes de enviar mensajes al chat.")
            return
        entry = self.stream_admin_ui._widget("entry_stream_chat_message")
        message = entry.get().strip() if entry else ""
        if not message:
            self._notify_operator("Stream Admin", "Escribe un mensaje para el chat.")
            return
        import re
        message = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]", "", message)[:500]
        self._run_stream_admin_task("Enviar mensaje al chat", lambda: self.stream_admin.send_chat_message(message))

    def _stream_admin_force_kira_comment(self) -> None:
        if self.smart_agg_ui.is_busy():
            self._on_stream_admin_log("[StreamAdmin] Forzar Kira omitido: Kira está ocupada.")
            return
        context = []
        if self.smart_agg and getattr(self.smart_agg, "_session_id", None):
            try:
                intent_summary = self.smart_agg.intent_aggregator.summarize()
                prompt = intent_summary.get("prompt", "")
                if prompt and prompt != "No hay un tema dominante claro en el chat filtrado.":
                    context = [{"user": "Contexto compacto", "text": prompt}]
                if not context:
                    snapshots = self.smart_agg.history.get_recent_context_snapshots(self.smart_agg._session_id, max_items=1)
                    context = [
                        {"user": "Contexto compacto", "text": s.get("summary", "")}
                        for s in snapshots
                        if s.get("summary")
                    ]
            except Exception as e:
                logger.warning(f"No se pudo obtener contexto RF3 para Forzar Kira: {e}")
        if not context:
            entry = self.stream_admin_ui._widget("entry_stream_chat_message")
            manual = entry.get().strip() if entry else ""
            if manual:
                context = [{"user": "Streamer", "text": manual}]
            elif self.stream_admin_ui.last_metadata:
                context = [{"user": "Stream Admin", "text": f"Live actual: {self.stream_admin_ui.last_metadata.get('title', '')}. Categoria {self.stream_admin_ui.last_metadata.get('category_id', '')}."}]
        if not context:
            self._notify_operator("Stream Admin", "No hay mensajes recientes. Escribe una idea en 'Kira Chat' o espera chat.")
            return
        self.smart_agg_ui.on_aggregated_context({"trigger": {"manual": True, "source": "stream_admin"}, "context": context})
        self._on_stream_admin_log("[StreamAdmin] Forzar Kira ejecutado con contexto reciente.")

    def _stream_admin_toggle_small_stream(self) -> None:
        if not self.smart_agg:
            return
        switch = self.stream_admin_ui._widget("switch_stream_small")
        if switch and switch.get():
            self.smart_agg.set_activity_limits(threshold_per_second=0.2, cooldown_seconds=20.0, reset=True)
            self.smart_agg.set_spam_limits(max_messages_per_user=30)
            self._on_stream_admin_log("[StreamAdmin] Modo Stream Chico ON: Kira reaccionará con menos mensajes.")
        else:
            defaults = self.stream_admin_ui._smart_agg_default_activity or {"threshold": 1.0, "cooldown": 45.0}
            self.smart_agg.set_activity_limits(threshold_per_second=defaults.get("threshold", 1.0), cooldown_seconds=defaults.get("cooldown", 45.0), reset=True)
            self.smart_agg_ui.apply_spam_limit(log=False)
            self._on_stream_admin_log("[StreamAdmin] Modo Stream Chico OFF: umbrales RF3 restaurados.")

    def _stream_admin_simulate_chat(self) -> None:
        if not self.smart_agg:
            self._notify_operator("Stream Admin", "Smart Aggregator no inicializado.")
            return
        if self.smart_agg_ui.is_busy():
            self._on_stream_admin_log("[StreamAdmin] Simular Chat omitido: Kira está ocupada.")
            return
        if getattr(self.smart_agg, "_session_id", None) is None:
            channel = self.stream_admin_ui.last_metadata.get("video_id") if self.stream_admin_ui.last_metadata else "simulated"
            self.smart_agg.start_session("youtube", channel or "simulated")

        now = time.time()
        sample_sets = [
            [("TesterUno", "Kira comenta algo del Minecraft con mods ahora mismo"), ("TesterDos", "El chat quiere saber si estos cultivos van a crecer rapido"), ("TesterTres", "Esto se esta poniendo caotico con tantos mobs alrededor"), ("TesterCuatro", "La base necesita nombre antes de que explote todo"), ("TesterCinco", "Kira deberia burlarse un poquito del survival"), ("TesterSeis", "Momento perfecto para que Kira diga algo divertido")],
            [("CreeperFan", "Ese mod se ve peligrosamente roto para un episodio uno"), ("CultivosOP", "Si los cultivos no crecen rapido esto es estafa agricola"), ("NetherPronto", "Fran va a morir antes de hacer una casa decente"), ("ChatCaos", "Kira tiene que elegir si la base se llama rancho del desastre"), ("ModWatcher", "Hay demasiadas cosas raras en pantalla y apenas empezamos"), ("Ironia", "Survival con mods significa sufrir pero con pasos extra")],
            [("Aldeano", "Necesitamos una meta clara antes de que el chat se distraiga"), ("DiamanteFake", "Eso no parece seguro pero si parece divertido"), ("HornoLento", "Kira deberia exigir armadura antes de otra idea brillante"), ("BiomeFan", "Explora ese bioma raro o no hay respeto"), ("PicoRoto", "Este survival ya huele a inventario perdido"), ("ChatPlan", "Objetivo del stream: no morir por una gallina mutante")],
        ]
        samples = sample_sets[self.stream_admin_ui.sim_round % len(sample_sets)]
        self.stream_admin_ui.sim_round += 1
        for idx, (user, text) in enumerate(samples):
            self.smart_agg.process_message({"id": f"sim-{int(now)}-{idx}", "user": user, "text": text, "timestamp": now + (idx * 0.05), "source": "stream_admin_simulator"})
        self._on_stream_admin_log("[StreamAdmin] Chat simulado enviado a RF3.")

    def _stream_admin_propose_high_risk(self, action: str) -> None:
        entry_user = self.stream_admin_ui._widget("entry_stream_mod_user")
        entry_reason = self.stream_admin_ui._widget("entry_stream_mod_reason")
        user = entry_user.get().strip() if entry_user else ""
        reason = entry_reason.get().strip() if entry_reason else "moderacion manual RF4"
        if not user:
            self._notify_operator("Stream Admin", "Ingresa el channelId del usuario a moderar.")
            return
        self._run_stream_admin_task(f"Proponer {action}", lambda: self.stream_admin.propose_high_risk_moderation(action, user, reason, 300))

    def _stream_admin_refresh_user_list(self) -> None:
        frame_users = self.stream_admin_ui._widget("frame_stream_users")
        if not frame_users:
            return
        for child in frame_users.winfo_children():
            child.destroy()
        users = sorted(self.stream_admin_ui.chat_users.values(), key=lambda item: item.get("last_seen", 0), reverse=True)[:10]
        if not users:
            ctk.CTkLabel(frame_users, text="Sin usuarios recientes con channelId. Conecta chat autenticado y espera mensajes.", text_color="#aaaaaa").grid(row=0, column=0, padx=6, pady=6, sticky="w")
            return
        headers = ["Usuario", "Mensajes", "Razón", "Acción"]
        for col, title in enumerate(headers):
            ctk.CTkLabel(frame_users, text=title, font=ctk.CTkFont(size=11, weight="bold")).grid(row=0, column=col, padx=4, pady=2, sticky="w")
        for row, item in enumerate(users, start=1):
            user = item.get("user", "YouTube")
            channel_id = item.get("channel_id", "")
            badges = []
            if item.get("is_owner"):
                badges.append("owner")
            if item.get("is_moderator"):
                badges.append("mod")
            if item.get("is_member"):
                badges.append("member")
            label = user if not badges else f"{user} ({', '.join(badges)})"
            ctk.CTkLabel(frame_users, text=label, anchor="w").grid(row=row, column=0, padx=4, pady=3, sticky="w")
            ctk.CTkLabel(frame_users, text=str(item.get("count", 0)), width=55).grid(row=row, column=1, padx=4, pady=3, sticky="w")
            reason_entry = ctk.CTkEntry(frame_users, placeholder_text="razón", width=260)
            reason_entry.insert(0, self.stream_admin_ui.default_mod_reason(item))
            reason_entry.grid(row=row, column=2, padx=4, pady=3, sticky="ew")
            action_frame = ctk.CTkFrame(frame_users, fg_color="transparent")
            action_frame.grid(row=row, column=3, padx=4, pady=3, sticky="w")
            button_state = "disabled" if item.get("is_owner") or not self._stream_admin_can_write() else "normal"
            ctk.CTkButton(action_frame, text="Timeout", width=75, fg_color="#555555", hover_color="#666666", state=button_state, command=lambda cid=channel_id, u=user, e=reason_entry: self._stream_admin_moderate_user_from_list("timeout", cid, u, e)).pack(side="left", padx=(0, 4))
            ctk.CTkButton(action_frame, text="Banear", width=70, fg_color="#7d2a2a", state=button_state, command=lambda cid=channel_id, u=user, e=reason_entry: self._stream_admin_moderate_user_from_list("ban", cid, u, e)).pack(side="left")

    def _stream_admin_moderate_user_from_list(self, action: str, channel_id: str, user: str, reason_entry) -> None:
        if not self.stream_admin:
            return
        if not self._stream_admin_can_write():
            self._notify_operator("Stream Admin", "Modo solo lectura activo. Reconecta escritura antes de moderar usuarios.")
            return
        if not channel_id:
            self._notify_operator("Stream Admin", "Este usuario no tiene channelId disponible para moderar.")
            return
        reason = reason_entry.get().strip() or f"{action} manual desde Stream Admin"
        verb = "banear" if action == "ban" else "aplicar timeout a"
        if not messagebox.askyesno("Confirmar moderación", f"¿Seguro que quieres {verb} {user}?\n\nRazón: {reason}"):
            return
        self._run_stream_admin_task(f"Moderación {action}", lambda: self.stream_admin.apply_high_risk_moderation(action, channel_id, reason, 300))

    def _stream_admin_apply_runtime_settings(self, log: bool = True) -> None:
        self.stream_admin_ui.apply_runtime_settings(log=log)
        if self.stream_admin:
            self._sync_stream_admin_controls(self.stream_admin.status())

    def _stream_admin_can_write(self) -> bool:
        if not self.stream_admin:
            return False
        try:
            status = self.stream_admin.status()
        except Exception:
            return False
        return bool(status.get("write_enabled") and status.get("write_scope_active"))

    def _sync_stream_admin_controls(self, state: dict) -> None:
        self.stream_admin_ui._sync_controls(state)

    def _on_stream_admin_state(self, state: dict) -> None:
        self.stream_admin_ui.on_state(state)

    def _on_stream_admin_metadata(self, metadata: dict) -> None:
        self.stream_admin_ui.on_metadata(metadata)

    def _on_stream_admin_pending(self, pending: dict) -> None:
        self.stream_admin_ui.on_pending(pending)

    def _on_stream_admin_analytics(self, snapshot: dict) -> None:
        self.stream_admin_ui.on_analytics(snapshot)

    def _stream_admin_inject_silent_context(self, context: Any) -> None:
        self.stream_admin_ui._inject_silent_context(context)
