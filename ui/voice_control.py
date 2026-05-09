"""Voice Control Panel module for VoiceAI.

Encapsulates all voice input functionality:
- WebSocket management (LiveAudio connection with auto-reconnect)
- Audio recording (calibration capture with RMS validation)
- RMS animation (simulated level indicator during listening)
- Voice button state machine (pipeline-driven UI updates)

This module is decoupled from app.py internals and uses UIState for
all state communication.  Widget references are injected at construction
time so the panel can update the UI without knowing about app.py structure.
"""

from __future__ import annotations

import asyncio
import json
import os
import random
import threading
from typing import Any, Callable, Optional

import numpy as np
import sounddevice as sd
import soundfile as sf
import websockets

import customtkinter as ctk

from config.settings import (
    WS_URI, WS_TIMEOUT, WS_RECONNECT_BASE_DELAY,
    WS_RECONNECT_MAX_DELAY, WS_MAX_RETRIES,
    RECORDING_DURATION, RECORDING_SAMPLERATE, MIN_AUDIO_RMS,
    BASE_DIR, TEMP_DIR,
)
from config.logger import get_logger
from ui.state import UIState

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

        # Internal state
        self._pipeline_state: str = "idle"
        self._ws_connected: bool = False
        self._ws_should_reconnect: bool = False
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_lock = threading.Lock()
        self._is_recording: bool = False
        self._recording_thread: Optional[threading.Thread] = None
        self._recording_stream: Any = None
        self._rms_animating: bool = False
        self._ptt_accept_logged: bool = False

        # Motor IA reference (set after construction)
        self._motor_ia: Any = None

        # Widget references (created in create_voice_panel)
        self.btn_primary_voice: Optional[ctk.CTkButton] = None
        self.barra_rms: Optional[ctk.CTkProgressBar] = None
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

    def set_dispositivo(self, device_id: Optional[int]) -> None:
        """Update the selected audio input device."""
        self._dispositivo_seleccionado = device_id

    # ------------------------------------------------------------------
    # Panel creation
    # ------------------------------------------------------------------

    def create_voice_panel(self) -> None:
        """Create and layout all voice control widgets within parent_frame.

        Builds the voice panel header, state strip, hint label, primary
        voice button, and RMS progress bar.  All widgets are stored as
        instance attributes for later manipulation.
        """
        # State strip
        kira_state_strip = ctk.CTkFrame(self._parent, fg_color="transparent")
        kira_state_strip.grid(row=1, column=0, sticky="ew", padx=12, pady=(4, 6))
        for col in range(4):
            kira_state_strip.grid_columnconfigure(col, weight=1)

        self.lbl_kira_voice_state = ctk.CTkLabel(
            kira_state_strip, text="Voz/PTT: listo",
            fg_color="#1b2633", corner_radius=12, anchor="w"
        )
        self.lbl_kira_voice_state.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=0)

        self.lbl_kira_tts_state = ctk.CTkLabel(
            kira_state_strip, text="TTS: idle",
            fg_color="#1b2633", corner_radius=12, anchor="w"
        )
        self.lbl_kira_tts_state.grid(row=0, column=1, sticky="ew", padx=4, pady=0)

        self.lbl_kira_memory_state = ctk.CTkLabel(
            kira_state_strip, text="Memoria: disponible",
            fg_color="#1b2633", corner_radius=12, anchor="w"
        )
        self.lbl_kira_memory_state.grid(row=0, column=2, sticky="ew", padx=4, pady=0)

        self.lbl_kira_chat_state = ctk.CTkLabel(
            kira_state_strip, text="Chat: desconectado",
            fg_color="#1b2633", corner_radius=12, anchor="w"
        )
        self.lbl_kira_chat_state.grid(row=0, column=3, sticky="ew", padx=(4, 0), pady=0)

        # Hint label
        self.lbl_voice_hint = ctk.CTkLabel(
            self._parent,
            text="Usa LiveAudio o PTT. El botón principal conserva el comportamiento de Conectar LiveAudio.",
            text_color="#8fa3b8", anchor="w"
        )
        self.lbl_voice_hint.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))

        # Voice actions frame
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

        self.barra_rms = ctk.CTkProgressBar(voice_actions, width=150, height=10)
        self.barra_rms.set(0)
        self.barra_rms.grid(row=0, column=1, sticky="ew", padx=4, pady=0)
        self.barra_rms.grid_remove()

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
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ws_reconnect_loop())
        finally:
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
                        self._logger.debug(
                            f"WS recibido ({len(texto_transcrito)} chars): "
                            f"{texto_transcrito[:60]}..."
                        )

                    # PTT gate: filtering handled at higher level (WS listener in app.py
                    # or via callback from PTTManager).  The previous tautology
                    # (ptt_active and not ptt_active) has been removed.

                    # Motor IA busy check
                    if self._motor_ia and (getattr(self._motor_ia, 'is_speaking', False) or getattr(self._motor_ia, 'is_processing', False)):
                        if texto_transcrito:
                            self._logger.debug(
                                f"WS descartado (IA ocupada): {texto_transcrito[:40]}..."
                            )
                        continue

                    # Minimum length filter
                    palabras = texto_transcrito.split()
                    if len(palabras) < 4:
                        if texto_transcrito:
                            self._logger.debug(
                                f"WS descartado (muy corto, {len(palabras)} palabras): "
                                f"{texto_transcrito}"
                            )
                        continue

                    if texto_transcrito:
                        if self._motor_ia is None:
                            self._logger.warning("Motor IA no disponible, transcripción descartada.")
                            continue
                        self._on_log(f"[LiveAudio]: {texto_transcrito}")
                        self._motor_ia.command_queue.put((
                            "process_context",
                            f"El streamer acaba de decir: {texto_transcrito}"
                        ))
                        self._logger.info(
                            f"Transcripción aceptada: {texto_transcrito[:80]}..."
                        )

                except asyncio.TimeoutError:
                    continue
                except json.JSONDecodeError as e:
                    self._logger.warning(f"JSON inválido del WebSocket: {e}")
                except websockets.exceptions.ConnectionClosed as e:
                    self._logger.warning(f"WebSocket cerrado: {e}")
                    raise

    # ------------------------------------------------------------------
    # Audio recording
    # ------------------------------------------------------------------

    def start_recording(self, filepath: Optional[str] = None) -> None:
        """Start audio recording from the selected input device.

        Args:
            filepath: Destination WAV file path. Uses default calibration
                path if None.
        """
        if self._dispositivo_seleccionado is None:
            self._on_log("[Grabación] No hay dispositivo de audio seleccionado.")
            return

        if filepath is None:
            filepath = os.path.join(BASE_DIR, "referencia_grabada.wav")

        self._is_recording = True
        self._recording_thread = threading.Thread(
            target=self._hilo_grabacion,
            args=(filepath,),
            daemon=True
        )
        self._recording_thread.start()

    def stop_recording(self) -> Optional[bytes]:
        """Stop the current recording session.

        Returns:
            Raw audio bytes if recording was active, None otherwise.
        """
        self._is_recording = False
        if self._recording_stream is not None:
            try:
                self._recording_stream.stop()
            except Exception:
                pass
        return None

    def is_recording(self) -> bool:
        """Return whether a recording session is in progress."""
        return self._is_recording

    def _hilo_grabacion(self, filepath: str) -> None:
        """Background thread that captures audio and validates RMS level."""
        self._on_log(
            f"\n[Grabación] 🔴 GRABANDO {RECORDING_DURATION}s... Habla ahora."
        )

        try:
            frames = int(RECORDING_DURATION * RECORDING_SAMPLERATE)
            recording = np.zeros((frames, 1), dtype='float32')
            with sd.InputStream(
                samplerate=RECORDING_SAMPLERATE,
                channels=1,
                dtype='float32',
                device=self._dispositivo_seleccionado,
            ) as self._recording_stream:
                idx = 0
                blocksize = 1024
                while self._is_recording and idx < frames:
                    chunk, overflowed = self._recording_stream.read(blocksize)
                    if overflowed:
                        self._logger.warning("Overflow de buffer de grabación.")
                    remaining = frames - idx
                    take = min(blocksize, remaining)
                    recording[idx:idx + take] = chunk[:take]
                    idx += take

            if not self._is_recording:
                recording = recording[:idx]

            rms = float(np.sqrt(np.mean(recording ** 2)))
            if rms < MIN_AUDIO_RMS:
                self._on_log(
                    "[Grabación] ⚠️ Audio demasiado bajo o silencio. "
                    "Intenta de nuevo."
                )
                self._logger.warning(
                    f"Grabación descartada por RMS bajo: {rms:.6f}"
                )
                return

            sf.write(filepath, recording, RECORDING_SAMPLERATE)
            self._on_log(f"[Grabación] ⏹️ Audio capturado (RMS: {rms:.4f})")
            self._logger.info(f"Grabación guardada: {filepath}, RMS={rms:.4f}")

        except Exception as e:
            self._on_log(f"[ERROR Grabación]: {e}")
            self._logger.exception("Error durante grabación")
        finally:
            self._is_recording = False
            self._recording_stream = None

    # ------------------------------------------------------------------
    # RMS animation
    # ------------------------------------------------------------------

    def update_rms(self, level: float) -> None:
        """Set the RMS progress bar to a specific level.

        Args:
            level: Value between 0.0 and 1.0.
        """
        if self.barra_rms is not None:
            self.barra_rms.set(level)

    def start_rms_animation(self) -> None:
        """Start the simulated RMS animation loop.

        Updates the progress bar with random values every 150ms to
        simulate audio level during listening state.
        """
        self._rms_animating = True
        self._animar_rms()

    def stop_rms_animation(self) -> None:
        """Stop the RMS animation and hide the progress bar."""
        self._rms_animating = False
        if self.barra_rms is not None:
            self.barra_rms.grid_remove()

    def _animar_rms(self) -> None:
        """Single frame of RMS animation. Schedules next frame if active."""
        if self._pipeline_state != "listening" or not self._rms_animating:
            return

        nivel = random.uniform(0.2, 0.9)
        if self.barra_rms is not None:
            self.barra_rms.set(nivel)

        # Schedule next frame via tkinter after (must be called from main thread)
        # This method is called from _on_state_change which runs on main thread
        if self._rms_animating:
            self._schedule_rms_frame()

    def _schedule_rms_frame(self) -> None:
        """Schedule the next RMS animation frame on the main thread."""
        self._logger.warning("RMS animation: _schedule_rms_frame not overridden. Animation will stop.")

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

        if state == "listening":
            self.start_rms_animation()
        else:
            self.stop_rms_animation()

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
                text="Voz/PTT: escuchando", fg_color="#1f5a3a"
            )
        elif self._dispositivo_seleccionado is None:
            self.lbl_kira_voice_state.configure(
                text="Voz/PTT: sin mic", fg_color="#4a2630"
            )
        else:
            self.lbl_kira_voice_state.configure(
                text="Voz/PTT: listo", fg_color="#1b2633"
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

        if estado == "speaking":
            self.lbl_kira_tts_state.configure(
                text="TTS: generando", fg_color="#1f3f6f"
            )
        elif estado == "playing":
            self.lbl_kira_tts_state.configure(
                text="TTS: hablando", fg_color="#1f526f"
            )
        else:
            self.lbl_kira_tts_state.configure(
                text="TTS: idle", fg_color="#1b2633"
            )

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """Disconnect WebSocket and release resources.

        Should be called during application shutdown to ensure clean
        termination of background threads and WebSocket connections.
        """
        self.disconnect_ws()
        self.stop_rms_animation()
        self._ui_state.unsubscribe(self._state_sub_id)
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=3)
        if self._recording_thread and self._recording_thread.is_alive():
            self._recording_thread.join(timeout=3)
