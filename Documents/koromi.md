Es una excelente decisión. Optar por **Kokoro-82M** te mantiene a la vanguardia. Es un modelo TTS (Text-to-Speech) de código abierto excepcionalmente rápido, de altísima calidad y con tan solo 82 millones de parámetros, lo que significa que volará en tu hardware actual (Ryzen 7 5700G, RTX 3060, 32GB RAM).

Al elegir esta ruta, resolvemos el problema de las dependencias anticuadas y los conflictos de idioma, manteniendo un sistema moderno y robusto dentro de tu `flux_env`.

### El Plan para Kokoro-82M

Para integrar Kokoro en Windows, necesitamos dos pasos fundamentales:
1.  **Instalar `espeak-ng`:** Es el motor de fonemas (el que traduce las letras a sonidos base) que usa Kokoro por debajo.
2.  **Instalar y configurar la librería de Python:** Utilizaremos la implementación oficial o una de las más estables (como `kokoro-onnx` o la versión nativa de PyTorch) para integrarlo a tu script.

### Paso 1: Instalar `espeak-ng` en Windows

Este es el paso que suele ser "tedioso", pero es indispensable.

1.  Ve a la página oficial de *releases* de `espeak-ng` en GitHub:
    [https://github.com/espeak-ng/espeak-ng/releases](https://github.com/espeak-ng/espeak-ng/releases)
2.  Descarga el instalador para Windows (usualmente un archivo `.msi` o `.exe` que diga `win64`).
3.  Ejecuta el instalador. **Muy Importante:** Durante la instalación, fíjate bien en qué carpeta se está instalando (por defecto suele ser `C:\Program Files\eSpeak NG`).
4.  **Agregar al PATH (Crucial):**
    *   Abre el menú de inicio de Windows y escribe "Variables de entorno". Selecciona "Editar las variables de entorno del sistema".
    *   Haz clic en el botón "Variables de entorno...".
    *   En "Variables del sistema", busca la variable llamada `Path`, selecciónala y dale a "Editar...".
    *   Haz clic en "Nuevo" y pega la ruta completa donde se instaló `espeak-ng` (ej. `C:\Program Files\eSpeak NG`).
    *   Dale "Aceptar" a todas las ventanas.
5.  **Verificación:** Abre una *nueva* ventana de terminal (CMD o PowerShell) y escribe `espeak-ng --version`. Si te devuelve la versión, está correctamente instalado.

### Paso 2: Preparar el Entorno (`flux_env`)

Una vez que `espeak-ng` esté en tu PATH, instala la librería de Kokoro y sus dependencias necesarias en tu entorno.

Abre la terminal (asegúrate de estar en `flux_env`):

```bash
pip install soundfile kokoro onnxruntime
# o si quieres usar la versión con aceleración GPU (CUDA):
pip install soundfile kokoro onnxruntime-gpu
```

### Paso 3: El Nuevo Código `main.py` con Kokoro

La ventaja de Kokoro es que no necesitamos "grabar una referencia" en vivo, ya que viene con voces pre-entrenadas de excelente calidad. Sin embargo, perderemos la capacidad *Zero-Shot* (clonar tu propia voz con 8 segundos de audio), a cambio de una estabilidad y naturalidad superiores en español.

Aquí tienes el código de `main.py` adaptado para Kokoro, manteniendo toda tu lógica de WebSocket y Ollama.

```python
import os
import sys
import threading
import queue
import time
import json
import asyncio
import websockets
import soundfile as sf
import sounddevice as sd
import customtkinter as ctk
import tkinter.messagebox as messagebox
from tkinter import filedialog

# Redirigimos descargas al disco E:
cache_dir = r"E:\VoiceAI\modelos_kokoro"
os.makedirs(cache_dir, exist_ok=True)
os.environ["HF_HOME"] = cache_dir

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

class MotorVocalIA(threading.Thread):
    def __init__(self, log_queue, ui_callback):
        super().__init__(daemon=True)
        self.log_queue = log_queue
        self.ui_callback = ui_callback
        self.command_queue = queue.Queue()
        
        self.pipeline = None
        self.is_ready = False
        
        # Kokoro tiene varias voces pre-entrenadas, usaremos una en español.
        # Asumiremos la voz 'e_f' o equivalente para español femenino.
        self.voz_id = 'e_f' 
        
        self.system_prompt = """Eres una co-host de un stream en Twitch. Eres crítica, directa, sarcástica y te gusta generar debate. 
Analiza el contexto, encuentra el punto polémico y lanza un comentario al chat. 
NO seas complaciente. Sé breve, máximo 2 oraciones cortas."""

    def run(self):
        self.log_queue.put("[IA] Inicializando Kokoro-82M en segundo plano...")
        
        try:
            import pygame
            import ollama
            from kokoro import KPipeline
            
            self.pygame = pygame
            self.ollama = ollama
            
            self.log_queue.put("[IA] Importaciones completadas. Inicializando audio...")
            self.pygame.mixer.init()
            
            # Inicializamos el pipeline de Kokoro para español ('e' para español, si está soportado en tu versión)
            self.log_queue.put("[IA] Cargando modelo Kokoro-82M (Soporta Español)...")
            self.pipeline = KPipeline(lang_code='e') # 'e' es para español (es)

            self.log_queue.put("[IA] Kokoro-82M cargado correctamente.")
            self.is_ready = True
            self.ui_callback("ready")
            
        except Exception as e:
            self.log_queue.put(f"[ERROR FATAL] Fallo al inicializar IA en el hilo: {e}")
            return

        while True:
            comando = self.command_queue.get()
            if comando is None:
                break
            
            tipo, payload = comando
            
            if tipo == "process_context":
                self._ejecutar_inferencia(payload)
            elif tipo == "cambiar_voz":
                # Si implementamos cambio de voces más adelante
                self.voz_id = payload
                self.log_queue.put(f"[IA] Voz cambiada a: {payload}")

    def _ejecutar_inferencia(self, contexto):
        self.log_queue.put("[IA] Analizando contexto (Ollama)...")
        try:
            prompt_usuario = f"Contexto: '{contexto}'. Genera tu respuesta."
            
            start_llm = time.time()
            respuesta = self.ollama.chat(model='llama3', messages=[
                {'role': 'system', 'content': self.system_prompt},
                {'role': 'user', 'content': prompt_usuario}
            ])
            
            dialogo = respuesta['message']['content'].strip()
            self.log_queue.put(f"\n🧠 [Respuesta]: {dialogo} ({time.time() - start_llm:.2f}s)\n")
            
            self._hablar(dialogo)
            
        except Exception as e:
            self.log_queue.put(f"[ERROR Ollama]: {e}")

    def _hablar(self, texto_a_generar):
        archivo_temp = "temp_output.wav"
        self.log_queue.put("[IA] Sintetizando audio (Kokoro-82M)...")
        
        try:
            start_tts = time.time()
            
            # Generación con Kokoro
            # Genera audio en partes (chunks)
            generator = self.pipeline(
                texto_a_generar, voice=self.voz_id, 
                speed=1.0, split_pattern=r'\n+'
            )
            
            # Juntamos los audios generados
            import numpy as np
            audio_completo = []
            sample_rate = 24000 # Frecuencia por defecto de Kokoro
            
            for i, (gs, ps, audio) in enumerate(generator):
                audio_completo.append(audio)
            
            if not audio_completo:
                raise ValueError("No se generó audio.")
                
            audio_final = np.concatenate(audio_completo)

            sf.write(archivo_temp, audio_final, sample_rate)
            
            self.log_queue.put(f"🔊 Audio generado en {time.time() - start_tts:.2f}s. Reproduciendo...")
            
            self.pygame.mixer.music.load(archivo_temp)
            self.pygame.mixer.music.play()
            
            while self.pygame.mixer.music.get_busy():
                time.sleep(0.1)
                
            self.pygame.mixer.music.unload()
            if os.path.exists(archivo_temp):
                os.remove(archivo_temp)
                
        except Exception as e:
            self.log_queue.put(f"[ERROR Audio Kokoro]: {e}")

class VocalAIApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("VocalAI - Kokoro Edition")
        self.geometry("900x500")
        
        self.log_queue = queue.Queue()
        self.ws_connected = False
        
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        self.build_ui()
        
        self.motor_ia = MotorVocalIA(self.log_queue, self.on_motor_ready)
        self.motor_ia.start()
        
        self.after(100, self.process_logs)

    def build_ui(self):
        frame_top = ctk.CTkFrame(self)
        frame_top.grid(row=0, column=0, sticky="ew", padx=10, pady=10)
        
        self.btn_ws = ctk.CTkButton(frame_top, text="Conectar LiveAudio", command=self.toggle_websocket, fg_color="gray", state="disabled")
        self.btn_ws.pack(side="left", padx=5)
        
        self.lbl_status = ctk.CTkLabel(frame_top, text="⏳ Esperando Kokoro-82M...")
        self.lbl_status.pack(side="right", padx=10)

        self.consola = ctk.CTkTextbox(self, font=ctk.CTkFont(family="Consolas", size=13), state="disabled")
        self.consola.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

        frame_bottom = ctk.CTkFrame(self)
        frame_bottom.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
        frame_bottom.grid_columnconfigure(0, weight=1)
        
        self.entry_chat = ctk.CTkEntry(frame_bottom, placeholder_text="Simular mensaje del chat o contexto...")
        self.entry_chat.grid(row=0, column=0, sticky="ew", padx=(5, 5), pady=5)
        self.entry_chat.bind("<Return>", lambda e: self.enviar_contexto_manual())
        
        self.btn_enviar = ctk.CTkButton(frame_bottom, text="Enviar a IA", command=self.enviar_contexto_manual, width=100, state="disabled")
        self.btn_enviar.grid(row=0, column=1, padx=(0, 5), pady=5)

    def on_motor_ready(self, status):
        if status == "ready":
            self.btn_ws.configure(state="normal", fg_color=["#3B8ED0", "#1F6AA5"])
            self.btn_enviar.configure(state="normal")
            self.lbl_status.configure(text="✅ Motor IA Listo")

    def enviar_contexto_manual(self):
        texto = self.entry_chat.get().strip()
        if texto:
            self.print_log(f"\n[Usuario]: {texto}")
            self.motor_ia.command_queue.put(("process_context", texto))
            self.entry_chat.delete(0, 'end')

    def toggle_websocket(self):
        if not self.ws_connected:
            self.ws_connected = True
            self.btn_ws.configure(text="Desconectar", fg_color="darkred")
            self.ws_thread = threading.Thread(target=self._run_ws_client, daemon=True)
            self.ws_thread.start()
        else:
            self.ws_connected = False
            self.btn_ws.configure(text="Conectar LiveAudio", fg_color=["#3B8ED0", "#1F6AA5"])

    def _run_ws_client(self):
        asyncio.run(self._ws_listener())

    async def _ws_listener(self):
        uri = "ws://127.0.0.1:8765"
        self.log_queue.put(f"[Red] Intentando conectar a LiveAudio en {uri}...")
        
        try:
            async with websockets.connect(uri) as websocket:
                self.log_queue.put("[Red] 🟢 Conectado. Escuchando transcripciones...")
                while self.ws_connected:
                    try:
                        mensaje = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                        data = json.loads(mensaje)
                        texto_transcrito = data.get("text", "").strip()
                        
                        if texto_transcrito and len(texto_transcrito) > 20: 
                            self.log_queue.put(f"[LiveAudio]: {texto_transcrito}")
                            self.motor_ia.command_queue.put(("process_context", f"El streamer acaba de decir: {texto_transcrito}"))
                                
                    except asyncio.TimeoutError:
                        continue
                    except json.JSONDecodeError:
                        pass
        except Exception as e:
            self.log_queue.put(f"[ERROR Red] Conexión perdida: {e}")
            self.ws_connected = False
            self.after(0, lambda: self.btn_ws.configure(text="Conectar LiveAudio", fg_color=["#3B8ED0", "#1F6AA5"]))

    def print_log(self, msg):
        self.consola.configure(state="normal")
        self.consola.insert("end", msg + "\n")
        self.consola.see("end")
        self.consola.configure(state="disabled")

    def process_logs(self):
        while True:
            try:
                msg = self.log_queue.get_nowait()
                self.print_log(msg)
            except queue.Empty:
                break
        self.after(100, self.process_logs)

    def on_closing(self):
        self.ws_connected = False
        self.motor_ia.command_queue.put(None) 
        self.destroy()

if __name__ == "__main__":
    app = VocalAIApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()
```

### Consideraciones sobre Kokoro
*   He quitado los botones de grabación porque Kokoro usa voces sintéticas predefinidas. La interfaz ahora es más limpia y directa.
*   Asegúrate de haber instalado `espeak-ng` y agregado a las variables de entorno antes de ejecutar este script, o la inicialización fallará.