import os
import queue
import threading
import json
import asyncio
import time
import urllib.parse
import numpy as np
import websockets
import soundfile as sf
import sounddevice as sd
import customtkinter as ctk
import tkinter.messagebox as messagebox
from tkinter import filedialog

from pynput import keyboard, mouse

from config.settings import (
    DEFAULT_MODEL, MODELS_CATALOG, BASE_DIR, TEMP_DIR,
    WS_URI, WS_TIMEOUT, WS_RECONNECT_BASE_DELAY,
    WS_RECONNECT_MAX_DELAY, WS_MAX_RETRIES,
    RECORDING_DURATION, RECORDING_SAMPLERATE, MIN_AUDIO_RMS,
    PTT_DEFAULT_HOTKEY, PTT_HOTKEY_LIST, PTT_CONFIG_FILE,
    WINDOW_GEOMETRY_FILE, ACCIONES_LOG_FILE
)
from config.logger import get_logger
from core.profiles import cargar_perfiles, guardar_perfiles
from core.llm_engine import MotorVocalIA
from smart_aggregator import Aggregator
from stream_admin import AdminManager
from ui.profiles_window import ConfiguradorPerfiles

# ── Mapeo de teclas PTT (display → pynput) ──
_PTT_KB_MAP = {f"F{i}": getattr(keyboard.Key, f"f{i}") for i in range(1, 13)}
_PTT_KB_MAP.update({
    "ScrollLock": keyboard.Key.scroll_lock,
    "Insert": keyboard.Key.insert,
    "Pause": keyboard.Key.pause,
})
_PTT_MOUSE_MAP = {
    "Mouse4": mouse.Button.x2,
    "Mouse5": mouse.Button.x1,
}

# Mapa inverso: pynput key → display name
_PTT_REVERSE_MAP = {}
_PTT_REVERSE_MAP.update({v: k for k, v in _PTT_KB_MAP.items()})
_PTT_REVERSE_MAP.update({v: k for k, v in _PTT_MOUSE_MAP.items()})

def _cargar_ptt_config():
    try:
        if os.path.exists(PTT_CONFIG_FILE):
            with open(PTT_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                hotkey = data.get("hotkey", PTT_DEFAULT_HOTKEY)
                if hotkey in PTT_HOTKEY_LIST:
                    return hotkey
                else:
                    logger.warning(f"[PTT] Hotkey en archivo no valida: '{hotkey}', usando default")
    except Exception as e:
        logger.warning(f"[PTT] Error cargando config PTT: {e}")
    return PTT_DEFAULT_HOTKEY

def _guardar_ptt_config(hotkey):
    try:
        os.makedirs(os.path.dirname(PTT_CONFIG_FILE), exist_ok=True)
        with open(PTT_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump({"hotkey": hotkey}, f, indent=2)
    except Exception as e:
        logger.warning(f"No se pudo guardar config PTT: {e}")

def _cargar_geometria():
    try:
        if os.path.exists(WINDOW_GEOMETRY_FILE):
            with open(WINDOW_GEOMETRY_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return None

def _guardar_geometria(x, y, w, h):
    try:
        os.makedirs(os.path.dirname(WINDOW_GEOMETRY_FILE), exist_ok=True)
        with open(WINDOW_GEOMETRY_FILE, "w") as f:
            json.dump({"x": x, "y": y, "width": w, "height": h}, f)
    except Exception:
        pass

def _guardar_accion(msg):
    try:
        os.makedirs(os.path.dirname(ACCIONES_LOG_FILE), exist_ok=True)
        with open(ACCIONES_LOG_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), "msg": msg}, ensure_ascii=False) + "\n")
    except Exception:
        pass

