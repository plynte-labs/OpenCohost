"""StatusBar — encapsulates status pill creation and state-driven visual feedback.

Manages the main status label and four core status pills (model, mic, TTS, chat)
in the top status bar.  Subscribes to UIState observer for automatic updates
when state fields change, and exposes explicit update methods for pipeline-driven
transitions.
"""

from __future__ import annotations

from typing import Any

import customtkinter as ctk

from opencohost.ui import theme
from opencohost.ui.state import UIState


# ---------------------------------------------------------------------------
# Color palettes — dark background tints for pill fg_color
# All values reference design tokens (theme.*); raw hex lives only in theme.py.
# Consolidation shifts (close-but-not-exact source hex folded into the nearest
# semantic token) are annotated inline as: was #oldhex -> TOKEN.
# ---------------------------------------------------------------------------

_MODEL_STATUS_COLORS: dict[str, str] = {
    "loading": theme.WARNING,        # exact #cc8800
    "ready": theme.SUCCESS,          # exact #22cc66
    "error": theme.DANGER,           # exact #cc3333
    "offline": theme.NEUTRAL_HOVER,  # exact #666666
}

_MIC_STATUS_COLORS: dict[str, str] = {
    "disconnected": theme.MIC_OFFLINE_BG,  # exact #4a2630 (dedicated token)
    "idle": theme.SURFACE_INSET,           # exact #1b2633
    "listening": theme.SUCCESS_DIM,        # exact #1f5a3a
    "recording": theme.WARNING,            # exact #cc8800
}

_TTS_STATUS_COLORS: dict[str, str] = {
    "idle": theme.SURFACE_INSET,  # exact #1b2633
    "generating": theme.INFO,     # exact #1f3f6f
    "speaking": theme.INFO,       # consolidation: was #1f526f -> INFO (#1f3f6f)
    "paused": theme.WARNING,      # consolidation: was #ffaa00 -> WARNING (#cc8800)
    "error": theme.DANGER,        # exact #cc3333
}

_CHAT_STATUS_COLORS: dict[str, str] = {
    "disconnected": theme.SURFACE_INSET,  # exact #1b2633
    "connecting": theme.WARNING,          # exact #cc8800
    "connected": theme.SUCCESS_DIM,       # exact #1f5a3a
    "error": theme.DANGER,                # exact #cc3333
}

_HEALTH_STATUS_COLORS: dict[str, str] = {
    "unknown": theme.NEUTRAL_HOVER,  # exact #666666
    "green": theme.SUCCESS,          # exact #22cc66
    "yellow": theme.WARNING,         # exact #cc8800
    "red": theme.DANGER,             # exact #cc3333
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
    "qwen_active": theme.SUCCESS_DIM,     # green — heavy/cloned voice is what spoke (exact #1f5a3a)
    "edge_fallback": theme.WARNING,       # amber — fell back to Edge (exact #cc8800)
    "qwen_starting": theme.INFO,          # blue — Qwen warming up. DEAD value: update_engine_status
                                          # forces WARNING for qwen_starting, so this entry is never
                                          # rendered. Tokenized faithfully (consolidation: was #33558a -> INFO).
    "not_configured": theme.NEUTRAL,      # grey — voice cloning not set up (exact #555555)
    "piper_local": theme.SUCCESS_DIM,     # green — local light engine (exact #1f5a3a)
    "unknown": theme.NEUTRAL_HOVER,       # exact #666666
}

# Module-level constant — hoisted from _recompute_rollup so it is allocated once,
# not on every state-change call (FIX E: ui_declutter_20260614 adversarial review).
_ROLLUP_CONFIG: dict[str, tuple[str, str]] = {
    "OK":    ("Sistema: OK",     theme.SURFACE_INSET),  # exact #1b2633
    "QUIET": ("Sistema: ...",    theme.NEUTRAL_DIM),    # exact #444444
    "INFO":  ("Sistema: activo", theme.INFO),           # exact #1f3f6f
    "WARN":  ("Sistema: alerta", theme.WARNING),        # exact #cc8800
    "CRIT":  ("Sistema: error",  theme.DANGER),         # exact #cc3333
}

