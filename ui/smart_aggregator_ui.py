"""SmartAggregatorUI — encapsulates Smart Aggregator UI behavior.

Manages the YouTube Live chat connection lifecycle, message display,
vibe/activity logging, and aggregated-context prompting for the VoiceAI
main window.  Subscribes to UIState observer for automatic updates when
``smart_agg_connected`` or ``smart_agg_connecting`` changes.

All UI updates are routed through an injected ``schedule_ui_update``
callable so the class is testable without a running Tkinter mainloop.

Usage::

    agg_ui = SmartAggregatorUI(
        ui_state=state,
        dispatcher=dispatcher,
        smart_agg=aggregator_instance,
        motor_ia=motor,
        entry_youtube_video=url_entry,
        btn_youtube_chat=connect_btn,
        entry_youtube_user_limit=limit_entry,
        consola_youtube=chat_textbox,
        lbl_kira_chat_state=chat_state_label,
        status_bar=status_bar,
        on_log=lambda msg: log_queue.put(msg),
        schedule_ui_update=lambda fn: app.after(0, fn),
        on_track_chat_user=lambda msg: app._stream_admin_track_chat_user(msg),
        on_ingest_rf3=lambda evt, data: app._stream_admin_ingest_rf3_event(evt, data),
    )
    agg_ui.initialize()
"""

from __future__ import annotations

import os
import urllib.parse
from queue import Queue
from typing import Any, Callable, Optional

import customtkinter as ctk
import tkinter.messagebox as messagebox

from config.settings import BASE_DIR
from config.logger import get_logger
from ui.state import UIState
from ui.protocols import CallbackDispatcher

logger = get_logger()