def _cargar_acciones(limite=50):
    acciones = []
    try:
        if os.path.exists(ACCIONES_LOG_FILE):
            with open(ACCIONES_LOG_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    try:
                        entry = json.loads(line)
                        acciones.append(
                            f"[{time.strftime('%H:%M:%S', time.localtime(entry['ts']))}] {entry['msg']}"
                        )
                    except Exception:
                        continue
            return acciones[-limite:]
    except Exception:
        pass
    return []

logger = get_logger()

class VocalAIApp(ctk.CTk):
    """
    Interfaz principal del Cliente VoiceAI.
    """
    def __init__(self):
        super().__init__()
        self.title(f"VocalAI — Qwen3-TTS + {DEFAULT_MODEL}")

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
        self.ws_connected = False
        self.ws_should_reconnect = False
        self.dispositivo_seleccionado = None
        self._ptt_lock = threading.RLock()
        self._pipeline_state = "idle"

        self.ptt_enabled = False
        self.ptt_hotkey = _cargar_ptt_config()
        self.ptt_pressed = False
        self.ptt_listener = None
        self._ptt_target = None
        self._ptt_mapping = False
        self._ptt_mapping_listeners = []

        self._ptt_buffer = []
        self._ptt_buffer_lock = threading.Lock()
        self.smart_agg = None
        self.smart_agg_connected = False
        self.smart_agg_connecting = False
        self._smart_agg_manual_disconnect = False
        self.stream_admin = None
        self._stream_admin_last_metadata = {}
        self.stream_admin_chat_connected = False
        self._stream_admin_chat_stop = None
        self._stream_admin_chat_thread = None
        self._stream_admin_seen_chat_ids = set()
        self._stream_admin_sim_round = 0
        self._stream_admin_chat_users = {}
        self._smart_agg_default_activity = None
        
        self.perfiles = cargar_perfiles()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self.lista_dispositivos = self._obtener_dispositivos_entrada()
        self._build_ui()

        self.motor_ia = MotorVocalIA(self.log_queue, self._on_motor_event)
        self.motor_ia.start()
        self._init_smart_aggregator()
        self._init_stream_admin()

        self.after(100, self._process_logs)
        self.after(500, self._aplicar_perfil_actual)
        self._print_log(f"[Sistema] PTT hotkey cargada: {self.ptt_hotkey}")
        logger.info("Aplicación VoiceAI iniciada.")

    def _aplicar_perfil_actual(self):
        nombre = self.combo_perfiles.get()
        if nombre in self.perfiles:
            self.motor_ia.command_queue.put(("set_profile", self.perfiles[nombre]))

    def _build_ui(self):
        self.configure(fg_color="#0b0f14")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=0)
        self.grid_rowconfigure(1, weight=1)
        self.grid_rowconfigure(2, weight=0)
        self.grid_rowconfigure(3, weight=0, minsize=0)
        self.grid_rowconfigure(4, weight=0, minsize=0)
        self.grid_rowconfigure(5, weight=0, minsize=0)

        status_bar = ctk.CTkFrame(self, fg_color="#111820", corner_radius=14)
        status_bar.grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 8))

        self.lbl_status = ctk.CTkLabel(
            status_bar,
            text="Modelo cargando",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color="#ffaa00"
        )
        self.lbl_status.pack(side="left", padx=(12, 8), pady=10)

        self.lbl_mic_status_pill = ctk.CTkLabel(status_bar, text="Mic: revisando", fg_color="#1b2633", corner_radius=12)
        self.lbl_mic_status_pill.pack(side="left", padx=4, pady=8)
        self.lbl_tts_status_pill = ctk.CTkLabel(status_bar, text="TTS: idle", fg_color="#1b2633", corner_radius=12)
        self.lbl_tts_status_pill.pack(side="left", padx=4, pady=8)
        self.lbl_chat_status_pill = ctk.CTkLabel(status_bar, text="Chat: desconectado", fg_color="#1b2633", corner_radius=12)
        self.lbl_chat_status_pill.pack(side="left", padx=4, pady=8)
        self.lbl_oauth_status_pill = ctk.CTkLabel(status_bar, text="OAuth: desconectado", fg_color="#1b2633", corner_radius=12)
        self.lbl_oauth_status_pill.pack(side="left", padx=4, pady=8)
        self.lbl_memory_status_pill = ctk.CTkLabel(status_bar, text="Memoria: disponible", fg_color="#1b2633", corner_radius=12)
        self.lbl_memory_status_pill.pack(side="left", padx=4, pady=8)
        self.lbl_moderation_status_pill = ctk.CTkLabel(status_bar, text="Moderación: sin pendientes", fg_color="#1b2633", corner_radius=12)
        self.lbl_moderation_status_pill.pack(side="left", padx=4, pady=8)

        self.switch_advanced = ctk.CTkSwitch(
            status_bar,
            text="Mostrar logs",
            command=self._toggle_logs_panel,
            onvalue=True,
            offvalue=False
        )
        self.switch_advanced.pack(side="right", padx=(8, 12), pady=8)

        self.switch_compacto = ctk.CTkSwitch(
            status_bar,
            text="Compacto",
            command=self._toggle_modo_compacto,
            onvalue=True,
            offvalue=False
        )
        self.switch_compacto.pack(side="right", padx=8, pady=8)

        app_shell = ctk.CTkFrame(self, fg_color="transparent")
        app_shell.grid(row=1, column=0, sticky="nsew", padx=12, pady=(0, 8))
        app_shell.grid_columnconfigure(0, weight=1)
        app_shell.grid_columnconfigure(1, weight=0)
        app_shell.grid_rowconfigure(0, weight=1)

        main_panel = ctk.CTkFrame(app_shell, fg_color="#10161d", corner_radius=18)
        main_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8), pady=0)
        main_panel.grid_columnconfigure(0, weight=0)
        main_panel.grid_columnconfigure(1, weight=1)
        main_panel.grid_rowconfigure(0, weight=1)

        main_nav = ctk.CTkFrame(main_panel, width=140, fg_color="#0c1117", corner_radius=14)
        main_nav.grid(row=0, column=0, sticky="ns", padx=(10, 6), pady=10)
        main_nav.grid_propagate(False)
        ctk.CTkLabel(main_nav, text="Vista", font=ctk.CTkFont(size=12, weight="bold"), text_color="#8fa3b8").pack(fill="x", padx=10, pady=(12, 6))

        main_content = ctk.CTkFrame(main_panel, fg_color="transparent")
        main_content.grid(row=0, column=1, sticky="nsew", padx=(0, 10), pady=10)
        main_content.grid_columnconfigure(0, weight=1)
        main_content.grid_rowconfigure(0, weight=1)

        tab_main_kira = ctk.CTkFrame(main_content, fg_color="transparent")
        tab_main_stream_admin = ctk.CTkFrame(main_content, fg_color="transparent")
        for frame in (tab_main_kira, tab_main_stream_admin):
            frame.grid(row=0, column=0, sticky="nsew")

        self._main_view_buttons = {}
        self._main_view_frames = {
            "Kira": tab_main_kira,
            "Stream Admin": tab_main_stream_admin,
        }
        for name in self._main_view_frames:
            btn = ctk.CTkButton(
                main_nav,
                text=name,
                command=lambda view=name: self._show_main_view(view),
                fg_color="#151d26",
                hover_color="#1d2a38",
                anchor="w"
            )
            btn.pack(fill="x", padx=8, pady=4)
            self._main_view_buttons[name] = btn

        tab_main_kira.grid_columnconfigure(0, weight=1)
        tab_main_kira.grid_rowconfigure(1, weight=1)
        tab_main_stream_admin.grid_columnconfigure(0, weight=1)
        tab_main_stream_admin.grid_rowconfigure(0, weight=1)

        kira_header = ctk.CTkFrame(tab_main_kira, fg_color="transparent")
        kira_header.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 8))
        kira_header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            kira_header,
            text="Kira",
            font=ctk.CTkFont(size=22, weight="bold"),
            anchor="w"
        ).grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            kira_header,
            text="Experiencia principal",
            text_color="#8fa3b8",
            anchor="e"
        ).grid(row=0, column=1, sticky="e")

        kira_response_shell = ctk.CTkFrame(tab_main_kira, fg_color="#0c1117", corner_radius=18)
        kira_response_shell.grid(row=1, column=0, sticky="nsew", padx=16, pady=(0, 10))
        kira_response_shell.grid_columnconfigure(0, weight=1)
        kira_response_shell.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(
            kira_response_shell,
            text="Respuesta de Kira",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#d8e2ef",
            anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=14, pady=(12, 4))

        self.text_kira_response = ctk.CTkTextbox(
            kira_response_shell,
            font=ctk.CTkFont(size=17),
            fg_color="#090d12",
            border_width=1,
            border_color="#1f2b38",
            state="disabled"
        )
        self.text_kira_response.grid(row=1, column=0, sticky="nsew", padx=14, pady=(0, 14))
        self.text_kira_response.configure(state="normal")
        self.text_kira_response.insert("end", "La respuesta de Kira aparecerá aquí. Los logs completos se muestran abajo solo si activas Mostrar logs.\n")
        self.text_kira_response.configure(state="disabled")

        voice_panel = ctk.CTkFrame(tab_main_kira, fg_color="#121d27", corner_radius=16)
        voice_panel.grid(row=2, column=0, sticky="ew", padx=16, pady=(0, 10))
        voice_panel.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            voice_panel,
            text="Entrada de voz / PTT",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color="#d8e2ef"
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 2))

        kira_state_strip = ctk.CTkFrame(voice_panel, fg_color="transparent")
        kira_state_strip.grid(row=1, column=0, sticky="ew", padx=12, pady=(4, 6))
        for col in range(4):
            kira_state_strip.grid_columnconfigure(col, weight=1)
        self.lbl_kira_voice_state = ctk.CTkLabel(kira_state_strip, text="Voz/PTT: listo", fg_color="#1b2633", corner_radius=12, anchor="w")
        self.lbl_kira_voice_state.grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=0)
        self.lbl_kira_tts_state = ctk.CTkLabel(kira_state_strip, text="TTS: idle", fg_color="#1b2633", corner_radius=12, anchor="w")
        self.lbl_kira_tts_state.grid(row=0, column=1, sticky="ew", padx=4, pady=0)
        self.lbl_kira_memory_state = ctk.CTkLabel(kira_state_strip, text="Memoria: disponible", fg_color="#1b2633", corner_radius=12, anchor="w")
        self.lbl_kira_memory_state.grid(row=0, column=2, sticky="ew", padx=4, pady=0)
        self.lbl_kira_chat_state = ctk.CTkLabel(kira_state_strip, text="Chat: desconectado", fg_color="#1b2633", corner_radius=12, anchor="w")
        self.lbl_kira_chat_state.grid(row=0, column=3, sticky="ew", padx=(4, 0), pady=0)

        self.lbl_voice_hint = ctk.CTkLabel(
            voice_panel,
            text="Usa LiveAudio o PTT. El botón principal conserva el comportamiento de Conectar LiveAudio.",
            text_color="#8fa3b8",
            anchor="w"
        )
        self.lbl_voice_hint.grid(row=2, column=0, sticky="ew", padx=12, pady=(0, 8))

        voice_actions = ctk.CTkFrame(voice_panel, fg_color="transparent")
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

        frame_bottom = ctk.CTkFrame(tab_main_kira, fg_color="#121d27", corner_radius=16)
        frame_bottom.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 16))
        frame_bottom.grid_columnconfigure(0, weight=1)

        self.entry_chat = ctk.CTkEntry(
            frame_bottom,
            placeholder_text="Escribe un mensaje para Kira (contexto o pregunta)..."
        )
        self.entry_chat.grid(row=0, column=0, sticky="ew", padx=(10, 6), pady=10)
        self.entry_chat.bind("<Return>", lambda e: self._enviar_contexto_manual())

        self.btn_enviar = ctk.CTkButton(
            frame_bottom, text="Enviar a IA",
            command=self._enviar_contexto_manual,
            width=110, state="disabled",
            fg_color="#555555",
            hover_color="#666666"
        )
        self.btn_enviar.grid(row=0, column=1, padx=(0, 10), pady=10)

        side_panel = ctk.CTkFrame(app_shell, width=390, fg_color="#0f151c", corner_radius=18)
        side_panel.grid(row=0, column=1, sticky="nsew", padx=(8, 0), pady=0)
        side_panel.grid_columnconfigure(0, weight=1)
        side_panel.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            side_panel,
            text="Configuración",
            font=ctk.CTkFont(size=16, weight="bold"),
            anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=12, pady=(12, 6))

        config_tabs = ctk.CTkTabview(
            side_panel,
            width=370,
            fg_color="#0f151c",
            segmented_button_fg_color="#0c1117",
            segmented_button_selected_color="#2f5f8f",
            segmented_button_selected_hover_color="#3670aa",
            segmented_button_unselected_color="#151d26",
            segmented_button_unselected_hover_color="#1d2a38",
            text_color="#d8e2ef"
        )
        config_tabs.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 12))
        tab_cfg_model_profile = config_tabs.add("Modelo/Perfil")
        tab_cfg_audio_voice = config_tabs.add("Audio/TTS")
        tab_cfg_ptt = config_tabs.add("PTT")
        tab_cfg_youtube = config_tabs.add("YouTube")
        tab_cfg_admin = config_tabs.add("Admin")
        for tab in (tab_cfg_model_profile, tab_cfg_audio_voice, tab_cfg_ptt, tab_cfg_youtube, tab_cfg_admin):
            tab.grid_columnconfigure(0, weight=1)

        frame_model = ctk.CTkFrame(tab_cfg_model_profile, fg_color="#151d26", corner_radius=14)
        frame_model.grid(row=0, column=0, sticky="ew", padx=8, pady=8)

        ctk.CTkLabel(frame_model, text="Modelo", font=ctk.CTkFont(size=13, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(10, 4))

        self._model_display_to_tag = {}
        self._model_tag_to_display = {}
        model_display_list = []
        for tag, info in MODELS_CATALOG.items():
            display = info['display']
            self._model_display_to_tag[display] = tag
            self._model_tag_to_display[tag] = display
            model_display_list.append(display)

        default_display = self._model_tag_to_display.get(DEFAULT_MODEL, DEFAULT_MODEL)

        self.combo_modelos = ctk.CTkOptionMenu(
            frame_model,
            values=model_display_list,
            command=self._al_seleccionar_modelo,
            width=300
        )
        self.combo_modelos.set(default_display)
        self.combo_modelos.pack(fill="x", padx=10, pady=4)

        self.btn_download = ctk.CTkButton(
            frame_model, text="⬇️ Descargar", command=self._descargar_modelo,
            width=110, fg_color="#555555", hover_color="#666666"
        )
        self.btn_download.pack(fill="x", padx=10, pady=4)

        self.lbl_modelo_info = ctk.CTkLabel(
            frame_model, text="", font=ctk.CTkFont(size=11),
            text_color="#aaaaaa",
            anchor="w",
            justify="left",
            wraplength=300
        )
        self.lbl_modelo_info.pack(fill="x", padx=10, pady=4)
        self._actualizar_info_modelo(DEFAULT_MODEL)

        self.progress_download = ctk.CTkProgressBar(frame_model, width=150)
        self.progress_download.pack(fill="x", padx=10, pady=(4, 10))
        self.progress_download.set(0)
        self.progress_download.pack_forget()

        frame_profile = ctk.CTkFrame(tab_cfg_model_profile, fg_color="#151d26", corner_radius=14)
        frame_profile.grid(row=1, column=0, sticky="ew", padx=8, pady=8)

        ctk.CTkLabel(frame_profile, text="Perfil", font=ctk.CTkFont(size=13, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(10, 4))

        self.combo_perfiles = ctk.CTkOptionMenu(
            frame_profile,
            values=list(self.perfiles.keys()),
            command=self._al_seleccionar_perfil,
            width=300
        )
        default_perfil = "Kira (Default)" if "Kira (Default)" in self.perfiles else list(self.perfiles.keys())[0]
        self.combo_perfiles.set(default_perfil)
        self.combo_perfiles.pack(fill="x", padx=10, pady=4)

        self.btn_editar_perfiles = ctk.CTkButton(
            frame_profile, text="✏️ Editar Perfiles", command=self._abrir_configurador_perfiles,
            width=130, fg_color="#555555", hover_color="#666666"
        )
        self.btn_editar_perfiles.pack(fill="x", padx=10, pady=(4, 10))

        frame_audio = ctk.CTkFrame(tab_cfg_audio_voice, fg_color="#151d26", corner_radius=14)
        frame_audio.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        ctk.CTkLabel(frame_audio, text="Dispositivo de audio", font=ctk.CTkFont(size=13, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(10, 4))

        self.combo_dispositivos = ctk.CTkOptionMenu(
            frame_audio,
            values=self.lista_dispositivos,
            command=self._al_seleccionar_dispositivo,
            width=300
        )
        self.combo_dispositivos.pack(fill="x", padx=10, pady=4)
        if self.lista_dispositivos:
            self.combo_dispositivos.set(self.lista_dispositivos[0])
            self.dispositivo_seleccionado = int(self.lista_dispositivos[0].split(":")[0])
            self.lbl_mic_status_pill.configure(text="Mic: conectado", fg_color="#1b2633")
        else:
            self.combo_dispositivos.set("Sin dispositivos de audio")
            self.lbl_mic_status_pill.configure(text="Mic: desconectado", fg_color="#4a2630")

        audio_buttons = ctk.CTkFrame(frame_audio, fg_color="transparent")
        audio_buttons.pack(fill="x", padx=10, pady=4)

        self.btn_grabar = ctk.CTkButton(
            audio_buttons, text="🎤 Grabar", command=self._iniciar_grabacion,
            state="disabled", width=90, fg_color="#555555", hover_color="#666666"
        )
        self.btn_grabar.pack(side="left", expand=True, fill="x", padx=(0, 4))

        self.btn_voz = ctk.CTkButton(
            audio_buttons, text="📂 Cargar WAV", command=self._cargar_voz,
            state="disabled", fg_color="#555555", width=110
        )
        self.btn_voz.pack(side="left", expand=True, fill="x", padx=(4, 0))

        self.btn_ws = ctk.CTkButton(
            frame_audio, text="Conectar LiveAudio", command=self._toggle_websocket,
            fg_color="#555555", state="disabled"
        )
        self.btn_ws.pack(fill="x", padx=10, pady=(4, 10))

        frame_tts_memory = ctk.CTkFrame(tab_cfg_audio_voice, fg_color="#151d26", corner_radius=14)
        frame_tts_memory.grid(row=1, column=0, sticky="ew", padx=8, pady=8)
        ctk.CTkLabel(frame_tts_memory, text="TTS / Memoria", font=ctk.CTkFont(size=13, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(10, 4))

        self.switch_modo_ligero = ctk.CTkSwitch(
            frame_tts_memory, text="🎛️ TTS: Ligero",
            onvalue="ligero", offvalue="pesado",
            command=self._al_cambiar_motor_tts
        )
        self.switch_modo_ligero.pack(fill="x", padx=10, pady=4)
        self.switch_modo_ligero.select()

        self.btn_clear = ctk.CTkButton(
            frame_tts_memory, text="🗑️ Limpiar Memoria", command=self._limpiar_historial,
            width=130, fg_color="#555555", hover_color="#777777"
        )
        self.btn_clear.pack(fill="x", padx=10, pady=(4, 10))

        frame_ptt = ctk.CTkFrame(tab_cfg_ptt, fg_color="#151d26", corner_radius=14)
        frame_ptt.grid(row=0, column=0, sticky="ew", padx=8, pady=8)

        ctk.CTkLabel(frame_ptt, text="PTT", font=ctk.CTkFont(size=13, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(10, 4))

        self.switch_ptt = ctk.CTkSwitch(
            frame_ptt, text="PTT OFF",
            command=self._al_toggle_ptt,
            onvalue=True, offvalue=False
        )
        self.switch_ptt.pack(fill="x", padx=10, pady=4)

        ptt_hotkey_row = ctk.CTkFrame(frame_ptt, fg_color="transparent")
        ptt_hotkey_row.pack(fill="x", padx=10, pady=4)
        ctk.CTkLabel(ptt_hotkey_row, text="Tecla:", font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 6))

        self.lbl_hotkey = ctk.CTkLabel(
            ptt_hotkey_row, text=self.ptt_hotkey,
            font=ctk.CTkFont(size=13, weight="bold"),
            width=80
        )
        self.lbl_hotkey.pack(side="left", padx=3)

        self.btn_mapear = ctk.CTkButton(
            ptt_hotkey_row, text="Mapear", command=self._mapear_hotkey,
            width=70, fg_color="#555555", hover_color="#666666"
        )
        self.btn_mapear.pack(side="right", padx=3)

        self.lbl_ptt_status = ctk.CTkLabel(
            frame_ptt, text="", font=ctk.CTkFont(size=12),
            text_color="#888888",
            anchor="w",
            justify="left"
        )
        self.lbl_ptt_status.pack(fill="x", padx=10, pady=(0, 10))

        frame_youtube = ctk.CTkFrame(tab_cfg_youtube, fg_color="#151d26", corner_radius=14)
        frame_youtube.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        ctk.CTkLabel(frame_youtube, text="YouTube", font=ctk.CTkFont(size=13, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(10, 4))

        self.entry_youtube_video = ctk.CTkEntry(
            frame_youtube,
            placeholder_text="URL o video_id del live",
            width=300
        )
        self.entry_youtube_video.pack(fill="x", padx=10, pady=4)
        self.entry_youtube_video.bind("<Return>", lambda e: self._toggle_smart_aggregator())

        self.btn_youtube_chat = ctk.CTkButton(
            frame_youtube,
            text="Conectar Chat",
            command=self._toggle_smart_aggregator,
            width=120,
            fg_color="#2f5f8f",
            hover_color="#3670aa"
        )
        self.btn_youtube_chat.pack(fill="x", padx=10, pady=4)

        youtube_limit_row = ctk.CTkFrame(frame_youtube, fg_color="transparent")
        youtube_limit_row.pack(fill="x", padx=10, pady=(4, 10))
        ctk.CTkLabel(youtube_limit_row, text="Max/u:", font=ctk.CTkFont(size=12)).pack(side="left", padx=(0, 6))
        self.entry_youtube_user_limit = ctk.CTkEntry(youtube_limit_row, width=60)
        self.entry_youtube_user_limit.insert(0, "10")
        self.entry_youtube_user_limit.pack(side="left", padx=3)
        self.entry_youtube_user_limit.bind("<Return>", lambda e: self._apply_smart_spam_limit())

        frame_oauth = ctk.CTkFrame(tab_cfg_admin, fg_color="#151d26", corner_radius=14)
        frame_oauth.grid(row=1, column=0, sticky="ew", padx=8, pady=8)
        ctk.CTkLabel(frame_oauth, text="OAuth", font=ctk.CTkFont(size=13, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(10, 4))
        self.lbl_oauth_side_status = ctk.CTkLabel(frame_oauth, text="Configura YouTube en la pestaña Stream Admin.", text_color="#8fa3b8", anchor="w", justify="left", wraplength=300)
        self.lbl_oauth_side_status.pack(fill="x", padx=10, pady=(0, 10))

        frame_moderation = ctk.CTkFrame(tab_cfg_admin, fg_color="#151d26", corner_radius=14)
        frame_moderation.grid(row=2, column=0, sticky="ew", padx=8, pady=8)
        ctk.CTkLabel(frame_moderation, text="Moderación", font=ctk.CTkFont(size=13, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(10, 4))
        self.lbl_moderation_side_status = ctk.CTkLabel(frame_moderation, text="Sin acciones pendientes. Detalles en Stream Admin.", text_color="#8fa3b8", anchor="w", justify="left", wraplength=300)
        self.lbl_moderation_side_status.pack(fill="x", padx=10, pady=(0, 10))

        frame_view = ctk.CTkFrame(tab_cfg_admin, fg_color="#151d26", corner_radius=14)
        frame_view.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        ctk.CTkLabel(frame_view, text="Vista", font=ctk.CTkFont(size=13, weight="bold"), anchor="w").pack(fill="x", padx=10, pady=(10, 4))

        self.switch_logs = ctk.CTkSwitch(
            frame_view, text="Registrar logs en avanzado",
            onvalue=True, offvalue=False
        )
        self.switch_logs.pack(fill="x", padx=10, pady=(4, 10))
        self.switch_logs.select()

        self.consola = None  # will be the Log tab textbox

        stream_admin_panel = ctk.CTkFrame(tab_main_stream_admin, fg_color="#0f151c", corner_radius=18)
        stream_admin_panel.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        stream_admin_panel.grid_columnconfigure(0, weight=1)
        stream_admin_panel.grid_rowconfigure(0, weight=1)

        self._build_stream_admin_tab(stream_admin_panel)

        advanced_panel = ctk.CTkFrame(self, fg_color="#0f151c", corner_radius=18, height=260)
        advanced_panel.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 12))
        advanced_panel.grid_propagate(False)
        advanced_panel.grid_columnconfigure(0, weight=1)
        advanced_panel.grid_rowconfigure(1, weight=1)

        ctk.CTkLabel(
            advanced_panel,
            text="Logs / Terminales",
            font=ctk.CTkFont(size=15, weight="bold"),
            anchor="w"
        ).grid(row=0, column=0, sticky="ew", padx=12, pady=(10, 4))

        self.tabview = ctk.CTkTabview(
            advanced_panel,
            command=self._on_tab_change,
            height=210
        )
        self.tabview.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

        tab_log = self.tabview.add("Log General")
        tab_acciones = self.tabview.add("Kira Acciones")
        tab_youtube = self.tabview.add("YT Chat")
        tab_stream_log = self.tabview.add("Stream Log")

        self.consola = ctk.CTkTextbox(
            tab_log, font=ctk.CTkFont(family="Consolas", size=13),
            state="disabled"
        )
        self.consola.pack(fill="both", expand=True)

        self.consola_acciones = ctk.CTkTextbox(
            tab_acciones, font=ctk.CTkFont(family="Consolas", size=13),
            state="disabled"
        )
        self.consola_acciones.pack(fill="both", expand=True)

        self.consola_youtube = ctk.CTkTextbox(
            tab_youtube, font=ctk.CTkFont(family="Consolas", size=13),
            state="disabled"
        )
        self.consola_youtube.pack(fill="both", expand=True)

        self.text_stream_admin_log = ctk.CTkTextbox(
            tab_stream_log, font=ctk.CTkFont(family="Consolas", size=12), state="disabled"
        )
        self.text_stream_admin_log.pack(fill="both", expand=True)

        for accion in _cargar_acciones():
            self._append_limited_textbox(self.consola_acciones, accion, max_lines=1000)

        if not _cargar_acciones():
            mensajes_demo = [
                "🎮 [Sistema] Modo de juego detectado: Streaming",
                "📺 [Kira] Título del stream actualizado a 'Jugando con la IA'",
                "🔇 [Kira] Slow Mode activado (5 min)",
                "🎵 [Sistema] Audio de fondo: OFF",
                "📋 [Kira] Descripción actualizada en canal",
            ]
            for msg in mensajes_demo:
                self._append_limited_textbox(self.consola_acciones, f"[demo] {msg}", max_lines=1000)

        self._frame_model = frame_model
        self._frame_profile = frame_profile
        self._frame_bottom = frame_bottom
        self._app_shell = app_shell
        self._main_interaction_panel = main_panel
        self._side_config_panel = side_panel
        self._advanced_mode_panel = advanced_panel
        self._logs_panel_visible = True
        self._show_main_view("Kira")
        self._toggle_logs_panel()

    def _build_stream_admin_tab(self, parent):
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)

        stream_tabs = ctk.CTkTabview(
            parent,
            fg_color="#0f151c",
            segmented_button_fg_color="#0c1117",
            segmented_button_selected_color="#2f5f8f",
            segmented_button_selected_hover_color="#3670aa",
            segmented_button_unselected_color="#151d26",
            segmented_button_unselected_hover_color="#1d2a38",
            text_color="#d8e2ef"
        )
        stream_tabs.grid(row=0, column=0, sticky="nsew", padx=0, pady=0)
        tab_stream_connection = stream_tabs.add("Conexión")
        tab_stream_metadata = stream_tabs.add("Metadata")
        tab_stream_moderation = stream_tabs.add("Moderación")
        tab_stream_chat = stream_tabs.add("Chat")
        tab_stream_status = stream_tabs.add("Estado")
        for tab in (tab_stream_connection, tab_stream_metadata, tab_stream_moderation, tab_stream_chat, tab_stream_status):
            tab.grid_columnconfigure(0, weight=1)
            tab.grid_rowconfigure(0, weight=1)

        frame_auth = ctk.CTkFrame(tab_stream_connection, fg_color="#151d26", corner_radius=14)
        frame_auth.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 4))
        frame_auth.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(frame_auth, text="OAuth / Proveedor", font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=0, padx=8, pady=6, sticky="w")
        self.lbl_stream_admin_status = ctk.CTkLabel(frame_auth, text="RF4 iniciando...", text_color="#aaaaaa")
        self.lbl_stream_admin_status.grid(row=0, column=1, padx=8, pady=6, sticky="w")

        self.btn_stream_youtube_read = ctk.CTkButton(frame_auth, text="Conectar YouTube Lectura", command=lambda: self._stream_admin_connect(False), width=170, fg_color="#2f5f8f", hover_color="#3670aa")
        self.btn_stream_youtube_read.grid(row=0, column=2, padx=4, pady=6)
        self.btn_stream_youtube_write = ctk.CTkButton(frame_auth, text="Reconectar Escritura", command=lambda: self._stream_admin_connect(True), width=150, fg_color="#555555", hover_color="#666666")
        self.btn_stream_youtube_write.grid(row=0, column=3, padx=4, pady=6)
        self.btn_stream_disconnect = ctk.CTkButton(frame_auth, text="Desconectar", command=self._stream_admin_disconnect, width=105, fg_color="#555555")
        self.btn_stream_disconnect.grid(row=0, column=4, padx=4, pady=6)
        self.btn_stream_twitch = ctk.CTkButton(frame_auth, text="Twitch Próximamente", state="disabled", width=145, fg_color="#444444")
        self.btn_stream_twitch.grid(row=0, column=5, padx=4, pady=6)

        ctk.CTkLabel(frame_auth, text="Client ID:").grid(row=1, column=0, padx=8, pady=4, sticky="e")
        self.entry_stream_client_id = ctk.CTkEntry(frame_auth, placeholder_text="tu_client_id.apps.googleusercontent.com")
        self.entry_stream_client_id.grid(row=1, column=1, columnspan=2, padx=4, pady=4, sticky="ew")
        ctk.CTkLabel(frame_auth, text="Secret:").grid(row=1, column=3, padx=8, pady=4, sticky="e")
        self.entry_stream_client_secret = ctk.CTkEntry(frame_auth, placeholder_text="OAuth client secret", show="*")
        self.entry_stream_client_secret.grid(row=1, column=4, padx=4, pady=4, sticky="ew")
        ctk.CTkButton(frame_auth, text="Guardar OAuth", command=self._stream_admin_save_oauth_client, width=125, fg_color="#555555", hover_color="#666666").grid(row=1, column=5, padx=4, pady=4)

        frame_meta = ctk.CTkFrame(tab_stream_metadata, fg_color="#151d26", corner_radius=14)
        frame_meta.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        frame_meta.grid_columnconfigure(1, weight=1)
        frame_meta.grid_columnconfigure(3, weight=1)

        ctk.CTkLabel(frame_meta, text="Metadata", font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=0, padx=8, pady=6, sticky="w")
        self.lbl_stream_metadata_state = ctk.CTkLabel(frame_meta, text="Sin metadata", text_color="#aaaaaa")
        self.lbl_stream_metadata_state.grid(row=0, column=1, columnspan=3, padx=8, pady=6, sticky="w")
        self.btn_stream_read_metadata = ctk.CTkButton(frame_meta, text="Leer", command=self._stream_admin_refresh_metadata, width=80, fg_color="#555555", hover_color="#666666")
        self.btn_stream_read_metadata.grid(row=0, column=4, padx=4, pady=6)
        self.btn_stream_suggest_metadata = ctk.CTkButton(frame_meta, text="Sugerir", command=self._stream_admin_suggest_metadata, width=85, fg_color="#2f5f8f", hover_color="#3670aa")
        self.btn_stream_suggest_metadata.grid(row=0, column=5, padx=4, pady=6)
        self.btn_stream_apply_metadata = ctk.CTkButton(frame_meta, text="Aplicar", command=self._stream_admin_apply_metadata, width=85, fg_color="#2a7d3f")
        self.btn_stream_apply_metadata.grid(row=0, column=6, padx=4, pady=6)
        self.btn_stream_reject_pending = ctk.CTkButton(frame_meta, text="Rechazar", command=self._stream_admin_reject_pending, width=85, fg_color="#7d2a2a")
        self.btn_stream_reject_pending.grid(row=0, column=7, padx=4, pady=6)
        ctk.CTkLabel(frame_meta, text="Título:").grid(row=1, column=0, padx=8, pady=4, sticky="e")
        self.entry_stream_title = ctk.CTkEntry(frame_meta, placeholder_text="Título del stream")
        self.entry_stream_title.grid(row=1, column=1, columnspan=7, padx=8, pady=4, sticky="ew")

        ctk.CTkLabel(frame_meta, text="Categoría:").grid(row=2, column=0, padx=8, pady=4, sticky="e")
        self.entry_stream_category = ctk.CTkEntry(frame_meta, placeholder_text="ID categoría YouTube, ej. 20")
        self.entry_stream_category.grid(row=2, column=1, padx=8, pady=4, sticky="ew")
        ctk.CTkLabel(frame_meta, text="Tags:").grid(row=2, column=2, padx=8, pady=4, sticky="e")
        self.entry_stream_tags = ctk.CTkEntry(frame_meta, placeholder_text="tag1, tag2, tag3")
        self.entry_stream_tags.grid(row=2, column=3, columnspan=5, padx=8, pady=4, sticky="ew")

        ctk.CTkLabel(frame_meta, text="Descripción:").grid(row=3, column=0, padx=8, pady=4, sticky="ne")
        self.text_stream_description = ctk.CTkTextbox(frame_meta, height=70)
        self.text_stream_description.grid(row=3, column=1, columnspan=7, padx=8, pady=4, sticky="ew")

        frame_mod = ctk.CTkFrame(tab_stream_moderation, fg_color="#151d26", corner_radius=14)
        frame_mod.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        frame_mod.grid_columnconfigure(7, weight=1)

        ctk.CTkLabel(frame_mod, text="Moderación", font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=0, padx=8, pady=6, sticky="w")
        self.switch_stream_mod_enabled = ctk.CTkSwitch(frame_mod, text="AutoMod", command=self._stream_admin_apply_runtime_settings)
        self.switch_stream_mod_enabled.grid(row=0, column=1, padx=4, pady=6)
        self.combo_stream_mod_mode = ctk.CTkOptionMenu(frame_mod, values=["alerts_only", "confirm_required", "automatic"], command=lambda _: self._stream_admin_apply_runtime_settings(), width=150)
        self.combo_stream_mod_mode.set("alerts_only")
        self.combo_stream_mod_mode.grid(row=0, column=2, padx=4, pady=6)
        self.switch_stream_announce = ctk.CTkSwitch(frame_mod, text="Anunciar al chat", command=self._stream_admin_apply_runtime_settings)
        self.switch_stream_announce.grid(row=0, column=3, padx=4, pady=6)

        ctk.CTkLabel(frame_mod, text="User Channel ID:").grid(row=1, column=0, padx=8, pady=4, sticky="e")
        self.entry_stream_mod_user = ctk.CTkEntry(frame_mod, width=180, placeholder_text="channelId del usuario")
        self.entry_stream_mod_user.grid(row=1, column=1, padx=4, pady=4)
        ctk.CTkLabel(frame_mod, text="Razón:").grid(row=1, column=2, padx=8, pady=4, sticky="e")
        self.entry_stream_mod_reason = ctk.CTkEntry(frame_mod, placeholder_text="spam/toxicidad/etc.")
        self.entry_stream_mod_reason.grid(row=1, column=3, columnspan=2, padx=4, pady=4, sticky="ew")
        self.btn_stream_propose_timeout = ctk.CTkButton(frame_mod, text="Proponer Timeout", command=lambda: self._stream_admin_propose_high_risk("timeout"), width=135, fg_color="#555555", hover_color="#666666")
        self.btn_stream_propose_timeout.grid(row=1, column=5, padx=4, pady=4)
        self.btn_stream_propose_ban = ctk.CTkButton(frame_mod, text="Proponer Ban", command=lambda: self._stream_admin_propose_high_risk("ban"), width=115, fg_color="#7d2a2a")
        self.btn_stream_propose_ban.grid(row=1, column=6, padx=4, pady=4)

        ctk.CTkLabel(frame_mod, text="Usuarios recientes", font=ctk.CTkFont(size=12, weight="bold")).grid(row=2, column=0, padx=8, pady=(8, 4), sticky="w")
        ctk.CTkButton(frame_mod, text="Actualizar lista", command=self._stream_admin_refresh_user_list, width=115, fg_color="#555555").grid(row=2, column=1, padx=4, pady=(8, 4), sticky="w")
        self.frame_stream_users = ctk.CTkScrollableFrame(frame_mod, height=115)
        self.frame_stream_users.grid(row=3, column=0, columnspan=8, padx=8, pady=(0, 8), sticky="ew")
        self.frame_stream_users.grid_columnconfigure(2, weight=1)
        self._stream_admin_refresh_user_list()

        frame_chat = ctk.CTkFrame(tab_stream_chat, fg_color="#151d26", corner_radius=14)
        frame_chat.grid(row=0, column=0, sticky="ew", padx=8, pady=8)
        frame_chat.grid_columnconfigure(1, weight=1)
        ctk.CTkLabel(frame_chat, text="Kira Chat", font=ctk.CTkFont(size=14, weight="bold")).grid(row=0, column=0, padx=8, pady=6, sticky="w")
        self.btn_stream_connect_chat = ctk.CTkButton(frame_chat, text="Conectar Chat", command=self._stream_admin_connect_current_chat, width=125, fg_color="#2f5f8f")
        self.btn_stream_connect_chat.grid(row=0, column=1, padx=4, pady=6, sticky="w")
        self.switch_stream_chat_enabled = ctk.CTkSwitch(frame_chat, text="Permitir mensajes", command=self._stream_admin_apply_runtime_settings)
        self.switch_stream_chat_enabled.grid(row=0, column=2, padx=4, pady=6, sticky="w")
        self.switch_stream_small = ctk.CTkSwitch(frame_chat, text="Stream Chico", command=self._stream_admin_toggle_small_stream)
        self.switch_stream_small.grid(row=0, column=3, padx=4, pady=6, sticky="w")
        ctk.CTkButton(frame_chat, text="Simular Chat", command=self._stream_admin_simulate_chat, width=115, fg_color="#555555", hover_color="#666666").grid(row=0, column=4, padx=8, pady=6)
        self.entry_stream_chat_message = ctk.CTkEntry(frame_chat, placeholder_text="Mensaje breve de Kira para el chat")
        self.entry_stream_chat_message.grid(row=1, column=0, columnspan=3, padx=8, pady=4, sticky="ew")
        self.btn_stream_send_chat = ctk.CTkButton(frame_chat, text="Enviar al chat", command=self._stream_admin_send_chat, width=120)
        self.btn_stream_send_chat.grid(row=1, column=3, padx=8, pady=4)
        ctk.CTkButton(frame_chat, text="Forzar Kira", command=self._stream_admin_force_kira_comment, width=115, fg_color="#555555", hover_color="#666666").grid(row=1, column=4, padx=8, pady=4)

        frame_bottom_admin = ctk.CTkFrame(tab_stream_status, fg_color="#151d26", corner_radius=14)
        frame_bottom_admin.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        frame_bottom_admin.grid_columnconfigure(0, weight=1)
        frame_bottom_admin.grid_columnconfigure(1, weight=1)
        frame_bottom_admin.grid_rowconfigure(1, weight=1)

        self.lbl_stream_analytics = ctk.CTkLabel(frame_bottom_admin, text="Analíticas: esperando RF3", anchor="w")
        self.lbl_stream_analytics.grid(row=0, column=0, padx=8, pady=6, sticky="ew")
        self.lbl_stream_pending = ctk.CTkLabel(frame_bottom_admin, text="Acción pendiente: ninguna", anchor="w", text_color="#aaaaaa")
        self.lbl_stream_pending.grid(row=0, column=1, padx=8, pady=6, sticky="ew")

        ctk.CTkLabel(
            frame_bottom_admin,
            text="El log detallado de Stream Admin está abajo en Logs / Terminales > Stream Log.",
            text_color="#8fa3b8",
            anchor="w"
        ).grid(row=1, column=0, columnspan=2, padx=8, pady=(0, 8), sticky="ew")

    def _show_main_view(self, name):
        frames = getattr(self, "_main_view_frames", {})
        if name not in frames:
            return
        frames[name].tkraise()
        for view_name, button in getattr(self, "_main_view_buttons", {}).items():
            if view_name == name:
                button.configure(fg_color="#2f5f8f")
            else:
                button.configure(fg_color="#151d26")

    def _show_stream_admin_view(self, name):
        frames = getattr(self, "_stream_admin_view_frames", {})
        if name not in frames:
            return
        frames[name].tkraise()
        for view_name, button in getattr(self, "_stream_admin_view_buttons", {}).items():
            if view_name == name:
                button.configure(fg_color="#2f5f8f")
            else:
                button.configure(fg_color="#151d26")

    def _actualizar_pipeline(self, estado):
        self._pipeline_state = estado
        estados = {
            "idle": ("Modelo listo", "#44cc66"),
            "listening": ("Micrófono escuchando", "#44ff44"),
            "processing": ("Modelo procesando", "#ffaa00"),
            "speaking": ("TTS generando", "#4488ff"),
            "playing": ("TTS hablando", "#44ccff"),
            "downloading": ("Modelo cargando", "#ff8800"),
            "init": ("Modelo cargando", "#888888"),
            "error": ("Modelo error", "#ff5555"),
        }
        text, color = estados.get(estado, ("", "#aaaaaa"))
        self.after(0, lambda t=text, c=color: (
            self.lbl_status.configure(text=t, text_color=c)
        ))

        def update_status_details():
            if hasattr(self, "lbl_tts_status_pill"):
                if estado == "speaking":
                    self.lbl_tts_status_pill.configure(text="TTS: generando", fg_color="#1f3f6f")
                    if hasattr(self, "lbl_kira_tts_state"):
                        self.lbl_kira_tts_state.configure(text="TTS: generando", fg_color="#1f3f6f")
                elif estado == "playing":
                    self.lbl_tts_status_pill.configure(text="TTS: hablando", fg_color="#1f526f")
                    if hasattr(self, "lbl_kira_tts_state"):
                        self.lbl_kira_tts_state.configure(text="TTS: hablando", fg_color="#1f526f")
                else:
                    self.lbl_tts_status_pill.configure(text="TTS: idle", fg_color="#1b2633")
                    if hasattr(self, "lbl_kira_tts_state"):
                        self.lbl_kira_tts_state.configure(text="TTS: idle", fg_color="#1b2633")
            if hasattr(self, "lbl_mic_status_pill"):
                if estado == "listening":
                    self.lbl_mic_status_pill.configure(text="Mic: escuchando", fg_color="#1f5a3a")
                    if hasattr(self, "lbl_kira_voice_state"):
                        self.lbl_kira_voice_state.configure(text="Voz/PTT: escuchando", fg_color="#1f5a3a")
                elif self.dispositivo_seleccionado is None:
                    self.lbl_mic_status_pill.configure(text="Mic: desconectado", fg_color="#4a2630")
                    if hasattr(self, "lbl_kira_voice_state"):
                        self.lbl_kira_voice_state.configure(text="Voz/PTT: sin mic", fg_color="#4a2630")
                else:
                    self.lbl_mic_status_pill.configure(text="Mic: conectado", fg_color="#1b2633")
                    if hasattr(self, "lbl_kira_voice_state"):
                        self.lbl_kira_voice_state.configure(text="Voz/PTT: listo", fg_color="#1b2633")
            if hasattr(self, "btn_primary_voice"):
                if estado == "listening":
                    self.btn_primary_voice.configure(text="Detener", fg_color="darkred", hover_color="#8b1a1a")
                elif estado == "processing":
                    self.btn_primary_voice.configure(text="Pensando...", fg_color="#8a6400", hover_color="#a67800")
                elif estado in ("speaking", "playing"):
                    self.btn_primary_voice.configure(text="Detener voz", fg_color="#2f5f8f", hover_color="#3670aa")
                elif estado == "downloading":
                    self.btn_primary_voice.configure(text="Modelo cargando", fg_color="#555555", hover_color="#666666")
                else:
                    self.btn_primary_voice.configure(text="Hablar", fg_color="#1f7a5a", hover_color="#24946c")
        self.after(0, update_status_details)

        if estado == "listening":
            self.after(0, lambda: self.barra_rms.grid())
            self.after(0, self._animar_rms)
        else:
            self.after(0, lambda: self.barra_rms.grid_remove())

    def _animar_rms(self):
        if self._pipeline_state != "listening":
            return
        import random
        nivel = random.uniform(0.2, 0.9)
        self.barra_rms.set(nivel)
        self.after(150, self._animar_rms)

    def _toggle_modo_compacto(self):
        self._modo_compacto = self.switch_compacto.get()
        if self._modo_compacto:
            if hasattr(self, "_side_config_panel"):
                self._side_config_panel.grid_remove()
            self._show_main_view("Kira")
            if hasattr(self, "_advanced_mode_panel"):
                self._set_logs_panel_visible(False)
            self.switch_compacto.configure(text="Completo")
        else:
            if hasattr(self, "_side_config_panel"):
                self._side_config_panel.grid()
            self._toggle_logs_panel()
            self.switch_compacto.configure(text="Compacto")

    def _set_logs_panel_visible(self, visible):
        if not hasattr(self, "_advanced_mode_panel"):
            return
        if getattr(self, "_logs_panel_visible", None) == visible:
            return
        self._logs_panel_visible = visible
        if visible:
            self.grid_rowconfigure(2, weight=0, minsize=260)
            self._advanced_mode_panel.grid()
        else:
            self.grid_rowconfigure(2, weight=0, minsize=0)
            self._advanced_mode_panel.grid_remove()

    def _toggle_logs_panel(self):
        if not hasattr(self, "_advanced_mode_panel"):
            return
        if hasattr(self, "switch_compacto") and self.switch_compacto.get():
            self._set_logs_panel_visible(False)
            return
        visible = bool(hasattr(self, "switch_advanced") and self.switch_advanced.get())
        self._set_logs_panel_visible(visible)

    def _toggle_advanced_mode(self):
        self._toggle_logs_panel()

    def _log_accion(self, msg):
        ts = time.strftime("%H:%M:%S")
        entrada = f"[{ts}] {msg}"
        self._append_limited_textbox(self.consola_acciones, entrada, max_lines=1000)
        _guardar_accion(msg)

    def _on_tab_change(self):
        pass

    def _init_stream_admin(self):
        try:
            config_path = os.path.join(BASE_DIR, "config", "stream_admin.yaml")
            self.stream_admin = AdminManager(
                config_path=config_path,
                llm_interface=self._smart_agg_llm_interface
            )
            self.stream_admin.on_log = self._on_stream_admin_log
            self.stream_admin.on_state = self._on_stream_admin_state
            self.stream_admin.on_metadata = self._on_stream_admin_metadata
            self.stream_admin.on_pending_action = self._on_stream_admin_pending
            self.stream_admin.on_analytics = self._on_stream_admin_analytics
            self._stream_admin_apply_runtime_settings(log=False)
            self._populate_stream_oauth_client_fields()
            self._on_stream_admin_state(self.stream_admin.status())
            self._on_stream_admin_log("[StreamAdmin] RF4 listo. YouTube read-only disponible; Twitch en placeholder.")
        except Exception as e:
            self.stream_admin = None
            logger.exception("No se pudo inicializar Stream Admin")
            self.log_queue.put(f"[StreamAdmin] No disponible: {e}")

    def _run_stream_admin_task(self, action_name, func):
        if not self.stream_admin:
            messagebox.showwarning("Stream Admin", "RF4 no inicializado. Revisa config/stream_admin.yaml.")
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
                self.after(0, lambda err=e: messagebox.showerror("Stream Admin", str(err)))
                self._on_stream_admin_log(f"[StreamAdmin] {action_name} falló: {e}{hint}")

        threading.Thread(target=worker, daemon=True).start()

    def _stream_admin_connect(self, request_write):
        self._run_stream_admin_task(
            "OAuth YouTube",
            lambda: self.stream_admin.authenticate("youtube", request_write_scopes=request_write)
        )

    def _stream_admin_save_oauth_client(self):
        client_id = self.entry_stream_client_id.get().strip()
        client_secret = self.entry_stream_client_secret.get().strip()
        self._run_stream_admin_task(
            "Guardar OAuth client",
            lambda: self.stream_admin.save_oauth_client_config(client_id, client_secret)
        )
        self.entry_stream_client_secret.delete(0, "end")

    def _populate_stream_oauth_client_fields(self):
        if not self.stream_admin or not hasattr(self, "entry_stream_client_id"):
            return
        cfg = self.stream_admin.get_oauth_client_config()
        client_id = cfg.get("client_id", "")
        if client_id and not client_id.startswith("${"):
            self.entry_stream_client_id.delete(0, "end")
            self.entry_stream_client_id.insert(0, client_id)
        if cfg.get("has_client_secret"):
            self.entry_stream_client_secret.configure(placeholder_text="Secret guardado localmente; escribe uno nuevo para reemplazar")

    def _stream_admin_disconnect(self):
        self._run_stream_admin_task("Desconectar proveedor", self.stream_admin.disconnect)

    def _stream_admin_refresh_metadata(self):
        self._run_stream_admin_task("Leer metadata", self.stream_admin.refresh_metadata)

    def _stream_admin_connect_current_chat(self):
        metadata = self._stream_admin_last_metadata or {}
        video_id = metadata.get("video_id")
        live_chat_id = metadata.get("live_chat_id")
        if not video_id and self.stream_admin:
            video_id = getattr(getattr(self.stream_admin, "metadata", None), "video_id", "")
            live_chat_id = getattr(getattr(self.stream_admin, "metadata", None), "live_chat_id", "")
        if not video_id:
            messagebox.showwarning("Stream Admin", "Primero usa 'Leer' para detectar el live activo.")
            return

        if self.stream_admin_chat_connected:
            self._stream_admin_disconnect_api_chat()
            return

        if live_chat_id and self.stream_admin:
            self._stream_admin_connect_api_chat(video_id, live_chat_id)
            return

        if self.smart_agg_connected or self.smart_agg_connecting:
            current = self._extract_youtube_video_id(self.entry_youtube_video.get())
            if current == video_id:
                self._on_stream_admin_log(f"[StreamAdmin] Chat ya conectado al live {video_id}.")
                return
            messagebox.showwarning("Stream Admin", "Ya hay un chat conectado. Desconéctalo antes de cambiar de live.")
            return

        self.entry_youtube_video.delete(0, "end")
        self.entry_youtube_video.insert(0, video_id)
        self._on_stream_admin_log(f"[StreamAdmin] Conectando RF3 al chat del live {video_id}.")
        self._toggle_smart_aggregator()

    def _stream_admin_connect_api_chat(self, video_id, live_chat_id):
        if not self.smart_agg:
            messagebox.showwarning("Stream Admin", "Smart Aggregator no inicializado.")
            return
        if self.smart_agg_connected or self.smart_agg_connecting:
            messagebox.showwarning("Stream Admin", "Ya hay un chat RF3 conectado. Desconéctalo antes de usar chat autenticado.")
            return
        self.stream_admin_chat_connected = True
        self._stream_admin_chat_stop = threading.Event()
        self._stream_admin_seen_chat_ids = set()
        self.smart_agg.start_session("youtube", video_id)
        self.entry_youtube_video.delete(0, "end")
        self.entry_youtube_video.insert(0, video_id)
        self.btn_stream_connect_chat.configure(text="Desconectar Chat", fg_color="darkred")
        if hasattr(self, "lbl_chat_status_pill"):
            self.lbl_chat_status_pill.configure(text="Chat: conectado", fg_color="#1f5a3a")
        if hasattr(self, "lbl_kira_chat_state"):
            self.lbl_kira_chat_state.configure(text="Chat: conectado", fg_color="#1f5a3a")
        self._on_stream_admin_log(f"[StreamAdmin] Chat autenticado conectado al live {video_id}.")

        def worker():
            page_token = None
            failures = 0
            max_failures = 6
            while self.stream_admin_chat_connected and self._stream_admin_chat_stop and not self._stream_admin_chat_stop.is_set():
                try:
                    result = self.stream_admin.provider.list_live_chat_messages(live_chat_id, page_token=page_token)
                    failures = 0
                    page_token = result.get("next_page_token") or page_token
                    for message in result.get("messages", []):
                        msg_id = message.get("id")
                        if msg_id and msg_id in self._stream_admin_seen_chat_ids:
                            continue
                        if msg_id:
                            self._stream_admin_seen_chat_ids.add(msg_id)
                            if len(self._stream_admin_seen_chat_ids) > 2000:
                                self._stream_admin_seen_chat_ids = set(list(self._stream_admin_seen_chat_ids)[-1000:])
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
                if self._stream_admin_chat_stop:
                    self._stream_admin_chat_stop.wait(delay)

        self._stream_admin_chat_thread = threading.Thread(target=worker, daemon=True)
        self._stream_admin_chat_thread.start()

    def _stream_admin_disconnect_api_chat(self):
        self.stream_admin_chat_connected = False
        if self._stream_admin_chat_stop:
            self._stream_admin_chat_stop.set()
        self._stream_admin_chat_stop = None
        try:
            if self.smart_agg:
                self.smart_agg.end_session()
        except Exception:
            pass
        if hasattr(self, "btn_stream_connect_chat"):
            self.btn_stream_connect_chat.configure(text="Conectar Chat", fg_color="#2f5f8f")
        if hasattr(self, "lbl_chat_status_pill"):
            self.lbl_chat_status_pill.configure(text="Chat: desconectado", fg_color="#1b2633")
        if hasattr(self, "lbl_kira_chat_state"):
            self.lbl_kira_chat_state.configure(text="Chat: desconectado", fg_color="#1b2633")
        self._on_stream_admin_log("[StreamAdmin] Chat autenticado desconectado.")

    def _stream_admin_suggest_metadata(self):
        context = ""
        if self.smart_agg and getattr(self.smart_agg, "_session_id", None):
            try:
                recent = self.smart_agg.history.get_session_context(self.smart_agg._session_id, max_messages=12)
                context = "\n".join(f"{m.get('user', '')}: {m.get('text', '')}" for m in recent)
            except Exception:
                context = ""
        self._run_stream_admin_task("Sugerir metadata", lambda: self.stream_admin.suggest_metadata(context))

    def _stream_admin_apply_metadata(self):
        if not self._stream_admin_can_write():
            messagebox.showwarning("Stream Admin", "Modo solo lectura activo. Usa 'Reconectar Escritura' antes de aplicar cambios.")
            return
        payload = self._stream_admin_metadata_payload_from_ui()
        if self.stream_admin and self.stream_admin.pending_action:
            action = lambda: self.stream_admin.apply_pending_action(payload, force=True)
        else:
            action = lambda: self.stream_admin.apply_metadata(payload)
        self._run_stream_admin_task("Aplicar metadata", action)

    def _stream_admin_reject_pending(self):
        self._run_stream_admin_task("Rechazar acción", self.stream_admin.reject_pending_action)

    def _stream_admin_send_chat(self):
        if not self._stream_admin_can_write():
            messagebox.showwarning("Stream Admin", "Modo solo lectura activo. Reconecta escritura antes de enviar mensajes al chat.")
            return
        message = self.entry_stream_chat_message.get().strip()
        if not message:
            messagebox.showwarning("Stream Admin", "Escribe un mensaje para el chat.")
            return
        self._run_stream_admin_task("Enviar mensaje al chat", lambda: self.stream_admin.send_chat_message(message))

    def _stream_admin_force_kira_comment(self):
        if self._smart_agg_is_busy():
            self._on_stream_admin_log("[StreamAdmin] Forzar Kira omitido: Kira está ocupada.")
            return
        context = []
        if self.smart_agg and getattr(self.smart_agg, "_session_id", None):
            try:
                context = self.smart_agg.history.get_session_context(self.smart_agg._session_id, max_messages=12)
            except Exception as e:
                logger.warning(f"No se pudo obtener contexto RF3 para Forzar Kira: {e}")
        if not context:
            manual = self.entry_stream_chat_message.get().strip()
            if manual:
                context = [{"user": "Streamer", "text": manual}]
            elif self._stream_admin_last_metadata:
                context = [{
                    "user": "Stream Admin",
                    "text": f"Live actual: {self._stream_admin_last_metadata.get('title', '')}. Categoria {self._stream_admin_last_metadata.get('category_id', '')}."
                }]
        if not context:
            messagebox.showwarning("Stream Admin", "No hay mensajes recientes. Escribe una idea en 'Kira Chat' o espera chat.")
            return
        self._on_smart_aggregated_context({"trigger": {"manual": True, "source": "stream_admin"}, "context": context})
        self._on_stream_admin_log("[StreamAdmin] Forzar Kira ejecutado con contexto reciente.")

    def _stream_admin_toggle_small_stream(self):
        if not self.smart_agg:
            return
        if self.switch_stream_small.get():
            self.smart_agg.set_activity_limits(threshold_per_second=0.2, cooldown_seconds=20.0, reset=True)
            self.smart_agg.set_spam_limits(max_messages_per_user=30)
            self._on_stream_admin_log("[StreamAdmin] Modo Stream Chico ON: Kira reaccionará con menos mensajes.")
        else:
            defaults = self._smart_agg_default_activity or {"threshold": 1.0, "cooldown": 45.0}
            self.smart_agg.set_activity_limits(
                threshold_per_second=defaults.get("threshold", 1.0),
                cooldown_seconds=defaults.get("cooldown", 45.0),
                reset=True,
            )
            self._apply_smart_spam_limit(log=False)
            self._on_stream_admin_log("[StreamAdmin] Modo Stream Chico OFF: umbrales RF3 restaurados.")

    def _stream_admin_simulate_chat(self):
        if not self.smart_agg:
            messagebox.showwarning("Stream Admin", "Smart Aggregator no inicializado.")
            return
        if self._smart_agg_is_busy():
            self._on_stream_admin_log("[StreamAdmin] Simular Chat omitido: Kira está ocupada.")
            return

        if getattr(self.smart_agg, "_session_id", None) is None:
            channel = self._stream_admin_last_metadata.get("video_id") if self._stream_admin_last_metadata else "simulated"
            self.smart_agg.start_session("youtube", channel or "simulated")

        now = time.time()
        sample_sets = [
            [
                ("TesterUno", "Kira comenta algo del Minecraft con mods ahora mismo"),
                ("TesterDos", "El chat quiere saber si estos cultivos van a crecer rapido"),
                ("TesterTres", "Esto se esta poniendo caotico con tantos mobs alrededor"),
                ("TesterCuatro", "La base necesita nombre antes de que explote todo"),
                ("TesterCinco", "Kira deberia burlarse un poquito del survival"),
                ("TesterSeis", "Momento perfecto para que Kira diga algo divertido"),
            ],
            [
                ("CreeperFan", "Ese mod se ve peligrosamente roto para un episodio uno"),
                ("CultivosOP", "Si los cultivos no crecen rapido esto es estafa agricola"),
                ("NetherPronto", "Fran va a morir antes de hacer una casa decente"),
                ("ChatCaos", "Kira tiene que elegir si la base se llama rancho del desastre"),
                ("ModWatcher", "Hay demasiadas cosas raras en pantalla y apenas empezamos"),
                ("Ironia", "Survival con mods significa sufrir pero con pasos extra"),
            ],
            [
                ("Aldeano", "Necesitamos una meta clara antes de que el chat se distraiga"),
                ("DiamanteFake", "Eso no parece seguro pero si parece divertido"),
                ("HornoLento", "Kira deberia exigir armadura antes de otra idea brillante"),
                ("BiomeFan", "Explora ese bioma raro o no hay respeto"),
                ("PicoRoto", "Este survival ya huele a inventario perdido"),
                ("ChatPlan", "Objetivo del stream: no morir por una gallina mutante"),
            ],
        ]
        samples = sample_sets[self._stream_admin_sim_round % len(sample_sets)]
        self._stream_admin_sim_round += 1
        for idx, (user, text) in enumerate(samples):
            self.smart_agg.process_message({
                "id": f"sim-{int(now)}-{idx}",
                "user": user,
                "text": text,
                "timestamp": now + (idx * 0.05),
                "source": "stream_admin_simulator",
            })
        self._on_stream_admin_log("[StreamAdmin] Chat simulado enviado a RF3.")

    def _stream_admin_propose_high_risk(self, action):
        user = self.entry_stream_mod_user.get().strip()
        reason = self.entry_stream_mod_reason.get().strip() or "moderacion manual RF4"
        if not user:
            messagebox.showwarning("Stream Admin", "Ingresa el channelId del usuario a moderar.")
            return
        self._run_stream_admin_task(
            f"Proponer {action}",
            lambda: self.stream_admin.propose_high_risk_moderation(action, user, reason, 300)
        )

    def _stream_admin_track_chat_user(self, message):
        channel_id = message.get("author_channel_id") or message.get("channel_id") or ""
        if not channel_id:
            return
        user = message.get("user", "YouTube")
        current = self._stream_admin_chat_users.get(channel_id, {})
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
        self._stream_admin_chat_users[channel_id] = current
        self.after(0, self._stream_admin_refresh_user_list)

    def _stream_admin_refresh_user_list(self):
        if not hasattr(self, "frame_stream_users"):
            return
        for child in self.frame_stream_users.winfo_children():
            child.destroy()
        users = sorted(
            self._stream_admin_chat_users.values(),
            key=lambda item: item.get("last_seen", 0),
            reverse=True,
        )[:10]
        if not users:
            ctk.CTkLabel(
                self.frame_stream_users,
                text="Sin usuarios recientes con channelId. Conecta chat autenticado y espera mensajes.",
                text_color="#aaaaaa"
            ).grid(row=0, column=0, padx=6, pady=6, sticky="w")
            return

        headers = ["Usuario", "Mensajes", "Razón", "Acción"]
        for col, title in enumerate(headers):
            ctk.CTkLabel(self.frame_stream_users, text=title, font=ctk.CTkFont(size=11, weight="bold")).grid(row=0, column=col, padx=4, pady=2, sticky="w")

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
            ctk.CTkLabel(self.frame_stream_users, text=label, anchor="w").grid(row=row, column=0, padx=4, pady=3, sticky="w")
            ctk.CTkLabel(self.frame_stream_users, text=str(item.get("count", 0)), width=55).grid(row=row, column=1, padx=4, pady=3, sticky="w")
            reason_entry = ctk.CTkEntry(self.frame_stream_users, placeholder_text="razón", width=260)
            reason_entry.insert(0, self._stream_admin_default_mod_reason(item))
            reason_entry.grid(row=row, column=2, padx=4, pady=3, sticky="ew")
            action_frame = ctk.CTkFrame(self.frame_stream_users, fg_color="transparent")
            action_frame.grid(row=row, column=3, padx=4, pady=3, sticky="w")
            button_state = "disabled" if item.get("is_owner") or not self._stream_admin_can_write() else "normal"
            ctk.CTkButton(
                action_frame,
                text="Timeout",
                width=75,
                fg_color="#555555",
                hover_color="#666666",
                state=button_state,
                command=lambda cid=channel_id, u=user, e=reason_entry: self._stream_admin_moderate_user_from_list("timeout", cid, u, e)
            ).pack(side="left", padx=(0, 4))
            ctk.CTkButton(
                action_frame,
                text="Banear",
                width=70,
                fg_color="#7d2a2a",
                state=button_state,
                command=lambda cid=channel_id, u=user, e=reason_entry: self._stream_admin_moderate_user_from_list("ban", cid, u, e)
            ).pack(side="left")

    def _stream_admin_default_mod_reason(self, item):
        text = (item.get("last_message") or "").strip()
        if len(text) > 80:
            text = text[:77] + "..."
        return f"Revisado desde Stream Admin: {text}" if text else "Moderación manual desde Stream Admin"

    def _stream_admin_moderate_user_from_list(self, action, channel_id, user, reason_entry):
        if not self.stream_admin:
            return
        if not self._stream_admin_can_write():
            messagebox.showwarning("Stream Admin", "Modo solo lectura activo. Reconecta escritura antes de moderar usuarios.")
            return
        if not channel_id:
            messagebox.showwarning("Stream Admin", "Este usuario no tiene channelId disponible para moderar.")
            return
        reason = reason_entry.get().strip() or f"{action} manual desde Stream Admin"
        verb = "banear" if action == "ban" else "aplicar timeout a"
        if not messagebox.askyesno("Confirmar moderación", f"¿Seguro que quieres {verb} {user}?\n\nRazón: {reason}"):
            return
        self._run_stream_admin_task(
            f"Moderación {action}",
            lambda: self.stream_admin.apply_high_risk_moderation(action, channel_id, reason, 300)
        )

    def _stream_admin_apply_runtime_settings(self, log=True):
        if not self.stream_admin:
            return
        mod_cfg = self.stream_admin.config.setdefault("moderation", {})
        chat_cfg = self.stream_admin.config.setdefault("chat", {})
        mod_cfg["enabled"] = bool(self.switch_stream_mod_enabled.get()) if hasattr(self, "switch_stream_mod_enabled") else False
        mod_cfg["mode"] = self.combo_stream_mod_mode.get() if hasattr(self, "combo_stream_mod_mode") else "alerts_only"
        mod_cfg["announce_actions_to_chat"] = bool(self.switch_stream_announce.get()) if hasattr(self, "switch_stream_announce") else False
        chat_cfg["allow_kira_chat_messages"] = bool(self.switch_stream_chat_enabled.get()) if hasattr(self, "switch_stream_chat_enabled") else False
        self.stream_admin.moderation.enabled = mod_cfg["enabled"]
        self.stream_admin.moderation.mode = mod_cfg["mode"]
        if log:
            self._on_stream_admin_log(f"[StreamAdmin] Runtime: moderación={mod_cfg['enabled']} modo={mod_cfg['mode']} chat={chat_cfg['allow_kira_chat_messages']}")
        self._sync_stream_admin_controls(self.stream_admin.status())

    def _stream_admin_can_write(self):
        if not self.stream_admin:
            return False
        try:
            status = self.stream_admin.status()
        except Exception:
            return False
        return bool(status.get("write_enabled") and status.get("write_scope_active"))

    def _sync_stream_admin_controls(self, state):
        connected = bool(state.get("connected"))
        write_ready = bool(state.get("write_enabled") and state.get("write_scope_active"))
        pending = bool(state.get("pending_action"))

        widgets = {
            "btn_stream_disconnect": connected,
            "btn_stream_read_metadata": connected,
            "btn_stream_suggest_metadata": connected,
            "btn_stream_connect_chat": connected,
            "btn_stream_apply_metadata": write_ready,
            "btn_stream_send_chat": write_ready,
            "switch_stream_chat_enabled": write_ready,
            "switch_stream_announce": write_ready,
            "btn_stream_reject_pending": pending,
        }
        for name, enabled in widgets.items():
            widget = getattr(self, name, None)
            if widget is not None:
                try:
                    widget.configure(state="normal" if enabled else "disabled")
                except Exception:
                    pass

        if not write_ready and hasattr(self, "switch_stream_chat_enabled"):
            try:
                self.switch_stream_chat_enabled.deselect()
                self.switch_stream_announce.deselect()
            except Exception:
                pass

        if hasattr(self, "frame_stream_users"):
            self._stream_admin_refresh_user_list()

    def _stream_admin_metadata_payload_from_ui(self):
        tags = [t.strip() for t in self.entry_stream_tags.get().split(",") if t.strip()]
        description = self.text_stream_description.get("1.0", "end").strip()
        return {
            "title": self.entry_stream_title.get().strip(),
            "category_id": self.entry_stream_category.get().strip(),
            "description": description,
            "tags": tags,
        }

    def _on_stream_admin_log(self, msg):
        self.after(0, lambda m=msg: self._append_stream_admin_log(m))
        clean = msg.replace("[StreamAdmin] ", "")
        self.after(0, lambda m=clean: self._log_accion(m))

    def _append_stream_admin_log(self, msg):
        if not hasattr(self, "text_stream_admin_log"):
            return
        self._append_limited_textbox(self.text_stream_admin_log, msg, max_lines=1000)

    def _on_stream_admin_state(self, state):
        def update():
            account = state.get("account") or {}
            name = account.get("title") or "sin cuenta"
            mode = state.get("mode", "read_only")
            connected = "conectado" if state.get("connected") else "desconectado"
            oauth_text = "OAuth: desconectado"
            if state.get("connected"):
                oauth_text = "OAuth: escritura" if mode == "write" else "OAuth: lectura"
            self.lbl_stream_admin_status.configure(
                text=f"YouTube {connected} ({name}) · modo {mode} · OAuth client {'OK' if state.get('oauth_client_configured') else 'pendiente'} · Twitch placeholder",
                text_color="#44cc66" if state.get("connected") else "#aaaaaa"
            )
            if hasattr(self, "lbl_oauth_status_pill"):
                color = "#1f5a3a" if state.get("connected") and mode == "write" else "#1f3f6f" if state.get("connected") else "#1b2633"
                self.lbl_oauth_status_pill.configure(text=oauth_text, fg_color=color)
            if hasattr(self, "lbl_oauth_side_status"):
                self.lbl_oauth_side_status.configure(
                    text=f"YouTube {connected} ({name}) · modo {mode}. Controles completos en Stream Admin.",
                    text_color="#44cc66" if state.get("connected") else "#8fa3b8"
                )
            self._sync_stream_admin_controls(state)
        self.after(0, update)

    def _on_stream_admin_metadata(self, metadata):
        self._stream_admin_last_metadata = metadata or {}
        self.after(0, lambda m=metadata: self._populate_stream_metadata(m))

    def _populate_stream_metadata(self, metadata):
        metadata = metadata or {}
        self.lbl_stream_metadata_state.configure(
            text=f"Estado: {metadata.get('status', 'unknown')} · Video: {metadata.get('video_id', '') or 'N/A'}",
            text_color="#44cc66" if metadata.get("video_id") else "#ffaa00"
        )
        self.entry_stream_title.delete(0, "end")
        self.entry_stream_title.insert(0, metadata.get("title", ""))
        self.entry_stream_category.delete(0, "end")
        self.entry_stream_category.insert(0, metadata.get("category_id", ""))
        self.entry_stream_tags.delete(0, "end")
        self.entry_stream_tags.insert(0, ", ".join(metadata.get("tags", []) or []))
        self.text_stream_description.delete("1.0", "end")
        self.text_stream_description.insert("1.0", metadata.get("description", ""))

    def _on_stream_admin_pending(self, pending):
        def update():
            if not pending:
                self.lbl_stream_pending.configure(text="Acción pendiente: ninguna", text_color="#aaaaaa")
                if hasattr(self, "lbl_moderation_status_pill"):
                    self.lbl_moderation_status_pill.configure(text="Moderación: sin pendientes", fg_color="#1b2633")
                if hasattr(self, "lbl_moderation_side_status"):
                    self.lbl_moderation_side_status.configure(text="Sin acciones pendientes. Detalles en Stream Admin.", text_color="#8fa3b8")
                if self.stream_admin:
                    self._sync_stream_admin_controls(self.stream_admin.status())
                return
            payload = pending.get("payload", {})
            label = payload.get("title") or payload.get("action") or pending.get("type")
            self.lbl_stream_pending.configure(text=f"Acción pendiente: {pending.get('type')} · {label}", text_color="#ffaa00")
            if hasattr(self, "lbl_moderation_status_pill"):
                self.lbl_moderation_status_pill.configure(text="Moderación: acción pendiente", fg_color="#5f461b")
            if hasattr(self, "lbl_moderation_side_status"):
                self.lbl_moderation_side_status.configure(text=f"Pendiente: {pending.get('type')} · {label}", text_color="#ffaa00")
            if pending.get("type") == "metadata_update":
                self._populate_stream_metadata({
                    "status": "suggested",
                    "title": payload.get("title", ""),
                    "category_id": payload.get("category_id", ""),
                    "description": payload.get("description", ""),
                    "tags": payload.get("tags", []),
                    "video_id": self._stream_admin_last_metadata.get("video_id", ""),
                })
            if self.stream_admin:
                self._sync_stream_admin_controls(self.stream_admin.status())
        self.after(0, update)

    def _on_stream_admin_analytics(self, snapshot):
        def update():
            viewers = snapshot.get("viewers")
            viewers = "N/A" if viewers is None else viewers
            self.lbl_stream_analytics.configure(
                text=(
                    f"Analíticas: viewers={viewers} · uptime={snapshot.get('uptime_seconds', 0)//60}m · "
                    f"chat={snapshot.get('chat_rate_avg', 0.0):.2f} msg/s · "
                    f"vibe={snapshot.get('last_vibe_dominant', 'neutral')} {snapshot.get('last_vibe_temperature', 0):.0f}/100"
                )
            )
        self.after(0, update)

    def _stream_admin_ingest_rf3_event(self, event_type, payload):
        if not self.stream_admin:
            return
        try:
            self.stream_admin.ingest_rf3_event(event_type, payload)
            context = self.stream_admin.analytics_context_if_due()
            if context:
                self._stream_admin_inject_silent_context(context)
        except Exception as e:
            logger.warning(f"Stream Admin no pudo consumir evento RF3 {event_type}: {e}")

    def _stream_admin_inject_silent_context(self, context):
        try:
            if self.motor_ia and hasattr(self.motor_ia, "historial"):
                self.motor_ia.historial.append({
                    "role": "user",
                    "content": f"[Contexto administrativo silencioso RF4, no responder directamente]: {context}"
                })
                self._on_stream_admin_log("[StreamAdmin] Analíticas inyectadas como contexto silencioso para Kira.")
        except Exception as e:
            logger.warning(f"No se pudo inyectar contexto RF4: {e}")

    def _init_smart_aggregator(self):
        try:
            config_path = os.path.join(BASE_DIR, "config", "smart_aggregator.yaml")
            self.smart_agg = Aggregator(
                config_path=config_path,
                llm_interface=self._smart_agg_llm_interface
            )
            self.smart_agg.set_busy_callback(self._smart_agg_is_busy)
            self.smart_agg.on_filtered_message = self._on_smart_filtered_message
            self.smart_agg.on_vibe_update = self._on_smart_vibe_update
            self.smart_agg.on_activity_trigger = self._on_smart_activity_trigger
            self.smart_agg.on_aggregated_context = self._on_smart_aggregated_context
            self.smart_agg.on_source_error = self._on_smart_source_error
            self.smart_agg.on_source_connect = self._on_smart_source_connect
            self.smart_agg.on_source_disconnect = self._on_smart_source_disconnect
            self._smart_agg_default_activity = {
                "threshold": self.smart_agg.activity.threshold_per_second,
                "cooldown": self.smart_agg.activity.cooldown_seconds,
            }
            self.log_queue.put("[SmartAggregator] RF3 listo. Ingresa un video_id/URL de YouTube Live para conectar chat.")
        except Exception as e:
            self.smart_agg = None
            logger.exception("No se pudo inicializar Smart Aggregator")
            self.log_queue.put(f"[SmartAggregator] No disponible: {e}")

    def _smart_agg_is_busy(self):
        if not self.motor_ia:
            return True
        return self.motor_ia.is_processing or self.motor_ia.is_speaking

    def _smart_agg_llm_interface(self, prompt):
        if self._smart_agg_is_busy():
            raise RuntimeError("Motor IA ocupado")

        ollama_client = getattr(self.motor_ia, "ollama", None)
        if ollama_client is None:
            import ollama as ollama_client

        response = ollama_client.chat(
            model=self.motor_ia.current_model,
            messages=[{"role": "user", "content": prompt}],
            keep_alive=-1,
            options={"temperature": 0.2, "num_predict": 180}
        )
        msg_obj = response.get("message", {})
        if isinstance(msg_obj, dict):
            return msg_obj.get("content", "")
        return getattr(msg_obj, "content", "")

    def _extract_youtube_video_id(self, value):
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

    def _toggle_smart_aggregator(self):
        if not self.smart_agg:
            self._print_log("[SmartAggregator] No inicializado. Revisa config/smart_aggregator.yaml.")
            return

        if self.smart_agg_connected or self.smart_agg_connecting:
            self._smart_agg_manual_disconnect = True
            try:
                self.smart_agg.disconnect()
            finally:
                self.smart_agg_connected = False
                self.smart_agg_connecting = False
                self.btn_youtube_chat.configure(text="Conectar Chat", fg_color="#2f5f8f")
            return

        video_id = self._extract_youtube_video_id(self.entry_youtube_video.get())
        if not video_id:
            messagebox.showwarning("YouTube Live", "Ingresa una URL o video_id de un live de YouTube.")
            return

        try:
            self._apply_smart_spam_limit(log=False)
            self._smart_agg_manual_disconnect = False
            self.smart_agg_connecting = True
            self.btn_youtube_chat.configure(text="Conectando...", fg_color="#a66a00")
            self._print_log(f"[SmartAggregator] Conectando chat YouTube: {video_id}")
            self.smart_agg.connect(video_id)
        except Exception as e:
            self.smart_agg_connecting = False
            self.btn_youtube_chat.configure(text="Conectar Chat", fg_color="#2f5f8f")
            logger.exception("Error conectando Smart Aggregator")
            messagebox.showerror("YouTube Live", f"No se pudo conectar al chat: {e}")

    def _apply_smart_spam_limit(self, log=True):
        if not self.smart_agg:
            return
        raw_value = self.entry_youtube_user_limit.get().strip()
        try:
            limit = max(1, int(raw_value))
        except ValueError:
            limit = 10
            self.entry_youtube_user_limit.delete(0, "end")
            self.entry_youtube_user_limit.insert(0, str(limit))
        self.smart_agg.set_spam_limits(max_messages_per_user=limit)
        if log:
            self._print_log(f"[SmartAggregator] Anti-spam actualizado: max {limit} mensajes/usuario por ventana.")

    def _on_smart_filtered_message(self, message):
        self._stream_admin_track_chat_user(message)
        user = message.get("user", "")
        text = message.get("text", "")
        self.after(0, lambda u=user, t=text: self._print_youtube_chat(u, t))

    def _print_youtube_chat(self, user, text):
        if not hasattr(self, "consola_youtube") or self.consola_youtube is None:
            return
        self._append_limited_textbox(self.consola_youtube, f"[{user}] {text}", max_lines=1500)

    def _on_smart_source_error(self, error):
        self.log_queue.put(f"[SmartAggregator] Aviso YouTube: reconectando por fallo transitorio ({error})")

    def _on_smart_source_connect(self, info):
        was_connected = self.smart_agg_connected
        self.smart_agg_connecting = False
        self.smart_agg_connected = True
        self.after(0, lambda: self.btn_youtube_chat.configure(text="Desconectar Chat", fg_color="darkred"))
        self.after(0, lambda: self.lbl_chat_status_pill.configure(text="Chat: conectado", fg_color="#1f5a3a") if hasattr(self, "lbl_chat_status_pill") else None)
        self.after(0, lambda: self.lbl_kira_chat_state.configure(text="Chat: conectado", fg_color="#1f5a3a") if hasattr(self, "lbl_kira_chat_state") else None)
        if not was_connected:
            self.log_queue.put(f"[SmartAggregator] Chat YouTube conectado: {info.get('video_id', '')}")

    def _on_smart_source_disconnect(self):
        was_active = self.smart_agg_connected or self.smart_agg_connecting
        self.smart_agg_connected = False
        self.smart_agg_connecting = False
        self.after(0, lambda: self.btn_youtube_chat.configure(text="Conectar Chat", fg_color="#2f5f8f"))
        self.after(0, lambda: self.lbl_chat_status_pill.configure(text="Chat: desconectado", fg_color="#1b2633") if hasattr(self, "lbl_chat_status_pill") else None)
        self.after(0, lambda: self.lbl_kira_chat_state.configure(text="Chat: desconectado", fg_color="#1b2633") if hasattr(self, "lbl_kira_chat_state") else None)
        if was_active:
            reason = "desconectado por usuario" if self._smart_agg_manual_disconnect else "desconectado tras agotar reconexiones"
            self.log_queue.put(f"[SmartAggregator] Chat YouTube {reason}.")
        self._smart_agg_manual_disconnect = False

    def _on_smart_vibe_update(self, vibe):
        self._stream_admin_ingest_rf3_event("vibe", vibe)
        temp = vibe.get("temperature", 0.0)
        emotions = vibe.get("emotions", {})
        dominant = max(emotions, key=emotions.get) if emotions else "neutral"
        note = vibe.get("note")
        if note == "fallback_due_to_busy":
            self.log_queue.put("[SmartAggregator] Vibe omitido: Kira ocupada.")
        elif note in ("fallback_due_to_parse_error", "fallback_due_to_empty_llm_response", "fallback_due_to_llm_error"):
            self.log_queue.put(f"[SmartAggregator] Vibe no interpretable; usando neutral ({note}).")
        elif note:
            self.log_queue.put(f"[SmartAggregator] Vibe: {temp:.0f}/100 ({dominant}) [{note}]")
        else:
            self.log_queue.put(f"[SmartAggregator] Vibe: {temp:.0f}/100 ({dominant})")

    def _on_smart_activity_trigger(self, data):
        self._stream_admin_ingest_rf3_event("activity", data)
        rate = data.get("rate", 0.0)
        self.log_queue.put(f"[SmartAggregator] Pico de actividad detectado: {rate:.2f} msg/s")

    def _on_smart_aggregated_context(self, data):
        if self._smart_agg_is_busy():
            self.log_queue.put("[SmartAggregator] Contexto agregado omitido: Kira ocupada.")
            return

        context = data.get("context", [])[-12:]
        if not context:
            return

        highlight = self._select_smart_highlight(context)
        lines = [f"- {m.get('user', '')}: {m.get('text', '')}" for m in context]
        prompt = (
            "Estas viendo el chat de YouTube como co-host del stream. Di EXACTAMENTE lo que Kira diria al aire, "
            "no describas lo que esta pasando ni prometas que vas a hablar. Reacciona con una broma, critica o comentario concreto. "
            "Prohibido empezar con 'Parece que', 'Bueno, parece', 'Vale, parece', 'Voy a', 'Tengo que', 'El chat esta', "
            "'energia del flujo', 'mensaje destacado', 'contexto reciente' o 'mantener la energia'. "
            "No saludes ni preguntes 'que te trae por aqui'. No digas que Kira va a comentar: comenta directamente. "
            "Responde en 1-2 frases cortas con personalidad de Kira. "
            "Usa este mensaje solo como posible referencia interna, no lo nombres como destacado: "
            f"{highlight}\n"
            "Mensajes recientes del chat:\n" + "\n".join(lines)
        )
        self.motor_ia.command_queue.put(("process_context", prompt))
        self.log_queue.put("[SmartAggregator] Contexto agregado enviado a Kira.")

    def _select_smart_highlight(self, context):
        candidates = []
        for msg in context:
            text = msg.get("text", "").strip()
            if 20 <= len(text) <= 180:
                candidates.append(msg)
        if not candidates:
            candidates = context
        selected = max(candidates, key=lambda m: len(m.get("text", "")))
        return f"{selected.get('user', '')}: {selected.get('text', '')}"

    def _on_motor_event(self, status):
        if status == "ready":
            self.after(0, lambda: self.btn_grabar.configure(state="normal"))
            self.after(0, lambda: self.btn_voz.configure(state="normal"))
            self.after(0, lambda: self.btn_ws.configure(state="normal"))
            self.after(0, lambda: self.btn_primary_voice.configure(state="normal"))
            self.after(0, lambda: self.btn_enviar.configure(state="normal"))
            self._actualizar_pipeline("idle")
            self.after(0, self._refresh_modelo_instalado)
        elif status == "processing":
            self._actualizar_pipeline("processing")
            self.after(0, lambda: self.btn_enviar.configure(state="disabled"))
            self.after(0, lambda: self.combo_modelos.configure(state="disabled"))
            self.after(0, lambda: self.btn_download.configure(state="disabled"))
            self.after(0, lambda: self.switch_ptt.configure(state="disabled"))
            self.after(0, lambda: self.btn_mapear.configure(state="disabled"))
        elif status == "idle":
            self._actualizar_pipeline("idle")
            self.after(0, lambda: self.btn_enviar.configure(state="normal"))
            self.after(0, lambda: self.combo_modelos.configure(state="normal"))
            self.after(0, lambda: self.btn_download.configure(state="normal"))
            self.after(0, lambda: self.switch_ptt.configure(state="normal"))
            self.after(0, lambda: self.btn_mapear.configure(state="normal"))
            self._ensure_ptt_listener()
        elif status == "speaking_start":
            self._actualizar_pipeline("speaking")
            self._log_accion("Kira comenzó a sintetizar respuesta")
        elif status == "speaking_end":
            estado = "listening" if self.ws_connected else "idle"
            self._actualizar_pipeline(estado)
            self.after(0, lambda: self.switch_ptt.configure(state="normal"))
            self._ensure_ptt_listener()
        elif status == "model_changed":
            model = self.motor_ia.current_model
            self.after(0, lambda: self.title(f"VocalAI — Qwen3-TTS + {model}"))
            self.after(0, lambda: self._actualizar_info_modelo(model))
            self._actualizar_pipeline("idle")
        elif status == "download_start":
            self.after(0, lambda: self.btn_download.configure(state="disabled", text="Descargando..."))
            self.after(0, lambda: self.combo_modelos.configure(state="disabled"))
            self.after(0, lambda: self.progress_download.pack(fill="x", padx=10, pady=(4, 10)))
            self.after(0, lambda: self.progress_download.set(0))
            self.after(0, lambda: self.btn_primary_voice.configure(state="disabled"))
            self._actualizar_pipeline("downloading")
        elif status == "download_done":
            model = self.motor_ia.current_model
            self.after(0, lambda: self.btn_download.configure(state="normal", text="⬇️ Descargar"))
            self.after(0, lambda: self.combo_modelos.configure(state="normal"))
            self.after(0, lambda: self.progress_download.pack_forget())
            self.after(0, lambda: self.btn_primary_voice.configure(state="normal"))
            self.after(0, lambda: self.title(f"VocalAI — Qwen3-TTS + {model}"))
            self.after(0, lambda: self._actualizar_info_modelo(model))
            self._actualizar_pipeline("idle")
            self.after(0, self._refresh_modelo_instalado)
        elif status == "download_error":
            self.after(0, lambda: self.btn_download.configure(state="normal", text="⬇️ Descargar"))
            self.after(0, lambda: self.combo_modelos.configure(state="normal"))
            self.after(0, lambda: self.progress_download.pack_forget())
            self.after(0, lambda: self.btn_primary_voice.configure(state="normal"))
            self._actualizar_pipeline("error")

    def _al_seleccionar_modelo(self, display_name):
        tag = self._model_display_to_tag.get(display_name, display_name)
        self._actualizar_info_modelo(tag)

        if self._modelo_instalado(tag):
            self.motor_ia.command_queue.put(("switch_model", tag))
            self._print_log(f"[Sistema] Cambiando a modelo: {tag}")
        else:
            self._print_log(f"[Sistema] Modelo '{tag}' no instalado. Presiona '⬇️ Descargar' para obtenerlo.")

    def _descargar_modelo(self):
        display_name = self.combo_modelos.get()
        tag = self._model_display_to_tag.get(display_name, display_name)

        if self._modelo_instalado(tag):
            self.motor_ia.command_queue.put(("switch_model", tag))
            self._print_log(f"[Sistema] '{tag}' ya está instalado. Activado.")
            return

        info = MODELS_CATALOG.get(tag, {})
        size = info.get('size_gb', '?')
        confirmar = messagebox.askyesno(
            "Descargar Modelo",
            f"Descargar '{tag}'?\n\nTamaño aprox: {size} GB\n{info.get('desc', '')}\n\nEsto puede tardar varios minutos."
        )
        if confirmar:
            self.motor_ia.command_queue.put(("download_model", tag))

    def _modelo_instalado(self, model_tag):
        try:
            import ollama
            for mod in ollama.list().models:
                if mod.model == model_tag or mod.model == f"{model_tag}:latest":
                    return True
                if ':' not in model_tag and mod.model.startswith(model_tag + ':'):
                    return True
            return False
        except Exception:
            return False

    def _actualizar_info_modelo(self, model_tag):
        info = MODELS_CATALOG.get(model_tag, {})
        desc = info.get('desc', 'Modelo personalizado')
        size = info.get('size_gb', '?')
        installed = "✅" if self._modelo_instalado(model_tag) else "❌ No instalado"
        self.lbl_modelo_info.configure(text=f"{desc} ({size}GB) {installed}")

    def _refresh_modelo_instalado(self):
        display_name = self.combo_modelos.get()
        tag = self._model_display_to_tag.get(display_name, display_name)
        self._actualizar_info_modelo(tag)

    def _al_cambiar_motor_tts(self):
        motor_seleccionado = self.switch_modo_ligero.get()
        self.motor_ia.command_queue.put(("set_motor_tts", motor_seleccionado))
        modo_texto = "Ligero" if motor_seleccionado == "ligero" else "Pesado"
        self.switch_modo_ligero.configure(text=f"🎛️ TTS: {modo_texto}")
        if hasattr(self, "lbl_tts_status_pill"):
            self.lbl_tts_status_pill.configure(text="TTS: idle", fg_color="#1b2633")
        if hasattr(self, "lbl_kira_tts_state"):
            self.lbl_kira_tts_state.configure(text="TTS: idle", fg_color="#1b2633")

    def _al_seleccionar_perfil(self, nombre):
        if nombre in self.perfiles:
            self.motor_ia.command_queue.put(("set_profile", self.perfiles[nombre]))

    def _abrir_configurador_perfiles(self):
        ventana = ConfiguradorPerfiles(self, self.perfiles, self._on_perfiles_guardados)
        ventana.grab_set()

    def _on_perfiles_guardados(self, nuevos_perfiles):
        self.perfiles = nuevos_perfiles
        guardar_perfiles(self.perfiles)
        
        nombres = list(self.perfiles.keys())
        self.combo_perfiles.configure(values=nombres)
        
        actual = self.combo_perfiles.get()
        if actual not in nombres:
            actual = nombres[0]
            self.combo_perfiles.set(actual)
            
        self._al_seleccionar_perfil(actual)
        self._print_log("[Sistema] 💾 Perfiles actualizados y guardados.")

    def _obtener_dispositivos_entrada(self):
        dispositivos_validos = []
        try:
            dispositivos = sd.query_devices()
            for i, d in enumerate(dispositivos):
                if d['max_input_channels'] > 0:
                    dispositivos_validos.append(f"{i}: {d['name']}")
        except Exception as e:
            logger.error(f"No se pudieron listar dispositivos de audio: {e}")
        return dispositivos_validos

    def _al_seleccionar_dispositivo(self, seleccion):
        try:
            self.dispositivo_seleccionado = int(seleccion.split(":")[0])
            if hasattr(self, "lbl_mic_status_pill"):
                self.lbl_mic_status_pill.configure(text="Mic: conectado", fg_color="#1b2633")
            if hasattr(self, "lbl_kira_voice_state"):
                self.lbl_kira_voice_state.configure(text="Voz/PTT: listo", fg_color="#1b2633")
            self._print_log(f"[Sistema] Fuente de audio: ID {self.dispositivo_seleccionado}")
        except (ValueError, IndexError):
            self.dispositivo_seleccionado = None
            if hasattr(self, "lbl_mic_status_pill"):
                self.lbl_mic_status_pill.configure(text="Mic: desconectado", fg_color="#4a2630")
            if hasattr(self, "lbl_kira_voice_state"):
                self.lbl_kira_voice_state.configure(text="Voz/PTT: sin mic", fg_color="#4a2630")

    def _iniciar_grabacion(self):
        if self.dispositivo_seleccionado is None:
            messagebox.showwarning("Atención", "Selecciona una fuente de audio primero.")
            return

        dialog = ctk.CTkInputDialog(
            text=(
                f"Grabarás {RECORDING_DURATION} segundos de audio para calibrar la voz.\n"
                "Habla con tu tono natural.\n\n"
                "Presiona OK para empezar."
            ),
            title="Confirmar Grabación"
        )
        res = dialog.get_input()

        if res is not None:
            threading.Thread(target=self._hilo_grabacion, daemon=True).start()
        else:
            self._print_log("[Grabación] Acción cancelada.")

    def _hilo_grabacion(self):
        filepath = os.path.join(BASE_DIR, "referencia_grabada.wav")

        self._print_log(f"\n[Grabación] 🔴 GRABANDO {RECORDING_DURATION}s... Habla ahora.")
        self.after(0, lambda: self.btn_grabar.configure(
            state="disabled", text="Grabando...", fg_color="darkred"
        ))

        try:
            recording = sd.rec(
                int(RECORDING_DURATION * RECORDING_SAMPLERATE),
                samplerate=RECORDING_SAMPLERATE,
                channels=1,
                dtype='float32',
                device=self.dispositivo_seleccionado
            )
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
                response["use"] = messagebox.askyesno(
                    "Grabación Finalizada",
                    "Audio capturado correctamente.\n\n¿Usar como voz de referencia para la IA?"
                )
                response_ready.set()

            self.after(0, ask_use_audio)
            if not response_ready.wait(timeout=120):
                self._print_log("[Grabación] Confirmación agotó tiempo; audio descartado por seguridad.")
            usar_audio = response["use"]

            if usar_audio:
                self._print_log("[Sistema] Perfil de voz enviado a la IA.")
                self.motor_ia.command_queue.put(("set_voice", filepath))
                self.after(0, lambda: self.btn_ws.configure(
                    state="normal", fg_color="#555555"
                ))
                self.after(0, lambda: self.btn_enviar.configure(state="normal"))
            else:
                self._print_log("[Grabación] ❌ Descartada.")
                if os.path.exists(filepath):
                    os.remove(filepath)

        except Exception as e:
            self._print_log(f"[ERROR Grabación]: {e}")
            logger.exception("Error durante grabación")
        finally:
            self.after(0, lambda: self.btn_grabar.configure(
                state="normal", text="🎤 Grabar", fg_color="#555555"
            ))

    def _cargar_voz(self):
        filepath = filedialog.askopenfilename(
            title="Seleccionar muestra de voz",
            filetypes=[("Audio WAV", "*.wav")]
        )
        if filepath:
            try:
                data, sr = sf.read(filepath)
                duration = len(data) / sr
                if duration < 2.0:
                    messagebox.showwarning("Audio muy corto", "El audio debe durar al menos 2 segundos.")
                    return
                if duration > 30.0:
                    messagebox.showwarning("Audio muy largo", "El audio no debe durar más de 30 segundos.")
                    return
            except Exception as e:
                messagebox.showerror("Error", f"No se pudo leer el archivo de audio:\n{e}")
                return

            self.motor_ia.command_queue.put(("set_voice", filepath))
            self.btn_ws.configure(state="normal", fg_color="#555555")
            self.btn_enviar.configure(state="normal")
            self._print_log(f"[Sistema] Perfil de voz cargado ({duration:.1f}s).")

    def _enviar_contexto_manual(self):
        texto = self.entry_chat.get().strip()
        if texto:
            self._print_log(f"\n[Tú]: {texto}")
            self.motor_ia.command_queue.put(("process_context", texto))
            self.entry_chat.delete(0, 'end')

    def _limpiar_historial(self):
        if hasattr(self, "lbl_memory_status_pill"):
            self.lbl_memory_status_pill.configure(text="Memoria: limpiando", fg_color="#5f461b")
        if hasattr(self, "lbl_kira_memory_state"):
            self.lbl_kira_memory_state.configure(text="Memoria: limpiando", fg_color="#5f461b")
        self.motor_ia.command_queue.put(("clear_history", None))
        self._print_log("[Sistema] 🗑️ Memoria de conversación limpiada.")
        self.after(800, lambda: self.lbl_memory_status_pill.configure(text="Memoria: disponible", fg_color="#1b2633") if hasattr(self, "lbl_memory_status_pill") else None)
        self.after(800, lambda: self.lbl_kira_memory_state.configure(text="Memoria: disponible", fg_color="#1b2633") if hasattr(self, "lbl_kira_memory_state") else None)

    # ──────────────────────────────────────────────
    # PTT — Push-to-Talk (gate sobre WebSocket)
    # ──────────────────────────────────────────────

    def _al_toggle_ptt(self):
        nuevo_estado = self.switch_ptt.get()
        if nuevo_estado == self.ptt_enabled:
            logger.debug(f"[PTT] Toggle ignorado (ya en estado {nuevo_estado})")
            return
        self.ptt_enabled = nuevo_estado
        logger.debug(f"[PTT] Toggle: ptt_enabled={self.ptt_enabled}")
        if self.ptt_enabled:
            self.switch_ptt.configure(text="PTT ON")
            if not self._ptt_mapping:
                self._start_ptt_listener()
                self._set_ptt_status(
                    f"Manten presionado [{self.ptt_hotkey}] para hablar",
                    "#888888"
                )
            self._print_log(f"[PTT] Activado — hotkey: {self.ptt_hotkey}")
        else:
            self.switch_ptt.configure(text="PTT OFF")
            self._stop_ptt_listener()
            for lst in getattr(self, '_ptt_mapping_listeners', []):
                try:
                    lst.stop()
                except Exception:
                    pass
            self._set_ptt_status("", "#888888")
            self._print_log("[PTT] Desactivado — modo continuo WebSocket")

    def _set_ptt_status(self, text, color):
        def update():
            self.lbl_ptt_status.configure(text=text, text_color=color)
            if hasattr(self, "lbl_mic_status_pill") and text:
                if "ESCUCHANDO" in text:
                    self.lbl_mic_status_pill.configure(text="PTT: activo")
                else:
                    self.lbl_mic_status_pill.configure(text="PTT: listo")
        self.after(0, update)

    def _mapear_hotkey(self):
        if self._ptt_mapping:
            return

        self._ptt_mapping = True
        self._ptt_mapping_listeners = []
        self.btn_mapear.configure(text="Escuchando...", state="disabled", fg_color="#cc8800")
        self._set_ptt_status("Presiona la tecla o boton deseado...", "#cc8800")

        kb_listener = keyboard.Listener(on_press=self._on_mapear_key)
        kb_listener.daemon = True
        kb_listener.start()

        ms_listener = mouse.Listener(on_click=self._on_mapear_mouse)
        ms_listener.daemon = True
        ms_listener.start()

        self._ptt_mapping_listeners = [kb_listener, ms_listener]
        self._print_log("[PTT] Modo mapeo: esperando pulsacion...")

    def _on_mapear_key(self, key):
        display = _PTT_REVERSE_MAP.get(key)
        if display:
            self._save_mapped_hotkey(display)
            return False

    def _on_mapear_mouse(self, x, y, button, pressed):
        if pressed:
            display = _PTT_REVERSE_MAP.get(button)
            if display:
                self._save_mapped_hotkey(display)
                return False

    def _save_mapped_hotkey(self, hotkey):
        self.ptt_hotkey = hotkey
        _guardar_ptt_config(hotkey)

        self.after(0, lambda: self.lbl_hotkey.configure(text=hotkey))
        self.after(0, lambda: self.btn_mapear.configure(
            text="Mapear", state="normal",
            fg_color="#555555"
        ))
        self._print_log(f"[PTT] Tecla mapeada y guardada: {hotkey}")

        for lst in self._ptt_mapping_listeners:
            try:
                lst.stop()
            except Exception:
                pass
        self._ptt_mapping_listeners = []
        self._ptt_mapping = False

        if self.ptt_enabled:
            self._start_ptt_listener()
            self._set_ptt_status(
                f"Manten presionado [{hotkey}] para hablar",
                "#888888"
            )

    def _build_ptt_target(self):
        name = self.ptt_hotkey
        if name in _PTT_KB_MAP:
            return ("keyboard", _PTT_KB_MAP[name])
        if name in _PTT_MOUSE_MAP:
            return ("mouse", _PTT_MOUSE_MAP[name])
        return (None, None)

    def _start_ptt_listener(self):
        with self._ptt_lock:
            self._stop_ptt_listener()
            kind, target = self._build_ptt_target()
            if kind == "keyboard":
                self.ptt_listener = keyboard.Listener(
                    on_press=self._on_ptt_press,
                    on_release=self._on_ptt_release
                )
            elif kind == "mouse":
                self.ptt_listener = mouse.Listener(
                    on_click=self._on_ptt_click
                )
            else:
                logger.warning(f"[PTT] Tecla no soportada: {self.ptt_hotkey}")
                return
            self.ptt_listener.daemon = True
            self._ptt_target = (kind, target)
            self.ptt_listener.start()
            logger.debug(f"[PTT] Listener iniciado: kind={kind} target={target}")

    def _stop_ptt_listener(self):
        with self._ptt_lock:
            if self.ptt_listener:
                try:
                    self.ptt_listener.stop()
                except Exception as e:
                    logger.debug(f"[PTT] Error stopping listener: {e}")
                self.ptt_listener = None
            self.ptt_pressed = False
            import traceback
            logger.debug(f"[PTT] Listener detenido\n{traceback.format_stack()[-3].strip()}")

    def _ensure_ptt_listener(self):
        with self._ptt_lock:
            if self.ptt_enabled and self.ptt_listener is None and not self._ptt_mapping:
                logger.debug("[PTT] Reconciliando listener...")
                # Don't call _start_ptt_listener here (it would acquire lock again)
                kind, target = self._build_ptt_target()
                if kind == "keyboard":
                    self.ptt_listener = keyboard.Listener(
                        on_press=self._on_ptt_press,
                        on_release=self._on_ptt_release
                    )
                elif kind == "mouse":
                    self.ptt_listener = mouse.Listener(
                        on_click=self._on_ptt_click
                    )
                else:
                    return
                self.ptt_listener.daemon = True
                self._ptt_target = (kind, target)
                self.ptt_listener.start()
                logger.debug(f"[PTT] Listener reconciliado: kind={kind} target={target}")

    def _on_ptt_press(self, key):
        kind, target = getattr(self, '_ptt_target', (None, None))
        if kind == "keyboard" and key == target and not self.ptt_pressed:
            self.ptt_pressed = True
            self._ptt_accept_logged = False
            logger.debug(f"[PTT] MATCH press: {key}")
            self._set_ptt_status("🔴 ESCUCHANDO...", "#44ff44")
            self._actualizar_pipeline("listening")

    def _on_ptt_release(self, key):
        kind, target = getattr(self, '_ptt_target', (None, None))
        if kind == "keyboard" and key == target and self.ptt_pressed:
            self.ptt_pressed = False
            self._ptt_accept_logged = False
            logger.debug(f"[PTT] MATCH release: {key}")
            self._set_ptt_status(
                f"Manten presionado [{self.ptt_hotkey}] para hablar",
                "#888888"
            )
            self._actualizar_pipeline("idle")

    def _on_ptt_click(self, x, y, button, pressed):
        kind, target = getattr(self, '_ptt_target', (None, None))
        if kind == "mouse" and button == target:
            self.ptt_pressed = pressed
            if not pressed:
                self._ptt_accept_logged = False
            logger.debug(f"[PTT] MOUSE {'PRESS' if pressed else 'RELEASE'}: {button} → ptt_pressed={pressed}")
            if pressed:
                self._set_ptt_status("🔴 ESCUCHANDO...", "#44ff44")
                self._actualizar_pipeline("listening")
            else:
                self._set_ptt_status(
                    f"Manten presionado [{self.ptt_hotkey}] para hablar",
                    "#888888"
                )
                self._actualizar_pipeline("idle")

    # ─── Fin PTT ───

    def _toggle_websocket(self):
        if not self.ws_connected:
            self.ws_connected = True
            self.ws_should_reconnect = True
            self.btn_ws.configure(text="Desconectar", fg_color="darkred")
            if hasattr(self, "btn_primary_voice"):
                self.btn_primary_voice.configure(text="Detener", fg_color="darkred", hover_color="#8b1a1a")
            self.ws_thread = threading.Thread(target=self._run_ws_client, daemon=True)
            self.ws_thread.start()
        else:
            self.ws_connected = False
            self.ws_should_reconnect = False
            self.btn_ws.configure(text="Conectar LiveAudio", fg_color="#555555")
            if hasattr(self, "btn_primary_voice"):
                self.btn_primary_voice.configure(text="Hablar", fg_color="#1f7a5a", hover_color="#24946c")
            self._print_log("[Red] Desconexión solicitada.")

    def _run_ws_client(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ws_reconnect_loop())
        finally:
            loop.close()

    async def _ws_reconnect_loop(self):
        retry_count = 0
        delay = WS_RECONNECT_BASE_DELAY

        while self.ws_should_reconnect and retry_count < WS_MAX_RETRIES:
            try:
                await self._ws_listener()
                if not self.ws_should_reconnect:
                    break
            except Exception as e:
                retry_count += 1
                logger.warning(f"WebSocket error (intento {retry_count}/{WS_MAX_RETRIES}): {e}")
                self.log_queue.put(
                    f"[Red] ⚠️ Conexión perdida. Reintentando en {delay:.0f}s... ({retry_count}/{WS_MAX_RETRIES})"
                )

                if not self.ws_should_reconnect:
                    break

                await asyncio.sleep(delay)
                delay = min(delay * 2, WS_RECONNECT_MAX_DELAY)

        if retry_count >= WS_MAX_RETRIES:
            self.log_queue.put("[Red] ❌ Máximo de reintentos alcanzado. Desconectado.")
            logger.error("WebSocket: máximo de reintentos alcanzado.")

        self.ws_connected = False
        self.ws_should_reconnect = False
        self.after(0, lambda: self.btn_ws.configure(
            text="Conectar LiveAudio", fg_color="#555555"
        ))
        self.after(0, lambda: self.btn_primary_voice.configure(
            text="Hablar", fg_color="#1f7a5a", hover_color="#24946c"
        ) if hasattr(self, "btn_primary_voice") else None)

    async def _ws_listener(self):
        self.log_queue.put(f"[Red] Conectando a LiveAudio en {WS_URI}...")

        async with websockets.connect(
            WS_URI,
            ping_interval=20,
            ping_timeout=10,
            close_timeout=5,
        ) as websocket:
            self.log_queue.put("[Red] 🟢 Conectado. Escuchando transcripciones...")
            logger.info("WebSocket conectado.")
            self._actualizar_pipeline("listening")

            while self.ws_connected:
                try:
                    mensaje = await asyncio.wait_for(
                        websocket.recv(),
                        timeout=WS_TIMEOUT
                    )
                    data = json.loads(mensaje)
                    texto_transcrito = data.get("text", "").strip()

                    if texto_transcrito:
                        logger.debug(f"WS recibido ({len(texto_transcrito)} chars): {texto_transcrito[:60]}...")

                    if self.ptt_enabled and not self.ptt_pressed:
                        if texto_transcrito:
                            self.log_queue.put(f"[PTT] Descartado (tecla no presionada)")
                        continue

                    if self.ptt_enabled and self.ptt_pressed:
                        if not getattr(self, '_ptt_accept_logged', False):
                            self._ptt_accept_logged = True
                            logger.debug("[PTT] Gate abierto: aceptando transcripciones")

                    if self.motor_ia.is_speaking or self.motor_ia.is_processing:
                        if texto_transcrito:
                            logger.debug(f"WS descartado (IA ocupada): {texto_transcrito[:40]}...")
                        continue

                    palabras = texto_transcrito.split()
                    if len(palabras) < 4:
                        if texto_transcrito:
                            logger.debug(f"WS descartado (muy corto, {len(palabras)} palabras): {texto_transcrito}")
                        continue

                    if texto_transcrito:
                        self.log_queue.put(f"[LiveAudio]: {texto_transcrito}")
                        self.motor_ia.command_queue.put((
                            "process_context",
                            f"El streamer acaba de decir: {texto_transcrito}"
                        ))
                        logger.info(f"Transcripción aceptada: {texto_transcrito[:80]}...")

                except asyncio.TimeoutError:
                    continue
                except json.JSONDecodeError as e:
                    logger.warning(f"JSON inválido del WebSocket: {e}")
                except websockets.exceptions.ConnectionClosed as e:
                    logger.warning(f"WebSocket cerrado: {e}")
                    raise

    def _update_kira_response_panel(self, msg):
        if not hasattr(self, "text_kira_response") or self.text_kira_response is None:
            return
        if "[Kira]:" not in msg:
            return
        response = msg.strip()
        response = response.replace("🧠 ", "")
        self.text_kira_response.configure(state="normal")
        self.text_kira_response.delete("1.0", "end")
        self.text_kira_response.insert("end", response + "\n")
        self.text_kira_response.configure(state="disabled")

    def _print_log(self, msg):
        self._update_kira_response_panel(msg)
        if hasattr(self, 'switch_logs') and not self.switch_logs.get():
            return
        if self.consola is None:
            return
        self._append_limited_textbox(self.consola, msg, max_lines=1500)

    def _append_limited_textbox(self, widget, line, max_lines=1000):
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

    def _process_logs(self):
        while True:
            try:
                msg = self.log_queue.get_nowait()
                self._print_log(msg)
            except queue.Empty:
                break
        self.after(100, self._process_logs)

    def on_closing(self):
        logger.info("Cerrando aplicación...")
        self.ws_connected = False
        self.ws_should_reconnect = False

        self._stop_ptt_listener()

        try:
            if self.smart_agg:
                self.smart_agg.disconnect()
        except Exception as e:
            logger.warning(f"No se pudo desconectar Smart Aggregator: {e}")

        try:
            if self.stream_admin_chat_connected:
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
            import ollama
            model_to_unload = getattr(self.motor_ia, 'current_model', None)
            if model_to_unload:
                logger.info(f"Liberando modelo {model_to_unload} de la memoria...")
                ollama.generate(model=model_to_unload, prompt='', keep_alive=0)
        except Exception as e:
            logger.warning(f"No se pudo liberar memoria al salir: {e}")

        self.motor_ia.command_queue.put(None)

        try:
            for f in os.listdir(TEMP_DIR):
                fpath = os.path.join(TEMP_DIR, f)
                try:
                    os.remove(fpath)
                except OSError:
                    pass
        except Exception:
            pass

        self.destroy()
