"""StatusBar — encapsulates status pill creation and state-driven visual feedback.

Manages the main status label and four core status pills (model, mic, TTS, chat)
in the top status bar.  Subscribes to UIState observer for automatic updates
when state fields change, and exposes explicit update methods for pipeline-driven
transitions.
"""

from __future__ import annotations

from typing import Any

import customtkinter as ctk

from ui.state import UIState


# ---------------------------------------------------------------------------
# Color palettes — dark background tints for pill fg_color
# ---------------------------------------------------------------------------

_MODEL_STATUS_COLORS: dict[str, str] = {
    "loading": "#cc8800",
    "ready": "#22cc66",
    "error": "#cc3333",
    "offline": "#666666",
}

_MIC_STATUS_COLORS: dict[str, str] = {
    "disconnected": "#4a2630",
    "idle": "#1b2633",
    "listening": "#1f5a3a",
    "recording": "#cc8800",
}

_TTS_STATUS_COLORS: dict[str, str] = {
    "idle": "#1b2633",
    "generating": "#1f3f6f",
    "speaking": "#1f526f",
    "paused": "#ffaa00",
    "error": "#cc3333",
}

_CHAT_STATUS_COLORS: dict[str, str] = {
    "disconnected": "#1b2633",
    "connecting": "#cc8800",
    "connected": "#1f5a3a",
    "error": "#cc3333",
}

_HEALTH_STATUS_COLORS: dict[str, str] = {
    "unknown": "#666666",
    "green": "#22cc66",
    "yellow": "#cc8800",
    "red": "#cc3333",
}

# ---------------------------------------------------------------------------
# Pipeline state → display text + main label color
# ---------------------------------------------------------------------------

_PIPELINE_DISPLAY: dict[str, tuple[str, str]] = {
    "idle": ("Modelo listo", "#44cc66"),
    "listening": ("Micrófono escuchando", "#44ff44"),
    "processing": ("Modelo procesando", "#ffaa00"),
    "speaking": ("TTS renderizando voz", "#4488ff"),
    "playing": ("TTS hablando", "#44ccff"),
    "downloading": ("Modelo cargando", "#ff8800"),
    "init": ("Modelo cargando", "#888888"),
    "error": ("Modelo error", "#ff5555"),
}