# ---------------------------------------------------------------------------
# Pipeline state → display text + main label color
# ---------------------------------------------------------------------------

_PIPELINE_DISPLAY: dict[str, tuple[str, str]] = {
    "idle": ("Modelo listo", theme.SUCCESS),                  # consolidation: was #44cc66 -> SUCCESS (#22cc66)
    "listening": ("Micrófono escuchando", theme.SUCCESS),     # consolidation: was #44ff44 -> SUCCESS (#22cc66)
    "processing": ("Modelo procesando", theme.WARNING),       # consolidation: was #ffaa00 -> WARNING (#cc8800)
    "speaking": ("TTS renderizando voz", theme.INFO_BRIGHT),  # exact #4488ff
    "playing": ("TTS hablando", theme.INFO_BRIGHT),           # consolidation: was #44ccff -> INFO_BRIGHT (#4488ff)
    "downloading": ("Modelo cargando", theme.WARNING),        # consolidation: was #ff8800 -> WARNING (#cc8800)
    "init": ("Modelo cargando", theme.NEUTRAL_HOVER),         # consolidation: was #888888 -> NEUTRAL_HOVER (#666666)
    "error": ("Modelo error", theme.DANGER),                  # consolidation: was #ff5555 -> DANGER (#cc3333)
}


class StatusBar:
    """Manages status pills display and state-driven visual feedback.

    Creates and owns the main status label plus the top-bar status pills inside
    a parent frame.  The Mic/TTS/Chat pills are created and kept in sync (and
    rolled up into the Sistema pill) but are NOT displayed — those states live in
    the left "Experiencia principal" panel; Sistema/Modelo/Health/Voz stay visible.
    Subscribes to a :class:`UIState` observer so that any change to
    ``model_status``, ``mic_status``, ``tts_status``, or ``chat_status``
    automatically updates the corresponding pill.

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
        """Create the main status label and the status pills.

        Must be called once after the parent frame exists.  Packs the VISIBLE
        pills (Sistema, Modelo, Health, Voz) left-to-right with consistent
        spacing; Mic/TTS/Chat are created but left unpacked (see inline note).
        """
        self.lbl_status = ctk.CTkLabel(
            self._parent,
            text="Modelo cargando",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=theme.WARNING,  # consolidation: was #ffaa00 -> WARNING (#cc8800)
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
            fg_color=theme.WARNING,  # exact #cc8800
            corner_radius=12,
            font=ctk.CTkFont(size=11, weight="normal"),
        )
        self.lbl_sistema_pill.pack(side="left", padx=4, pady=8)
        # Synchronise pill display with actual startup _sistema_state values
        self._recompute_rollup()

        # Modelo + Health stay VISIBLE — top-bar-native status not shown elsewhere.
        self.lbl_model_status_pill = ctk.CTkLabel(
            self._parent,
            text="Modelo: cargando",
            fg_color=theme.WARNING,  # exact #cc8800
            corner_radius=12,
            font=ctk.CTkFont(size=11, weight="normal"),
        )
        self.lbl_model_status_pill.pack(side="left", padx=4, pady=8)

        # Mic / TTS / Chat pills are CREATED (kept in sync by their update_* methods and
        # rolled up into the Sistema pill) but intentionally NOT packed: these states now
        # live in the left "Experiencia principal" panel (🎤 / 🔊 / 💬), so repeating them
        # in the top bar is redundant. Re-add .pack(side="left", padx=4, pady=8) to restore.
        self.lbl_mic_status_pill = ctk.CTkLabel(
            self._parent, text="Mic: revisando",
            fg_color=theme.SURFACE_INSET, corner_radius=12, font=ctk.CTkFont(size=11, weight="normal"),  # exact #1b2633
        )

        self.lbl_tts_status_pill = ctk.CTkLabel(
            self._parent, text="TTS: inactivo",
            fg_color=theme.SURFACE_INSET, corner_radius=12, font=ctk.CTkFont(size=11, weight="normal"),  # exact #1b2633
        )

        self.lbl_chat_status_pill = ctk.CTkLabel(
            self._parent, text="Chat: desconectado",
            fg_color=theme.SURFACE_INSET, corner_radius=12, font=ctk.CTkFont(size=11, weight="normal"),  # exact #1b2633
        )

        self.lbl_health_status_pill = ctk.CTkLabel(
            self._parent, text="Health: --",
            fg_color=theme.SURFACE_INSET, corner_radius=12, font=ctk.CTkFont(size=11, weight="normal"),  # exact #1b2633
        )
        self.lbl_health_status_pill.pack(side="left", padx=4, pady=8)

        self.lbl_engine_status_pill = ctk.CTkLabel(
            self._parent, text="Voz: --",
            fg_color=theme.SURFACE_INSET, corner_radius=12, font=ctk.CTkFont(size=11, weight="normal"),  # exact #1b2633
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
        color = _HEALTH_STATUS_COLORS.get(status, theme.NEUTRAL_HOVER)  # exact #666666
        label = _HEALTH_LABELS.get(status, status)
        self.lbl_health_status_pill.configure(text=f"Health: {label}", fg_color=color)

    def update_engine_status(self, status: str, reason: str = "") -> None:
        """Update the engine (voice) badge pill with the EFFECTIVE engine.

        Visibility gating (owner decision 2026-06-14):
        - qwen_active, piper_local: badge uses dim color (theme.SURFACE_INSET) — normal
          steady-state, no need to draw operator attention.
        - qwen_starting: badge uses INFO-visible amber (theme.WARNING) — Edge speaks during
          Qwen warmup and the operator should understand the transient voice change.
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
            color = theme.SURFACE_INSET  # dim — normal operation, no operator action needed (exact #1b2633)
        elif status == "qwen_starting":
            # Owner decision: qwen_starting → INFO-visible (amber) because Edge
            # is speaking during warmup and the operator should know.
            color = theme.WARNING  # exact #cc8800
        else:
            # edge_fallback, not_configured, unknown — use the standard alert color
            color = _ENGINE_STATUS_COLORS.get(status, theme.NEUTRAL_HOVER)  # exact #666666
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
        # consolidation: unknown-state fallback label was #aaaaaa -> TEXT_MUTED (#a9bdd3)
        text, color = _PIPELINE_DISPLAY.get(state, ("", theme.TEXT_MUTED))
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

        Display (colors come from _ROLLUP_CONFIG, all theme tokens):
          OK    → "Sistema: OK"     / dim dark  theme.SURFACE_INSET
          QUIET → "Sistema: ..."    / grey      theme.NEUTRAL_DIM
          INFO  → "Sistema: activo" / blue      theme.INFO
          WARN  → "Sistema: alerta" / amber     theme.WARNING
          CRIT  → "Sistema: error"  / red       theme.DANGER
        CRIT and WARN append the degraded dimension(s) — e.g. "Sistema: error · salud" —
        because the per-dimension pills are collapsed (see create_status_pills).
        """
        if self.lbl_sistema_pill is None:
            return

        s = self._sistema_state

        # Collect which dimension(s) drive the current severity so the collapsed Sistema
        # pill names WHAT degraded (the per-dimension pills are not displayed). Same
        # predicates and priority as before — only the text gains a "· dim" suffix.
        crit = [lbl for lbl, bad in (
            ("modelo", s.get("model") == "error"),
            ("salud", s.get("health") == "red"),
            ("TTS", s.get("tts") == "error"),
        ) if bad]
        warn = [lbl for lbl, bad in (
            ("modelo", s.get("model") == "loading"),
            ("salud", s.get("health") == "yellow"),
            ("mic", s.get("mic") == "disconnected"),
        ) if bad]

        if crit:
            severity, dims = "CRIT", crit
        elif warn:
            severity, dims = "WARN", warn
        elif (
            s.get("tts") in ("generating", "paused", "speaking")
            or s.get("mic") in ("recording", "listening")
        ):
            severity, dims = "INFO", []
        elif s.get("health") == "unknown":
            severity, dims = "QUIET", []
        else:
            severity, dims = "OK", []

        text, color = _ROLLUP_CONFIG[severity]
        if dims:
            text = f"{text} · {', '.join(dims)}"
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
        return palette.get(status, theme.SURFACE_INSET)  # default pill bg, exact #1b2633

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
