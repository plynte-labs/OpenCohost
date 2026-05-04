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
    PTT_DEFAULT_HOTKEY, PTT_MIN_DURATION, PTT_MAX_DURATION,
    PTT_RMS_THRESHOLD, PTT_HOTKEY_LIST
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

logger = get_logger()

class VocalAIApp(ctk.CTk):
    """
    Interfaz principal del Cliente VoiceAI.
    """
    def __init__(self):
        super().__init__()
        self.title(f"VocalAI — Qwen3-TTS + {DEFAULT_MODEL}")
        self.geometry("1100x700")
        self.minsize(800, 500)

        self.log_queue = queue.Queue()
        self.ws_connected = False
        self.ws_should_reconnect = False
        self.dispositivo_seleccionado = None

        self.ptt_enabled = False
        self.ptt_hotkey = PTT_DEFAULT_HOTKEY
        self.ptt_pressed = False
        self.ptt_listener = None
        self._ptt_target = None
        
        self.perfiles = cargar_perfiles()

        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(4, weight=1)

        self.lista_dispositivos = self._obtener_dispositivos_entrada()
        self._build_ui()

        self.motor_ia = MotorVocalIA(self.log_queue, self._on_motor_event)
        self.motor_ia.start()

        self.after(100, self._process_logs)
        self.after(500, self._aplicar_perfil_actual)
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

        self.lbl_status = ctk.CTkLabel(frame_top, text="⏳ Inicializando IA...")
        self.lbl_status.pack(side="right", padx=10)

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

        self.combo_hotkey = ctk.CTkOptionMenu(
            frame_ptt,
            values=PTT_HOTKEY_LIST,
            command=self._al_seleccionar_hotkey,
            width=100
        )
        self.combo_hotkey.set(PTT_DEFAULT_HOTKEY)
        self.combo_hotkey.pack(side="left", padx=10)
        self.combo_hotkey.configure(state="disabled")

        self.lbl_ptt_status = ctk.CTkLabel(
            frame_ptt, text="", font=ctk.CTkFont(size=12),
            text_color="#888888"
        )
        self.lbl_ptt_status.pack(side="left", padx=10)

        self.consola = ctk.CTkTextbox(
            self, font=ctk.CTkFont(family="Consolas", size=13),
            state="disabled"
        )
        self.consola.grid(row=4, column=0, sticky="nsew", padx=10, pady=(0, 10))

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

    def _on_motor_event(self, status):
        if status == "ready":
            self.after(0, lambda: self.btn_grabar.configure(state="normal"))
            self.after(0, lambda: self.btn_voz.configure(state="normal"))
            self.after(0, lambda: self.btn_ws.configure(state="normal"))
            self.after(0, lambda: self.btn_enviar.configure(state="normal"))
            self.after(0, lambda: self.lbl_status.configure(text="✅ Motor IA Listo"))
            self.after(0, self._refresh_modelo_instalado)
        elif status == "processing":
            self.after(0, lambda: self.lbl_status.configure(text="🔄 Procesando..."))
            self.after(0, lambda: self.btn_enviar.configure(state="disabled"))
            self.after(0, lambda: self.combo_modelos.configure(state="disabled"))
            self.after(0, lambda: self.btn_download.configure(state="disabled"))
            self.after(0, lambda: self.switch_ptt.configure(state="disabled"))
            self.after(0, lambda: self.combo_hotkey.configure(state="disabled"))
        elif status == "idle":
            self.after(0, lambda: self.lbl_status.configure(text="✅ Listo"))
            self.after(0, lambda: self.btn_enviar.configure(state="normal"))
            self.after(0, lambda: self.combo_modelos.configure(state="normal"))
            self.after(0, lambda: self.btn_download.configure(state="normal"))
            self.after(0, lambda: self.switch_ptt.configure(state="normal"))
            if self.ptt_enabled:
                self.after(0, lambda: self.combo_hotkey.configure(state="normal"))
        elif status == "model_changed":
            model = self.motor_ia.current_model
            self.after(0, lambda: self.title(f"VocalAI — Qwen3-TTS + {model}"))
            self.after(0, lambda: self._actualizar_info_modelo(model))
            self.after(0, lambda: self.lbl_status.configure(text=f"✅ Modelo: {model}"))
        elif status == "download_start":
            self.after(0, lambda: self.btn_download.configure(state="disabled", text="Descargando..."))
            self.after(0, lambda: self.combo_modelos.configure(state="disabled"))
            self.after(0, lambda: self.progress_download.pack(side="right", padx=10))
            self.after(0, lambda: self.progress_download.set(0))
            self.after(0, lambda: self.lbl_status.configure(text="📥 Descargando modelo..."))
        elif status == "download_done":
            model = self.motor_ia.current_model
            self.after(0, lambda: self.btn_download.configure(state="normal", text="⬇️ Descargar"))
            self.after(0, lambda: self.combo_modelos.configure(state="normal"))
            self.after(0, lambda: self.progress_download.pack_forget())
            self.after(0, lambda: self.title(f"VocalAI — Qwen3-TTS + {model}"))
            self.after(0, lambda: self._actualizar_info_modelo(model))
            self.after(0, lambda: self.lbl_status.configure(text=f"✅ {model} listo"))
            self.after(0, self._refresh_modelo_instalado)
        elif status == "download_error":
            self.after(0, lambda: self.btn_download.configure(state="normal", text="⬇️ Descargar"))
            self.after(0, lambda: self.combo_modelos.configure(state="normal"))
            self.after(0, lambda: self.progress_download.pack_forget())
            self.after(0, lambda: self.lbl_status.configure(text="❌ Error en descarga"))

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
        if self.ptt_enabled:
            self.switch_ptt.configure(text="PTT ON")
            self.combo_hotkey.configure(state="normal")
            self._start_ptt_listener()
            self.lbl_ptt_status.configure(text="[manten presionado para hablar]", text_color="#888888")
            self._print_log(f"[PTT] Activado — hotkey: {self.ptt_hotkey}")
        else:
            self.switch_ptt.configure(text="PTT OFF")
            self.combo_hotkey.configure(state="disabled")
            self._stop_ptt_listener()
            self.lbl_ptt_status.configure(text="")
            self._print_log("[PTT] Desactivado — modo continuo WebSocket")

    def _al_seleccionar_hotkey(self, value):
        self.ptt_hotkey = value
        if self.ptt_enabled:
            self._start_ptt_listener()
            self.lbl_ptt_status.configure(text="[manten presionado para hablar]", text_color="#888888")
        self._print_log(f"[PTT] Hotkey: {value}")

    def _build_ptt_target(self):
        name = self.ptt_hotkey
        if name in _PTT_KB_MAP:
            return ("keyboard", _PTT_KB_MAP[name])
        if name in _PTT_MOUSE_MAP:
            return ("mouse", _PTT_MOUSE_MAP[name])
        return (None, None)

    def _start_ptt_listener(self):
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
            return
        self.ptt_listener.daemon = True
        self.ptt_listener.start()
        self._ptt_target = (kind, target)

    def _stop_ptt_listener(self):
        if self.ptt_listener:
            try:
                self.ptt_listener.stop()
            except Exception:
                pass
            self.ptt_listener = None
        self.ptt_pressed = False

    def _on_ptt_press(self, key):
        kind, target = getattr(self, '_ptt_target', (None, None))
        if kind == "keyboard" and key == target:
            self.ptt_pressed = True
            self.after(0, lambda: self.lbl_ptt_status.configure(
                text="🟢 ESCUCHANDO...", text_color="#44ff44"
            ))

    def _on_ptt_release(self, key):
        kind, target = getattr(self, '_ptt_target', (None, None))
        if kind == "keyboard" and key == target:
            self.ptt_pressed = False
            self.after(0, lambda: self.lbl_ptt_status.configure(
                text="[manten presionado para hablar]", text_color="#888888"
            ))

    def _on_ptt_click(self, x, y, button, pressed):
        kind, target = getattr(self, '_ptt_target', (None, None))
        if kind == "mouse" and button == target:
            self.ptt_pressed = pressed
            if pressed:
                self.after(0, lambda: self.lbl_ptt_status.configure(
                    text="🟢 ESCUCHANDO...", text_color="#44ff44"
                ))
            else:
                self.after(0, lambda: self.lbl_ptt_status.configure(
                    text="[manten presionado para hablar]", text_color="#888888"
                ))

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
                            logger.debug("WS descartado (PTT ON, tecla no presionada)")
                        continue

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
