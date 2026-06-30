"""Voice Control Panel module for OpenCohost.

Encapsulates all voice input functionality:
- WebSocket management (LiveAudio connection with auto-reconnect)
- Voice button state machine (pipeline-driven UI updates)

This module is decoupled from app.py internals and uses UIState for
all state communication.  Widget references are injected at construction
time so the panel can update the UI without knowing about app.py structure.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from typing import Any, Callable, Optional

import websockets

import customtkinter as ctk

from opencohost.config.settings import (
    WS_URI, WS_TIMEOUT, WS_RECONNECT_BASE_DELAY,
    WS_RECONNECT_MAX_DELAY, WS_MAX_RETRIES,
    BASE_DIR, TEMP_DIR,
)
from opencohost.config.logger import get_logger
from opencohost.ui.state import UIState

logger = get_logger()


class VoiceControlPanel:
    """Manages voice input: WebSocket, audio recording, RMS, button states.

    The panel does NOT own top-level application state.  It reads/writes
    through the injected ``ui_state`` object and calls injected callbacks
    for logging and motor-IA commands.  This keeps it testable and
    decoupled from VocalAIApp internals.

    Usage::

        panel = VoiceControlPanel(
            parent_frame=voice_panel,
            ui_state=UIState(),
            logger=logger,
            on_log=lambda msg: print(msg),
            on_motor_event=lambda status: ...,
            on_pipeline_change=lambda state: ...,
        )
        panel.create_voice_panel()
        panel.connect_ws()
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        parent_frame: ctk.CTkFrame,
        ui_state: UIState,
        logger: Any,
        on_log: Callable[[str], None],
        on_motor_event: Callable[[str], None],
        on_pipeline_change: Callable[[str], None],
        dispositivo_seleccionado: Optional[int] = None,
        schedule_ui_update: Optional[Callable[[Callable[[], None]], None]] = None,
        external_primary_button: Optional[ctk.CTkButton] = None,
    ) -> None:
        """Initialize the voice control panel.

        Args:
            parent_frame: CTkFrame where voice widgets will be placed.
            ui_state: Thread-safe UIState for reading/writing state.
            logger: Application logger instance.
            on_log: Callback to send log messages to the main UI.
            on_motor_event: Callback for motor IA status events.
            on_pipeline_change: Callback invoked when pipeline state changes.
            dispositivo_seleccionado: Currently selected audio device ID.
            schedule_ui_update: Callable to schedule UI updates on main thread.
        """
        self._parent = parent_frame
        self._ui_state = ui_state
        self._logger = logger
        self._on_log = on_log
        self._on_motor_event = on_motor_event
        self._on_pipeline_change = on_pipeline_change
        self._dispositivo_seleccionado = dispositivo_seleccionado
        self._schedule_ui_update: Callable[[Callable[[], None]], None] = (
            schedule_ui_update if schedule_ui_update is not None else (lambda fn: fn())
        )
        self._external_primary_button = external_primary_button

        # Internal state
        self._pipeline_state: str = "idle"
        self._ws_connected: bool = False
        self._ws_should_reconnect: bool = False
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_lock = threading.Lock()
        self._recording_thread: Optional[threading.Thread] = None
        self._recording_stream: Any = None
        self._ptt_accept_logged: bool = False

        # PTT transcription buffer with grace period
        self._ptt_buffer: str = ""
        # D2 (ptt_keyup_reconcile_20260627): raised 500 → 2000 so a long held
        # dictation (the 'biblia') is not truncated to ~30-40s before the
        # missed-key-up flush delivers it. ~2000 chars ≈ a few minutes of speech.
        self._ptt_max_chars: int = 2000
        self._ptt_grace_period: float = 5.0
        self._ptt_grace_deadline: float = 0.0
        self._ptt_prev_active: bool = False  # track press→release transition
        self._ptt_flush_thread: Optional[threading.Thread] = None
        self._ptt_flush_stop = threading.Event()
        # Fix: audit/ui-security-perf-2026-05-17 — _ptt_buffer, _ptt_grace_deadline,
        # _ptt_prev_active are accessed from WebSocket async listener AND PTT flush
        # watcher threads concurrently. Single lock prevents torn reads and corrupted
        # buffer accumulation.
        self._ptt_lock = threading.Lock()

        # Motor IA reference (set after construction)
        self._motor_ia: Any = None

        # Widget references (created in create_voice_panel)
        self.btn_primary_voice: Optional[ctk.CTkButton] = None
        self.lbl_kira_voice_state: Optional[ctk.CTkLabel] = None
        self.lbl_kira_tts_state: Optional[ctk.CTkLabel] = None
        self.lbl_kira_memory_state: Optional[ctk.CTkLabel] = None
        self.lbl_kira_chat_state: Optional[ctk.CTkLabel] = None
        self.lbl_voice_hint: Optional[ctk.CTkLabel] = None

        # Subscribe to UIState changes for state-driven UI updates
        self._state_sub_id: int = self._ui_state.subscribe(self._on_state_change)

    def set_motor_ia(self, motor_ia: Any) -> None:
        """Set the MotorVocalIA reference after it has been created.

        Must be called before any WebSocket or recording operations.
        """
        self._motor_ia = motor_ia

    def clear_ptt_buffer(self) -> None:
        """Clear the PTT transcription buffer.

        Called when PTT is pressed to start a new accumulation cycle.
        """
        # Fix: audit/ui-security-perf-2026-05-17 — lock all PTT state writes;
        # prevents flush watcher thread from seeing partially-cleared buffer.
        with self._ptt_lock:
            self._ptt_buffer = ""
            self._ptt_grace_deadline = 0.0
            self._ptt_prev_active = True  # Mark PTT as active; release will trigger grace
        self._start_ptt_flush_watcher()

    def on_ptt_release(self) -> None:
        """Called when PTT key is released. Starts the grace period immediately."""
        # Fix: audit/ui-security-perf-2026-05-17 — lock prevents WebSocket listener
        # from observing inconsistent _ptt_prev_active / _ptt_grace_deadline.
        with self._ptt_lock:
            self._ptt_prev_active = False
            self._ptt_grace_deadline = time.time() + self._ptt_grace_period
        self._logger.debug(f"[PTT] Release → grace period {self._ptt_grace_period}s (deadline set immediately)")
        self._on_log(f"[PTT] Soltado — esperando transcripción final ({self._ptt_grace_period:.0f}s)...")

    def _start_ptt_flush_watcher(self) -> None:
        """Start background thread that flushes PTT buffer when grace period expires."""
        if self._ptt_flush_thread and self._ptt_flush_thread.is_alive():
            return
        self._ptt_flush_stop.clear()
        self._ptt_flush_thread = threading.Thread(
            target=self._ptt_flush_watcher, daemon=True
        )
        self._ptt_flush_thread.start()

    def _ptt_flush_watcher(self) -> None:
        """Watch for grace period expiration and flush buffer."""
        while not self._ptt_flush_stop.is_set():
            try:
                now = time.time()
                # Fix: audit/ui-security-perf-2026-05-17 — snapshot shared state under lock
                # to avoid torn reads from concurrent WebSocket listener writes.
                with self._ptt_lock:
                    deadline = self._ptt_grace_deadline
                    has_buffer = bool(self._ptt_buffer)
                if deadline > 0 and now >= deadline:
                    if has_buffer:
                        self._flush_ptt_buffer()
                    else:
                        self._logger.debug("[PTT Flush] Grace period expiró sin transcripciones acumuladas")
                    # Fix: audit/ui-security-perf-2026-05-17 — lock reset to prevent
                    # WebSocket listener from extending an already-expired deadline.
                    with self._ptt_lock:
                        self._ptt_grace_deadline = 0.0
            except Exception:
                self._logger.exception("Unexpected error in PTT flush watcher")
            self._ptt_flush_stop.wait(0.5)

    def _flush_ptt_buffer(self) -> None:
        """Flush accumulated PTT buffer to motor IA."""
        # Fix: audit/ui-security-perf-2026-05-17 — lock protects buffer read+clear
        # against concurrent writes from WebSocket listener thread.
        with self._ptt_lock:
            texto = self._ptt_buffer.strip()
            self._ptt_buffer = ""

        if not texto:
            return

        # Fix: audit/ui-security-perf-2026-05-17 — truncate speech to prevent PII in logs
        self._logger.info(f"[PTT Flush] Enviando texto acumulado ({len(texto)} chars): {texto[:30]}...")

        motor_busy = self._motor_ia and (
            getattr(self._motor_ia, 'is_speaking', False)
            or getattr(self._motor_ia, 'is_processing', False)
        )

        if motor_busy:
            self._logger.debug("[PTT Flush] Motor ocupado → cola prioritaria")
            self._motor_ia.enqueue(
                f"El streamer acaba de decir (PTT): {texto}",
                priority=0,
                source="ptt",
            )
            return

        palabras = texto.split()
        if len(palabras) < 2:
            self._logger.debug(f"[PTT Flush] Muy corto ({len(palabras)} palabras): {texto}")
            return

        # Fix: audit/ui-security-perf-2026-05-17 — truncate speech to prevent PII in logs
        self._on_log(f"[PTT]: {texto[:30]}{'...' if len(texto) > 30 else ''}")
        self._motor_ia.command_queue.put((
            "process_context",
            f"El streamer acaba de decir (PTT): {texto}"
        ))

    # ------------------------------------------------------------------
    # Panel creation
    # ------------------------------------------------------------------

    def create_voice_panel(self) -> None:
        """Create and layout all voice control widgets within parent_frame.

        Builds the voice panel header, state strip, hint label, and primary
        voice button.  All widgets are stored as instance attributes for
        later manipulation.
        """
        # State strip
        kira_state_strip = ctk.CTkFrame(self._parent, fg_color="transparent")
        kira_state_strip.grid(row=1, column=0, sticky="ew", padx=12, pady=(2, 2))
        for col in range(2):
            kira_state_strip.grid_columnconfigure(col, weight=1, uniform="pill")

        self.lbl_kira_voice_state = ctk.CTkLabel(
            kira_state_strip, text="🎤 listo",
            fg_color="#1b2633", corner_radius=10, anchor="center", font=ctk.CTkFont(size=12)
        )
        self.lbl_kira_voice_state.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=(0, 4))

        self.lbl_kira_tts_state = ctk.CTkLabel(
            kira_state_strip, text="🔊 idle",
            fg_color="#1b2633", corner_radius=10, anchor="center", font=ctk.CTkFont(size=12)
        )
        self.lbl_kira_tts_state.grid(row=0, column=1, sticky="ew", padx=(4, 0), pady=(0, 4))

        self.lbl_kira_memory_state = ctk.CTkLabel(
            kira_state_strip, text="🧠 disponible",
            fg_color="#1b2633", corner_radius=10, anchor="center", font=ctk.CTkFont(size=12)
        )
        self.lbl_kira_memory_state.grid(row=1, column=0, sticky="ew", padx=(0, 4), pady=(4, 0))

        self.lbl_kira_chat_state = ctk.CTkLabel(
            kira_state_strip, text="💬 desconectado",
            fg_color="#1b2633", corner_radius=10, anchor="center", font=ctk.CTkFont(size=12)
        )
        self.lbl_kira_chat_state.grid(row=1, column=1, sticky="ew", padx=(4, 0), pady=(4, 0))

        # Hint label
        self.lbl_voice_hint = ctk.CTkLabel(
            self._parent,
            text="Conecta el reconocimiento de voz en tiempo real. PTT requiere LiveAudio activo.",
            text_color="#8fa3b8", anchor="w"
        )
        self.lbl_voice_hint.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 2))

        # Voice actions
        if self._external_primary_button is not None:
            self.btn_primary_voice = self._external_primary_button
        else:
            voice_actions = ctk.CTkFrame(self._parent, fg_color="transparent")
            voice_actions.grid(row=3, column=0, sticky="ew", padx=12, pady=(0, 12))
            voice_actions.grid_columnconfigure(0, weight=1)
            self.btn_primary_voice = ctk.CTkButton(
                voice_actions,
                text="Hablar",
                command=self._toggle_websocket,
                state="disabled",
                height=72,
                font=ctk.CTkFont(size=21, weight="bold"),
                fg_color="#1f7a5a",
                hover_color="#24946c"
            )
            self.btn_primary_voice.grid(row=0, column=0, sticky="ew", padx=(0, 10), pady=0)

    # ------------------------------------------------------------------
    # WebSocket management
    # ------------------------------------------------------------------

    def connect_ws(self, uri: Optional[str] = None) -> None:
        """Start WebSocket connection to LiveAudio server.

        Initiates a background thread that runs the async WebSocket
        client with automatic reconnection on failure.

        Args:
            uri: WebSocket URI. Uses WS_URI from settings if None.
        """
        with self._ws_lock:
            if self._ws_connected or (self._ws_thread and self._ws_thread.is_alive()):
                return

            self._ws_connected = True
            self._ws_should_reconnect = True
            self._ui_state.ws_connected = True
            self._ui_state.ws_should_reconnect = True

            self._ws_thread = threading.Thread(target=self._run_ws_client, daemon=True)
            self._ws_thread.start()

    def disconnect_ws(self) -> None:
        """Request WebSocket disconnection.

        Sets flags to stop the reconnection loop.  The actual thread
        will exit gracefully after the current WebSocket operation.
        """
        self._ws_connected = False
        self._ws_should_reconnect = False
        self._ui_state.ws_connected = False
        self._ui_state.ws_should_reconnect = False
        self._on_log("[Red] Desconexión solicitada.")

    def is_ws_connected(self) -> bool:
        """Return whether the WebSocket is currently connected."""
        return self._ws_connected

    def _toggle_websocket(self) -> None:
        """Toggle WebSocket connection on/off (button handler)."""
        if not self._ws_connected:
            self.connect_ws()
            self._update_button_for_state("listening")
        else:
            self.disconnect_ws()
            self._update_button_for_state("idle")

    def _run_ws_client(self) -> None:
        """Run the async WebSocket client in a dedicated event loop."""
        loop = None
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._ws_reconnect_loop())
        except Exception:
            self._logger.exception("Unexpected error in WebSocket client thread")
            self._ws_connected = False
            self._ws_should_reconnect = False
            self._ui_state.ws_connected = False
            self._ui_state.ws_should_reconnect = False
            try:
                self._on_log("[Red] Error inesperado en LiveAudio. Desconectado.")
            except Exception:
                self._logger.exception("Unexpected error while reporting WebSocket failure")
            try:
                self._schedule_ui_update(lambda: self.set_state("error"))
            except Exception:
                self._logger.exception("Unexpected error while marking WebSocket failure state")
        finally:
            if loop is not None:
                loop.close()

    async def _ws_reconnect_loop(self) -> None:
        """Main reconnection loop with exponential backoff."""
        retry_count = 0
        delay = WS_RECONNECT_BASE_DELAY

        while self._ws_should_reconnect and retry_count < WS_MAX_RETRIES:
            try:
                await self._ws_listener()
                if not self._ws_should_reconnect:
                    break
            except Exception as e:
                retry_count += 1
                self._logger.warning(
                    f"WebSocket error (intento {retry_count}/{WS_MAX_RETRIES}): {e}"
                )
                self._on_log(
                    f"[Red] ⚠️ Conexión perdida. Reintentando en {delay:.0f}s... "
                    f"({retry_count}/{WS_MAX_RETRIES})"
                )

                if not self._ws_should_reconnect:
                    break

                await asyncio.sleep(delay)
                delay = min(delay * 2, WS_RECONNECT_MAX_DELAY)

        if retry_count >= WS_MAX_RETRIES:
            self._on_log("[Red] ❌ Máximo de reintentos alcanzado. Desconectado.")
            self._logger.error("WebSocket: máximo de reintentos alcanzado.")
            self._schedule_ui_update(lambda: self._update_button_for_state("idle"))

        self._ws_connected = False
        self._ws_should_reconnect = False
        self._ui_state.ws_connected = False
        self._ui_state.ws_should_reconnect = False

    async def _ws_listener(self) -> None:
        """Single WebSocket session: receive and process transcriptions."""
        self._on_log(f"[Red] Conectando a LiveAudio en {WS_URI}...")

        async with websockets.connect(
            WS_URI,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as websocket:
            self._on_log("[Red] 🟢 Conectado. Escuchando transcripciones...")
            self._logger.info("WebSocket conectado.")
            self._schedule_ui_update(lambda: self.set_state("listening"))

            while self._ws_connected:
                try:
                    mensaje = await asyncio.wait_for(
                        websocket.recv(),
                        timeout=WS_TIMEOUT
                    )
                    data = json.loads(mensaje)
                    texto_transcrito = data.get("text", "").strip()

                    if texto_transcrito:
                        # Fix: audit/ui-security-perf-2026-05-17 — truncate speech to prevent PII in logs
                        self._logger.debug(
                            f"WS recibido ({len(texto_transcrito)} chars): "
                            f"{texto_transcrito[:30]}..."
                        )

                    # --- Anti-Loop Whisper Filter (Sanitización Agresiva) ---
                    # Reduce frases o palabras repetidas consecutivamente 3 o más veces a una sola.
                    texto_original = texto_transcrito
                    texto_transcrito = re.sub(r'\b(.+?)(?:\s+\1\b){2,}', r'\1', texto_transcrito, flags=re.IGNORECASE).strip()
                    
                    if texto_transcrito != texto_original:
                        # Fix: audit/ui-security-perf-2026-05-17 — truncate speech to prevent PII in logs
                        self._logger.info(
                            f"Filtro Anti-Loop aplicado. "
                            f"Original: {texto_original[:30]}... "
                            f"Limpio: {texto_transcrito[:30]}..."
                        )

                    # PTT gate with buffer + grace period
                    ptt_now = self._ui_state.ptt_active
                    now = time.time()

                    # Fix: audit/ui-security-perf-2026-05-17 — lock all PTT buffer
                    # state access; prevents flush watcher thread from seeing
                    # partially-accumulated buffer or inconsistent grace deadline.
                    with self._ptt_lock:
                        # Detect press→release transition: start grace period
                        if self._ptt_prev_active and not ptt_now:
                            self._ptt_grace_deadline = now + self._ptt_grace_period
                            self._logger.debug(f"[PTT] Release → grace period {self._ptt_grace_period}s")

                        self._ptt_prev_active = ptt_now

                        in_grace = now < self._ptt_grace_deadline

                        if self._ui_state.ptt_enabled and not ptt_now and not in_grace:
                            # Outside PTT window and grace period → discard
                            if texto_transcrito:
                                # Fix: audit/ui-security-perf-2026-05-17 — truncate speech to prevent PII in logs
                                self._logger.debug(
                                    f"WS descartado (PTT inactivo, fuera de gracia): {texto_transcrito[:30]}..."
                                )
                            continue

                        # When PTT is active or in grace period, accumulate to buffer
                        if self._ui_state.ptt_enabled and (ptt_now or in_grace):
                            if texto_transcrito:
                                if len(self._ptt_buffer) < self._ptt_max_chars:
                                    if self._ptt_buffer:
                                        self._ptt_buffer += " " + texto_transcrito
                                    else:
                                        self._ptt_buffer = texto_transcrito
                                    if in_grace and not ptt_now:
                                        self._ptt_grace_deadline = now + self._ptt_grace_period
                                    # Fix: audit/ui-security-perf-2026-05-17 — truncate speech to prevent PII in logs
                                    self._logger.debug(
                                        f"[PTT Buffer] Acumulado ({len(self._ptt_buffer)} chars): {self._ptt_buffer[:30]}..."
                                    )
                                else:
                                    self._logger.warning(
                                        f"[PTT Buffer] Límite de {self._ptt_max_chars} chars alcanzado. Truncando."
                                    )
                            continue  # Don't send yet — flush watcher handles expiration

                    # Non-PTT mode (continuous WebSocket) — original behavior
                    motor_busy = self._motor_ia and (
                        getattr(self._motor_ia, 'is_speaking', False)
                        or getattr(self._motor_ia, 'is_processing', False)
                    )
                    if motor_busy:
                        if texto_transcrito:
                            # Fix: audit/ui-security-perf-2026-05-17 — truncate speech to prevent PII in logs
                            self._logger.debug(
                                f"WS descartado (IA ocupada): {texto_transcrito[:30]}..."
                            )
                        continue

                    # Minimum length filter
                    palabras = texto_transcrito.split()
                    if len(palabras) < 4:
                        if texto_transcrito:
                            # Fix: audit/ui-security-perf-2026-05-17 — truncate speech to prevent PII in logs
                            self._logger.debug(
                                f"WS descartado (muy corto, {len(palabras)} palabras): "
                                f"{texto_transcrito[:30]}..."
                            )
                        continue

                    if texto_transcrito:
                        if self._motor_ia is None:
                            self._logger.warning("Motor IA no disponible, transcripción descartada.")
                            continue
                        # Fix: audit/ui-security-perf-2026-05-17 — truncate speech to prevent PII in logs
                        self._on_log(f"[LiveAudio]: {texto_transcrito[:30]}{'...' if len(texto_transcrito) > 30 else ''}")
                        self._motor_ia.command_queue.put((
                            "process_context",
                            f"El streamer acaba de decir: {texto_transcrito}"
                        ))
                        # Fix: audit/ui-security-perf-2026-05-17 — truncate speech to prevent PII in logs
                        self._logger.info(
                            f"Transcripción aceptada: {texto_transcrito[:30]}..."
                        )

                except asyncio.TimeoutError:
                    continue
                except json.JSONDecodeError as e:
                    self._logger.warning(f"JSON inválido del WebSocket: {e}")
                except websockets.exceptions.ConnectionClosed as e:
                    self._logger.warning(f"WebSocket cerrado: {e}")
                    raise

    # ------------------------------------------------------------------
    # State machine
    # ------------------------------------------------------------------

    def set_state(self, state: str) -> None:
        """Set the pipeline state and trigger UI updates.

        Valid states: idle, listening, processing, speaking, playing,
        downloading, error.

        Args:
            state: New pipeline state.
        """
        self._pipeline_state = state
        self._ui_state.pipeline_state = state
        self._on_pipeline_change(state)
        self._schedule_ui_update(lambda: self._update_button_for_state(state))
        self._schedule_ui_update(lambda: self._update_voice_state_label(state))

    def get_state(self) -> str:
        """Return the current pipeline state."""
        return self._pipeline_state

    # ------------------------------------------------------------------
    # UI state observer
    # ------------------------------------------------------------------

    def _on_state_change(self, key: str, value: Any) -> None:
        """Handle UIState changes that affect voice control.

        This callback runs on the observer dispatch thread.  UI updates
        must be scheduled on the main thread via the app's after() method.
        """
        if key == "ptt_active":
            self._logger.debug(f"[VoiceControl] PTT active changed: {value}")

    # ------------------------------------------------------------------
    # Button and label updates
    # ------------------------------------------------------------------

    def _update_button_for_state(self, estado: str) -> None:
        """Update primary voice button appearance based on pipeline state."""
        if self.btn_primary_voice is None:
            return

        button_config = {
            "listening": {"text": "Detener", "fg_color": "darkred", "hover_color": "#8b1a1a"},
            "processing": {"text": "Pensando...", "fg_color": "#8a6400", "hover_color": "#a67800"},
            "speaking": {"text": "Detener voz", "fg_color": "#2f5f8f", "hover_color": "#3670aa"},
            "playing": {"text": "Detener voz", "fg_color": "#2f5f8f", "hover_color": "#3670aa"},
            "downloading": {"text": "Modelo cargando", "fg_color": "#555555", "hover_color": "#666666"},
        }

        config = button_config.get(estado, {"text": "Hablar", "fg_color": "#1f7a5a", "hover_color": "#24946c"})
        self.btn_primary_voice.configure(**config)

    def _update_voice_state_label(self, estado: str) -> None:
        """Update the voice state pill label."""
        if self.lbl_kira_voice_state is None:
            return

        if estado == "listening":
            self.lbl_kira_voice_state.configure(
                text="🎤 escuchando", fg_color="#1f5a3a"
            )
        elif self._dispositivo_seleccionado is None:
            self.lbl_kira_voice_state.configure(
                text="🎤 sin mic", fg_color="#4a2630"
            )
        else:
            self.lbl_kira_voice_state.configure(
                text="🎤 listo", fg_color="#1b2633"
            )

    # ------------------------------------------------------------------
    # TTS state label update (called from app.py motor events)
    # ------------------------------------------------------------------

    def update_tts_label(self, estado: str) -> None:
        """Update the TTS state pill label.

        Args:
            estado: Current pipeline state.
        """
        if self.lbl_kira_tts_state is None:
            return

        if estado == "processing":
            self.lbl_kira_tts_state.configure(
                text="💭 pensando…", fg_color="#1f3f6f"
            )
        elif estado == "speaking":
            self.lbl_kira_tts_state.configure(
                text="🔊 generando", fg_color="#1f3f6f"
            )
        elif estado == "playing":
            self.lbl_kira_tts_state.configure(
                text="🔊 hablando", fg_color="#1f526f"
            )
        else:
            self.lbl_kira_tts_state.configure(
                text="🔊 idle", fg_color="#1b2633"
            )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """Disconnect WebSocket and release resources.

        Should be called during application shutdown to ensure clean
        termination of background threads and WebSocket connections.
        """
        self._ptt_flush_stop.set()
        if self._ptt_flush_thread and self._ptt_flush_thread.is_alive():
            self._ptt_flush_thread.join(timeout=2)
        self.disconnect_ws()
        self._ui_state.unsubscribe(self._state_sub_id)
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=3)
        if self._recording_thread and self._recording_thread.is_alive():
            self._recording_thread.join(timeout=3)
