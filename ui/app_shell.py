"""VocalAIApp shell — thin composition and wiring layer.
This module contains the ``VocalAIApp`` class that is ONLY responsible for:
- Creating UIState and all panel instances
- Wiring callbacks between panels
- Calling panel.build() methods
- Setting up window geometry
- Delegating cleanup to all panels
No UI construction code is inline — all delegated to panels.
"""
from __future__ import annotations
import json
import logging
import os
import queue
import threading
import time
from typing import Any, Optional
import customtkinter as ctk
import numpy as np
import sounddevice as sd
import soundfile as sf
from tkinter import filedialog
import tkinter.messagebox as messagebox
from pynput import keyboard, mouse
from ui.state import UIState
from ui.crash_reporting import install_crash_handler
from ui.protocols import CallbackDispatcher, SmartAggregatorCallbacks
from ui.ptt_manager import PTTManager
from ui.voice_control import VoiceControlPanel
from ui.model_panel import ModelPanel
from ui.profile_panel import ProfilePanel
from ui.status_bar import StatusBar
from ui.smart_aggregator_ui import SmartAggregatorUI
from ui.stream_admin_ui import StreamAdminUI
from ui.cohost_agenda_panel import CoHostAgendaPanel
from ui.music_panel import MusicPanel
from ui.advanced_panel import AdvancedModePanel
from ui.profiles_window import ConfiguradorPerfiles
from ui.avatar_panel import AvatarPanel
from avatar.obs_client import OBSClient, OBSConfig
from avatar.avatar_state import AvatarState, AvatarStateBridge
from config.settings import (
    DEFAULT_MODEL, MODELS_CATALOG, BASE_DIR, TEMP_DIR,
    RECORDING_DURATION, RECORDING_SAMPLERATE, MIN_AUDIO_RMS,
    PTT_DEFAULT_HOTKEY, PTT_HOTKEY_LIST, PTT_CONFIG_FILE,
    WINDOW_GEOMETRY_FILE, ACCIONES_LOG_FILE,
)
from config.logger import get_logger
from core.profiles import cargar_perfiles, guardar_perfiles
from core.cohost_profiles import load_cohost_profiles, save_cohost_profiles, normalize_cohost_profile, sanitize_profile_name
from core.audio_bed import AudioBedEngine
from core.editorial_agenda_bridge import EditorialAgendaBridge
from core.editorial_cards import EditorialCard, EditorialCardStore
from core.llm_engine import MotorVocalIA
from core.health_monitor import HealthMonitor
from core.temp_file_cleanup import cleanup_voiceai_temp_artifacts
from core.music_library import MusicLibrary
from smart_aggregator import AgendaAction, AgendaState, Aggregator, ErrorCode, generate_suggestions, KiraAgendaController, RecoveryPolicy, TopicStatus
from smart_aggregator.chat_input_contract import ChatContextPacketBuilder
from stream_admin import AdminManager
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
def _cargar_geometria() -> dict | None:
    try:
        if os.path.exists(WINDOW_GEOMETRY_FILE):
            with open(WINDOW_GEOMETRY_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return None
def _guardar_geometria(x: int, y: int, w: int, h: int) -> None:
    try:
        os.makedirs(os.path.dirname(WINDOW_GEOMETRY_FILE), exist_ok=True)
        with open(WINDOW_GEOMETRY_FILE, "w") as f:
            json.dump({"x": x, "y": y, "width": w, "height": h}, f)
    except Exception:
        pass
class _EntryStub:
    """Stand-in for CTkEntry widgets removed from sidebar (moved to Stream Admin)."""
    def __init__(self, default: str = "") -> None:
        self._text = default
    def get(self) -> str:
        return self._text
    def delete(self, first: object, last: object = None) -> None:
        self._text = ""
    def insert(self, index: object, value: str) -> None:
        self._text = value
    def pack(self, **kw: object) -> None:
        pass
class _ButtonStub:
    """Stand-in for CTkButton widgets removed from sidebar."""
    def configure(self, **kw: object) -> None:
        pass
    def pack(self, **kw: object) -> None:
        pass
# ── Global crash handler: log unhandled exceptions before the process dies ──
install_crash_handler()
class VocalAIApp(ctk.CTk):
    """Thin composition layer — delegates all work to panel modules."""
    def __init__(self) -> None:
        super().__init__()
        self.title(f"OpenCohost — Qwen3-TTS + {DEFAULT_MODEL}")
        geo = _cargar_geometria()
        if geo:
            try:
                self.geometry(f"{geo['width']}x{geo['height']}+{geo['x']}+{geo['y']}")
            except Exception:
                self.geometry("1100x700")
        else:
            self.geometry("1100x700")
        self.minsize(800, 500)
        self.log_queue = queue.Queue()
        self._run_startup_janitor()
        self.dispositivo_seleccionado: int | None = None
        self._modo_compacto: bool = False
        self._ptt_accept_logged: bool = False
        self._stream_admin_manual_disconnect: bool = False
        self._logs_panel_visible: bool = True
        self._closing: bool = False
        self._motor_started: bool = False
        self._motor_heartbeat_failure_reported: bool = False
        self._kira_avatar_preview_after_id: Any = None
        self.perfiles = cargar_perfiles()
        self.cohost_profiles = load_cohost_profiles()
        self._current_cohost_profile = "Natural" if "Natural" in self.cohost_profiles else next(iter(self.cohost_profiles), "")
        self.music_library = MusicLibrary()
        self.music_library.load()
        self.audio_bed = AudioBedEngine(self.music_library, on_log=lambda msg: self._print_log(msg))
        self.editorial_cards = EditorialCardStore(os.path.join(BASE_DIR, "data", "editorial_cards", "cards.db"))
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)
        self.lista_dispositivos = self._obtener_dispositivos_entrada()
        # Shared UIState
        self._ui_state = UIState()
        # Fix: audit/ui-security-perf-2026-05-17 — store subscription ID for cleanup.
        # Without unsubscribe, UIState holds a reference to VocalAIApp preventing GC
        # after destroy(). Same pattern used by StatusBar, ModelPanel, etc.
        self._ui_state_sub_id = self._ui_state.subscribe(self._on_ui_state_change)
        # Callback dispatchers
        self._model_dispatcher = CallbackDispatcher(source="ModelPanel")
        self._model_dispatcher.subscribe("on_switch_model", lambda tag: self.motor_ia.command_queue.put(("switch_model", tag)))
        self._model_dispatcher.subscribe("on_switch_llm_tier", lambda tier: self.motor_ia.command_queue.put(("switch_llm_tier", tier)))
        self._model_dispatcher.subscribe("on_download_model", lambda tag: self.motor_ia.command_queue.put(("download_model", tag)))
        self._profile_dispatcher = CallbackDispatcher(source="ProfilePanel")
        self._profile_dispatcher.subscribe("on_set_profile", lambda p: self.motor_ia.command_queue.put(("set_profile", p)))
        self._smart_agg_dispatcher = CallbackDispatcher(source="SmartAggregatorUI")
        self._smart_agg_dispatcher.set_protocol(SmartAggregatorCallbacks)
        # PTT Manager
        self.ptt = PTTManager(logger=logger)
        self.ptt.set_status_callback(self._on_ptt_status_change)
        self.ptt.set_state_callback(self._actualizar_pipeline)
        self.ptt.set_log_callback(lambda msg: self._print_log(msg))
        # Avatar state bridge (independent from Tkinter)
        self._avatar_bridge = AvatarStateBridge()
        self._speaking_alt_timer_id: str | None = None
        self._speaking_is_alt: bool = False
        self._inactivity_timer_id: str | None = None
        self._joyita_obs_timer_id: str | None = None
        self._inactivity_timeout_ms: int = 2 * 60 * 1000  # 2 minutes to sleeping
        # OBS WebSocket client (initialized after UI build to access config)
        self._obs_client: Optional[OBSClient] = None
        self._obs_retry_thread: threading.Thread | None = None
        self._obs_retry_cancel: threading.Event | None = None
        # ── Build UI structure ──
        self._build_ui()
        # Motor IA (deferred until mainloop is running to avoid
        # "main thread is not in main loop" race condition)
        self.motor_ia = MotorVocalIA(self.log_queue, self._on_motor_event)
        self.kira_agenda = KiraAgendaController()
        self.editorial_agenda = EditorialAgendaBridge(self.editorial_cards, self.kira_agenda)
        self.editorial_agenda.register_provider()
        self.motor_ia.agenda_output_validator = self.kira_agenda.accept_output
        self.motor_ia.agenda_output_preview_validator = self.kira_agenda.preview_accept_output
        self.motor_ia.agenda_output_recorder = self._record_accepted_kira_agenda_output
        self.motor_ia.agenda_output_transformer = self.kira_agenda.enforce_live_safety_cap
        self.motor_ia.agenda_controller = self.kira_agenda            # Phase 0: metrics access
        # Health Monitor — system health daemon (graceful if init fails)
        self.health_monitor: HealthMonitor | None = None
        try:
            self.health_monitor = HealthMonitor()
            self.motor_ia.health_monitor = self.health_monitor  # wire for TTS fallback
        except Exception as e:
            logger.warning(f"HealthMonitor init failed (non-fatal): {e}")
        if self._current_cohost_profile:
            self.kira_agenda.set_profile(self.cohost_profiles.get(self._current_cohost_profile, {}))
        self._kira_agenda_tick_id: str | None = None
        self._kira_agenda_prefetched_action: AgendaAction | None = None
        self._idle_ticks: int = 0
        self._kira_agenda_pending_compact_chat: str = ""
        self.after(100, self._start_motor)
        # Wire motor_ia to voice control panel
        if hasattr(self, "voice_panel"):
            self.voice_panel.set_motor_ia(self.motor_ia)
        # Initialize smart aggregator and stream admin
        self._init_smart_aggregator()
        self._init_stream_admin()
        # Start log processing
        self.after(100, self._process_logs)
        self.after(500, self._aplicar_perfil_actual)
        self._print_log(f"[Sistema] PTT hotkey cargada: {self.ptt.hotkey}")
        logger.info("Aplicación OpenCohost iniciada.")
    def _start_motor(self) -> None:
        """Start the IA motor thread after mainloop is running.
        Deferring this avoids 'main thread is not in main loop' errors
        when the motor calls ui_callback before Tkinter's mainloop starts.
        """
        self.motor_ia.start()
        self._motor_started = True
        # Start health monitor daemon
        if self.health_monitor:
            # Try to attach to manually-started Qwen server first
            try:
                self.health_monitor.qwen_manager.attach_existing()
            except Exception as e:
                logger.debug(f"HealthMonitor: could not attach existing server: {e}")
            self.health_monitor.start()
        # Begin UI health status polling and passive motor heartbeat checks.
        self._poll_health_status()
    def _run_startup_janitor(self) -> None:
        """Recover only known VoiceAI temp leftovers from a previous run."""
        try:
            stats = cleanup_voiceai_temp_artifacts(TEMP_DIR, logger, min_age_seconds=60.0)
        except Exception as exc:
            logger.warning("Startup temp janitor failed: %s", exc)
            return
        if stats.get("removed"):
            logger.info("Startup temp janitor removed %s legacy app artifact(s)", stats["removed"])
    def _poll_health_status(self) -> None:
        """Poll health monitor state and push to UI state (thread-safe via after)."""
        if self.health_monitor and not getattr(self, "_motor_heartbeat_failure_reported", False):
            try:
                state = self.health_monitor.state
                self._ui_state.health_status = state.overall_status
            except Exception:
                pass
        self._check_motor_heartbeat()
        # Reschedule every 2 seconds for UI responsiveness
        self.after(2000, self._poll_health_status)
    def _check_motor_heartbeat(self) -> None:
        """Surface an operator-visible warning if MotorVocalIA dies after startup."""
        motor = getattr(self, "motor_ia", None)
        if (
            motor is None
            or not getattr(self, "_motor_started", False)
            or getattr(self, "_closing", False)
            or getattr(self, "_motor_heartbeat_failure_reported", False)
        ):
            return
        try:
            alive = motor.is_alive()
        except Exception:
            logger.exception("No se pudo verificar el heartbeat de MotorVocalIA")
            return
        if alive:
            return
        self._motor_heartbeat_failure_reported = True
        logger.critical("MotorVocalIA thread died unexpectedly; UI remains open but Kira is offline")
        try:
            self._ui_state.health_status = "red"
        except Exception:
            logger.exception("No se pudo marcar health_status tras fallo de MotorVocalIA")
        try:
            self._print_log("[CRITICO] MotorVocalIA se detuvo inesperadamente. Kira esta offline; reinicia la app.")
        except Exception:
            logger.exception("No se pudo informar en UI el fallo de MotorVocalIA")
    def _on_ui_state_change(self, key: str, value: Any) -> None:
        if key == "ws_connected":
            def update_btn():
                if hasattr(self, "btn_ws"):
                    if value:
                        self.btn_ws.configure(text="Desconectar LiveAudio", fg_color="darkred")
                    else:
                        self.btn_ws.configure(text="Conectar LiveAudio", fg_color="#555555")
            self.after(0, update_btn)
    def _on_avatar_state_for_preview(self, state: AvatarState) -> None:
        """Update the left-panel avatar preview when bridge state changes."""
        previous_after_id = getattr(self, "_kira_avatar_preview_after_id", None)
        if previous_after_id is not None:
            try:
                self.after_cancel(previous_after_id)
            except Exception:
                logger.debug("No se pudo cancelar update pendiente de preview de avatar", exc_info=True)
            finally:
                self._kira_avatar_preview_after_id = None
        def update():
            self._kira_avatar_preview_after_id = None
            if self._kira_avatar_label is None or not self._kira_avatar_label.winfo_exists():
                return
            from avatar.avatar_config import load_avatar_config
            config = load_avatar_config()
            image_path = config.get_image_for_state(state.value)
            if image_path and os.path.isfile(image_path):
                try:
                    from PIL import Image
                    img = Image.open(image_path)
                    img.thumbnail((220, 220), Image.Resampling.LANCZOS)
                    # Keep BOTH references alive to prevent Tkinter image GC.
                    # CTkImage wraps the PIL Image but doesn't always hold a
                    # strong reference to it — if the PIL Image is collected,
                    # Tkinter raises "image pyimageN doesn't exist".
                    self._kira_avatar_pil = img
                    ctk_img = ctk.CTkImage(light_image=img, dark_image=img, size=img.size)
                    self._kira_avatar_ref = ctk_img
                    if state.value == "idle" or image_path == config.state_images.get("idle"):
                        self._kira_avatar_idle_pil = img
                        self._kira_avatar_idle_ref = ctk_img
                    self._kira_avatar_label.configure(image=ctk_img, text="")
                except Exception as e:
                    if not self._show_cached_idle_avatar_preview():
                        self._kira_avatar_pil = None
                        self._kira_avatar_ref = None
                        self._kira_avatar_label.configure(
                            image=None,
                            text=f"Error al cargar avatar: {state.value}",
                            text_color="#aa5555",
                        )
                    self._print_log(f"[Avatar] No se pudo cargar preview '{state.value}' desde {image_path}: {e}")
            else:
                if not self._show_cached_idle_avatar_preview():
                    self._kira_avatar_pil = None
                    self._kira_avatar_ref = None
                    self._kira_avatar_label.configure(
                        image=None,
                        text=f"Sin imagen para: {state.value}",
                        text_color="#6b7b8d",
                    )
                self._print_log(f"[Avatar] Sin imagen configurada o accesible para '{state.value}'")
        self._kira_avatar_preview_after_id = self.after(0, update)
    def _show_cached_idle_avatar_preview(self) -> bool:
        """Keep a known idle avatar visible when a transient state cannot load."""
        idle_ref = getattr(self, "_kira_avatar_idle_ref", None)
        label = getattr(self, "_kira_avatar_label", None)
        if not idle_ref or label is None:
            return False
        self._kira_avatar_pil = getattr(self, "_kira_avatar_idle_pil", None)
        self._kira_avatar_ref = idle_ref
        label.configure(image=idle_ref, text="")
        return True
    # ──────────────────────────────────────────────
    # UI Construction — delegates to panels
    # ──────────────────────────────────────────────
    def _build_ui(self) -> None:
        self.configure(fg_color="#0b0f14")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0, minsize=0)
        self.grid_rowconfigure(3, weight=0, minsize=0)
        self.grid_rowconfigure(4, weight=0, minsize=0)
        self.grid_rowconfigure(5, weight=0, minsize=0)
        # Status bar
        status_bar_frame = ctk.CTkFrame(self, fg_color="#111820", corner_radius=14)
        status_bar_frame.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 8))
        self.status_bar = StatusBar(status_bar_frame, self._ui_state)
        self.status_bar.create_status_pills()
        self.lbl_status = self.status_bar.lbl_status
        # Additional pills not managed by StatusBar
        self.lbl_oauth_status_pill = ctk.CTkLabel(status_bar_frame, text="OAuth: desconectado", fg_color="#1b2633", corner_radius=12)
        self.lbl_oauth_status_pill.pack(side="left", padx=4, pady=8)
        self.lbl_memory_status_pill = ctk.CTkLabel(status_bar_frame, text="Memoria: disponible", fg_color="#1b2633", corner_radius=12)
        self.lbl_memory_status_pill.pack(side="left", padx=4, pady=8)
        self.lbl_moderation_status_pill = ctk.CTkLabel(status_bar_frame, text="Moderación: sin pendientes", fg_color="#1b2633", corner_radius=12)
        self.lbl_moderation_status_pill.pack(side="left", padx=4, pady=8)
        self.switch_advanced = ctk.CTkSwitch(status_bar_frame, text="Mostrar logs", command=self._toggle_logs_panel, onvalue=True, offvalue=False)
        self.switch_advanced.pack(side="right", padx=(8, 12), pady=8)
        self.switch_advanced.select()
        self.switch_compacto = ctk.CTkSwitch(status_bar_frame, text="Compacto", command=self._toggle_modo_compacto, onvalue=True, offvalue=False)
        self.switch_compacto.pack(side="right", padx=8, pady=8)
        # Autoría / Marca interactiva
        import webbrowser
        self.lbl_author = ctk.CTkLabel(
            status_bar_frame,
            text="OpenCohost by FranGuh",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color="#3a86ff",
            cursor="hand2"
        )
        self.lbl_author.pack(side="right", padx=(20, 8), pady=8)
        self.lbl_author.bind("<Button-1>", lambda e: webbrowser.open_new_tab("https://github.com/Franguh"))
        self.lbl_author.bind("<Enter>", lambda e: self.lbl_author.configure(text_color="#5390ff", font=ctk.CTkFont(size=12, weight="bold", underline=True)))
        self.lbl_author.bind("<Leave>", lambda e: self.lbl_author.configure(text_color="#3a86ff", font=ctk.CTkFont(size=12, weight="bold", underline=False)))

        # Product shell: Kira stays visible on the left; configuration and
        # stream operations live on the right.  Phase 2 only moves containers,
        # not callbacks or business logic.
        app_shell = ctk.CTkFrame(self, fg_color="transparent")
        app_shell.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))
        app_shell.grid_columnconfigure(0, weight=0, minsize=460)
        app_shell.grid_columnconfigure(1, weight=1)
        app_shell.grid_rowconfigure(0, weight=1)
        # Persistent Kira panel
        main_panel = ctk.CTkFrame(app_shell, fg_color="#10161d", corner_radius=18)
        main_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=0)
        main_panel.grid_columnconfigure(0, weight=1)
        main_panel.grid_rowconfigure(0, weight=1)
        main_content = ctk.CTkFrame(main_panel, fg_color="transparent")
        main_content.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        main_content.grid_columnconfigure(0, weight=1)
        main_content.grid_rowconfigure(0, weight=1)
        tab_main_kira = ctk.CTkFrame(main_content, fg_color="transparent")
        tab_main_kira.grid(row=0, column=0, sticky="nsew")
        self._main_view_buttons: dict[str, Any] = {}
        self._main_view_frames = {"Kira": tab_main_kira}
        tab_main_kira.grid_columnconfigure(0, weight=1)
        tab_main_kira.grid_rowconfigure(1, weight=1)
        tab_main_kira.grid_rowconfigure(2, weight=0)
        tab_main_kira.grid_rowconfigure(3, weight=1)
        # Kira header
        kira_header = ctk.CTkFrame(tab_main_kira, fg_color="transparent")
        kira_header.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))
        kira_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(kira_header, text="Kira", font=ctk.CTkFont(size=22, weight="bold"), anchor="w").grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(kira_header, text="Experiencia principal", text_color="#8fa3b8", anchor="e").grid(row=0, column=1, sticky="e")
        # Avatar preview in left Kira panel
        self._kira_avatar_label: ctk.CTkLabel | None = None
        self._kira_avatar_ref: Any = None
        avatar_preview_frame = ctk.CTkFrame(tab_main_kira, fg_color="#0c1117", corner_radius=12)
        avatar_preview_frame.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 8))
        avatar_preview_frame.grid_columnconfigure(0, weight=1)
        avatar_preview_frame.grid_rowconfigure(0, weight=1)
        self._kira_avatar_label = ctk.CTkLabel(
            avatar_preview_frame, text="",
            text_color="#6b7b8d",
            font=ctk.CTkFont(size=12),
            height=140,
            corner_radius=8,
        )
        self._kira_avatar_label.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        # Subscribe avatar bridge to update left-panel preview
        self._avatar_bridge.subscribe(self._on_avatar_state_for_preview)
        # Primary action button — Hablar (prominent, at Kira level)
        self._primary_speak_btn = ctk.CTkButton(
            tab_main_kira,
            text="Hablar",
            command=None,  # Wired after VoiceControlPanel is created
            state="disabled",
            height=72,
            font=ctk.CTkFont(size=21, weight="bold"),
            fg_color="#1f7a5a",
            hover_color="#24946c",
        )
        self._primary_speak_btn.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 10))
        # Kira response: compact scrollable panel so the avatar remains the hero.
        kira_response_shell = ctk.CTkFrame(tab_main_kira, fg_color="#0c1117", corner_radius=18)
        kira_response_shell.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 10))
        kira_response_shell.grid_columnconfigure(0, weight=1)
        kira_response_shell.grid_rowconfigure(1, weight=0)
        ctk.CTkLabel(kira_response_shell, text="Respuesta de Kira", font=ctk.CTkFont(size=13, weight="bold"), text_color="#d8e2ef", anchor="w").grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))
        self.text_kira_response = ctk.CTkTextbox(kira_response_shell, font=ctk.CTkFont(size=14), fg_color="#090d12", border_width=1, border_color="#1f2b38", state="disabled", height=130, wrap="word")
        self.text_kira_response.grid(row=1, column=0, sticky="ew", padx=14, pady=(0, 14))
        self.text_kira_response.configure(state="normal")
        self.text_kira_response.insert("end", "La respuesta de Kira aparecerá aquí. Los logs completos se muestran abajo solo si activas Mostrar logs.\n")
        self.text_kira_response.configure(state="disabled")
        # Voice panel (compact — primary button is at Kira level)
        voice_panel_frame = ctk.CTkFrame(tab_main_kira, fg_color="#121d27", corner_radius=16)
        voice_panel_frame.grid(row=4, column=0, sticky="ew", padx=16, pady=(0, 4))
        voice_panel_frame.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(voice_panel_frame, text="Entrada de voz / PTT", font=ctk.CTkFont(size=13, weight="bold"), text_color="#d8e2ef").grid(row=0, column=0, sticky="w", padx=12, pady=(8, 2))
        self.voice_panel = VoiceControlPanel(
            parent_frame=voice_panel_frame,
            ui_state=self._ui_state,
            logger=logger,
            on_log=self._print_log,
            on_motor_event=self._on_motor_event,
            on_pipeline_change=self._actualizar_pipeline,
            dispositivo_seleccionado=self.dispositivo_seleccionado,
            schedule_ui_update=lambda fn: self.after(0, fn),
            external_primary_button=self._primary_speak_btn,
        )
        self.voice_panel.create_voice_panel()
        # Expose widgets for backward compatibility
        self.lbl_kira_voice_state = self.voice_panel.lbl_kira_voice_state
        self.lbl_kira_tts_state = self.voice_panel.lbl_kira_tts_state
        self.lbl_kira_memory_state = self.voice_panel.lbl_kira_memory_state
        self.lbl_kira_chat_state = self.voice_panel.lbl_kira_chat_state
        self.lbl_voice_hint = self.voice_panel.lbl_voice_hint
        self.btn_primary_voice = self.voice_panel.btn_primary_voice
        self.barra_rms = self.voice_panel.barra_rms
        self.voice_panel._schedule_rms_frame = lambda: self.after(150, self.voice_panel._animar_rms)
        # Wire primary button to VoiceControlPanel's toggle
        self._primary_speak_btn.configure(command=self.voice_panel._toggle_websocket)
        # Bottom chat entry
        frame_bottom = ctk.CTkFrame(tab_main_kira, fg_color="#121d27", corner_radius=16)
        frame_bottom.grid(row=5, column=0, sticky="ew", padx=16, pady=(0, 16))
        frame_bottom.grid_columnconfigure(0, weight=1)
        self.entry_chat = ctk.CTkEntry(frame_bottom, placeholder_text="Escribe un mensaje para Kira (contexto o pregunta)...")
        self.entry_chat.grid(row=0, column=0, sticky="ew", padx=(10, 6), pady=10)
        self.entry_chat.bind("<Return>", lambda e: self._enviar_contexto_manual())
        self.btn_enviar = ctk.CTkButton(frame_bottom, text="Enviar a IA", command=self._enviar_contexto_manual, width=110, state="disabled", fg_color="#555555", hover_color="#666666")
        self.btn_enviar.grid(row=0, column=1, padx=(0, 10), pady=10)
        # Product workspace: current configuration plus full Stream Admin.
        side_panel = ctk.CTkFrame(app_shell, fg_color="#0f151c", corner_radius=18)
        side_panel.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=0)
        side_panel.grid_columnconfigure(0, weight=1)
        side_panel.grid_rowconfigure(3, weight=1)
        ctk.CTkLabel(side_panel, text="Paneles de producto", font=ctk.CTkFont(size=16, weight="bold"), anchor="w").grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))
        # Product tabs — custom buttons (full-width, clear active state)
        product_tab_bar = ctk.CTkFrame(side_panel, fg_color="transparent")
        product_tab_bar.grid(row=1, column=0, sticky="ew", padx=10, pady=(6, 6))
        for col in range(5):
            product_tab_bar.grid_columnconfigure(col, weight=1, uniform="product_tab")
        product_content = ctk.CTkFrame(side_panel, fg_color="#0f151c", corner_radius=18)
        product_content.grid(row=3, column=0, sticky="nsew", padx=10, pady=(0, 12))
        product_content.grid_columnconfigure(0, weight=1)
        product_content.grid_rowconfigure(0, weight=1)
        self._product_tab_data: dict[str, dict] = {}
        self._active_product_tab: str = "config"
        TAB_DEFS = [
            ("config", "Configuración"),
            ("stream", "Stream"),
            ("cohost", "Co-host"),
            ("music", "Música"),
            ("avatar", "Avatar / OBS"),
        ]
        for idx, (key, label) in enumerate(TAB_DEFS):
            is_active = (key == self._active_product_tab)
            btn = ctk.CTkButton(
                product_tab_bar,
                text=label,
                font=ctk.CTkFont(size=13, weight="bold"),
                fg_color="#2f5f8f" if is_active else "#151d26",
                hover_color="#3670aa" if is_active else "#1d2a38",
                text_color="#ffffff" if is_active else "#6b7b8d",
                corner_radius=8,
                height=36,
            )
            btn.grid(row=0, column=idx, sticky="ew", padx=2, pady=2)
            btn.configure(command=lambda k=key: self._switch_product_tab(k))
            self._product_tab_data[key] = {"button": btn}
        # Sub-tab container — same module as product tabs, shows context-specific subtabs
        sub_tab_container = ctk.CTkFrame(side_panel, fg_color="transparent")
        sub_tab_container.grid(row=2, column=0, sticky="ew", padx=10, pady=(4, 0))
        sub_tab_container.grid_columnconfigure(0, weight=1)
        # Config sub-tabs (visible when "Configuración" is active)
        cfg_sub_bar = ctk.CTkFrame(sub_tab_container, fg_color="transparent")
        cfg_sub_bar.grid(row=0, column=0, sticky="ew", padx=2, pady=0)
        for col in range(3):
            cfg_sub_bar.grid_columnconfigure(col, weight=1, uniform="cfg_subtab")
        self._cfg_subtab_data: dict[str, dict] = {}
        self._active_cfg_subtab: str = "modelo_perfil"
        CFG_SUBTABS = [("modelo_perfil", "M/Perfil"), ("audio_tts", "Audio/TTS"), ("ayuda", "Ayuda")]
        for idx, (key, label) in enumerate(CFG_SUBTABS):
            is_active = (key == self._active_cfg_subtab)
            btn = ctk.CTkButton(cfg_sub_bar, text=label,
                font=ctk.CTkFont(size=11, weight="bold"),
                fg_color="#1e4060" if is_active else "#0f151c",
                hover_color="#255478" if is_active else "#151d26",
                text_color="#d8e2ef" if is_active else "#6b7b8d",
                corner_radius=6, height=28)
            btn.grid(row=0, column=idx, sticky="ew", padx=2, pady=0)
            btn.configure(command=lambda k=key: self._switch_cfg_subtab(k))
            self._cfg_subtab_data[key] = {"button": btn}
        # Stream sub-tabs (visible when "Stream" is active, initially hidden)
        stream_sub_bar = ctk.CTkFrame(sub_tab_container, fg_color="transparent")
        stream_sub_bar.grid(row=0, column=0, sticky="ew", padx=2, pady=0)
        stream_sub_bar.grid_remove()
        for col in range(2):
            stream_sub_bar.grid_columnconfigure(col, weight=1, uniform="stream_subtab")
        self._stream_subtab_data: dict[str, dict] = {}
        self._active_stream_subtab: str = "emision"
        STREAM_SUBTABS = [("emision", "Emisión"), ("acciones", "Acciones")]
        for idx, (key, label) in enumerate(STREAM_SUBTABS):
            is_active = (key == self._active_stream_subtab)
            btn = ctk.CTkButton(stream_sub_bar, text=label,
                font=ctk.CTkFont(size=11, weight="bold"),
                fg_color="#1e4060" if is_active else "#0f151c",
                hover_color="#255478" if is_active else "#151d26",
                text_color="#d8e2ef" if is_active else "#6b7b8d",
                corner_radius=6, height=28)
            btn.grid(row=0, column=idx, sticky="ew", padx=2, pady=0)
            btn.configure(command=lambda k=key: self._switch_stream_subtab(k))
            self._stream_subtab_data[key] = {"button": btn}
        # Store sub-bar references for visibility toggling
        self._cfg_sub_bar = cfg_sub_bar
        self._stream_sub_bar = stream_sub_bar
        # Content frames — only active one visible
        tab_product_config = ctk.CTkFrame(product_content, fg_color="transparent")
        tab_product_config.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        tab_product_config.grid_columnconfigure(0, weight=1)
        tab_product_config.grid_rowconfigure(0, weight=1)
        self._product_tab_data["config"]["frame"] = tab_product_config
        tab_product_stream = ctk.CTkFrame(product_content, fg_color="transparent")
        tab_product_stream.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        tab_product_stream.grid_columnconfigure(0, weight=1)
        tab_product_stream.grid_rowconfigure(0, weight=1)
        tab_product_stream.grid_remove()
        self._product_tab_data["stream"]["frame"] = tab_product_stream
        tab_product_cohost = ctk.CTkFrame(product_content, fg_color="transparent")
        tab_product_cohost.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        tab_product_cohost.grid_columnconfigure(0, weight=1)
        tab_product_cohost.grid_rowconfigure(0, weight=1)
        tab_product_cohost.grid_remove()
        self._product_tab_data["cohost"]["frame"] = tab_product_cohost
        tab_product_music = ctk.CTkFrame(product_content, fg_color="transparent")
        tab_product_music.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        tab_product_music.grid_columnconfigure(0, weight=1)
        tab_product_music.grid_rowconfigure(0, weight=1)
        tab_product_music.grid_remove()
        self._product_tab_data["music"]["frame"] = tab_product_music
        tab_product_avatar = ctk.CTkFrame(product_content, fg_color="transparent")
        tab_product_avatar.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        tab_product_avatar.grid_columnconfigure(0, weight=1)
        tab_product_avatar.grid_rowconfigure(0, weight=1)
        tab_product_avatar.grid_remove()
        self._product_tab_data["avatar"]["frame"] = tab_product_avatar
        # Config content frames — directly in tab_product_config (sub-tabs at side_panel level)
        tab_cfg_model_profile = ctk.CTkScrollableFrame(tab_product_config, fg_color="transparent", scrollbar_button_color="#2f5f8f", scrollbar_button_hover_color="#3670aa")
        tab_cfg_model_profile.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        tab_cfg_model_profile.grid_columnconfigure(0, weight=1)
        tab_cfg_model_profile.grid_columnconfigure(1, weight=1)
        self._cfg_subtab_data["modelo_perfil"]["frame"] = tab_cfg_model_profile
        tab_cfg_audio_voice = ctk.CTkScrollableFrame(tab_product_config, fg_color="transparent", scrollbar_button_color="#2f5f8f", scrollbar_button_hover_color="#3670aa")
        tab_cfg_audio_voice.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        tab_cfg_audio_voice.grid_columnconfigure(0, weight=1)
        tab_cfg_audio_voice.grid_remove()
        self._cfg_subtab_data["audio_tts"]["frame"] = tab_cfg_audio_voice
        tab_cfg_admin = ctk.CTkFrame(tab_product_config, fg_color="transparent")
        tab_cfg_admin.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        tab_cfg_admin.grid_columnconfigure(0, weight=1)
        tab_cfg_admin.grid_remove()
        self._cfg_subtab_data["ayuda"]["frame"] = tab_cfg_admin
        tab_cfg_admin.grid_rowconfigure(0, weight=1)
        tab_cfg_admin.grid_remove()
        self._cfg_subtab_data["ayuda"]["frame"] = tab_cfg_admin
        # Model panel
        frame_model = ctk.CTkFrame(tab_cfg_model_profile, fg_color="#151d26", corner_radius=14)
        frame_model.grid(row=0, column=0, sticky="nsew", padx=(8, 4), pady=8)
        self.model_panel = ModelPanel(
            parent_frame=frame_model,
            ui_state=self._ui_state,
            dispatcher=self._model_dispatcher,
            on_log=self._print_log,
            schedule_ui_update=lambda fn: self.after(0, fn),
            on_check_ollama=lambda: self.motor_ia.command_queue.put(("check_ollama", None)),
        )
        self.model_panel.build()
        self.combo_modelos = self.model_panel.combo_modelos
        self.btn_download = self.model_panel.btn_download
        self.lbl_modelo_info = self.model_panel.lbl_modelo_info
        self.progress_download = self.model_panel.progress_download
        self._model_display_to_tag = self.model_panel._model_display_to_tag
        self._model_tag_to_display = self.model_panel._model_tag_to_display
        self.after(250, self.model_panel.refresh_ollama_state)
        # Profile panel
        frame_profile = ctk.CTkFrame(tab_cfg_model_profile, fg_color="#151d26", corner_radius=14)
        frame_profile.grid(row=0, column=1, sticky="nsew", padx=(4, 8), pady=8)
        self.profile_panel = ProfilePanel(parent_frame=frame_profile, ui_state=self._ui_state, dispatcher=self._profile_dispatcher, on_log=self._print_log, configurador_class=ConfiguradorPerfiles, schedule_ui_update=lambda fn: self.after(0, fn))
        self.profile_panel.set_profiles(self.perfiles)
        self.profile_panel.build()
        self.combo_perfiles = self.profile_panel.combo_perfiles
        self.btn_editar_perfiles = self.profile_panel.btn_editar_perfiles
        # Audio tab
        frame_audio = ctk.CTkFrame(tab_cfg_audio_voice, fg_color="#151d26", corner_radius=14)
        frame_audio.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        ctk.CTkLabel(frame_audio, text="Dispositivo de audio", font=ctk.CTkFont(size=13, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(10, 4))
        self.combo_dispositivos = ctk.CTkOptionMenu(frame_audio, values=self.lista_dispositivos, command=self._al_seleccionar_dispositivo, width=300)
        self.combo_dispositivos.pack(fill="x", padx=10, pady=4)
        if self.lista_dispositivos:
            self.combo_dispositivos.set(self.lista_dispositivos[0])
            self.dispositivo_seleccionado = int(self.lista_dispositivos[0].split(":")[0])
            if self.status_bar:
                self.status_bar.update_mic_status("idle")
        else:
            self.combo_dispositivos.set("Sin dispositivos de audio")
            if self.status_bar:
                self.status_bar.update_mic_status("disconnected")
        audio_buttons = ctk.CTkFrame(frame_audio, fg_color="transparent")
        audio_buttons.pack(fill="x", padx=10, pady=4)
        self.btn_grabar = ctk.CTkButton(audio_buttons, text="🎤 Grabar", command=self._iniciar_grabacion, state="disabled", width=90, fg_color="#555555", hover_color="#666666")
        self.btn_grabar.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.btn_voz = ctk.CTkButton(audio_buttons, text="📂 Cargar WAV", command=self._cargar_voz, state="disabled", fg_color="#555555", width=110)
        self.btn_voz.pack(side="left", expand=True, fill="x", padx=(4, 0))
        # LiveAudio section
        frame_liveaudio = ctk.CTkFrame(frame_audio, fg_color="#101923", corner_radius=10)
        frame_liveaudio.pack(fill="x", padx=10, pady=(8, 0))
        ctk.CTkLabel(
            frame_liveaudio,
            text="LiveAudio es el motor que permite a Kira escuchar tu voz en tiempo real.",
            font=ctk.CTkFont(size=10),
            text_color="#8fa3b8",
            anchor="w",
            justify="left",
            wraplength=400,
        ).pack(fill="x", padx=10, pady=(8, 4))
        self.btn_ws = ctk.CTkButton(
            frame_liveaudio, text="Conectar LiveAudio",
            command=self._toggle_websocket,
            fg_color="#2f5f8f", hover_color="#3670aa",
            state="disabled",
        )
        self.btn_ws.pack(fill="x", padx=10, pady=(0, 10))
        # TTS / Memory
        frame_tts_memory = ctk.CTkFrame(tab_cfg_audio_voice, fg_color="#151d26", corner_radius=14)
        frame_tts_memory.grid(row=1, column=0, sticky="ew", padx=8, pady=8)
        ctk.CTkLabel(frame_tts_memory, text="TTS / Memoria", font=ctk.CTkFont(size=13, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(10, 4))
        self.switch_modo_ligero = ctk.CTkSwitch(frame_tts_memory, text="🎛️ TTS: Ligero", onvalue="ligero", offvalue="pesado", command=self._al_cambiar_motor_tts)
        self.switch_modo_ligero.pack(fill="x", padx=10, pady=4)
        self.switch_modo_ligero.select()
        ctk.CTkLabel(frame_tts_memory, text="Ligero: rápido, usa Edge-TTS (cloud). Pesado: Qwen3-TTS local, requiere descarga previa del modelo.", font=ctk.CTkFont(size=10), text_color="#8fa3b8", anchor="w", justify="left", wraplength=400).pack(fill="x", padx=10, pady=(0, 2))
        self.btn_clear = ctk.CTkButton(frame_tts_memory, text="🗑️ Limpiar Memoria", command=self._limpiar_historial, width=130, fg_color="#555555", hover_color="#777777")
        self.btn_clear.pack(fill="x", padx=10, pady=(4, 10))
        ctk.CTkLabel(frame_tts_memory, text="Limpia el historial de conversación. Kira olvidará el contexto previo.", font=ctk.CTkFont(size=10), text_color="#8fa3b8", anchor="w", justify="left", wraplength=400).pack(fill="x", padx=10, pady=(0, 10))
        # PTT controls live with model/profile because they configure how Kira is operated.
        frame_ptt = ctk.CTkFrame(tab_cfg_model_profile, fg_color="#151d26", corner_radius=14)
        frame_ptt.grid(row=1, column=0, columnspan=2, sticky="ew", padx=8, pady=8)
        ctk.CTkLabel(frame_ptt, text="PTT (Push-to-Talk)", font=ctk.CTkFont(size=13, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(10, 2))
        ctk.CTkLabel(frame_ptt, text="Activá PTT y mantené presionada la tecla para hablar. Soltá para que Kira procese y responda. Con PTT OFF, Kira escucha continuamente (modo LiveAudio).", font=ctk.CTkFont(size=10), text_color="#8fa3b8", anchor="w", justify="left", wraplength=400).pack(fill="x", padx=10, pady=(0, 4))
        self.switch_ptt = ctk.CTkSwitch(frame_ptt, text="PTT OFF", command=self._al_toggle_ptt, onvalue=True, offvalue=False)
        self.switch_ptt.pack(fill="x", padx=10, pady=4)
        ptt_hotkey_row = ctk.CTkFrame(frame_ptt, fg_color="transparent")
        ptt_hotkey_row.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(ptt_hotkey_row, text="Tecla:", font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 6))
        self.lbl_hotkey = ctk.CTkLabel(ptt_hotkey_row, text=self.ptt.hotkey, font=ctk.CTkFont(size=13, weight="bold"), width=80)
        self.lbl_hotkey.pack(side="left", padx=3)
        self.btn_mapear = ctk.CTkButton(ptt_hotkey_row, text="Mapear", command=self._mapear_hotkey, width=70, fg_color="#555555", hover_color="#666666")
        self.btn_mapear.pack(side="right", padx=3)
        self.lbl_ptt_status = ctk.CTkLabel(frame_ptt, text="", font=ctk.CTkFont(size=12), text_color="#888888", anchor="w", justify="left")
        self.lbl_ptt_status.pack(fill="x", padx=10, pady=(0, 10))
        # Ayuda tab — contextual help for each product tab
        # YouTube chat controls moved to Stream Admin > Acciones > Chat Live (RF3).
        # These stubs preserve backward compat with existing methods that reference them.
        self.entry_youtube_video = _EntryStub()
        self.btn_youtube_chat = _ButtonStub()
        self.entry_youtube_user_limit = _EntryStub("10")
        self.entry_youtube_threshold = _EntryStub("1.0")
        # Backward compat stubs (stream_admin_ui references them via _widget lookup; skips when None)
        self.lbl_oauth_side_status = None
        self.lbl_moderation_side_status = None
        # Ayuda tab — collapsible help cards per section
        frame_ayuda = ctk.CTkScrollableFrame(tab_cfg_admin, fg_color="transparent")
        frame_ayuda.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        frame_ayuda.grid_columnconfigure(0, weight=1)

        # Keep the Vista card at the top
        frame_view = ctk.CTkFrame(frame_ayuda, fg_color="#151d26", corner_radius=14)
        frame_view.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        ctk.CTkLabel(frame_view, text="Vista", font=ctk.CTkFont(size=13, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(10, 4))
        self.switch_logs = ctk.CTkSwitch(frame_view, text="Registrar logs en avanzado", onvalue=True, offvalue=False)
        self.switch_logs.pack(fill="x", padx=10, pady=(4, 10))
        self.switch_logs.select()

        # Help sections — one per product tab, collapsible
        help_sections = [
            ("Configuración", "Modelo/Perfil: elegí el modelo LLM (Ollama) y el perfil de personalidad de Kira.\n\nAudio/TTS: seleccioná dispositivo de entrada, alterná entre TTS Ligero (Edge-TTS cloud, rápido) y Pesado (Qwen3-TTS local, mayor calidad).\n\nPTT (Push-to-Talk): mantené presionada la tecla asignada para hablar; soltá para que Kira responda."),
            ("Stream", "Emisión: conectá tu cuenta de YouTube (OAuth), gestioná metadata del stream (título, categoría, tags, descripción).\n\nAcciones: monitoreá el chat en vivo (Chat Live RF3), enviá mensajes como Kira, moderá usuarios (timeout/ban)."),
            ("Co-host", "Creá una agenda de temas aprobados para que Kira los desarrolle en vivo. Importá temas en lote desde texto estructurado. Controlá la sesión: Activar, Stop suave, Emergencia."),
            ("Música", "Importá loops de audio .mp3/.wav etiquetados por mood (Normal, Calmo, Épico, etc.). Probá cada mood con un clic. El sistema hace fade, ducking y fallback automático."),
            ("Avatar / OBS", "Cambiá la imagen de Kira para cada estado (idle, hablando, escuchando, etc.). Conectá con OBS Studio vía WebSocket para reflejar los cambios en vivo."),
        ]

        self._help_expanded = {}
        row_num = 1
        for title, description in help_sections:
            key = title.lower().replace(" ", "_").replace("/", "_")

            # Card frame
            card = ctk.CTkFrame(frame_ayuda, fg_color="#151d26", corner_radius=14)
            card.grid(row=row_num, column=0, sticky="ew", padx=8, pady=(0, 8))
            card.grid_columnconfigure(0, weight=1)

            # Toggle header
            btn = ctk.CTkButton(
                card, text=f"▶ {title}",
                anchor="w",
                fg_color="transparent", text_color="#d8e2ef",
                hover_color="#1d2a38",
                font=ctk.CTkFont(size=13, weight="bold"),
            )
            btn.grid(row=0, column=0, sticky="ew", padx=10, pady=(8, 4))

            # Content frame
            content = ctk.CTkFrame(card, fg_color="#101923", corner_radius=12)
            content.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 10))
            content.grid_columnconfigure(0, weight=1)
            content.grid_remove()

            # Use CTkTextbox instead of CTkLabel — guarantees text is always visible
            textbox = ctk.CTkTextbox(
                content,
                font=ctk.CTkFont(size=11),
                fg_color="#101923",
                text_color="#a9bdd3",
                wrap="word",
                height=100,
                border_width=0,
            )
            textbox.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
            textbox.insert("end", description)
            textbox.configure(state="disabled")

            self._help_expanded[key] = False
            btn.configure(command=lambda k=key, c=content, b=btn: self._toggle_help_section(k, c, b))
            row_num += 1

        # Stream Admin panel — internals preserved, only the container moved
        # into the Stream product workspace.
        stream_admin_panel = ctk.CTkFrame(tab_product_stream, fg_color="#0f151c", corner_radius=18)
        stream_admin_panel.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        stream_admin_panel.grid_columnconfigure(0, weight=1)
        stream_admin_panel.grid_rowconfigure(0, weight=1)

        self.stream_admin_ui = StreamAdminUI(
            ui_state=self._ui_state,
            dispatcher=CallbackDispatcher(source="StreamAdminUI"),
            on_log=self._on_stream_admin_log,
            schedule_ui_update=lambda fn: self.after(0, fn),
        )
        self.stream_admin_ui.build(stream_admin_panel)
        # Wire stream content frames to side_panel sub-tabs
        self._stream_subtab_data["emision"]["frame"] = self.stream_admin_ui.tab_stream_live
        self._stream_subtab_data["acciones"]["frame"] = self.stream_admin_ui.tab_stream_actions
        self._wire_stream_admin_callbacks()

        cohost_panel_frame = ctk.CTkFrame(tab_product_cohost, fg_color="#0f151c", corner_radius=18)
        cohost_panel_frame.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        cohost_panel_frame.grid_columnconfigure(0, weight=1)
        cohost_panel_frame.grid_rowconfigure(0, weight=1)
        self.cohost_agenda_panel = CoHostAgendaPanel(
            on_add_topic=lambda title, angle, constraints, priority, length, turns: self._kira_agenda_add_topic(title, angle, constraints, priority, length, turns),
            on_remove_topic=lambda index: self._kira_agenda_remove_topic(index),
            on_move_topic=lambda index, direction: self._kira_agenda_move_topic(index, direction),
            on_select_profile=lambda name: self._kira_agenda_select_profile(name),
            on_save_profile=lambda name, style, priority, length: self._kira_agenda_save_profile(name, style, priority, length),
            on_session_settings=lambda turns, rhythm, length, safety_mode: self._kira_agenda_set_session_settings(turns, rhythm, length, safety_mode),
            on_enable=lambda: self._kira_agenda_enable(),
            on_soft_stop=lambda: self._kira_agenda_soft_stop(),
            on_emergency_stop=lambda: self._kira_agenda_emergency_stop(),
            on_approve_suggestion=lambda topic_id: self._kira_agenda_approve_suggestion(topic_id),
            on_reject_suggestion=lambda topic_id: self._kira_agenda_reject_suggestion(topic_id),
        )
        self.cohost_agenda_panel.build(cohost_panel_frame)
        self.cohost_agenda_panel.set_profiles(self.cohost_profiles, self._current_cohost_profile)

        music_panel_frame = ctk.CTkFrame(tab_product_music, fg_color="#0f151c", corner_radius=18)
        music_panel_frame.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        music_panel_frame.grid_columnconfigure(0, weight=1)
        music_panel_frame.grid_rowconfigure(0, weight=1)
        self.music_panel = MusicPanel(
            on_import=lambda mood: self._music_import_track(mood),
            on_play_mood=lambda mood: self._music_play_mood(mood),
            on_stop=lambda: self._music_stop(),
            on_cleanup_missing=lambda: self._music_cleanup_missing(),
            on_delete_track=lambda track_id: self._music_delete_track(track_id),
        )
        self.music_panel.build(music_panel_frame)
        self._music_update_panel()

        # Avatar / OBS panel
        avatar_panel_frame = ctk.CTkFrame(tab_product_avatar, fg_color="#0f151c", corner_radius=18)
        avatar_panel_frame.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        avatar_panel_frame.grid_columnconfigure(0, weight=1)
        avatar_panel_frame.grid_rowconfigure(0, weight=1)

        self._avatar_panel = AvatarPanel(
            parent_frame=avatar_panel_frame,
            on_log=lambda msg: self._print_log(msg),
            schedule_ui_update=lambda fn: self.after(0, fn),
            on_obs_enable=lambda: self._obs_start_from_config(),
            on_obs_disable=lambda: self._obs_stop_runtime(),
            on_obs_connect=lambda: self._obs_connect_now(),
        )
        self._avatar_panel.build()
        self._avatar_panel.set_state_bridge(self._avatar_bridge)

        # OBS WebSocket client — initialize and connect if enabled
        self._init_obs_client()

        # Advanced panel
        self._advanced_panel = AdvancedModePanel(
            parent_frame=self,
            ui_state=self._ui_state,
            dispatcher=CallbackDispatcher(source="AdvancedModePanel"),
            log_queue=self.log_queue,
            text_kira_response=self.text_kira_response,
            on_log_action=None,
            schedule_ui_update=lambda fn: self.after(0, fn),
        )
        self._advanced_mode_panel = self._advanced_panel.build()
        self.consola = self._advanced_panel.consola
        self.consola_acciones = self._advanced_panel.consola_acciones
        self.consola_youtube = self._advanced_panel.consola_youtube
        self.text_stream_admin_log = self._advanced_panel.text_stream_admin_log
        self.tabview = self._advanced_panel.tabview

        # Store frame references
        self._frame_model = frame_model
        self._frame_profile = frame_profile
        self._frame_bottom = frame_bottom
        self._app_shell = app_shell
        self._main_interaction_panel = main_panel
        self._side_config_panel = side_panel
        self._product_workspace_panel = side_panel

        self._show_main_view("Kira")
        self._toggle_logs_panel()

    # ──────────────────────────────────────────────
    # Music bed wiring
    # ──────────────────────────────────────────────

    def _music_import_track(self, mood: str) -> None:
        paths = filedialog.askopenfilenames(
            title="Elegir música para mood",
            filetypes=[("Audio", "*.mp3 *.wav")],
        )
        if not paths:
            return
        imported = 0
        errors: list[str] = []
        for path in paths:
            try:
                track = self.music_library.add_file(path, mood)
                imported += 1
                self._print_log(f"[Música] Importado como {track.label}: {track.original_name}")
            except ValueError as exc:
                errors.append(f"{os.path.basename(path)}: {exc}")
        self._music_update_panel()
        if errors:
            self._notify_operator("Música", "\n".join(errors[:6]))
        elif imported:
            self._print_log(f"[Música] {imported} track(s) importados para {mood}.")

    def _music_play_mood(self, mood: str) -> None:
        if self.audio_bed.request_mood(mood, force=True, boundary=True):
            self._music_update_panel()

    def _music_stop(self) -> None:
        self.audio_bed.stop()
        self._print_log("[Música] Fade out solicitado.")

    def _music_cleanup_missing(self) -> None:
        if not messagebox.askyesno(
            "Limpiar faltantes",
            "¿Quitar de la biblioteca todos los tracks cuyo archivo ya no existe?\n\nNo borra archivos de música; solo limpia metadata faltante.",
        ):
            return
        removed = self.music_library.cleanup_missing()
        self._music_update_panel()
        self._print_log(f"[Música] Tracks faltantes limpiados: {removed}.")

    def _music_delete_track(self, track_id: str) -> None:
        track = self.music_library.tracks.get(track_id)
        if not track:
            self._music_update_panel()
            self._print_log("[Música] El track ya no existe en la biblioteca.")
            return
        if not messagebox.askyesno(
            "Eliminar track",
            f"¿Eliminar '{track.original_name}' de la biblioteca de música?\n\n"
            "Se borrará la metadata y, si el archivo importado está dentro de la carpeta administrada por la app, también ese archivo interno. "
            "No se borran archivos fuente externos.",
        ):
            return
        removed = self.music_library.remove(track_id, delete_file=True)
        self._music_update_panel()
        if removed:
            self._print_log(f"[Música] Track eliminado: {track.label} — {track.original_name}")
        else:
            self._print_log("[Música] No se pudo eliminar: el track ya no existe.")

    def _music_update_panel(self) -> None:
        if hasattr(self, "music_panel"):
            self.music_panel.update_library(
                self.music_library.counts_by_mood(),
                list(self.music_library.all_tracks()),
            )

    # ──────────────────────────────────────────────
    # Stream Admin wiring
    # ──────────────────────────────────────────────

    def _wire_stream_admin_callbacks(self) -> None:
        sa = self.stream_admin_ui
        sa.set_connect_callback(lambda rw: self._stream_admin_connect(rw))
        sa.set_disconnect_callback(lambda: self._stream_admin_disconnect())
        sa.set_save_oauth_callback(lambda: self._stream_admin_save_oauth_client())
        sa.set_refresh_metadata_callback(lambda: self._stream_admin_refresh_metadata())
        sa.set_suggest_metadata_callback(lambda: self._stream_admin_suggest_metadata())
        sa.set_apply_metadata_callback(lambda: self._stream_admin_apply_metadata())
        sa.set_reject_pending_callback(lambda: self._stream_admin_reject_pending())
        sa.set_apply_runtime_settings_callback(lambda: self._stream_admin_apply_runtime_settings())
        sa.set_propose_high_risk_callback(lambda action: self._stream_admin_propose_high_risk(action))
        sa.set_connect_current_chat_callback(lambda: self._stream_admin_connect_current_chat())
        sa.set_send_chat_callback(lambda: self._stream_admin_send_chat())
        sa.set_toggle_small_stream_callback(lambda: self._stream_admin_toggle_small_stream())
        sa.set_simulate_chat_callback(lambda: self._stream_admin_simulate_chat())
        sa.set_force_kira_callback(lambda: self._stream_admin_force_kira_comment())
        sa.set_connect_chat_live_callback(lambda: self._on_stream_admin_connect_chat_live())
        sa.set_connect_chat_twitch_callback(lambda: self._on_stream_admin_button_twitch())
        sa.set_threshold_preset_callback(lambda v: self._on_stream_admin_threshold_preset(v))
        sa.set_cooldown_preset_callback(lambda v: self._on_stream_admin_cooldown_preset(v))
        sa.set_refresh_user_list_callback(lambda: self._stream_admin_refresh_user_list())
        sa.set_agenda_add_topic_callback(lambda title, angle, constraints: self._kira_agenda_add_topic(title, angle, constraints))
        sa.set_agenda_enable_callback(lambda: self._kira_agenda_enable())
        sa.set_agenda_soft_stop_callback(lambda: self._kira_agenda_soft_stop())
        sa.set_agenda_emergency_stop_callback(lambda: self._kira_agenda_emergency_stop())

    def _kira_agenda_add_topic(self, title: str, angle: str, constraints: list[str], priority: str = "normal", response_length: str = "normal", max_turns: int | None = None) -> None:
        title = (title or "").strip()
        try:
            if max_turns is not None:
                self.kira_agenda.set_session_settings(max_turns_per_topic=max_turns, response_length=response_length)
            topic = self.kira_agenda.add_topic(title, angle, constraints, approved=True, priority=priority, response_length=response_length)
            self.kira_agenda.queue_topic(topic.id)
        except ValueError as e:
            self._notify_operator("Kira Agenda", str(e))
            return
        self._on_stream_admin_log(f"[Kira Agenda] Tema aprobado y encolado: {topic.title} ({topic.priority}; sesión: {self.kira_agenda.rhythm}/{self.kira_agenda.response_length}, {self.kira_agenda.max_turns_per_topic} turnos globales)")
        self._kira_agenda_update_status()

    def _editorial_card_create_or_update(
        self,
        *,
        topic: str,
        summary: str,
        streamer_take: str,
        counterpoints: list[str] | None = None,
        discussion_hooks: list[str] | None = None,
        triggers: list[str] | None = None,
    ) -> EditorialCard:
        return self.editorial_agenda.create_or_update_card(
            topic=topic,
            summary=summary,
            streamer_take=streamer_take,
            counterpoints=counterpoints,
            discussion_hooks=discussion_hooks,
            triggers=triggers,
        )

    def _editorial_card_arm(self, card_id: str) -> bool:
        return self.editorial_agenda.arm_card(card_id)

    def _editorial_card_link_to_agenda_topic(self, topic_id: str, card_id: str) -> bool:
        return self.editorial_agenda.link_card_to_topic(topic_id, card_id)

    def _record_accepted_kira_agenda_output(self, output: str) -> None:
        self.kira_agenda.record_accepted_output(output)
        if self.editorial_agenda.mark_used_after_successful_generation():
            self._on_stream_admin_log("[Editorial Cards] Cue card usada y marcada como consumida.")

    def _kira_agenda_set_session_settings(self, turns: int, rhythm: str, response_length: str, safety_mode: str = "live_safe") -> None:
        self.kira_agenda.set_session_settings(max_turns_per_topic=turns, rhythm=rhythm, response_length=response_length, safety_mode=safety_mode)
        self._kira_agenda_update_status()

    def _kira_agenda_select_profile(self, name: str) -> None:
        if name not in self.cohost_profiles:
            return
        self._current_cohost_profile = name
        self.kira_agenda.set_profile(self.cohost_profiles[name])
        self._on_stream_admin_log(f"[Kira Agenda] Perfil Co-host activo: {name}")

    def _kira_agenda_save_profile(self, name: str, style: str, priority: str, response_length: str) -> None:
        safe_name = sanitize_profile_name(name)
        if not safe_name:
            self._notify_operator("Kira Agenda", "El perfil necesita un nombre.")
            return
        try:
            normalized = normalize_cohost_profile({
                "style": self.kira_agenda.sanitize_topic_text(style, field="profile_style", required=True),
                "default_priority": priority,
                "default_response_length": response_length,
            })
            self.kira_agenda.set_profile(normalized)
        except ValueError as e:
            self._notify_operator("Kira Agenda", str(e))
            return
        self.cohost_profiles[safe_name] = normalized
        self._current_cohost_profile = safe_name
        save_cohost_profiles(self.cohost_profiles)
        self.cohost_agenda_panel.set_profiles(self.cohost_profiles, safe_name)
        self._on_stream_admin_log(f"[Kira Agenda] Perfil Co-host guardado: {safe_name}")

    def _kira_agenda_topic_by_queue_index(self, index: int):
        queued = self.kira_agenda.queued_topics()
        if index < 1 or index > len(queued):
            self._notify_operator("Kira Agenda", "Elegí un número válido de la cola.")
            return None
        return queued[index - 1]

    def _kira_agenda_remove_topic(self, index: int) -> None:
        topic = self._kira_agenda_topic_by_queue_index(index)
        if not topic:
            return
        if not messagebox.askyesno(
            "Eliminar tema de agenda",
            f"¿Eliminar de la cola el tema '{topic.title}'?\n\nLa acción no borra perfiles ni historial, solo este tema pendiente.",
        ):
            return
        self.kira_agenda.remove_queued_topic(topic.id)
        self._on_stream_admin_log(f"[Kira Agenda] Tema eliminado de la cola: {topic.title}")
        self._kira_agenda_update_status()

    def _kira_agenda_move_topic(self, index: int, direction: int) -> None:
        topic = self._kira_agenda_topic_by_queue_index(index)
        if not topic:
            return
        self.kira_agenda.move_queued_topic(topic.id, direction)
        self._on_stream_admin_log(f"[Kira Agenda] Tema reordenado: {topic.title}")
        self._kira_agenda_update_status()

    def _kira_agenda_enable(self) -> None:
        if not self.kira_agenda.queued_topics() and not self.kira_agenda.active_topic:
            self._on_stream_admin_log("[Kira Agenda] No se activa: no hay temas en cola.")
            self._kira_agenda_update_status()
            return
        self._kira_agenda_force_strict_chat_filter()
        self.kira_agenda.enable()
        self._on_stream_admin_log("[Kira Agenda] Modo co-host con agenda activado.")
        # Start music bed when co-host mode activates — it's an intentional segment
        if hasattr(self, "audio_bed") and self.audio_bed.current_track is None and self.audio_bed.enabled:
            self.audio_bed.request_mood("normal", force=True, boundary=True)
        self._kira_agenda_update_status()
        self._kira_agenda_tick()

    def _kira_agenda_soft_stop(self) -> None:
        action = self.kira_agenda.soft_stop()
        self._enqueue_kira_agenda_action(action)
        self._on_stream_admin_log("[Kira Agenda] Stop suave solicitado.")
        self._kira_agenda_update_status()

    def _kira_agenda_emergency_stop(self) -> None:
        self._kira_agenda_prefetched_action = None
        self.kira_agenda.emergency_stop()
        if self._kira_agenda_tick_id is not None:
            try:
                self.after_cancel(self._kira_agenda_tick_id)
            except Exception:
                pass
            self._kira_agenda_tick_id = None
        if hasattr(self.motor_ia, "drop_pending_sources"):
            self.motor_ia.drop_pending_sources(("kira-agenda",))
        self._on_stream_admin_log("[Kira Agenda] Emergencia: agenda detenida y pendientes descartados.")
        self._kira_agenda_restore_chat_filter()
        self._clear_obs_joyita("KiraJoyita")
        self._kira_agenda_update_status()

    def _kira_agenda_force_strict_chat_filter(self) -> None:
        if not getattr(self, "smart_agg", None):
            return
        if not hasattr(self, "_kira_agenda_previous_filter_policy"):
            self._kira_agenda_previous_filter_policy = self.smart_agg.get_filter_policy()
        self.smart_agg.set_filter_policy("strict")

    def _kira_agenda_restore_chat_filter(self) -> None:
        if not getattr(self, "smart_agg", None):
            return
        previous = getattr(self, "_kira_agenda_previous_filter_policy", None)
        if previous:
            self.smart_agg.set_filter_policy(previous)
            delattr(self, "_kira_agenda_previous_filter_policy")

    def _kira_agenda_approve_suggestion(self, topic_id: str) -> None:
        """Approve a DRAFTED suggestion: mark APPROVED then QUEUED."""
        try:
            self.kira_agenda.approve_topic(topic_id)
            self.kira_agenda.queue_topic(topic_id)
        except (ValueError, KeyError):
            pass
        self._kira_agenda_update_status()

    def _kira_agenda_reject_suggestion(self, topic_id: str) -> None:
        """Reject a DRAFTED suggestion: mark SKIPPED."""
        try:
            topic = next((t for t in self.kira_agenda.topics if t.id == topic_id), None)
            if topic is not None and topic.status == TopicStatus.DRAFTED:
                topic.status = TopicStatus.SKIPPED
        except (ValueError, KeyError, AttributeError):
            pass
        self._kira_agenda_update_status()

    def _kira_agenda_tick(self) -> None:
        if not hasattr(self, "kira_agenda"):
            return

        # Auto-recovery: if PAUSED, check if the recovery policy permits a retry
        if self.kira_agenda.state == AgendaState.PAUSED_NEEDS_OPERATOR:
            if self.kira_agenda.can_auto_resume():
                self._on_stream_admin_log(
                    f"[Kira Agenda] Auto-recuperación: intento {self.kira_agenda.recovery.retry_attempt}"
                    f" de {len(RecoveryPolicy.RETRY_DELAYS_SECONDS)}"
                    f" | Error: {self.kira_agenda.recovery.error_code.human()}"
                )
                # Fall through to next_action — state is now IDLE
            elif self.kira_agenda.state == AgendaState.HARD_PAUSED:
                self._on_stream_admin_log(
                    "[Kira Agenda] HARD_PAUSED: se requiere intervención del streamer. "
                    f"Motivo: {self.kira_agenda.recovery.last_failure_reason or 'fallos repetidos'}"
                )
                self._kira_agenda_update_status()
                return  # No tick for HARD_PAUSED
            else:
                # Timer not ready — reschedule and wait
                self._kira_agenda_update_status()
                self._kira_agenda_schedule_tick(4500)
                return

        # Auto-exit: when IDLE with no active topic and nothing queued,
        # co-host has exhausted all planned work.  Transition to OFF so
        # the tick stops, the UI reflects reality, and chat flows through
        # the standalone RF3 path without overhead.
        # Moved AFTER next_action() so _select_next_topic() has a chance
        # to pick the next queued topic before we declare the session done.

        try:
            action = self.kira_agenda.next_action(
                motor_busy=getattr(self.motor_ia, "is_processing", False),
                kira_speaking=getattr(self.motor_ia, "is_speaking", False),
            )
        except Exception:
            # Safety net: if next_action ever throws (shouldn't — it's
            # deterministic and runs on internal state only), log, skip
            # this tick, and reschedule so Kira doesn't go permanently
            # silent on a live stream.
            logger.exception("KiraAgendaController.next_action() raised")
            self._on_stream_admin_log(
                "[Kira Agenda] Error interno en next_action; reintentando en el siguiente tick."
            )
            action = AgendaAction.none()

        self._enqueue_kira_agenda_action(action)

        if (
            action.kind == "none"
            and self.kira_agenda.state == AgendaState.IDLE
            and self.kira_agenda.active_topic is None
            and not self.kira_agenda.queued_topics()
        ):
            self.kira_agenda.state = AgendaState.OFF
            self._on_stream_admin_log("[Kira Agenda] Sesión completada: sin temas pendientes.")
            self._kira_agenda_restore_chat_filter()
            self._kira_agenda_update_status()
            self._clear_obs_joyita("KiraJoyita")
            return

        # Auto-suggestion trigger: on 3rd consecutive IDLE+empty-queue tick (~13.5s)
        if self.kira_agenda.state == AgendaState.IDLE and not self.kira_agenda.queued_topics():
            self._idle_ticks += 1
            if self._idle_ticks >= 3 and self.kira_agenda.can_suggest():
                # Rich-context gate: skip if aggregator has no data
                if self.smart_agg and getattr(self.smart_agg, "_session_id", None):
                    try:
                        intent_summary = self.smart_agg.intent_aggregator.summarize()
                        vibe_data = self.smart_agg.vibe_thermometer.compute_vibe()
                        vibe_temp = vibe_data.get("temperature", 50) if isinstance(vibe_data, dict) else 50
                        snapshots = self.smart_agg.history.get_recent_context_snapshots(
                            self.smart_agg._session_id, max_items=3,
                        )
                        suggestions = generate_suggestions(
                            intent_summary=intent_summary,
                            snapshots=snapshots,
                            vibe_temperature=vibe_temp,
                            existing_topics=self.kira_agenda.topics,
                            last_outputs=self.kira_agenda.last_outputs,
                        )
                        if suggestions:
                            self.kira_agenda.suggest_topics(suggestions)
                    except Exception:
                        pass  # Swallow to avoid breaking the tick loop
                self._idle_ticks = 0
        else:
            self._idle_ticks = 0

        self._kira_agenda_update_status()
        self._kira_agenda_schedule_tick(4500)

    def _kira_agenda_schedule_tick(self, delay_ms: int) -> None:
        if self.kira_agenda.state in {AgendaState.OFF, AgendaState.PAUSED_NEEDS_OPERATOR, AgendaState.HARD_PAUSED}:
            return
        if self._kira_agenda_tick_id is not None:
            try:
                self.after_cancel(self._kira_agenda_tick_id)
            except Exception:
                pass
        self._kira_agenda_tick_id = self.after(delay_ms, self._kira_agenda_tick)

    def _enqueue_kira_agenda_action(self, action: AgendaAction) -> None:
        if action.kind != "enqueue" or not action.prompt:
            return
        self._kira_agenda_prefetched_action = None
        if hasattr(self.motor_ia, "clear_prefetched_agenda"):
            self.motor_ia.clear_prefetched_agenda()
        if action.source.startswith("kira-agenda") and hasattr(self.motor_ia, "replace_pending"):
            self.motor_ia.replace_pending(action.prompt, priority=action.priority, source=action.source)
        else:
            self.motor_ia.enqueue(action.prompt, priority=action.priority, source=action.source)

    def _kira_agenda_has_higher_priority_pending(self, action: AgendaAction) -> bool:
        if not hasattr(self.motor_ia, "has_pending_priority_before"):
            return False
        return bool(self.motor_ia.has_pending_priority_before(action.priority))

    def _kira_agenda_has_non_agenda_audio_work(self) -> bool:
        """Return True when a human/direct path owns processing or speech."""
        processing_source = str(getattr(self.motor_ia, "current_processing_source", "") or "")
        speech_source = str(getattr(self.motor_ia, "current_speech_source", "") or "")
        processing = bool(getattr(self.motor_ia, "is_processing", False))
        speaking = bool(getattr(self.motor_ia, "is_speaking", False))

        if processing and not processing_source.startswith("kira-agenda"):
            return True
        if speaking and not speech_source.startswith("kira-agenda"):
            return True
        return False

    def _kira_agenda_consume_pending_chat_if_due(self) -> bool:
        compact_chat = getattr(self, "_kira_agenda_pending_compact_chat", "").strip()
        if not compact_chat or not hasattr(self, "kira_agenda"):
            return False
        if not hasattr(self.kira_agenda, "chat_signal_due") or not self.kira_agenda.chat_signal_due():
            return False
        action = self.kira_agenda.next_action(compact_chat=compact_chat)
        if action.kind != "enqueue":
            return False
        self._kira_agenda_pending_compact_chat = ""
        self._enqueue_kira_agenda_action(action)
        self._kira_agenda_update_status()
        return True

    def _kira_agenda_prefetch_while_speaking(self) -> None:
        if not hasattr(self, "kira_agenda") or not hasattr(self.motor_ia, "prefetch_agenda"):
            return
        if not self._is_kira_agenda_speech_source():
            return
        action = self.kira_agenda.prefetch_action_after_current_speech()
        if action.kind != "enqueue" or not action.prompt:
            return
        if self.motor_ia.prefetch_agenda(action.prompt, priority=action.priority, source=action.source):
            self._kira_agenda_prefetched_action = action

    def _kira_agenda_play_prefetched_if_ready(self) -> bool:
        action = getattr(self, "_kira_agenda_prefetched_action", None)
        if not action or not hasattr(self.motor_ia, "wait_prefetched_agenda"):
            return False
        if self._kira_agenda_has_higher_priority_pending(action):
            self._on_stream_admin_log("[Kira Agenda] Prefetch pausado: hay PTT/chat pendiente con más prioridad.")
            self._kira_agenda_clear_prefetch()
            return False
        if self._kira_agenda_has_non_agenda_audio_work():
            self._on_stream_admin_log("[Kira Agenda] Prefetch cancelado: hay interacción directa activa.")
            self._kira_agenda_clear_prefetch()
            return False
        if not self.motor_ia.wait_prefetched_agenda(timeout=0.35):
            return False
        self.kira_agenda.start_prefetched_action(action)
        self._kira_agenda_prefetched_action = None
        if self.motor_ia.play_prefetched_agenda():
            self._kira_agenda_update_status()
            return True
        return False

    def _kira_agenda_clear_prefetch(self) -> None:
        self._kira_agenda_prefetched_action = None
        if hasattr(self.motor_ia, "clear_prefetched_agenda"):
            self.motor_ia.clear_prefetched_agenda()

    def _is_kira_agenda_speech_source(self) -> bool:
        source = getattr(self.motor_ia, "current_speech_source", "") or ""
        return str(source).startswith("kira-agenda")

    def _kira_agenda_update_status(self) -> None:
        if not hasattr(self, "stream_admin_ui") or not hasattr(self, "kira_agenda"):
            return
        active = self.kira_agenda.active_topic
        queued = len(self.kira_agenda.queued_topics())
        recovery = self.kira_agenda.recovery
        error_info = ""
        if recovery.error_code != ErrorCode.NONE:
            error_info = f" · ⚠ {recovery.error_code.human()} (intento {recovery.retry_attempt}/{len(RecoveryPolicy.RETRY_DELAYS_SECONDS)})"
        if active:
            text = f"Kira está desarrollando: “{active.title}” · Estado: {self.kira_agenda.state.value} · modo: {self.kira_agenda.safety_mode} · fallos: {self.kira_agenda.failure_count}{error_info}"
            current_topic = f"“{active.title}”\nPrioridad: {active.priority} · Sesión: {self.kira_agenda.rhythm}/{self.kira_agenda.response_length}/{self.kira_agenda.safety_mode}\nTurnos hablados: {active.turns_spoken}/{self.kira_agenda.max_turns_per_topic}"
        else:
            text = f"Agenda: {self.kira_agenda.state.value} · temas en cola: {queued} · modo: {self.kira_agenda.safety_mode} · fallos: {self.kira_agenda.failure_count}{error_info}"
            current_topic = "Sin tema activo"
        if self.kira_agenda.state == AgendaState.HARD_PAUSED:
            color = "#cc3333"
        elif self.kira_agenda.state == AgendaState.PAUSED_NEEDS_OPERATOR:
            color = "#ffaa00"
        else:
            color = "#8fa3b8"
        self.after(0, lambda: self.stream_admin_ui.set_agenda_status(text, color))
        if hasattr(self, "cohost_agenda_panel"):
            queue_lines = [
                f"{idx}. [{topic.priority}] {topic.title}"
                for idx, topic in enumerate(self.kira_agenda.queued_topics(), start=1)
            ]
            self.after(0, lambda: self.cohost_agenda_panel.update_status(
                state=self.kira_agenda.state.value,
                current_topic=current_topic,
                queue_lines=queue_lines,
                failures=self.kira_agenda.failure_count,
                error_code=recovery.error_code.human(),
                error_reasons=recovery.last_reasons,
            ))
            # Forward DRAFTED suggestions to the panel
            drafted = self.kira_agenda.drafted_topics()
            suggestions_list = [
                {
                    "title": t.title,
                    "angle": t.angle,
                    "confidence": getattr(t, "confidence", "LOW"),
                    "source": getattr(t, "source", ""),
                    "topic_id": t.id,
                }
                for t in drafted
            ]
            self.after(0, lambda sl=suggestions_list: self.cohost_agenda_panel.update_suggestions(sl))
        # BUG-003: visual signal for PAUSED_NEEDS_OPERATOR via TTS pill
        # Only update on state transitions to avoid overwriting pipeline-driven TTS state
        was_paused = getattr(self, "_agenda_was_paused", False)
        is_paused = self.kira_agenda.state == AgendaState.PAUSED_NEEDS_OPERATOR
        if self.status_bar and was_paused != is_paused:
            if is_paused:
                self.after(0, lambda: self.status_bar.update_tts_status("paused"))
            else:
                self.after(0, lambda: self.status_bar.update_tts_status("idle"))
        self._agenda_was_paused = is_paused

    def _init_stream_admin(self) -> None:
        try:
            config_path = os.path.join(BASE_DIR, "config", "stream_admin.yaml")
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

    # ──────────────────────────────────────────────
    # Stream Admin methods (delegated from UI)
    # ──────────────────────────────────────────────

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
                    self.after(0, lambda err=e: self._notify_operator("Stream Admin", str(err), level="error"))
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

    def _on_stream_admin_connect_chat_live(self) -> None:
        entry_url = self.stream_admin_ui._widget("entry_stream_chat_url")
        if not entry_url:
            return
        raw = entry_url.get().strip()
        raw = raw.replace("\x00", "").replace("\n", "").replace("\r", "")[:500]

        from smart_aggregator.url_parser import parse_chat_url

        try:
            parsed = parse_chat_url(raw)
        except ValueError:
            self._notify_operator("Chat Live", "URL no valida o no soportada")
            return

        platform = parsed["platform"]
        source_id = parsed["source_id"]

        if self._ui_state.smart_agg_connected or self._ui_state.smart_agg_connecting:
            self.smart_agg_ui.toggle_connection()
            lbl = self.stream_admin_ui._widget("lbl_stream_chat_live_status")
            if lbl:
                lbl.configure(text="Desconectado", text_color="#aa4444")
            return

        threshold_entry = self.stream_admin_ui._widget("entry_stream_chat_threshold")
        cooldown_entry = self.stream_admin_ui._widget("entry_stream_chat_cooldown")
        if threshold_entry or cooldown_entry:
            import math
            try:
                thr = float(threshold_entry.get().strip()) if threshold_entry else 1.0
                if not math.isfinite(thr):
                    thr = 1.0
                thr = max(0.1, min(thr, 100.0))
            except (ValueError, TypeError):
                thr = 1.0
            try:
                cd = float(cooldown_entry.get().strip()) if cooldown_entry else 45.0
                if not math.isfinite(cd):
                    cd = 45.0
                cd = max(5.0, min(cd, 3600.0))
            except (ValueError, TypeError):
                cd = 45.0
            self.smart_agg.set_activity_limits(threshold_per_second=thr, cooldown_seconds=cd, reset=True)

        spam_entry = self.stream_admin_ui._widget("entry_stream_chat_spam")
        if spam_entry:
            try:
                raw_sp = spam_entry.get().strip()[:4]
                sp = max(1, min(int(raw_sp), 9999))
            except (ValueError, TypeError):
                sp = 10
            self.smart_agg.set_spam_limits(max_messages_per_user=sp)

        previous_preset = self.smart_agg.get_filter_policy()
        preset = "twitch_relaxed" if platform == "twitch" else "balanced"
        self.smart_agg.set_filter_policy(preset)

        if self.smart_agg_ui.connect_to(source_id, platform=platform):
            self.entry_youtube_video.delete(0, "end")
            self.entry_youtube_video.insert(0, source_id)
            lbl = self.stream_admin_ui._widget("lbl_stream_chat_live_status")
            if lbl:
                lbl.configure(text=f"Conectado [{platform}]: {source_id}", text_color="#44aa44")
            self._on_stream_admin_log(f"[StreamAdmin] Chat Live conectado [{platform}]: {source_id}")
        else:
            self.smart_agg.set_filter_policy(previous_preset)

    def _on_stream_admin_button_twitch(self) -> None:
        """Connect to a Twitch chat live using the URL entry."""
        from smart_aggregator.url_parser import parse_chat_url

        entry_url = self.stream_admin_ui._widget("entry_stream_chat_url")
        if not entry_url:
            return

        raw = entry_url.get().strip()
        raw = raw.replace("\x00", "").replace("\n", "").replace("\r", "")[:500]

        if not raw:
            self._notify_operator("Twitch Chat", "Ingresa una URL de Twitch (twitch.tv/canal).")
            return

        try:
            parsed = parse_chat_url(raw)
        except ValueError:
            self._notify_operator("Twitch Chat", "URL no valida o no soportada")
            return

        if parsed["platform"] != "twitch":
            self._notify_operator("Twitch Chat", "La URL no es de Twitch. Usa un enlace twitch.tv/canal.")
            return

        if self._ui_state.smart_agg_connected or self._ui_state.smart_agg_connecting:
            self.smart_agg_ui.toggle_connection()
            lbl = self.stream_admin_ui._widget("lbl_stream_chat_live_status")
            if lbl:
                lbl.configure(text="Desconectado", text_color="#aa4444")
            return

        source_id = parsed["source_id"]
        previous_preset = self.smart_agg.get_filter_policy()
        self.smart_agg.set_filter_policy("twitch_relaxed")

        if self.smart_agg_ui.connect_to(source_id, platform="twitch"):
            self.entry_youtube_video.delete(0, "end")
            self.entry_youtube_video.insert(0, source_id)
            lbl = self.stream_admin_ui._widget("lbl_stream_chat_live_status")
            if lbl:
                lbl.configure(text=f"Conectado [twitch]: {source_id}", text_color="#44aa44")
            self._on_stream_admin_log(f"[StreamAdmin] Chat Live Twitch conectado: {source_id}")
        else:
            self.smart_agg.set_filter_policy(previous_preset)

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
                        self.after(0, self._stream_admin_disconnect_api_chat)
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

    def _on_stream_admin_threshold_preset(self, value: str) -> None:
        import math
        try:
            thr = float(value)
            if not math.isfinite(thr):
                thr = 1.0
            thr = max(0.1, min(thr, 100.0))
        except (ValueError, TypeError):
            thr = 1.0
        if self.smart_agg:
            self.smart_agg.set_activity_limits(threshold_per_second=thr, reset=True)
            self._on_stream_admin_log(f"[StreamAdmin] Umbral de actividad: {thr:.1f} msg/s.")

    def _on_stream_admin_cooldown_preset(self, value: str) -> None:
        import math
        try:
            cd = float(value)
            if not math.isfinite(cd):
                cd = 45.0
            cd = max(5.0, min(cd, 3600.0))
        except (ValueError, TypeError):
            cd = 45.0
        if self.smart_agg:
            self.smart_agg.set_activity_limits(cooldown_seconds=cd, reset=True)
            self._on_stream_admin_log(f"[StreamAdmin] Cooldown de actividad: {int(cd)}s.")

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

    def _on_threshold_preset(self, value: str) -> None:
        if hasattr(self, "entry_youtube_threshold"):
            self.entry_youtube_threshold.delete(0, "end")
            self.entry_youtube_threshold.insert(0, value)
        if hasattr(self, "smart_agg_ui"):
            self.smart_agg_ui.apply_threshold(log=True)

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

    def _stream_admin_track_chat_user(self, message: dict) -> None:
        self.stream_admin_ui.track_chat_user(message)
        self.after(0, self.stream_admin_ui.refresh_user_list)

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

    def _on_stream_admin_log(self, msg: str) -> None:
        self.after(0, lambda m=msg: self._append_stream_admin_log(m))
        clean = msg.replace("[StreamAdmin] ", "")
        self.after(0, lambda m=clean: self._log_accion(m))

    def _append_stream_admin_log(self, msg: str) -> None:
        if not hasattr(self, "_advanced_panel"):
            return
        self._advanced_panel.append_to_textbox(self.text_stream_admin_log, msg, max_lines=1000)

    def _on_stream_admin_state(self, state: dict) -> None:
        self.stream_admin_ui.on_state(state)

    def _on_stream_admin_metadata(self, metadata: dict) -> None:
        self.stream_admin_ui.on_metadata(metadata)

    def _on_stream_admin_pending(self, pending: dict) -> None:
        self.stream_admin_ui.on_pending(pending)

    def _on_stream_admin_analytics(self, snapshot: dict) -> None:
        self.stream_admin_ui.on_analytics(snapshot)

    def _stream_admin_ingest_rf3_event(self, event_type: str, payload: dict) -> None:
        self.stream_admin_ui.ingest_rf3_event(event_type, payload)

    def _stream_admin_inject_silent_context(self, context: Any) -> None:
        self.stream_admin_ui._inject_silent_context(context)

    # ──────────────────────────────────────────────
    # Smart Aggregator initialization
    # ──────────────────────────────────────────────

    def _init_smart_aggregator(self) -> None:
        try:
            config_path = os.path.join(BASE_DIR, "config", "smart_aggregator.yaml")
            self.smart_agg = Aggregator(config_path=config_path, llm_interface=None)
        except Exception as e:
            self.smart_agg = None
            logger.exception("No se pudo inicializar Smart Aggregator")
            self.log_queue.put(f"[SmartAggregator] No disponible: {e}")
            return

        self.smart_agg_ui = SmartAggregatorUI(
            ui_state=self._ui_state,
            dispatcher=self._smart_agg_dispatcher,
            smart_agg=self.smart_agg,
            motor_ia=self.motor_ia,
            entry_youtube_video=self.entry_youtube_video,
            btn_youtube_chat=self.btn_youtube_chat,
            entry_youtube_user_limit=self.entry_youtube_user_limit,
            entry_youtube_threshold=self.entry_youtube_threshold,
            consola_youtube=self.consola_youtube,
            lbl_kira_chat_state=self.lbl_kira_chat_state,
            status_bar=self.status_bar,
            on_log=lambda msg: self.log_queue.put(msg),
            schedule_ui_update=lambda fn: self.after(0, fn),
            on_track_chat_user=lambda msg: self._stream_admin_track_chat_user(msg),
            on_ingest_rf3=lambda evt, data: self._stream_admin_ingest_rf3_event(evt, data),
            on_joyita=lambda text: self._on_joyita_to_obs(text),
            health_monitor=self.health_monitor,
        )
        self.smart_agg_ui.initialize()

        self.smart_agg.set_busy_callback(self.smart_agg_ui.is_busy)
        self.smart_agg.set_llm_interface(self.smart_agg_ui.llm_interface)
        self.smart_agg.on_filtered_message = self.smart_agg_ui.on_filtered_message
        self.smart_agg.on_vibe_update = self.smart_agg_ui.on_vibe_update
        self.smart_agg.on_activity_trigger = self.smart_agg_ui.on_activity_trigger
        self.smart_agg.on_aggregated_context = self._on_smart_aggregated_context
        self.smart_agg.on_live_safety_log = self._on_stream_admin_log
        self.smart_agg.on_source_error = self.smart_agg_ui.on_source_error
        self.smart_agg.on_source_connect = self.smart_agg_ui.on_source_connect
        self.smart_agg.on_source_disconnect = self.smart_agg_ui.on_source_disconnect

        self.stream_admin_ui.set_smart_agg(self.smart_agg)
        self.stream_admin_ui.set_motor_ia(self.motor_ia)
        self.stream_admin_ui.set_smart_agg_defaults({
            "threshold": self.smart_agg.activity.threshold_per_second,
            "cooldown": self.smart_agg.activity.cooldown_seconds,
        })
        self.log_queue.put("[SmartAggregator] RF3 listo. Ingresa un video_id/URL de YouTube Live para conectar chat.")

    def _on_smart_aggregated_context(self, data: dict) -> None:
        """Route compact chat either to Agenda Mode or the existing RF3 reaction."""
        # When the controller is PAUSED_NEEDS_OPERATOR, chat must fall
        # through to the standalone RF3 reaction path — otherwise chat
        # responses are silently dropped while the operator sees a frozen UI.
        if (
            getattr(self, "kira_agenda", None)
            and self.kira_agenda.state not in {AgendaState.OFF, AgendaState.PAUSED_NEEDS_OPERATOR, AgendaState.HARD_PAUSED}
            # When IDLE with no active topic and nothing queued, co-host has
            # exhausted all planned topics.  Let chat fall through to the
            # standalone RF3 reaction path instead of being silently consumed
            # by next_action() which returns none() in IDLE+empty state.
            and not (
                self.kira_agenda.state == AgendaState.IDLE
                and self.kira_agenda.active_topic is None
                and not self.kira_agenda.queued_topics()
            )
        ):
            intent_summary = data.get("intent_summary") or {}
            compact_chat = intent_summary.get("prompt") or ""
            if not compact_chat:
                context = data.get("context", [])[-6:]
                compact_chat = "\n".join(m.get("text", "") for m in context if m.get("text"))
            if getattr(self.motor_ia, "is_processing", False) or getattr(self.motor_ia, "is_speaking", False):
                self._kira_agenda_pending_compact_chat = compact_chat.strip()
                self._kira_agenda_update_status()
                return
            action = self.kira_agenda.next_action(
                motor_busy=getattr(self.motor_ia, "is_processing", False),
                kira_speaking=getattr(self.motor_ia, "is_speaking", False),
                compact_chat=compact_chat,
            )
            self._enqueue_kira_agenda_action(action)
            self._kira_agenda_update_status()
            return
        # ── Standalone RF3 path ─────────────────────────────────────────
        # Phase B: use ChatContextPacket instead of defective compact_chat
        import smart_aggregator.chat_input_contract as _ic
        if _ic.USE_INPUT_CONTRACT_PROMPT:
            try:
                context = data.get("context", [])
                intent_summary = data.get("intent_summary", {})
                old_compact = intent_summary.get("prompt", "")

                builder = ChatContextPacketBuilder()
                packet = builder.build(context)

                if not packet.should_call_llm:
                    # No useful signal — stay silent instead of generating
                    # "qué paz", "qué silencio", etc.
                    self._log_accion(
                        f"[InputContract] stay_silent: "
                        f"msgs={packet.total_messages} users={packet.unique_users} "
                        f"event={packet.primary_event} confidence={packet.confidence:.2f}"
                    )
                    return

                # Valid signal: use packet context as prompt source
                new_context = packet.to_prompt_context()
                # Inject into data dict for smart_agg_ui
                data["intent_summary"]["prompt"] = new_context
                data["_source_used"] = "input_contract"

                self._log_accion(
                    f"[InputContract] using packet: "
                    f"event={packet.primary_event} goal={packet.response_goal} "
                    f"highlight={'yes' if packet.selected_highlight else 'no'} "
                    f"clusters={len(packet.topic_clusters)} "
                    f"old_compact={old_compact[:80]!r}"
                )
            except Exception:
                # Fallback: use old compact_chat on any error
                data["_source_used"] = "fallback_old_compact"
        else:
            data["_source_used"] = "old_compact"

        self.smart_agg_ui.on_aggregated_context(data)

    # ──────────────────────────────────────────────
    # View switching
    # ──────────────────────────────────────────────

    def _show_main_view(self, name: str) -> None:
        frames = getattr(self, "_main_view_frames", {})
        if name not in frames:
            return
        frames[name].tkraise()
        for view_name, button in getattr(self, "_main_view_buttons", {}).items():
            if view_name == name:
                button.configure(fg_color="#2f5f8f")
            else:
                button.configure(fg_color="#151d26")

    # ──────────────────────────────────────────────
    # Pipeline and UI state
    # ──────────────────────────────────────────────

    def _actualizar_pipeline(self, estado: str) -> None:
        self._ui_state.pipeline_state = estado
        if self.status_bar:
            self.status_bar.update_pipeline_state(estado)
        if hasattr(self, "voice_panel"):
            self.voice_panel.update_tts_label(estado)

        # Safe automatic avatar transition from pipeline state.
        # NOTE: "speaking" is handled by _on_motor_speaking_start + alternation timer,
        # so we skip it here to avoid fighting with the timer.
        if hasattr(self, "_avatar_bridge") and estado != "speaking":
            avatar_state = AvatarStateBridge.from_pipeline_state(estado)
            self._avatar_bridge.set_state(avatar_state)

        # Reset inactivity timer on meaningful activity
        if estado in ("listening", "processing", "idle"):
            self._reset_inactivity_timer()

        def update_status_details():
            if estado == "listening":
                if hasattr(self, "lbl_kira_voice_state"):
                    self.lbl_kira_voice_state.configure(text="Voz/PTT: escuchando", fg_color="#1f5a3a")
            elif self.dispositivo_seleccionado is None:
                if hasattr(self, "lbl_kira_voice_state"):
                    self.lbl_kira_voice_state.configure(text="Voz/PTT: sin mic", fg_color="#4a2630")
            else:
                if hasattr(self, "lbl_kira_voice_state"):
                    self.lbl_kira_voice_state.configure(text="Voz/PTT: listo", fg_color="#1b2633")

        self.after(0, update_status_details)
        self.after(0, lambda: self.barra_rms.grid() if estado == "listening" else self.barra_rms.grid_remove())

    def _toggle_modo_compacto(self) -> None:
        self._modo_compacto = self.switch_compacto.get()
        if self._modo_compacto:
            if hasattr(self, "_side_config_panel"):
                self._side_config_panel.grid_remove()
            self._show_main_view("Kira")
            if hasattr(self, "_advanced_panel"):
                self._set_logs_panel_visible(False)
            self.switch_compacto.configure(text="Completo")
        else:
            if hasattr(self, "_side_config_panel"):
                self._side_config_panel.grid()
            self._toggle_logs_panel()
            self.switch_compacto.configure(text="Compacto")

    def _set_logs_panel_visible(self, visible: bool) -> None:
        if not hasattr(self, "_advanced_panel"):
            return
        self._advanced_panel.set_logs_visible(visible)
        self._logs_panel_visible = visible

    def _toggle_logs_panel(self) -> None:
        if not hasattr(self, "_advanced_panel"):
            return
        if hasattr(self, "switch_compacto") and self.switch_compacto.get():
            self._set_logs_panel_visible(False)
            return
        visible = bool(hasattr(self, "switch_advanced") and self.switch_advanced.get())
        self._set_logs_panel_visible(visible)

    def _toggle_help_section(self, key: str, frame: Any, button: Any) -> None:
        expanded = not self._help_expanded.get(key, False)
        self._help_expanded[key] = expanded
        if expanded:
            frame.grid()
            button.configure(text=button.cget("text").replace("▶", "▼"))
        else:
            frame.grid_remove()
            button.configure(text=button.cget("text").replace("▼", "▶"))

    def _switch_product_tab(self, key: str) -> None:
        """Switch the active product tab (custom button tabs)."""
        if key == self._active_product_tab:
            return
        prev = self._product_tab_data.get(self._active_product_tab)
        if prev:
            prev["button"].configure(fg_color="#151d26", hover_color="#1d2a38", text_color="#6b7b8d")
            prev["frame"].grid_remove()
        curr = self._product_tab_data.get(key)
        if curr:
            curr["button"].configure(fg_color="#2f5f8f", hover_color="#3670aa", text_color="#ffffff")
            curr["frame"].grid()
        self._active_product_tab = key
        # Toggle sub-tab bar visibility
        self._cfg_sub_bar.grid() if key == "config" else self._cfg_sub_bar.grid_remove()
        self._stream_sub_bar.grid() if key == "stream" else self._stream_sub_bar.grid_remove()

    def _switch_cfg_subtab(self, key: str) -> None:
        """Switch the active Configuración sub-tab."""
        if key == self._active_cfg_subtab:
            return
        prev = self._cfg_subtab_data.get(self._active_cfg_subtab)
        if prev:
            prev["button"].configure(fg_color="#0f151c", hover_color="#151d26", text_color="#6b7b8d")
            prev["frame"].grid_remove()
        curr = self._cfg_subtab_data.get(key)
        if curr:
            curr["button"].configure(fg_color="#1e4060", hover_color="#255478", text_color="#d8e2ef")
            curr["frame"].grid()
        self._active_cfg_subtab = key

    def _switch_stream_subtab(self, key: str) -> None:
        """Switch the active Stream sub-tab (called from side_panel-level buttons)."""
        if key == self._active_stream_subtab:
            return
        prev = self._stream_subtab_data.get(self._active_stream_subtab)
        if prev:
            prev["button"].configure(fg_color="#0f151c", hover_color="#151d26", text_color="#6b7b8d")
            prev["frame"].grid_remove()
        curr = self._stream_subtab_data.get(key)
        if curr:
            curr["button"].configure(fg_color="#1e4060", hover_color="#255478", text_color="#d8e2ef")
            curr["frame"].grid()
        self._active_stream_subtab = key

    def _log_accion(self, msg: str) -> None:
        self._advanced_panel.log_action(msg)

    # ──────────────────────────────────────────────
    # Motor event handler
    # ──────────────────────────────────────────────

    def _safe_after(self, func) -> None:
        """Schedule a UI update on the main thread, safely handling startup race conditions.

        During startup the motor thread may fire events before Tkinter enters
        mainloop().  ``self.after()`` raises RuntimeError in that case.  We
        silently skip — the UI will be in its initial state and subsequent
        events will update it once the loop is running.
        """
        try:
            self.after(0, func)
        except RuntimeError:
            pass

    def _on_motor_event(self, status: str) -> None:
        handlers = {
            "ready": self._on_motor_ready,
            "model_warming": self._on_motor_model_warming,
            "ollama_unavailable": self._on_motor_ollama_unavailable,
            "processing": self._on_motor_processing,
            "idle": self._on_motor_idle,
            "speaking_start": self._on_motor_speaking_start,
            "speaking_end": self._on_motor_speaking_end,
            "model_changed": self._on_motor_model_changed,
            "model_switch_pending": self._on_motor_switch_pending,
            "model_switch_applied": self._on_motor_model_changed,
            "model_switch_failed": self._on_motor_switch_failed,
            "llm_tier_switch_applied": self._on_motor_model_changed,
            "llm_tier_switch_failed": self._on_motor_switch_failed,
            "download_start": self._on_motor_download_start,
            "download_done": self._on_motor_download_done,
            "download_error": self._on_motor_download_error,
        }
        handler = handlers.get(status)
        if handler:
            handler()

    def _on_motor_ready(self) -> None:
        self._ui_state.model_status = "ready"
        self._safe_after(lambda: self.btn_grabar.configure(state="normal"))
        self._safe_after(lambda: self.btn_voz.configure(state="normal"))
        self._safe_after(lambda: self.btn_ws.configure(state="normal"))
        self._safe_after(lambda: self.btn_primary_voice.configure(state="normal"))
        self._safe_after(lambda: self.btn_enviar.configure(state="normal"))
        self._actualizar_pipeline("idle")
        self._safe_after(lambda: self.model_panel.update_model_info(self.model_panel.get_selected_tag()))
        if hasattr(self.motor_ia, "current_model"):
            self._safe_after(lambda: self.model_panel.set_active_model(self.motor_ia.current_model))
            # Sync combobox to actual startup model (may differ from DEFAULT_MODEL)
            model = self.motor_ia.current_model
            self._safe_after(lambda: self.model_panel.restore_to_active_model(model))
            self._safe_after(lambda: self.model_panel.set_llm_tier_state(self.motor_ia.llm_tiers.config.as_dict(), self.motor_ia.active_llm_tier))
            self._safe_after(lambda: self.title(f"OpenCohost — Qwen3-TTS + {model}"))
        # Start PTT flush watcher thread
        if hasattr(self, "voice_panel"):
            self.voice_panel._start_ptt_flush_watcher()

    def _on_motor_model_warming(self) -> None:
        self._ui_state.model_status = "loading"
        self._safe_after(lambda: self.btn_enviar.configure(state="disabled"))
        self._safe_after(lambda: self.btn_download.configure(state="disabled", text="Preparando modelo..."))
        self._actualizar_pipeline("init")

    def _on_motor_ollama_unavailable(self) -> None:
        self._safe_after(lambda: self.btn_grabar.configure(state="disabled"))
        self._safe_after(lambda: self.btn_voz.configure(state="disabled"))
        self._safe_after(lambda: self.btn_ws.configure(state="disabled"))
        self._safe_after(lambda: self.btn_primary_voice.configure(state="disabled"))
        self._safe_after(lambda: self.btn_enviar.configure(state="disabled"))
        self._safe_after(lambda: self.model_panel.refresh_ollama_state(on_check_ollama=lambda: self.motor_ia.command_queue.put(("check_ollama", None))))
        self._actualizar_pipeline("error")
        if hasattr(self, "_avatar_bridge"):
            self._avatar_bridge.set_state(AvatarState.ERROR)

    def _on_motor_processing(self) -> None:
        self._actualizar_pipeline("processing")
        self._safe_after(lambda: self.btn_enviar.configure(state="disabled"))
        self._safe_after(lambda: self.combo_modelos.configure(state="disabled"))
        self._safe_after(lambda: self.btn_download.configure(state="disabled"))
        self._safe_after(lambda: self.btn_mapear.configure(state="disabled"))

    def _on_motor_idle(self) -> None:
        self._actualizar_pipeline("idle")
        self._safe_after(lambda: self.btn_enviar.configure(state="normal"))
        self._safe_after(lambda: self.combo_modelos.configure(state="normal"))
        self._safe_after(lambda: self.model_panel.update_button_for_ollama_state())
        self._safe_after(lambda: self.switch_ptt.configure(state="normal"))
        self._safe_after(lambda: self.btn_mapear.configure(state="normal"))
        self.ptt.ensure_listener(on_press=self._on_ptt_press, on_release=self._on_ptt_release, on_click=self._on_ptt_click)
        if hasattr(self, "kira_agenda") and self.kira_agenda.state not in {AgendaState.OFF, AgendaState.PAUSED_NEEDS_OPERATOR, AgendaState.HARD_PAUSED}:
            self._kira_agenda_schedule_tick(500)

    def _on_motor_speaking_start(self) -> None:
        if hasattr(self, "audio_bed"):
            self.audio_bed.duck()
        # Use controller state, not motor source, to decide if this speech
        # was initiated by the agenda state machine.  The controller may
        # emit chat/PTT/stop actions whose motor source does not start
        # with "kira-agenda"; the state check is the authoritative signal.
        controller_generated = (
            hasattr(self, "kira_agenda")
            and self.kira_agenda.state in {AgendaState.SPEAKING, AgendaState.GENERATING, AgendaState.TOPIC_CLOSING}
        )
        if controller_generated:
            self.kira_agenda.mark_generation_accepted()
            self._kira_agenda_update_status()
            self._kira_agenda_prefetch_while_speaking()
        elif hasattr(self, "kira_agenda"):
            self._kira_agenda_clear_prefetch()
        self._actualizar_pipeline("speaking")
        self._log_accion("Kira comenzó a sintetizar respuesta")
        if hasattr(self, "_avatar_bridge"):
            self._avatar_bridge.set_state(AvatarState.SPEAKING)
            self._start_speaking_alt_timer()

    def _on_motor_speaking_end(self) -> None:
        prefetched_started = False
        # Use controller state, not motor source (see _on_motor_speaking_start).
        agenda_speech = (
            hasattr(self, "kira_agenda")
            and self.kira_agenda.state in {AgendaState.SPEAKING, AgendaState.GENERATING}
        )
        if agenda_speech:
            self.kira_agenda.mark_speech_complete()
            self._kira_agenda_update_status()
            if self._kira_agenda_consume_pending_chat_if_due():
                prefetched_started = True
            else:
                prefetched_started = self._kira_agenda_play_prefetched_if_ready()
            if not prefetched_started and self.kira_agenda.state not in {AgendaState.OFF, AgendaState.PAUSED_NEEDS_OPERATOR}:
                self._kira_agenda_schedule_tick(1200)
        elif hasattr(self, "kira_agenda"):
            self._kira_agenda_clear_prefetch()
        self._stop_speaking_alt_timer()
        if hasattr(self, "audio_bed"):
            self.audio_bed.unduck()
            if agenda_speech or self.audio_bed.current_track is not None:
                self.audio_bed.on_boundary()
        estado = "listening" if self.voice_panel.is_ws_connected() else "idle"
        self._actualizar_pipeline(estado)
        if hasattr(self, "_avatar_bridge"):
            self._avatar_bridge.set_state(
                AvatarState.LISTENING if estado == "listening" else AvatarState.IDLE
            )
        self._safe_after(lambda: self.switch_ptt.configure(state="normal"))
        self.ptt.ensure_listener(on_press=self._on_ptt_press, on_release=self._on_ptt_release, on_click=self._on_ptt_click)

    def _start_speaking_alt_timer(self) -> None:
        """Start a timer that alternates between SPEAKING and SPEAKING_ALT."""
        self._speaking_is_alt = False
        self._tick_speaking_alt()

    def _stop_speaking_alt_timer(self) -> None:
        """Stop the speaking alternation timer."""
        if self._speaking_alt_timer_id is not None:
            self.after_cancel(self._speaking_alt_timer_id)
            self._speaking_alt_timer_id = None

    def _tick_speaking_alt(self) -> None:
        """Toggle between SPEAKING and SPEAKING_ALT and reschedule."""
        self._speaking_is_alt = not self._speaking_is_alt
        if hasattr(self, "_avatar_bridge"):
            self._avatar_bridge.set_state(
                AvatarState.SPEAKING_ALT if self._speaking_is_alt else AvatarState.SPEAKING
            )
        # Alternate every 700ms — fast enough to look natural, slow enough to see both images
        self._speaking_alt_timer_id = self.after(700, self._tick_speaking_alt)

    def _reset_inactivity_timer(self) -> None:
        """Reset the inactivity timer. If Kira is idle for too long, she goes to sleep."""
        self._stop_inactivity_timer()
        self._inactivity_timer_id = self.after(self._inactivity_timeout_ms, self._on_inactivity_timeout)

    def _stop_inactivity_timer(self) -> None:
        """Stop the inactivity timer."""
        if self._inactivity_timer_id is not None:
            self.after_cancel(self._inactivity_timer_id)
            self._inactivity_timer_id = None

    def _on_inactivity_timeout(self) -> None:
        """Kira has been idle for too long — go to sleep."""
        self._inactivity_timer_id = None
        if hasattr(self, "_avatar_bridge"):
            current = self._avatar_bridge.get_state()
            if current in (AvatarState.IDLE,):
                self._avatar_bridge.set_state(AvatarState.SLEEPING)
                self._log_accion("Kira se durmió por inactividad")

    def _on_joyita_to_obs(self, text: str) -> None:
        """Send the joyita message to an OBS Text source named 'KiraJoyita'.

        Clears it after 120 seconds unless a new joyita arrives sooner.
        """
        if not text:
            return
        obs = getattr(self, "_obs_client", None)
        if not obs or not obs.is_connected:
            return
        source_name = "KiraJoyita"
        if obs.set_obs_text(source_name, text):
            if self._joyita_obs_timer_id is not None:
                try:
                    self.after_cancel(self._joyita_obs_timer_id)
                except Exception:
                    pass
            self._joyita_obs_timer_id = self.after(120_000, lambda: self._clear_obs_joyita(source_name))

    def _clear_obs_joyita(self, source_name: str) -> None:
        self._joyita_obs_timer_id = None
        obs = getattr(self, "_obs_client", None)
        if obs and obs.is_connected:
            obs.set_obs_text(source_name, "")

    def _init_obs_client(self) -> None:
        """Initialize OBS WebSocket client if enabled in config."""
        self._obs_start_from_config()

    def _obs_connect_now(self) -> None:
        """Start or refresh the live OBS runtime connection from current config."""
        self._obs_start_from_config()

    def _obs_start_from_config(self, retry_delay: float = 5) -> bool:
        """Create/refresh OBS runtime client and start one cancellable retry loop."""
        from avatar.avatar_config import load_avatar_config
        avatar_cfg = load_avatar_config()

        if not avatar_cfg.obs.enabled:
            self._obs_stop_runtime()
            return False

        existing_thread = getattr(self, "_obs_retry_thread", None)
        if (
            getattr(self, "_obs_client", None) is not None
            and existing_thread is not None
            and existing_thread.is_alive()
        ):
            return True

        existing_client = getattr(self, "_obs_client", None)
        if existing_client is not None and getattr(existing_client, "is_connected", False):
            try:
                self._avatar_panel.set_obs_client(existing_client)
            except Exception:
                pass
            return True

        try:
            self._obs_client = OBSClient(
                config=OBSConfig(
                    enabled=avatar_cfg.obs.enabled,
                    host=avatar_cfg.obs.host,
                    port=avatar_cfg.obs.port,
                    password=avatar_cfg.obs.password,
                    source_name=avatar_cfg.obs.source_name,
                    scene_name=avatar_cfg.obs.scene_name,
                ),
                assets_folder=avatar_cfg.assets_folder,
                state_images=avatar_cfg.state_images,
                on_log=lambda msg: self._print_log(msg),
            )
            self._obs_retry_cancel = threading.Event()
            self._obs_retry_thread = threading.Thread(
                target=self._connect_obs_loop,
                args=(self._obs_retry_cancel, self._obs_client, retry_delay),
                daemon=True,
            )
            self._obs_retry_thread.start()
            return True
        except Exception as e:
            self._print_log(f"[OBS] Failed to initialize: {e}")
            return False

    def _obs_stop_runtime(self) -> None:
        """Cancel OBS retry loop and disconnect the live runtime client."""
        cancel_event = getattr(self, "_obs_retry_cancel", None)
        if cancel_event is not None:
            cancel_event.set()
        obs_client = getattr(self, "_obs_client", None)
        if obs_client is not None:
            try:
                obs_client.disconnect()
            except Exception:
                logger.exception("Fallo al desconectar OBS")
        self._obs_client = None
        try:
            self._avatar_panel.set_obs_client(None)
        except Exception:
            pass

    def _connect_obs_loop(
        self,
        cancel_event: threading.Event | None = None,
        obs_client: OBSClient | None = None,
        retry_delay: float = 5,
    ) -> None:
        """Retry OBS connection without letting unexpected socket errors kill the thread."""
        logged_once = False
        managed_loop = cancel_event is not None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                break
            try:
                active_client = obs_client if obs_client is not None else self._obs_client
                if active_client is None or active_client is not getattr(self, "_obs_client", None):
                    break
                if active_client.connect(log_failures=not logged_once):
                    if cancel_event is not None and cancel_event.is_set():
                        break
                    if active_client is not getattr(self, "_obs_client", None):
                        break
                    active_client.subscribe_bridge(self._avatar_bridge)
                    active_client.on_state_change(self._avatar_bridge.get_state())
                    try:
                        if self.winfo_exists():
                            self.after(0, lambda: self._avatar_panel.set_obs_client(active_client))
                    except Exception:
                        pass
                    return
                if not logged_once:
                    self._print_log(
                        "[OBS] No se pudo conectar. OpenCohost reintentara cada 5s. "
                        "Abrí OBS y la conexión se restablecerá automáticamente."
                    )
                    logged_once = True
            except Exception:
                logger.exception("Fallo inesperado en loop de OBS")
                if not logged_once:
                    self._print_log(
                        "[OBS] Error inesperado conectando. OpenCohost seguira reintentando cada 5s."
                    )
                    logged_once = True
            if managed_loop:
                if cancel_event is not None and cancel_event.wait(retry_delay):
                    break
            else:
                time.sleep(retry_delay)
        # Client was destroyed (app closing)

    def _on_motor_model_changed(self) -> None:
        model = self.motor_ia.current_model
        self._safe_after(lambda: self.title(f"OpenCohost — Qwen3-TTS + {model}"))
        self._safe_after(lambda: self.model_panel.update_model_info(model))
        self._safe_after(lambda: self.model_panel.set_active_model(model))
        self._safe_after(lambda: self.model_panel.set_llm_tier_state(self.motor_ia.llm_tiers.config.as_dict(), self.motor_ia.active_llm_tier))
        self._actualizar_pipeline("idle")

    def _on_motor_switch_pending(self) -> None:
        desired = getattr(self.motor_ia, "_desired_model", None)
        if desired:
            display = self.model_panel.get_display_for_tag(desired)
            if not getattr(self.motor_ia, "is_ready", False):
                self._print_log(f"[Sistema] Cambio a {display} pendiente: Ollama no está listo.")
            else:
                self._print_log(f"[Sistema] Cambio a {display} pendiente: se aplicará al terminar la respuesta actual.")
        # Do NOT restore combobox — user's selection stays visible as intended target

    def _on_motor_switch_failed(self) -> None:
        self._ui_state.model_status = "error"
        failure = getattr(self.motor_ia, "_last_switch_failure", None)
        actual_model = self.motor_ia.current_model
        if failure:
            requested = failure.get("requested", "?")
            reason = failure.get("reason", "unknown")
            self._print_log(f"[Sistema] ❌ Cambio a {requested} fallido: {reason}. Modelo activo: {actual_model}")
            # Clear after read to prevent stale display
            self.motor_ia._last_switch_failure = None
        # Restore combobox to actual motor model
        self._safe_after(lambda: self.model_panel.restore_to_active_model(actual_model))
        self._safe_after(lambda: self.model_panel.set_llm_tier_state(self.motor_ia.llm_tiers.config.as_dict(), self.motor_ia.active_llm_tier))
        self._safe_after(lambda: self.model_panel.update_model_info(actual_model))

    def _on_motor_download_start(self) -> None:
        self._safe_after(lambda: self.btn_download.configure(state="disabled", text="Descargando..."))
        self._safe_after(lambda: self.combo_modelos.configure(state="disabled"))
        self._safe_after(lambda: self.progress_download.pack(fill="x", padx=10, pady=(4, 10)))
        self._safe_after(lambda: self.progress_download.set(0))
        self._safe_after(lambda: self.btn_primary_voice.configure(state="disabled"))
        self._actualizar_pipeline("downloading")

    def _on_motor_download_done(self) -> None:
        model = self.motor_ia.current_model
        self._safe_after(lambda: self.model_panel.update_button_for_ollama_state())
        self._safe_after(lambda: self.combo_modelos.configure(state="normal"))
        self._safe_after(lambda: self.progress_download.pack_forget())
        self._safe_after(lambda: self.btn_primary_voice.configure(state="normal"))
        self._safe_after(lambda: self.title(f"OpenCohost — Qwen3-TTS + {model}"))
        self._safe_after(lambda: self.model_panel.update_model_info(model))
        self._actualizar_pipeline("idle")
        self._safe_after(lambda: self.model_panel.update_model_info(self.model_panel.get_selected_tag()))

    def _on_motor_download_error(self) -> None:
        self._safe_after(lambda: self.model_panel.update_button_for_ollama_state())
        self._safe_after(lambda: self.combo_modelos.configure(state="normal"))
        self._safe_after(lambda: self.progress_download.pack_forget())
        self._safe_after(lambda: self.btn_primary_voice.configure(state="normal"))
        self._actualizar_pipeline("error")
        if hasattr(self, "_avatar_bridge"):
            self._avatar_bridge.set_state(AvatarState.ERROR)

    # ──────────────────────────────────────────────
    # Audio, TTS, Chat, PTT helpers
    # ──────────────────────────────────────────────

    def _al_cambiar_motor_tts(self) -> None:
        motor_seleccionado = self.switch_modo_ligero.get()
        self.motor_ia.command_queue.put(("set_motor_tts", motor_seleccionado))
        modo_texto = "Ligero" if motor_seleccionado == "ligero" else "Pesado"
        self.switch_modo_ligero.configure(text=f"🎛️ TTS: {modo_texto}")
        if self.status_bar:
            self.status_bar.update_tts_status("idle")
        if hasattr(self, "lbl_kira_tts_state"):
            self.lbl_kira_tts_state.configure(text="TTS: idle", fg_color="#1b2633")

    def _obtener_dispositivos_entrada(self) -> list:
        dispositivos_validos = []
        try:
            dispositivos = sd.query_devices()
            for i, d in enumerate(dispositivos):
                if d["max_input_channels"] > 0:
                    dispositivos_validos.append(f"{i}: {d['name']}")
        except Exception as e:
            logger.error(f"No se pudieron listar dispositivos de audio: {e}")
        return dispositivos_validos

    def _al_seleccionar_dispositivo(self, seleccion: str) -> None:
        try:
            self.dispositivo_seleccionado = int(seleccion.split(":")[0])
            if self.status_bar:
                self.status_bar.update_mic_status("idle")
            if hasattr(self, "lbl_kira_voice_state"):
                self.lbl_kira_voice_state.configure(text="Voz/PTT: listo", fg_color="#1b2633")
            self._print_log(f"[Sistema] Fuente de audio: ID {self.dispositivo_seleccionado}")
        except (ValueError, IndexError):
            self.dispositivo_seleccionado = None
            if self.status_bar:
                self.status_bar.update_mic_status("disconnected")
            if hasattr(self, "lbl_kira_voice_state"):
                self.lbl_kira_voice_state.configure(text="Voz/PTT: sin mic", fg_color="#4a2630")

    def _iniciar_grabacion(self) -> None:
        if self.dispositivo_seleccionado is None:
            self._notify_operator("Atención", "Selecciona una fuente de audio primero.")
            return
        dialog = ctk.CTkInputDialog(text=f"Grabarás {RECORDING_DURATION} segundos de audio para calibrar la voz.\nHabla con tu tono natural.\n\nPresiona OK para empezar.", title="Confirmar Grabación")
        res = dialog.get_input()
        if res is not None:
            threading.Thread(target=self._hilo_grabacion, daemon=True).start()
        else:
            self._print_log("[Grabación] Acción cancelada.")

    def _hilo_grabacion(self) -> None:
        filepath = os.path.join(BASE_DIR, "referencia_grabada.wav")
        self._print_log(f"\n[Grabación] 🔴 GRABANDO {RECORDING_DURATION}s... Habla ahora.")
        self.after(0, lambda: self.btn_grabar.configure(state="disabled", text="Grabando...", fg_color="darkred"))
        try:
            recording = sd.rec(int(RECORDING_DURATION * RECORDING_SAMPLERATE), samplerate=RECORDING_SAMPLERATE, channels=1, dtype="float32", device=self.dispositivo_seleccionado)
            sd.wait()
            rms = np.sqrt(np.mean(recording ** 2))
            if rms < MIN_AUDIO_RMS:
                self._print_log("[Grabación] ⚠️ Audio demasiado bajo o silencio. Intenta de nuevo.")
                logger.warning(f"Grabación descartada por RMS bajo: {rms:.6f}")
                return
            sf.write(filepath, recording, RECORDING_SAMPLERATE)
            self._print_log(f"[Grabación] ⏹️ Audio capturado (RMS: {rms:.4f})")
            logger.info(f"Grabación guardada: {filepath}, RMS={rms:.4f}")
            response = {"use": False}
            response_ready = threading.Event()
            def ask_use_audio():
                response["use"] = messagebox.askyesno("Grabación Finalizada", "Audio capturado correctamente.\n\n¿Usar como voz de referencia para la IA?")
                response_ready.set()
            self.after(0, ask_use_audio)
            if not response_ready.wait(timeout=120):
                self._print_log("[Grabación] Confirmación agotó tiempo; audio descartado por seguridad.")
            if response["use"]:
                self._print_log("[Sistema] Perfil de voz enviado a la IA.")
                self.motor_ia.command_queue.put(("set_voice", filepath))
                self.after(0, lambda: self.btn_ws.configure(state="normal", fg_color="#555555"))
                self.after(0, lambda: self.btn_enviar.configure(state="normal"))
            else:
                self._print_log("[Grabación] ❌ Descartada.")
                if os.path.exists(filepath):
                    os.remove(filepath)
        except Exception as e:
            self._print_log(f"[ERROR Grabación]: {e}")
            logger.exception("Error durante grabación")
        finally:
            self.after(0, lambda: self.btn_grabar.configure(state="normal", text="🎤 Grabar", fg_color="#555555"))

    def _cargar_voz(self) -> None:
        filepath = filedialog.askopenfilename(title="Seleccionar muestra de voz", filetypes=[("Audio WAV", "*.wav")])
        if filepath:
            self.btn_voz.configure(text="Cargando WAV...", fg_color="#cc8800")
            self.update_idletasks()
            try:
                data, sr = sf.read(filepath)
                duration = len(data) / sr
                if duration < 2.0:
                    self._notify_operator("Audio muy corto", "El audio debe durar al menos 2 segundos.")
                    self.btn_voz.configure(text="📂 Cargar WAV", fg_color="#555555")
                    return
                if duration > 30.0:
                    self._notify_operator("Audio muy largo", "El audio no debe durar más de 30 segundos.")
                    self.btn_voz.configure(text="📂 Cargar WAV", fg_color="#555555")
                    return
            except Exception as e:
                self._notify_operator("Error", f"No se pudo leer el archivo de audio:\n{e}", level="error")
                self.btn_voz.configure(text="📂 Cargar WAV", fg_color="#555555")
                return
            self.motor_ia.command_queue.put(("set_voice", filepath))
            self.btn_ws.configure(state="normal", fg_color="#555555")
            self.btn_enviar.configure(state="normal")
            self.btn_voz.configure(text="WAV Cargado ✅", fg_color="#1f5a3a")
            self._print_log(f"[Sistema] Perfil de voz cargado ({duration:.1f}s).")
            self.after(2000, lambda: self.btn_voz.configure(text="📂 Cargar WAV", fg_color="#555555"))

    def _enviar_contexto_manual(self) -> None:
        texto = self.entry_chat.get().strip()
        if texto:
            self._print_log(f"\n[Tú]: {texto}")
            self.motor_ia.command_queue.put(("process_context", texto))
            self.entry_chat.delete(0, "end")

    def _limpiar_historial(self) -> None:
        if hasattr(self, "lbl_memory_status_pill"):
            self.lbl_memory_status_pill.configure(text="Memoria: limpiando", fg_color="#5f461b")
        if hasattr(self, "lbl_kira_memory_state"):
            self.lbl_kira_memory_state.configure(text="Memoria: limpiando", fg_color="#5f461b")
        self.motor_ia.command_queue.put(("clear_history", None))
        self._print_log("[Sistema] 🗑️ Memoria de conversación limpiada.")
        self.after(800, lambda: self.lbl_memory_status_pill.configure(text="Memoria: disponible", fg_color="#1b2633") if hasattr(self, "lbl_memory_status_pill") else None)
        self.after(800, lambda: self.lbl_kira_memory_state.configure(text="Memoria: disponible", fg_color="#1b2633") if hasattr(self, "lbl_kira_memory_state") else None)

    def _toggle_websocket(self) -> None:
        if hasattr(self, "voice_panel"):
            self.voice_panel._toggle_websocket()

    # ──────────────────────────────────────────────
    # PTT
    # ──────────────────────────────────────────────

    def _on_ptt_status_change(self, text: str, color: str) -> None:
        def update():
            self.lbl_ptt_status.configure(text=text, text_color=color)
            if self.status_bar and text:
                if "ESCUCHANDO" in text:
                    self.status_bar.update_mic_status("listening")
                    if hasattr(self, "_avatar_bridge"):
                        self._avatar_bridge.set_state(AvatarState.LISTENING)
                else:
                    self.status_bar.update_mic_status("idle")
            elif self.status_bar:
                self.status_bar.update_mic_status("idle")
        self.after(0, update)

    def _al_toggle_ptt(self) -> None:
        nuevo_estado = self.switch_ptt.get()
        if nuevo_estado == self.ptt.enabled:
            return
        self.ptt.enabled = nuevo_estado
        self._ui_state.ptt_enabled = nuevo_estado
        if self.ptt.enabled:
            self.switch_ptt.configure(text="PTT ON")
            if not self.ptt.mapping:
                self.ptt.start_listener(on_press=self._on_ptt_press, on_release=self._on_ptt_release, on_click=self._on_ptt_click)
                self._on_ptt_status_change(f"Manten presionado [{self.ptt.hotkey}] para hablar", "#888888")
            self._print_log(f"[PTT] Activado — hotkey: {self.ptt.hotkey}")
        else:
            self.switch_ptt.configure(text="PTT OFF")
            self.ptt.stop_listener()
            self.ptt.stop_mapping()
            self._on_ptt_status_change("", "#888888")
            self._print_log("[PTT] Desactivado — modo continuo WebSocket")

    def _mapear_hotkey(self) -> None:
        if self.ptt.mapping:
            return
        self.btn_mapear.configure(text="Escuchando...", state="disabled", fg_color="#cc8800")
        self._on_ptt_status_change("Presiona la tecla o boton deseado...", "#cc8800")
        self.ptt.start_mapping(on_key=self._on_mapear_key, on_mouse=self._on_mapear_mouse)

    def _on_mapear_key(self, key) -> bool:
        display = self.ptt.get_reverse_mapping().get(key)
        if display:
            self._save_mapped_hotkey(display)
            return False

    def _on_mapear_mouse(self, x, y, button, pressed) -> bool:
        if pressed:
            display = self.ptt.get_reverse_mapping().get(button)
            if display:
                self._save_mapped_hotkey(display)
                return False

    def _save_mapped_hotkey(self, hotkey: str) -> None:
        self.ptt.hotkey = hotkey
        self.ptt.save_config(hotkey)
        self.after(0, lambda: self.lbl_hotkey.configure(text=hotkey))
        self.after(0, lambda: self.btn_mapear.configure(text="Mapear", state="normal", fg_color="#555555"))
        self._print_log(f"[PTT] Tecla mapeada y guardada: {hotkey}")
        self.ptt.stop_mapping()
        if self.ptt.enabled:
            self.ptt.start_listener(on_press=self._on_ptt_press, on_release=self._on_ptt_release, on_click=self._on_ptt_click)
            self._on_ptt_status_change(f"Manten presionado [{hotkey}] para hablar", "#888888")

    def _on_ptt_press(self, key) -> None:
        was_active = self.ptt.active
        self.ptt.on_ptt_press(key)
        if was_active or not self.ptt.active:
            return
        self._ui_state.ptt_active = True
        self._ptt_accept_logged = False
        # Clear buffer for new accumulation cycle
        if hasattr(self, "voice_panel"):
            self.voice_panel.clear_ptt_buffer()

    def _on_ptt_release(self, key) -> None:
        was_active = self.ptt.active
        self.ptt.on_ptt_release(key)
        if not was_active or self.ptt.active:
            return
        self._ui_state.ptt_active = False
        self._ptt_accept_logged = False
        # Start grace period immediately (don't wait for WS message)
        if hasattr(self, "voice_panel"):
            self.voice_panel.on_ptt_release()

    def _on_ptt_click(self, x, y, button, pressed) -> None:
        was_active = self.ptt.active
        self.ptt.on_ptt_click(x, y, button, pressed)
        is_active = self.ptt.active
        if was_active == is_active:
            return
        self._ui_state.ptt_active = is_active
        self._ptt_accept_logged = False
        if is_active and hasattr(self, "voice_panel"):
            self.voice_panel.clear_ptt_buffer()
        elif hasattr(self, "voice_panel"):
            self.voice_panel.on_ptt_release()

    # ──────────────────────────────────────────────
    # Logging
    # ──────────────────────────────────────────────

    def _notify_operator(self, title: str, message: str, level: str = "warning") -> None:
        level_name = (level or "warning").lower()
        log_method = getattr(logger, level_name, logger.warning)
        if not callable(log_method):
            log_method = logger.warning
        log_method("%s: %s", title, message)

        text = f"[{level_name.upper()}] {title}: {message}"
        printed = False
        printer = getattr(self, "_print_log", None)
        if callable(printer):
            try:
                printer(text)
                printed = True
            except Exception:
                logger.debug("No se pudo imprimir notificacion de operador", exc_info=True)

        if not printed:
            log_queue = getattr(self, "log_queue", None)
            if log_queue is not None:
                log_queue.put(text)

    def _print_log(self, msg: str) -> None:
        self._advanced_panel.print_log(msg)

    def _process_logs(self) -> None:
        self._advanced_panel.process_logs()
        self.after(100, self._process_logs)

    def _aplicar_perfil_actual(self) -> None:
        if hasattr(self, "profile_panel"):
            self.profile_panel.apply_current_profile()

    # ──────────────────────────────────────────────
    # Cleanup
    # ──────────────────────────────────────────────

    def on_closing(self) -> None:
        self._closing = True
        logger.info("Cerrando aplicación...")

        # Fix: audit/ui-security-perf-2026-05-17 — unsubscribe UIState observer to
        # release GC reference. Matches pattern used by all panel cleanup methods.
        if hasattr(self, "_ui_state_sub_id"):
            self._ui_state.unsubscribe(self._ui_state_sub_id)

        if hasattr(self, "voice_panel"):
            self.voice_panel.cleanup()
        if self.status_bar:
            self.status_bar.cleanup()
        if hasattr(self, "_advanced_panel"):
            self._advanced_panel.cleanup()
        if hasattr(self, "smart_agg_ui"):
            self.smart_agg_ui.cleanup()
        if hasattr(self, "stream_admin_ui"):
            self.stream_admin_ui.cleanup()
        if hasattr(self, "_avatar_panel"):
            self._avatar_panel.cleanup()

        self._stop_speaking_alt_timer()
        self._stop_inactivity_timer()

        # Disconnect OBS WebSocket and cancel any pending retry loop.
        self._obs_stop_runtime()

        self.ptt.stop_listener()

        # Stop health monitor daemon
        if self.health_monitor:
            try:
                self.health_monitor.stop()
            except Exception as e:
                logger.warning(f"HealthMonitor cleanup error: {e}")

        # Set avatar to sleeping on close
        if hasattr(self, "_avatar_bridge"):
            self._avatar_bridge.set_state(AvatarState.SLEEPING)

        try:
            if self.smart_agg:
                self.smart_agg.disconnect()
        except Exception as e:
            logger.warning(f"No se pudo desconectar Smart Aggregator: {e}")

        try:
            if self.stream_admin_ui.chat_connected:
                self._stream_admin_disconnect_api_chat()
        except Exception as e:
            logger.warning(f"No se pudo desconectar chat autenticado RF4: {e}")

        try:
            x = self.winfo_x()
            y = self.winfo_y()
            w = self.winfo_width()
            h = self.winfo_height()
            _guardar_geometria(x, y, w, h)
        except Exception:
            pass

        try:
            release_model = getattr(self.motor_ia, "release_owned_ollama_model", None)
            if callable(release_model):
                release_model(timeout=2.0)
        except Exception as e:
            logger.warning(f"No se pudo liberar memoria Ollama al salir: {e}")

        self.motor_ia.command_queue.put(None)

        try:
            cleanup_voiceai_temp_artifacts(TEMP_DIR, logger, min_age_seconds=0.0)
        except Exception as e:
            logger.warning(f"No se pudo limpiar temporales de la app al salir: {e}")

        self.destroy()