class StatusBar:
    """Manages status pills display and state-driven visual feedback.

    Creates and owns the main status label plus four pill widgets inside a
    parent frame.  Subscribes to a :class:`UIState` observer so that any
    change to ``model_status``, ``mic_status``, ``tts_status``, or
    ``chat_status`` automatically updates the corresponding pill.

    Call :meth:`update_pipeline_state` when the high-level pipeline state
    changes (e.g. from the PTT state machine) — this updates the main label
    and derives appropriate pill statuses.

    Call :meth:`cleanup` before the parent window is destroyed to
    unsubscribe from the UIState observer.
    """

    def __init__(self, parent_frame: ctk.CTkFrame, ui_state: UIState, schedule_ui_update=None) -> None:
        self._parent = parent_frame
        self._ui_state = ui_state
        self._observer_id: int | None = None
        self._schedule_ui_update = schedule_ui_update or (lambda fn: fn())

        # Widget references
        self.lbl_status: ctk.CTkLabel | None = None
        self.lbl_model_status_pill: ctk.CTkLabel | None = None
        self.lbl_mic_status_pill: ctk.CTkLabel | None = None
        self.lbl_tts_status_pill: ctk.CTkLabel | None = None
        self.lbl_chat_status_pill: ctk.CTkLabel | None = None
        self.lbl_health_status_pill: ctk.CTkLabel | None = None

    # ------------------------------------------------------------------
    # Pill creation
    # ------------------------------------------------------------------

    def create_status_pills(self) -> None:
        """Create the main status label and four pill widgets.

        Must be called once after the parent frame exists.  Packs widgets
        left-to-right with consistent spacing.
        """
        self.lbl_status = ctk.CTkLabel(
            self._parent,
            text="Modelo cargando",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#ffaa00",
        )
        self.lbl_status.pack(side="left", padx=(12, 8), pady=10)

        self.lbl_model_status_pill = ctk.CTkLabel(
            self._parent,
            text="Modelo: cargando",
            fg_color="#cc8800",
            corner_radius=12,
            font=ctk.CTkFont(size=11, weight="normal"),
        )
        self.lbl_model_status_pill.pack(side="left", padx=4, pady=8)

        self.lbl_mic_status_pill = ctk.CTkLabel(
            self._parent, text="Mic: revisando",
            fg_color="#1b2633", corner_radius=12,
        )
        self.lbl_mic_status_pill.pack(side="left", padx=4, pady=8)

        self.lbl_tts_status_pill = ctk.CTkLabel(
            self._parent, text="TTS: inactivo",
            fg_color="#1b2633", corner_radius=12,
        )
        self.lbl_tts_status_pill.pack(side="left", padx=4, pady=8)

        self.lbl_chat_status_pill = ctk.CTkLabel(
            self._parent, text="Chat: desconectado",
            fg_color="#1b2633", corner_radius=12,
        )
        self.lbl_chat_status_pill.pack(side="left", padx=4, pady=8)

        self.lbl_health_status_pill = ctk.CTkLabel(
            self._parent, text="Health: --",
            fg_color="#1b2633", corner_radius=12,
        )
        self.lbl_health_status_pill.pack(side="left", padx=4, pady=8)

        # Subscribe to UIState observer for automatic pill updates
        if self._observer_id is not None:
            self._ui_state.unsubscribe(self._observer_id)
        self._observer_id = self._ui_state.subscribe(self._on_state_change)

    # ------------------------------------------------------------------
    # Public status update methods
    # ------------------------------------------------------------------

    def update_model_status(self, status: str) -> None:
        """Update the model status pill.

        Args:
            status: One of ``loading``, ``ready``, ``error``, ``offline``.
        """
        if self.lbl_model_status_pill is None:
            return
        color = self._get_status_color("model_status", status)
        text = self._get_status_text("model_status", status)
        self.lbl_model_status_pill.configure(text=text, fg_color=color)

    def update_mic_status(self, status: str) -> None:
        """Update the mic status pill.

        Args:
            status: One of ``disconnected``, ``idle``, ``listening``, ``recording``.
        """
        if self.lbl_mic_status_pill is None:
            return
        color = self._get_status_color("mic_status", status)
        text = self._get_status_text("mic_status", status)
        self.lbl_mic_status_pill.configure(text=text, fg_color=color)

    def update_tts_status(self, status: str) -> None:
        """Update the TTS status pill.

        Args:
            status: One of ``idle``, ``generating``, ``speaking``, ``error``.
        """
        if self.lbl_tts_status_pill is None:
            return
        color = self._get_status_color("tts_status", status)
        text = self._get_status_text("tts_status", status)
        self.lbl_tts_status_pill.configure(text=text, fg_color=color)

    def update_chat_status(self, status: str) -> None:
        """Update the chat status pill.

        Args:
            status: One of ``disconnected``, ``connecting``, ``connected``, ``error``.
        """
        if self.lbl_chat_status_pill is None:
            return
        color = self._get_status_color("chat_status", status)
        text = self._get_status_text("chat_status", status)
        self.lbl_chat_status_pill.configure(text=text, fg_color=color)

    def update_health_status(self, status: str) -> None:
        """Update the health status pill.

        Args:
            status: One of ``unknown``, ``green``, ``yellow``, ``red``.
        """
        if self.lbl_health_status_pill is None:
            return
        color = _HEALTH_STATUS_COLORS.get(status, "#666666")
        label = status if status != "unknown" else "--"
        self.lbl_health_status_pill.configure(text=f"Health: {label}", fg_color=color)

    def update_pipeline_state(self, state: str) -> None:
        """Update the main status label based on pipeline state.

        Also derives and sets appropriate pill statuses from the pipeline
        state so that all pills stay in sync.

        Args:
            state: One of ``idle``, ``listening``, ``processing``,
                ``speaking``, ``playing``, ``downloading``, ``init``,
                ``error``.
        """
        text, color = _PIPELINE_DISPLAY.get(state, ("", "#aaaaaa"))
        if self.lbl_status is not None:
            self.lbl_status.configure(text=text, text_color=color)

        # Derive pill statuses from pipeline state
        if state == "idle":
            self.update_mic_status("idle")
            self.update_tts_status("idle")
        elif state == "listening":
            self.update_mic_status("listening")
            self.update_tts_status("idle")
        elif state == "processing":
            self.update_tts_status("idle")
        elif state == "speaking":
            self.update_tts_status("generating")
        elif state == "playing":
            self.update_tts_status("speaking")
        elif state in ("downloading", "init"):
            self.update_model_status("loading")
        elif state == "error":
            self.update_model_status("error")
            self.update_tts_status("idle")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_status_color(self, status_type: str, status: str) -> str:
        """Return the pill background color for a status type/value pair."""
        palettes = {
            "model_status": _MODEL_STATUS_COLORS,
            "mic_status": _MIC_STATUS_COLORS,
            "tts_status": _TTS_STATUS_COLORS,
            "chat_status": _CHAT_STATUS_COLORS,
        }
        palette = palettes.get(status_type, {})
        return palette.get(status, "#1b2633")

    def _get_status_text(self, status_type: str, status: str) -> str:
        """Return the display text for a status type/value pair."""
        texts: dict[str, dict[str, str]] = {
            "model_status": {
                "loading": "Modelo: cargando",
                "ready": "Modelo: listo",
                "error": "Modelo: error",
                "offline": "Modelo: offline",
            },
            "mic_status": {
                "disconnected": "Mic: desconectado",
                "idle": "Mic: conectado",
                "listening": "Mic: escuchando",
                "recording": "Mic: grabando",
            },
            "tts_status": {
                "idle": "TTS: inactivo",
                "generating": "TTS: renderizando",
                "speaking": "TTS: hablando",
                "paused": "TTS: pausado · Kira espera operador",
                "error": "TTS: error",
            },
            "chat_status": {
                "disconnected": "Chat: desconectado",
                "connecting": "Chat: conectando",
                "connected": "Chat: conectado",
                "error": "Chat: error",
            },
        }
        return texts.get(status_type, {}).get(status, f"{status_type}: {status}")

    def _on_state_change(self, key: str, value: Any) -> None:
        """UIState observer callback — updates the matching pill."""
        handlers = {
            "model_status": self.update_model_status,
            "mic_status": self.update_mic_status,
            "tts_status": self.update_tts_status,
            "chat_status": self.update_chat_status,
            "health_status": self.update_health_status,
        }
        handler = handlers.get(key)
        if handler and isinstance(value, str):
            self._schedule_ui_update(lambda: handler(value))

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
