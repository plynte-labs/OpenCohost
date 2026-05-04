import os
import queue
import threading
import json
import asyncio
import time
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
        
        self.perfiles = cargar_perfiles()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        self.lista_dispositivos = self._obtener_dispositivos_entrada()
        self._build_ui()

        self.motor_ia = MotorVocalIA(self.log_queue, self._on_motor_event)
        self.motor_ia.start()

        self.after(100, self._process_logs)
        self.after(500, self._aplicar_perfil_actual)
        self._print_log(f"[Sistema] PTT hotkey cargada: {self.ptt_hotkey}")
        logger.info("Aplicación VoiceAI iniciada.")

    def _aplicar_perfil_actual(self):
        nombre = self.combo_perfiles.get()
        if nombre in self.perfiles:
            self.motor_ia.command_queue.put(("set_profile", self.perfiles[nombre]))

    def _build_ui(self):
        frame_top = ctk.CTkFrame(self)
        frame_top.grid(row=0, column=0, sticky="ew", padx=10, pady=10)

        self.combo_dispositivos = ctk.CTkOptionMenu(
            frame_top,
            values=self.lista_dispositivos,
            command=self._al_seleccionar_dispositivo,
            width=250
        )
        self.combo_dispositivos.pack(side="left", padx=5)
        if self.lista_dispositivos:
            self.combo_dispositivos.set(self.lista_dispositivos[0])
            self.dispositivo_seleccionado = int(self.lista_dispositivos[0].split(":")[0])
        else:
            self.combo_dispositivos.set("Sin dispositivos de audio")

        self.btn_grabar = ctk.CTkButton(
            frame_top, text="🎤 Grabar", command=self._iniciar_grabacion,
            state="disabled", width=90
        )
        self.btn_grabar.pack(side="left", padx=5)

        self.btn_voz = ctk.CTkButton(
            frame_top, text="📂 Cargar (.wav)", command=self._cargar_voz,
            state="disabled", fg_color="gray", width=110
        )
        self.btn_voz.pack(side="left", padx=5)

        self.btn_ws = ctk.CTkButton(
            frame_top, text="Conectar LiveAudio", command=self._toggle_websocket,
            fg_color="gray", state="disabled"
        )
        self.btn_ws.pack(side="left", padx=5)

        self.btn_clear = ctk.CTkButton(
            frame_top, text="🗑️ Limpiar Memoria", command=self._limpiar_historial,
            width=130, fg_color="#555555", hover_color="#777777"
        )
        self.btn_clear.pack(side="left", padx=5)
        
        self.switch_logs = ctk.CTkSwitch(
            frame_top, text="Mostrar Logs",
            onvalue=True, offvalue=False
        )
        self.switch_logs.pack(side="left", padx=10)
        self.switch_logs.select()

        self.lbl_status = ctk.CTkLabel(frame_top, text="🟢 En Espera", font=ctk.CTkFont(size=13, weight="bold"))
        self.lbl_status.pack(side="right", padx=10)

        # ── Barra de audio RMS ──
        self.barra_rms = ctk.CTkProgressBar(frame_top, width=120, height=8)
        self.barra_rms.set(0)
        self.barra_rms.pack(side="right", padx=5)
        self.barra_rms.pack_forget()

        frame_model = ctk.CTkFrame(self)
        frame_model.grid(row=1, column=0, sticky="ew", padx=10, pady=(0, 5))

        ctk.CTkLabel(frame_model, text="🧠 Modelo:", font=ctk.CTkFont(size=13, weight="bold")).pack(side="left", padx=(5, 3))

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
            width=200
        )
        self.combo_modelos.set(default_display)
        self.combo_modelos.pack(side="left", padx=3)

        self.btn_download = ctk.CTkButton(
            frame_model, text="⬇️ Descargar", command=self._descargar_modelo,
            width=110, fg_color="#2d7d46", hover_color="#3a9e5a"
        )
        self.btn_download.pack(side="left", padx=3)

        self.lbl_modelo_info = ctk.CTkLabel(
            frame_model, text="", font=ctk.CTkFont(size=11),
            text_color="#aaaaaa"
        )
        self.lbl_modelo_info.pack(side="left", padx=10)
        self._actualizar_info_modelo(DEFAULT_MODEL)

        self.switch_modo_ligero = ctk.CTkSwitch(
            frame_model, text="🎮 Modo Juego Pesado",
            onvalue="ligero", offvalue="pesado",
            command=self._al_cambiar_motor_tts
        )
        self.switch_modo_ligero.pack(side="right", padx=10)
        self.switch_modo_ligero.select()

        self.progress_download = ctk.CTkProgressBar(frame_model, width=150)
        self.progress_download.pack(side="right", padx=10)
        self.progress_download.set(0)
        self.progress_download.pack_forget()

        frame_profile = ctk.CTkFrame(self)
        frame_profile.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 5))

        ctk.CTkLabel(frame_profile, text="🎭 Perfil/Prompt:", font=ctk.CTkFont(size=13, weight="bold")).pack(side="left", padx=(5, 3))
        
        self.combo_perfiles = ctk.CTkOptionMenu(
            frame_profile,
            values=list(self.perfiles.keys()),
            command=self._al_seleccionar_perfil,
            width=200
        )
        default_perfil = "Kira (Default)" if "Kira (Default)" in self.perfiles else list(self.perfiles.keys())[0]
        self.combo_perfiles.set(default_perfil)
        self.combo_perfiles.pack(side="left", padx=3)

        self.btn_editar_perfiles = ctk.CTkButton(
            frame_profile, text="✏️ Editar Perfiles", command=self._abrir_configurador_perfiles,
            width=130, fg_color="#3B8ED0", hover_color="#1F6AA5"
        )
        self.btn_editar_perfiles.pack(side="left", padx=3)

        # ── Frame PTT ──
        frame_ptt = ctk.CTkFrame(self)
        frame_ptt.grid(row=3, column=0, sticky="ew", padx=10, pady=(0, 5))

        ctk.CTkLabel(frame_ptt, text="🎙️ PTT:", font=ctk.CTkFont(size=13, weight="bold")).pack(side="left", padx=(5, 3))

        self.switch_ptt = ctk.CTkSwitch(
            frame_ptt, text="PTT OFF",
            command=self._al_toggle_ptt,
            onvalue=True, offvalue=False
        )
        self.switch_ptt.pack(side="left", padx=3)

        ctk.CTkLabel(frame_ptt, text="Tecla:", font=ctk.CTkFont(size=12)).pack(side="left", padx=(10, 3))

        self.lbl_hotkey = ctk.CTkLabel(
            frame_ptt, text=self.ptt_hotkey,
            font=ctk.CTkFont(size=13, weight="bold"),
            width=80
        )
        self.lbl_hotkey.pack(side="left", padx=3)

        self.btn_mapear = ctk.CTkButton(
            frame_ptt, text="Mapear", command=self._mapear_hotkey,
            width=70, fg_color=["#3B8ED0", "#1F6AA5"]
        )
        self.btn_mapear.pack(side="left", padx=3)

        self.lbl_ptt_status = ctk.CTkLabel(
            frame_ptt, text="", font=ctk.CTkFont(size=12),
            text_color="#888888"
        )
        self.lbl_ptt_status.pack(side="left", padx=10)

        self.consola = None  # will be the Log tab textbox

        self.tabview = ctk.CTkTabview(
            self,
            command=self._on_tab_change,
            height=1
        )
        self.tabview.grid(row=4, column=0, sticky="nsew", padx=10, pady=(0, 10))

        tab_log = self.tabview.add("📋 Log General")
        tab_acciones = self.tabview.add("📝 Kira Acciones")

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

        for accion in _cargar_acciones():
            self.consola_acciones.configure(state="normal")
            self.consola_acciones.insert("end", accion + "\n")
            self.consola_acciones.configure(state="disabled")
        self.consola_acciones.see("end")

        frame_bottom = ctk.CTkFrame(self)
        frame_bottom.grid(row=5, column=0, sticky="ew", padx=10, pady=(0, 10))
        frame_bottom.grid_columnconfigure(0, weight=1)

        self.entry_chat = ctk.CTkEntry(
            frame_bottom,
            placeholder_text="Escribe un mensaje para Kira (contexto o pregunta)..."
        )
        self.entry_chat.grid(row=0, column=0, sticky="ew", padx=(5, 5), pady=5)
        self.entry_chat.bind("<Return>", lambda e: self._enviar_contexto_manual())

        self.btn_enviar = ctk.CTkButton(
            frame_bottom, text="Enviar a IA",
            command=self._enviar_contexto_manual,
            width=100, state="disabled"
        )
        self.btn_enviar.grid(row=0, column=1, padx=(0, 5), pady=5)

    def _actualizar_pipeline(self, estado):
        self._pipeline_state = estado
        estados = {
            "idle": ("🟢 En Espera", "#aaaaaa"),
            "listening": ("🟢 Escuchando", "#44ff44"),
            "processing": ("🟡 Procesando LLM", "#ffaa00"),
            "speaking": ("🔵 Sintetizando Voz", "#4488ff"),
            "playing": ("🔵 Hablando", "#44ccff"),
            "downloading": ("📥 Descargando Modelo", "#ff8800"),
            "init": ("⏳ Inicializando IA...", "#888888"),
        }
        text, color = estados.get(estado, ("", "#aaaaaa"))
        self.after(0, lambda t=text, c=color: (
            self.lbl_status.configure(text=t, text_color=c)
        ))

        if estado == "listening":
            self.after(0, lambda: self.barra_rms.pack(side="right", padx=5))
            self._animar_rms()
        else:
            self.after(0, lambda: self.barra_rms.pack_forget())

    def _animar_rms(self):
        if self._pipeline_state != "listening":
            return
        import random
        nivel = random.uniform(0.2, 0.9)
        self.barra_rms.set(nivel)
        self.after(150, self._animar_rms)

    def _log_accion(self, msg):
        ts = time.strftime("%H:%M:%S")
        entrada = f"[{ts}] {msg}"
        self.consola_acciones.configure(state="normal")
        self.consola_acciones.insert("end", entrada + "\n")
        self.consola_acciones.see("end")
        self.consola_acciones.configure(state="disabled")
        _guardar_accion(msg)

    def _on_tab_change(self):
        pass

    def _on_motor_event(self, status):
        if status == "ready":
            self.after(0, lambda: self.btn_grabar.configure(state="normal"))
            self.after(0, lambda: self.btn_voz.configure(state="normal"))
            self.after(0, lambda: self.btn_ws.configure(state="normal"))
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
            self.after(0, lambda: self.progress_download.pack(side="right", padx=10))
            self.after(0, lambda: self.progress_download.set(0))
            self._actualizar_pipeline("downloading")
        elif status == "download_done":
            model = self.motor_ia.current_model
            self.after(0, lambda: self.btn_download.configure(state="normal", text="⬇️ Descargar"))
            self.after(0, lambda: self.combo_modelos.configure(state="normal"))
            self.after(0, lambda: self.progress_download.pack_forget())
            self.after(0, lambda: self.title(f"VocalAI — Qwen3-TTS + {model}"))
            self.after(0, lambda: self._actualizar_info_modelo(model))
            self._actualizar_pipeline("idle")
            self.after(0, self._refresh_modelo_instalado)
        elif status == "download_error":
            self.after(0, lambda: self.btn_download.configure(state="normal", text="⬇️ Descargar"))
            self.after(0, lambda: self.combo_modelos.configure(state="normal"))
            self.after(0, lambda: self.progress_download.pack_forget())
            self._actualizar_pipeline("idle")

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
            self._print_log(f"[Sistema] Fuente de audio: ID {self.dispositivo_seleccionado}")
        except (ValueError, IndexError):
            self.dispositivo_seleccionado = None

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

            usar_audio = messagebox.askyesno(
                "Grabación Finalizada",
                "Audio capturado correctamente.\n\n¿Usar como voz de referencia para la IA?"
            )

            if usar_audio:
                self._print_log("[Sistema] Perfil de voz enviado a la IA.")
                self.motor_ia.command_queue.put(("set_voice", filepath))
                self.after(0, lambda: self.btn_ws.configure(
                    state="normal", fg_color=["#3B8ED0", "#1F6AA5"]
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
                state="normal", text="🎤 Grabar", fg_color=["#3B8ED0", "#1F6AA5"]
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
            self.btn_ws.configure(state="normal", fg_color=["#3B8ED0", "#1F6AA5"])
            self.btn_enviar.configure(state="normal")
            self._print_log(f"[Sistema] Perfil de voz cargado ({duration:.1f}s).")

    def _enviar_contexto_manual(self):
        texto = self.entry_chat.get().strip()
        if texto:
            self._print_log(f"\n[Tú]: {texto}")
            self.motor_ia.command_queue.put(("process_context", texto))
            self.entry_chat.delete(0, 'end')

    def _limpiar_historial(self):
        self.motor_ia.command_queue.put(("clear_history", None))
        self._print_log("[Sistema] 🗑️ Memoria de conversación limpiada.")

    # ──────────────────────────────────────────────
    # PTT — Push-to-Talk (gate sobre WebSocket)
    # ──────────────────────────────────────────────

    def _al_toggle_ptt(self):
        self.ptt_enabled = self.switch_ptt.get()
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
        self.after(0, lambda: self.lbl_ptt_status.configure(text=text, text_color=color))

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
            fg_color=["#3B8ED0", "#1F6AA5"]
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
            self.ws_thread = threading.Thread(target=self._run_ws_client, daemon=True)
            self.ws_thread.start()
        else:
            self.ws_connected = False
            self.ws_should_reconnect = False
            self.btn_ws.configure(text="Conectar LiveAudio", fg_color=["#3B8ED0", "#1F6AA5"])
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
            text="Conectar LiveAudio", fg_color=["#3B8ED0", "#1F6AA5"]
        ))

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

    def _print_log(self, msg):
        if hasattr(self, 'switch_logs') and not self.switch_logs.get():
            return
        self.consola.configure(state="normal")
        self.consola.insert("end", msg + "\n")
        self.consola.see("end")
        self.consola.configure(state="disabled")

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