class SmartAggregatorUI:
    """Manages Smart Aggregator UI: YouTube chat, vibe, activity, context."""

    def __init__(
        self,
        ui_state: UIState,
        dispatcher: CallbackDispatcher,
        smart_agg: Any,
        motor_ia: Any,
        entry_youtube_video: Optional[ctk.CTkEntry] = None,
        btn_youtube_chat: Optional[ctk.CTkButton] = None,
        entry_youtube_user_limit: Optional[ctk.CTkEntry] = None,
        consola_youtube: Optional[ctk.CTkTextbox] = None,
        lbl_kira_chat_state: Optional[ctk.CTkLabel] = None,
        status_bar: Any = None,
        on_log: Callable[[str], None] | None = None,
        schedule_ui_update: Callable[[Callable[[], None]], None] | None = None,
        on_track_chat_user: Callable[[dict], None] | None = None,
        on_ingest_rf3: Callable[[str, dict], None] | None = None,
        health_monitor: Any = None,
    ) -> None:
        self._ui_state = ui_state
        self._dispatcher = dispatcher
        self._smart_agg = smart_agg
        self._motor_ia = motor_ia
        self._entry_youtube_video = entry_youtube_video
        self._btn_youtube_chat = btn_youtube_chat
        self._entry_youtube_user_limit = entry_youtube_user_limit
        self._consola_youtube = consola_youtube
        self._lbl_kira_chat_state = lbl_kira_chat_state
        self._status_bar = status_bar
        self._health_monitor = health_monitor

        self._on_log = on_log or (lambda msg: None)
        self._schedule_ui_update = schedule_ui_update or (lambda fn: fn())
        self._on_track_chat_user = on_track_chat_user or (lambda msg: None)
        self._on_ingest_rf3 = on_ingest_rf3 or (lambda evt, data: None)

        self._manual_disconnect: bool = False
        self._default_activity: dict[str, Any] = {}
        self._observer_id: Optional[int] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self) -> None:
        """Subscribe to UIState observer for connection-state changes."""
        if self._observer_id is not None:
            self._ui_state.unsubscribe(self._observer_id)
        self._observer_id = self._ui_state.subscribe(self._on_state_change)

    def cleanup(self) -> None:
        """Unsubscribe from UIState observer."""
        if self._observer_id is not None:
            self._ui_state.unsubscribe(self._observer_id)
            self._observer_id = None

    # ------------------------------------------------------------------
    # State observer
    # ------------------------------------------------------------------

    def _on_state_change(self, key: str, value: Any) -> None:
        """UIState observer callback for smart_agg state changes."""
        if key in ("smart_agg_connected", "smart_agg_connecting"):
            self._schedule_ui_update(lambda: None)

    # ------------------------------------------------------------------
    # Busy check
    # ------------------------------------------------------------------

    def is_busy(self) -> bool:
        """Return True if the motor IA is processing or speaking."""
        if not self._motor_ia:
            return True
        return self._motor_ia.is_processing or self._motor_ia.is_speaking

    # ------------------------------------------------------------------
    # LLM interface (called by aggregator for vibe analysis)
    # ------------------------------------------------------------------

    def llm_interface(self, prompt: str) -> str:
        """Call the LLM for vibe analysis.  Raises if motor is busy."""
        if self.is_busy():
            raise RuntimeError("Motor IA ocupado")

        # Health gate: block Vibe calls when VRAM is low or Ollama is down
        hm = getattr(self, "_health_monitor", None)
        if hm is not None:
            state = hm.state
            if state.vram_status in ("low", "critical"):
                raise RuntimeError("Vibe paused: low VRAM")
            if state.ollama_status == "down":
                raise RuntimeError("Vibe unavailable: Ollama not running")

        ollama_client = getattr(self._motor_ia, "ollama", None)
        if ollama_client is None:
            import ollama as ollama_client

        response = ollama_client.chat(
            model=self._motor_ia.current_model,
            messages=[{"role": "user", "content": prompt}],
            keep_alive=-1,
            options={"temperature": 0.2, "num_predict": 180},
        )
        msg_obj = response.get("message", {})
        if isinstance(msg_obj, dict):
            return msg_obj.get("content", "")
        return getattr(msg_obj, "content", "")

    # ------------------------------------------------------------------
    # YouTube video ID extraction
    # ------------------------------------------------------------------

    @staticmethod
    def extract_youtube_video_id(value: str) -> str:
        """Extract a YouTube video ID from a URL or return the raw value."""
        value = value.strip()
        if not value:
            return ""
        parsed = urllib.parse.urlparse(value)
        if parsed.netloc:
            if "youtu.be" in parsed.netloc:
                return parsed.path.strip("/").split("/")[0]
            query = urllib.parse.parse_qs(parsed.query)
            if query.get("v"):
                return query["v"][0]
            parts = [p for p in parsed.path.split("/") if p]
            if "live" in parts:
                idx = parts.index("live")
                if idx + 1 < len(parts):
                    return parts[idx + 1]
            if "shorts" in parts:
                idx = parts.index("shorts")
                if idx + 1 < len(parts):
                    return parts[idx + 1]
        return value

    # ------------------------------------------------------------------
    # Toggle connection
    # ------------------------------------------------------------------

    def toggle_connection(self) -> None:
        """Connect or disconnect the Smart Aggregator YouTube chat."""
        if not self._smart_agg:
            self._on_log("[SmartAggregator] No inicializado. Revisa config/smart_aggregator.yaml.")
            return

        if self._ui_state.smart_agg_connected or self._ui_state.smart_agg_connecting:
            self._manual_disconnect = True
            try:
                self._smart_agg.disconnect()
            finally:
                self._ui_state.smart_agg_connected = False
                self._ui_state.smart_agg_connecting = False
                self._set_chat_button("Conectar Chat", "#2f5f8f")
            return

        video_id = self._get_video_id_from_entry()
        if not video_id:
            messagebox.showwarning(
                "YouTube Live",
                "Ingresa una URL o video_id de un live de YouTube.",
            )
            return

        try:
            self._apply_spam_limit(log=False)
            self._manual_disconnect = False
            self._ui_state.smart_agg_connecting = True
            self._set_chat_button("Conectando...", "#a66a00")
            self._on_log(f"[SmartAggregator] Conectando chat YouTube: {video_id}")
            self._smart_agg.connect(video_id)
        except Exception:
            self._ui_state.smart_agg_connecting = False
            self._set_chat_button("Conectar Chat", "#2f5f8f")
            logger.exception("Error conectando Smart Aggregator")
            messagebox.showerror("YouTube Live", "No se pudo conectar al chat.")

    # ------------------------------------------------------------------
    # Spam limit
    # ------------------------------------------------------------------

    def apply_spam_limit(self, log: bool = True) -> None:
        """Read the spam-limit entry and apply it to the aggregator."""
        self._apply_spam_limit(log=log)

    def _apply_spam_limit(self, log: bool = True) -> None:
        if not self._smart_agg:
            return
        raw_value = self._get_user_limit_text()
        try:
            limit = max(1, int(raw_value))
        except ValueError:
            limit = 10
        self._smart_agg.set_spam_limits(max_messages_per_user=limit)
        if log:
            self._on_log(
                f"[SmartAggregator] Anti-spam actualizado: max {limit} mensajes/usuario por ventana."
            )

    # ------------------------------------------------------------------
    # Message handling
    # ------------------------------------------------------------------

    def on_filtered_message(self, message: dict) -> None:
        """Handle a filtered chat message from the aggregator."""
        self._on_track_chat_user(message)
        user = message.get("user", "")
        text = message.get("text", "")
        self._schedule_ui_update(lambda u=user, t=text: self._print_youtube_chat(u, t))

    def _print_youtube_chat(self, user: str, text: str) -> None:
        if self._consola_youtube is None:
            return
        self._append_textbox(self._consola_youtube, f"[{user}] {text}", max_lines=1500)

    # ------------------------------------------------------------------
    # Source callbacks
    # ------------------------------------------------------------------

    def on_source_error(self, error: str) -> None:
        """Handle a YouTube source error (transient reconnect)."""
        self._on_log(
            f"[SmartAggregator] Aviso YouTube: reconectando por fallo transitorio ({error})"
        )

    def on_source_connect(self, info: dict) -> None:
        """Handle successful YouTube chat connection."""
        was_connected = self._ui_state.smart_agg_connected
        self._ui_state.smart_agg_connecting = False
        self._ui_state.smart_agg_connected = True

        video_id = info.get("video_id", "")
        self._schedule_ui_update(lambda: self._set_chat_button("Desconectar Chat", "darkred"))
        self._schedule_ui_update(lambda: self._update_chat_pill("connected"))
        self._schedule_ui_update(lambda: self._set_kira_chat_state("Chat: conectado", "#1f5a3a"))
        if not was_connected:
            self._on_log(f"[SmartAggregator] Chat YouTube conectado: {video_id}")

    def on_source_disconnect(self) -> None:
        """Handle YouTube chat disconnection."""
        was_active = (
            self._ui_state.smart_agg_connected or self._ui_state.smart_agg_connecting
        )
        self._ui_state.smart_agg_connected = False
        self._ui_state.smart_agg_connecting = False

        self._schedule_ui_update(lambda: self._set_chat_button("Conectar Chat", "#2f5f8f"))
        self._schedule_ui_update(lambda: self._update_chat_pill("disconnected"))
        self._schedule_ui_update(lambda: self._set_kira_chat_state("Chat: desconectado", "#1b2633"))

        if was_active:
            reason = (
                "desconectado por usuario"
                if self._manual_disconnect
                else "desconectado tras agotar reconexiones"
            )
            self._on_log(f"[SmartAggregator] Chat YouTube {reason}.")
        self._manual_disconnect = False

    # ------------------------------------------------------------------
    # Vibe update
    # ------------------------------------------------------------------

    def on_vibe_update(self, vibe: dict) -> None:
        """Handle a vibe analysis update from the aggregator."""
        self._on_ingest_rf3("vibe", vibe)
        temp = vibe.get("temperature", 0.0)
        emotions = vibe.get("emotions", {})
        dominant = max(emotions, key=emotions.get) if emotions else "neutral"
        note = vibe.get("note")

        if note == "fallback_due_to_busy":
            self._on_log("[SmartAggregator] Vibe omitido: Kira ocupada.")
        elif note in (
            "fallback_due_to_parse_error",
            "fallback_due_to_empty_llm_response",
            "fallback_due_to_llm_error",
        ):
            self._on_log(f"[SmartAggregator] Vibe no interpretable; usando neutral ({note}).")
        elif note:
            self._on_log(f"[SmartAggregator] Vibe: {temp:.0f}/100 ({dominant}) [{note}]")
        else:
            self._on_log(f"[SmartAggregator] Vibe: {temp:.0f}/100 ({dominant})")

    # ------------------------------------------------------------------
    # Activity trigger
    # ------------------------------------------------------------------

    def on_activity_trigger(self, data: dict) -> None:
        """Handle an activity spike trigger from the aggregator."""
        self._on_ingest_rf3("activity", data)
        rate = data.get("rate", 0.0)
        self._on_log(f"[SmartAggregator] Pico de actividad detectado: {rate:.2f} msg/s")

    # ------------------------------------------------------------------
    # Aggregated context
    # ------------------------------------------------------------------

    def on_aggregated_context(self, data: dict) -> None:
        """Handle aggregated chat context — prompts Kira to react."""
        intent_summary = data.get("intent_summary") or {}
        intent_prompt = intent_summary.get("prompt")
        top_intents = intent_summary.get("top_intents") or []
        if self.is_busy():
            # Motor busy — enqueue to priority queue instead of dropping
            context = data.get("context", [])[-12:]
            if not context and not intent_prompt:
                return
            lines = [f"- {m.get('user', '')}: {m.get('text', '')}" for m in context]
            chat_context = intent_prompt or "Mensajes recientes del chat:\n" + "\n".join(lines)
            prompt = self._build_kira_chat_prompt(chat_context)
            self._motor_ia.enqueue(prompt, priority=1, source="chat")
            self._on_log("[SmartAggregator] Kira ocupada — contexto encolado en cola prioritaria.")
            return

        context = data.get("context", [])[-12:]
        if not context and not intent_prompt:
            return

        highlight = self._select_highlight(context)
        lines = [f"- {m.get('user', '')}: {m.get('text', '')}" for m in context]
        chat_context = intent_prompt or "Mensajes recientes del chat:\n" + "\n".join(lines)
        prompt = self._build_kira_chat_prompt(chat_context, highlight)
        self._motor_ia.command_queue.put(("process_context", prompt))
        if top_intents:
            top = top_intents[0]
            self._on_log(
                f"[SmartAggregator] Tema dominante: {top.get('label')} ({top.get('count')} msgs)."
            )
        self._on_log("[SmartAggregator] Contexto agregado enviado a Kira.")

    @staticmethod
    def _build_kira_chat_prompt(chat_context: str, highlight: str = "") -> str:
        """Build a chat prompt that prevents internal summaries leaking on air."""
        highlight_line = ""
        if highlight:
            highlight_line = (
                "Referencia opcional privada; NO nombres al autor ni digas que es destacado:\n"
                f"{highlight}\n\n"
            )
        return (
            "TAREA: respondé al aire como Kira, co-host del stream.\n"
            "SALIDA PERMITIDA: solo la frase final que Kira diría en voz alta.\n"
            "No expliques el resumen, no listes datos y no describas tu proceso.\n"
            "PROHIBIDO mencionar cantidades de mensajes, autores, ejemplos, 'temas/personas', "
            "'intención dominante', 'contexto privado', 'resumen', 'mensaje destacado' o 'el chat dice'.\n"
            "PROHIBIDO empezar con 'Parece que', 'Bueno, parece', 'Vale, parece', 'Voy a', "
            "'Tengo que', 'El chat esta', 'energia del flujo' o 'mantener la energia'.\n"
            "Si el contexto interno es confuso o pobre, hacé una reacción general corta sin inventar detalles.\n"
            "Respondé en 1-2 frases cortas, con personalidad de Kira: broma, crítica o comentario concreto.\n\n"
            f"{highlight_line}"
            "--- CONTEXTO PRIVADO, NO LEER LITERAL ---\n"
            f"{chat_context}\n"
            "--- FIN CONTEXTO PRIVADO ---"
        )

    # ------------------------------------------------------------------
    # Highlight selection
    # ------------------------------------------------------------------

    @staticmethod
    def _select_highlight(context: list[dict]) -> str:
        """Pick the best message to highlight from recent context."""
        candidates = []
        for msg in context:
            text = msg.get("text", "").strip()
            if 20 <= len(text) <= 180:
                candidates.append(msg)
        if not candidates:
            candidates = context
        selected = max(candidates, key=lambda m: len(m.get("text", "")))
        return f"{selected.get('user', '')}: {selected.get('text', '')}"

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_video_id_from_entry(self) -> str:
        """Read and extract the video ID from the YouTube URL entry."""
        if self._entry_youtube_video is None:
            return ""
        return self.extract_youtube_video_id(self._entry_youtube_video.get())

    def _get_user_limit_text(self) -> str:
        """Read the spam-limit entry text."""
        if self._entry_youtube_user_limit is None:
            return ""
        return self._entry_youtube_user_limit.get().strip()

    def _set_chat_button(self, text: str, color: str) -> None:
        """Update the YouTube chat connect/disconnect button."""
        if self._btn_youtube_chat is not None:
            self._btn_youtube_chat.configure(text=text, fg_color=color)

    def _update_chat_pill(self, status: str) -> None:
        """Update the chat status pill via the status bar."""
        if self._status_bar is not None:
            self._status_bar.update_chat_status(status)

    def _set_kira_chat_state(self, text: str, color: str) -> None:
        """Update the Kira chat state label."""
        if self._lbl_kira_chat_state is not None:
            self._lbl_kira_chat_state.configure(text=text, fg_color=color)

    @staticmethod
    def _append_textbox(widget: ctk.CTkTextbox, line: str, max_lines: int = 1000) -> None:
        """Append a line to a textbox, trimming to max_lines."""
        widget.configure(state="normal")
        widget.insert("end", str(line) + "\n")
        try:
            total_lines = int(widget.index("end-1c").split(".")[0])
            excess = total_lines - int(max_lines)
            if excess > 0:
                widget.delete("1.0", f"{excess + 1}.0")
        except Exception:
            pass
        widget.see("end")
        widget.configure(state="disabled")
