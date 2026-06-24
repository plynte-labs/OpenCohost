"""StatusBar — encapsulates status pill creation and state-driven visual feedback.

Manages the main status label and four core status pills (model, mic, TTS, chat)
in the top status bar.  Subscribes to UIState observer for automatic updates
when state fields change, and exposes explicit update methods for pipeline-driven
transitions.
"""

from __future__ import annotations

from typing import Any

import customtkinter as ctk

from opencohost.ui.state import UIState


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

# Human-readable labels for the health pill — internal tokens ("red") are not
# operator language. Unknown tokens fall through to the raw value (permissive).
_HEALTH_LABELS: dict[str, str] = {
    "green": "OK",
    "yellow": "alerta",
    "red": "falla crítica",
    "unknown": "--",
}

_ENGINE_STATUS_COLORS: dict[str, str] = {
    "qwen_active": "#1f5a3a",     # green — heavy/cloned voice is what spoke
    "edge_fallback": "#cc8800",   # amber — fell back to Edge
    "qwen_starting": "#33558a",   # blue — Qwen warming up
    "not_configured": "#555555",  # grey — voice cloning not set up
    "piper_local": "#1f5a3a",     # green — local light engine
    "unknown": "#666666",
}

# Module-level constant — hoisted from _recompute_rollup so it is allocated once,
# not on every state-change call (FIX E: ui_declutter_20260614 adversarial review).
_ROLLUP_CONFIG: dict[str, tuple[str, str]] = {
    "OK":    ("Sistema: OK",     "#1b2633"),
    "QUIET": ("Sistema: ...",    "#444444"),
    "INFO":  ("Sistema: activo", "#1f3f6f"),
    "WARN":  ("Sistema: alerta", "#cc8800"),
    "CRIT":  ("Sistema: error",  "#cc3333"),
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

        # Sistema rollup state — tracks per-dimension health for the aggregated pill.
        # Values are the last status string received from each dimension.
        # Initial values reflect the "unknown/startup" state before any update.
        self._sistema_state: dict[str, str] = {
            "model": "loading",
            "mic": "disconnected",
            "tts": "idle",
            "health": "unknown",
        }

        # Widget references
        self.lbl_status: ctk.CTkLabel | None = None
        self.lbl_sistema_pill: ctk.CTkLabel | None = None
        self.lbl_model_status_pill: ctk.CTkLabel | None = None
        self.lbl_mic_status_pill: ctk.CTkLabel | None = None
        self.lbl_tts_status_pill: ctk.CTkLabel | None = None
        self.lbl_chat_status_pill: ctk.CTkLabel | None = None
        self.lbl_health_status_pill: ctk.CTkLabel | None = None
        self.lbl_engine_status_pill: ctk.CTkLabel | None = None

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

        # Sistema rollup pill — aggregates model + mic + TTS + health into a
        # single always-visible indicator. Turns amber/red only on degradation;
        # stays dim ("Sistema: OK") during normal steady-state operation.
        # Initial text/color reflect WARN (model=loading, mic=disconnected startup state).
        # _recompute_rollup() immediately overwrites these to keep them in sync.
        self.lbl_sistema_pill = ctk.CTkLabel(
            self._parent,
            text="Sistema: alerta",
            fg_color="#cc8800",
            corner_radius=12,
            font=ctk.CTkFont(size=11, weight="normal"),
        )
        self.lbl_sistema_pill.pack(side="left", padx=4, pady=8)
        # Synchronise pill display with actual startup _sistema_state values
        self._recompute_rollup()

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

        self.lbl_engine_status_pill = ctk.CTkLabel(
            self._parent, text="Voz: --",
            fg_color="#1b2633", corner_radius=12,
        )
        self.lbl_engine_status_pill.pack(side="left", padx=4, pady=8)

        # Subscribe to UIState observer for automatic pill updates
        if self._observer_id is not None:
            self._ui_state.unsubscribe(self._observer_id)
        self._observer_id = self._ui_state.subscribe(self._on_state_change)

    # ------------------------------------------------------------------
    # Public status update methods
    # ------------------------------------------------------------------

    def update_model_status(self, status: str) -> None:
        """Update the model status pill and the Sistema rollup.

        Args:
            status: One of ``loading``, ``ready``, ``error``, ``offline``.
        """
        # Always update rollup cache and recompute — even when the individual pill is None.
        self._sistema_state["model"] = status
        self._recompute_rollup()
        if self.lbl_model_status_pill is None:
            return
        color = self._get_status_color("model_status", status)
        text = self._get_status_text("model_status", status)
        self.lbl_model_status_pill.configure(text=text, fg_color=color)

    def update_mic_status(self, status: str) -> None:
        """Update the mic status pill and the Sistema rollup.

        Args:
            status: One of ``disconnected``, ``idle``, ``listening``, ``recording``.
        """
        # Always update rollup cache and recompute — even when the individual pill is None.
        self._sistema_state["mic"] = status
        self._recompute_rollup()
        if self.lbl_mic_status_pill is None:
            return
        color = self._get_status_color("mic_status", status)
        text = self._get_status_text("mic_status", status)
        self.lbl_mic_status_pill.configure(text=text, fg_color=color)

    def update_tts_status(self, status: str) -> None:
        """Update the TTS status pill and the Sistema rollup.

        Args:
            status: One of ``idle``, ``generating``, ``speaking``, ``error``.
        """
        # Always update rollup cache and recompute — even when the individual pill is None.
        self._sistema_state["tts"] = status
        self._recompute_rollup()
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
        """Update the health status pill and the Sistema rollup.

        Args:
            status: One of ``unknown``, ``green``, ``yellow``, ``red``.
        """
        # Always update rollup cache and recompute — even when the individual pill is None.
        self._sistema_state["health"] = status
        self._recompute_rollup()
        if self.lbl_health_status_pill is None:
            return
        color = _HEALTH_STATUS_COLORS.get(status, "#666666")
        label = _HEALTH_LABELS.get(status, status)
        self.lbl_health_status_pill.configure(text=f"Health: {label}", fg_color=color)

    def update_engine_status(self, status: str, reason: str = "") -> None:
        """Update the engine (voice) badge pill with the EFFECTIVE engine.

        Visibility gating (owner decision 2026-06-14):
        - qwen_active, piper_local: badge uses dim color (#1b2633) — normal steady-state,
          no need to draw operator attention.
        - qwen_starting: badge uses INFO-visible amber (#cc8800) — Edge speaks during Qwen
          warmup and the operator should understand the transient voice change.
        - edge_fallback, not_configured, unknown: badge uses the standard alert color —
          operator must notice the fallback.

        Args:
            status: One of the qwen_markers.ENGINE_STATUSES values.
            reason: Free-form fallback reason, shown for ``edge_fallback``.
        """
        if self.lbl_engine_status_pill is None:
            return
        text = self._engine_status_text(status, reason)
        # Visibility rule: dim on normal steady-state; visible on fallback or startup warmup.
        if status in ("qwen_active", "piper_local"):
            color = "#1b2633"  # dim — normal operation, no operator action needed
        elif status == "qwen_starting":
            # Owner decision: qwen_starting → INFO-visible (amber) because Edge
            # is speaking during warmup and the operator should know.
            color = "#cc8800"
        else:
            # edge_fallback, not_configured, unknown — use the standard alert color
            color = _ENGINE_STATUS_COLORS.get(status, "#666666")
        self.lbl_engine_status_pill.configure(text=text, fg_color=color)

    def _engine_status_text(self, status: str, reason: str = "") -> str:
        """Display text for the engine badge — always the EFFECTIVE engine."""
        if status == "qwen_active":
            return "Voz: Qwen clonada"
        if status == "qwen_starting":
            return "Voz: Qwen iniciando"
        if status == "not_configured":
            return "Voz: clonación no configurada"
        if status == "piper_local":
            return "Voz: Piper local"
        if status == "edge_fallback":
            return f"Voz: Edge respaldo: {reason}" if reason else "Voz: Edge respaldo"
        return "Voz: iniciando…"

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

    def _recompute_rollup(self) -> None:
        """Recompute the Sistema rollup pill from the current _sistema_state.

        Priority table (highest wins):
          CRIT  — model=error | health=red | tts=error
          WARN  — model=loading | health=yellow | mic=disconnected
          INFO  — tts in (generating, paused) | mic=recording
          QUIET — health=unknown (and no higher severity)
          OK    — all nominal

        Display:
          OK    → "Sistema: OK"     / dim dark #1b2633
          QUIET → "Sistema: ..."    / grey #444444
          INFO  → "Sistema: activo" / blue #1f3f6f
          WARN  → "Sistema: alerta" / amber #cc8800
          CRIT  → "Sistema: error"  / red #cc3333
        """
        if self.lbl_sistema_pill is None:
            return

        s = self._sistema_state

        if s.get("model") == "error" or s.get("health") == "red" or s.get("tts") == "error":
            severity = "CRIT"
        elif (
            s.get("model") == "loading"
            or s.get("health") == "yellow"
            or s.get("mic") == "disconnected"
        ):
            severity = "WARN"
        elif (
            s.get("tts") in ("generating", "paused", "speaking")
            or s.get("mic") in ("recording", "listening")
        ):
            severity = "INFO"
        elif s.get("health") == "unknown":
            severity = "QUIET"
        else:
            severity = "OK"

        text, color = _ROLLUP_CONFIG[severity]
        self.lbl_sistema_pill.configure(text=text, fg_color=color)

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
                "error": "Modelo: error · revisa Ollama",
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
            "engine_status": lambda v: self.update_engine_status(
                v, self._ui_state.get("engine_reason", "")
            ),
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
