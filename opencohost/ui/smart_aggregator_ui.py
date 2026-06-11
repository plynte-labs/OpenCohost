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

from opencohost.config.settings import BASE_DIR
from opencohost.config.logger import get_logger
from opencohost.smart_aggregator.url_parser import parse_chat_url
from opencohost.ui.state import UIState
from opencohost.ui.protocols import CallbackDispatcher

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
        entry_youtube_threshold: Optional[ctk.CTkEntry] = None,
        consola_youtube: Optional[ctk.CTkTextbox] = None,
        lbl_kira_chat_state: Optional[ctk.CTkLabel] = None,
        status_bar: Any = None,
        on_log: Callable[[str], None] | None = None,
        schedule_ui_update: Callable[[Callable[[], None]], None] | None = None,
        on_track_chat_user: Callable[[dict], None] | None = None,
        on_ingest_rf3: Callable[[str, dict], None] | None = None,
        on_joyita: Callable[[str], None] | None = None,
        health_monitor: Any = None,
    ) -> None:
        self._ui_state = ui_state
        self._dispatcher = dispatcher
        self._smart_agg = smart_agg
        self._motor_ia = motor_ia
        self._entry_youtube_video = entry_youtube_video
        self._btn_youtube_chat = btn_youtube_chat
        self._entry_youtube_user_limit = entry_youtube_user_limit
        self._entry_youtube_threshold = entry_youtube_threshold
        self._consola_youtube = consola_youtube
        self._lbl_kira_chat_state = lbl_kira_chat_state
        self._status_bar = status_bar
        self._health_monitor = health_monitor

        self._on_log = on_log or (lambda msg: None)
        self._schedule_ui_update = schedule_ui_update or (lambda fn: fn())
        self._on_track_chat_user = on_track_chat_user or (lambda msg: None)
        self._on_ingest_rf3 = on_ingest_rf3 or (lambda evt, data: None)
        self._on_joyita = on_joyita or (lambda text: None)

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
        self._bind_threshold_events()

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
        """Extract a YouTube video ID from a URL or return the raw value.

        Only accepts youtube.com / youtu.be URLs and bare 11-char IDs.
        Rejects other platforms and unsafe schemes.
        """
        import re
        value = value.strip()
        if not value:
            return ""
        lowered = value.lower()
        for domain in ("facebook.com", "dlive.tv", "vk.com", "tiktok.com", "kick.com", "rumble.com"):
            if domain in lowered:
                return ""
        parsed = urllib.parse.urlparse(value)
        if parsed.netloc:
            if parsed.scheme and parsed.scheme not in ("http", "https"):
                return ""
            if "youtube.com" in parsed.netloc:
                query = urllib.parse.parse_qs(parsed.query)
                if query.get("v"):
                    return str(query["v"][0])[:32]
                parts = [p for p in parsed.path.split("/") if p]
                if "live" in parts:
                    idx = parts.index("live")
                    if idx + 1 < len(parts):
                        return str(parts[idx + 1])[:32]
                if "shorts" in parts:
                    idx = parts.index("shorts")
                    if idx + 1 < len(parts):
                        return str(parts[idx + 1])[:32]
                return ""
            if parsed.netloc == "youtu.be" or parsed.netloc.endswith(".youtu.be"):
                vid = parsed.path.strip("/").split("/")[0]
                return str(vid)[:32] if vid else ""
            return ""
        if re.match(r"^[a-zA-Z0-9_-]{6,}$", value):
            return value[:32]
        return ""

    # ------------------------------------------------------------------
    # Toggle connection
    # ------------------------------------------------------------------

    def toggle_connection(self) -> None:
        """Connect or disconnect the Smart Aggregator chat."""
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

        platform, source_id = self._get_chat_url_info()
        if not source_id:
            messagebox.showwarning(
                "Chat Live",
                "URL no valida o no soportada",
            )
            return

        self._do_connect(platform, source_id)

    def connect_to(self, source_id: str, platform: str = "youtube") -> bool:
        """Connect to a chat by source_id and platform. Returns True on success."""
        if not self._smart_agg:
            self._on_log("[SmartAggregator] No inicializado.")
            return False
        if not source_id:
            return False
        if self._ui_state.smart_agg_connected or self._ui_state.smart_agg_connecting:
            return False
        try:
            self._do_connect(platform, source_id)
            return True
        except Exception:
            return False

    def _do_connect(self, platform: str, source_id: str) -> None:
        try:
            self._apply_spam_limit(log=False)
            self._apply_threshold(log=False)
            self._manual_disconnect = False
            self._ui_state.smart_agg_connecting = True
            self._set_chat_button("Conectando...", "#a66a00")
            self._on_log(f"[SmartAggregator] Conectando chat {platform}: {source_id}")
            self._smart_agg.connect(source_id, platform=platform)
        except Exception:
            self._ui_state.smart_agg_connecting = False
            self._set_chat_button("Conectar Chat", "#2f5f8f")
            logger.exception("Error conectando Smart Aggregator")
            messagebox.showerror("Chat Live", "No se pudo conectar al chat.")

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

    def apply_threshold(self, log: bool = True) -> None:
        """Read the threshold entry and apply it to the activity trigger."""
        self._apply_threshold(log=log)

    def _apply_threshold(self, log: bool = True) -> None:
        if not self._smart_agg:
            return
        raw_value = self._get_threshold_text()
        try:
            threshold = max(0.1, float(raw_value))
        except (ValueError, TypeError):
            threshold = 1.0
        self._smart_agg.set_activity_limits(threshold_per_second=threshold, reset=True)
        self._on_log(
            f"[SmartAggregator] Umbral de actividad: {threshold:.1f} msg/s."
        ) if log else None

    def _bind_threshold_events(self) -> None:
        """Apply threshold on every keystroke so the user sees immediate effect."""
        if self._entry_youtube_threshold is None:
            return
        if not hasattr(self._entry_youtube_threshold, "bind"):
            return
        def on_change(*_args: Any) -> None:
            self._apply_threshold(log=True)
        self._entry_youtube_threshold.bind("<KeyRelease>", on_change)
        self._entry_youtube_threshold.bind("<FocusOut>", on_change)

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
        """Handle a chat source error (transient reconnect)."""
        self._on_log(
            f"[SmartAggregator] Aviso: reconectando por fallo transitorio ({error})"
        )

    def on_source_connect(self, info: dict) -> None:
        """Handle successful chat connection."""
        was_connected = self._ui_state.smart_agg_connected
        self._ui_state.smart_agg_connecting = False
        self._ui_state.smart_agg_connected = True

        platform = info.get("platform", "")
        source_id = info.get("source_id", info.get("video_id", ""))
        self._schedule_ui_update(lambda: self._set_chat_button("Desconectar Chat", "darkred"))
        self._schedule_ui_update(lambda: self._update_chat_pill("connected"))
        self._schedule_ui_update(lambda: self._set_kira_chat_state("Chat: conectado", "#1f5a3a"))
        if not was_connected:
            self._on_log(f"[SmartAggregator] Chat {platform} conectado: {source_id}")

    def on_source_disconnect(self) -> None:
        """Handle chat disconnection."""
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
            self._on_log(f"[SmartAggregator] Chat {reason}.")
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
            last_lines = self._recent_kira_lines(3)
            prompt = self._build_kira_chat_prompt(chat_context, last_kira_lines=last_lines)
            self._motor_ia.enqueue(prompt, priority=1, source="chat")
            self._on_log("[SmartAggregator] Kira ocupada — contexto encolado en cola prioritaria.")
            return

        context = data.get("context", [])[-12:]
        if not context and not intent_prompt:
            return

        highlight = self._select_highlight(context)
        if highlight:
            lowered = highlight.lower()
            has_question = "?" in highlight or "¿" in highlight
            has_emoji = any(ord(c) > 0x1F000 for c in highlight)
            has_humor = any(m in lowered for m in ("jaja", "jeje", "lol", "xd", "😂", "🤣"))
            score = self._joyita_score_raw(highlight)
            if (has_question or has_emoji or has_humor) and score >= 100:
                formatted = self._format_joyita_for_obs(highlight)
                self._on_joyita(formatted)
        lines = [f"- {m.get('user', '')}: {m.get('text', '')}" for m in context]
        chat_context = intent_prompt or "Mensajes recientes del chat:\n" + "\n".join(lines)
        last_lines = self._recent_kira_lines(3)
        prompt = self._build_kira_chat_prompt(chat_context, highlight, last_lines)
        self._motor_ia.command_queue.put(("process_context", prompt))
        if top_intents:
            top = top_intents[0]
            self._on_log(
                f"[SmartAggregator] Tema dominante: {top.get('label')} ({top.get('count')} msgs)."
            )
        self._on_log("[SmartAggregator] Contexto agregado enviado a Kira.")

    def _recent_kira_lines(self, count: int = 3) -> str:
        """Extract Kira's last N responses from motor IA history for anti-repeat."""
        if not self._motor_ia:
            return ""
        historial = getattr(self._motor_ia, "historial", None)
        if not historial:
            return ""
        items = list(historial)
        kira_msgs = [
            m.get("content", "") for m in items[-20:]
            if isinstance(m, dict) and m.get("role") == "assistant"
        ][-count:]
        return "\n".join(f"- {msg[:120]}" for msg in kira_msgs if msg)

    @staticmethod
    def _build_kira_chat_prompt(chat_context: str, highlight: str = "", last_kira_lines: str = "") -> str:
        """Build a chat prompt that prevents internal summaries leaking on air."""
        highlight_line = ""
        if highlight:
            highlight_line = (
                "Referencia opcional privada; NO nombres al autor ni digas que es destacado:\n"
                f"{highlight}\n\n"
            )
        anti_repeat = ""
        if last_kira_lines:
            anti_repeat = (
                "ÚLTIMAS RESPUESTAS DE KIRA (NO repetir estructura ni fraseo):\n"
                f"{last_kira_lines}\n\n"
            )
        return (
            "TAREA: respondé al aire como Kira, co-host del stream con actitud.\n"
            "SALIDA PERMITIDA: solo la frase final que Kira diría en voz alta.\n"
            "PERSONALIDAD: sarcasmo seco, humor ácido, cero condescendencia. No seas predecible.\n"
            "Si el chat repite lo mismo de siempre, señalalo con ironía, no con queja genérica.\n"
            "No expliques el resumen, no listes datos y no describas tu proceso.\n"
            "PROHIBIDO mencionar cantidades de mensajes, autores, ejemplos, 'temas/personas', "
            "'intención dominante', 'contexto privado', 'resumen', 'mensaje destacado' o 'el chat dice'.\n"
            "PROHIBIDO empezar con 'Parece que', 'Bueno, parece', 'Vale, parece', 'Voy a', "
            "'Tengo que', 'El chat esta', 'energia del flujo' o 'mantener la energia'.\n"
            "PROHIBIDO usar fórmulas repetitivas como 'es como decir que', 'es como si', 'eso es como'. NUNCA uses símiles forzados.\n"
            "Si el contexto interno es confuso o pobre, hacé una reacción general corta sin inventar detalles.\n"
            "Respondé en 1-2 frases cortas, con personalidad de Kira: broma, crítica o comentario concreto.\n"
            "NO repitas el mismo tipo de reacción del turno anterior. Variá entre burla, reflexión, dato curioso.\n\n"
            f"{anti_repeat}"
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
        """Pick the best 'joyita' message to highlight from recent context.

        Scoring prefers: questions, humor/emoji, unusual/out-of-context
        messages, then falls back to longest valid-length text.
        Deterministic and cheap — no LLM calls.
        """
        if not context:
            return ""

        candidates = []
        for msg in context:
            text = msg.get("text", "").strip()
            if not text:
                continue
            candidates.append(msg)

        if not candidates:
            return ""

        def _joyita_score(msg: dict) -> int:
            text = msg.get("text", "")
            length = len(text)
            if length < 10 or length > 180:
                return -1  # disqualify too short or too long

            lowered = text.lower()

            # ---- Content safety gates: reject immediately ----
            if SmartAggregatorUI._is_joyita_unsafe(text, lowered):
                return -1

            score = 0

            # Questions get highest priority
            if "?" in text or "¿" in text:
                score += 100

            # Humor / emoji signals
            emoji_count = sum(1 for c in text if ord(c) > 0x1F000)
            score += min(emoji_count * 10, 30)
            for marker in ("jaja", "jeje", "lol", "xd", "😂", "🤣", "😆", "jajaja"):
                if marker in lowered:
                    score += 15
                    break

            # Unusual / out-of-context: numbers mixed with text, rare chars
            has_digits = any(c.isdigit() for c in text)
            has_alpha = any(c.isalpha() for c in text)
            if has_digits and has_alpha:
                score += 8

            for marker in ("por qué", "cómo", "cuándo", "dónde", "quién"):
                if marker in lowered:
                    score += 5

            # Small bonus for being in the sweet-spot length range
            if 30 <= length <= 120:
                score += 3

            return score

        best = max(candidates, key=_joyita_score)
        return f"{best.get('user', '')}: {best.get('text', '')}"

    @staticmethod
    def _is_joyita_unsafe(text: str, lowered: str) -> bool:
        """Reject messages containing links, @mentions, or advertising.

        These signals indicate the message is promotional, a call-to-action,
        or otherwise inappropriate for on-stream highlighting.
        """
        # URLs / links — any protocol, bare domain, or tld
        url_patterns = (
            "http://", "https://", "www.", ".com", ".net", ".org",
            ".gg/", ".tv/", ".live/", "youtu.be/", "twitch.tv/",
            "discord.gg/", "tiktok.com/",
        )
        if any(p in lowered for p in url_patterns):
            return True

        # @mentions — directing viewers to another account
        if "@" in text:
            words = lowered.split()
            for w in words:
                # Only treat as mention if @ is followed by a letter/number
                if w.startswith("@") and len(w) > 1 and w[1].isalnum():
                    return True

        # Payment / advertising / hidden promotion
        ad_patterns = (
            "paga", "pagá", "pagame", "donación", "donacion", "dona",
            "subscribe", "suscribete", "suscríbete", "follow",
            "sígueme", "sigueme", "regalame", "regálame",
            "compra", "comprá", "vendo", "vendes", "barato",
            "promo", "sorteo", "giveaway", "sponsor",
            "bit.ly", "linktr.ee", "onlyfans",
        )
        if any(p in lowered for p in ad_patterns):
            return True

        return False

    @staticmethod
    def _joyita_score_raw(highlight: str) -> int:
        """Re-compute the joyita score from a formatted highlight string.

        Used by the OBS gateway to enforce the score ≥100 threshold without
        re-parsing the raw context dicts.
        """
        # highlight format: "user: text"
        if ":" in highlight:
            text = highlight.split(":", 1)[1].strip()
        else:
            text = highlight

        length = len(text)
        if length < 10 or length > 180:
            return -1

        lowered = text.lower()
        if SmartAggregatorUI._is_joyita_unsafe(text, lowered):
            return -1

        score = 0
        if "?" in text or "¿" in text:
            score += 100
        emoji_count = sum(1 for c in text if ord(c) > 0x1F000)
        score += min(emoji_count * 10, 30)
        for marker in ("jaja", "jeje", "lol", "xd", "😂", "🤣", "jajaja"):
            if marker in lowered:
                score += 15
                break
        has_digits = any(c.isdigit() for c in text)
        has_alpha = any(c.isalpha() for c in text)
        if has_digits and has_alpha:
            score += 8
        for marker in ("por qué", "cómo", "cuándo", "dónde", "quién"):
            if marker in lowered:
                score += 5
        if 30 <= length <= 120:
            score += 3
        return score

    @staticmethod
    def _format_joyita_for_obs(highlight: str) -> str:
        """Format a joyita for OBS display: word-wrap into 2–3 lines.

        Without wrapping, OBS renders long text in a single line, forcing
        the streamer to use huge font sizes that blow up the scene layout.
        """
        # Already clean: strip control chars, null bytes
        text = highlight.replace("\x00", "").strip()
        if not text:
            return ""

        MAX_CHARS_PER_LINE = 38
        MIN_CHARS_TO_WRAP = 42

        if len(text) <= MIN_CHARS_TO_WRAP:
            return text

        # Try to split on word boundaries
        words = text.split()
        lines = []
        current = ""
        for word in words:
            if current and len(current) + 1 + len(word) > MAX_CHARS_PER_LINE:
                lines.append(current)
                current = word
            else:
                current = f"{current} {word}".strip()
        if current:
            lines.append(current)

        # Cap at 3 lines — drop excess words
        if len(lines) > 3:
            lines = lines[:3]
            # Add "…" to last line to indicate truncation
            if not lines[-1].endswith("…"):
                lines[-1] = lines[-1].rstrip()[:-3].rstrip() + "…"

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_chat_url_info(self) -> tuple:
        """Parse the URL entry and return (platform, source_id) or (None, None)."""
        if self._entry_youtube_video is None:
            return (None, None)
        raw = self._entry_youtube_video.get().strip()
        if not raw:
            return (None, None)
        try:
            parsed = parse_chat_url(raw)
            return (parsed["platform"], parsed["source_id"])
        except ValueError:
            return (None, None)

    def _get_user_limit_text(self) -> str:
        """Read the spam-limit entry text."""
        if self._entry_youtube_user_limit is None:
            return ""
        return self._entry_youtube_user_limit.get().strip()

    def _get_threshold_text(self) -> str:
        """Read the activity-threshold entry text."""
        if self._entry_youtube_threshold is None:
            return ""
        return self._entry_youtube_threshold.get().strip()

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
        safe_line = str(line).replace("\r", " ").replace("\n", " ")
        widget.configure(state="normal")
        widget.insert("end", safe_line + "\n")
        try:
            total_lines = int(widget.index("end-1c").split(".")[0])
            excess = total_lines - int(max_lines)
            if excess > 0:
                widget.delete("1.0", f"{excess + 1}.0")
        except Exception:
            pass
        widget.see("end")
        widget.configure(state="disabled")
