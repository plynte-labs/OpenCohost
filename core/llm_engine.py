import os
import re
import threading
import queue
import time
import uuid
import asyncio
import requests
import edge_tts
from collections import deque

from config.settings import (
    DEFAULT_MODEL, SYSTEM_PROMPT, HISTORY_MAX_TURNS, LLM_TEMPERATURE, 
    LLM_TOP_P, LLM_MAX_TOKENS, TEMP_DIR, TTS_SERVER_URL,
    TTS_HEAVY_TIMEOUT, TTS_LIGHT_TIMEOUT
)
from config.logger import get_logger

logger = get_logger()

class MotorVocalIA(threading.Thread):
    """
    Hilo de IA: gestiona Ollama (LLM), memoria conversacional,
    y comunicación con el servidor TTS vía HTTP.
    """
    def __init__(self, log_queue, ui_callback):
        super().__init__(daemon=True)
        self.log_queue = log_queue
        self.ui_callback = ui_callback
        self.command_queue = queue.Queue()

        self.voz_referencia = None
        self.is_ready = False
        self._processing = False
        self._speaking = False
        self._downloading = False
        self.current_model = DEFAULT_MODEL
        self.motor_tts = "ligero"  # Default 'ligero' (edge-tts)
        
        self.system_prompt = SYSTEM_PROMPT
        self.use_system_role = False

        self.historial = deque(maxlen=HISTORY_MAX_TURNS * 2)

        self._lock = threading.Lock()

    @property
    def is_speaking(self):
        with self._lock:
            return self._speaking

    @property
    def is_processing(self):
        with self._lock:
            return self._processing

    def run(self):
        self._log("Inicializando cliente ligero...")
        try:
            import pygame
            import ollama
        except ImportError as e:
            self._log(f"FATAL: Dependencia faltante: {e}", level="error")
            return

        self.pygame = pygame
        self.ollama = ollama

        try:
            self.pygame.mixer.init()
        except Exception as e:
            self._log(f"FATAL: No se pudo inicializar pygame.mixer: {e}", level="error")
            return

        self._check_ollama_service()
        self._log("Motor IA inicializado. Esperando comandos...")

        while True:
            try:
                comando = self.command_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            if comando is None:
                self._log("Señal de cierre recibida. Terminando hilo IA.")
                break

            tipo, payload = comando

            if tipo == "set_voice":
                self.voz_referencia = payload
                if isinstance(payload, tuple):
                    self.voz_referencia = payload[0]
                self._log(f"Perfil de voz configurado: {self.voz_referencia}")

            elif tipo == "check_ollama":
                self._check_ollama_service()

            elif tipo == "process_context":
                if not self.is_ready:
                    self._log("Ollama no esta listo. Usa el boton de Ollama/modelo para iniciarlo.", level="warning")
                    self.ui_callback("ollama_unavailable")
                    continue
                if self.motor_tts == "pesado" and not self.voz_referencia:
                    self._log("ERROR: Falta audio de referencia (Modo Qwen3-TTS).", level="warning")
                    continue
                if self._processing:
                    self._log("Ya procesando una solicitud. Ignorando...", level="warning")
                    continue
                self._processing = True
                self.ui_callback("processing")
                try:
                    self._ejecutar_inferencia(payload)
                finally:
                    self._processing = False
                    self.ui_callback("idle")

            elif tipo == "clear_history":
                self.historial.clear()
                self._log("Historial de conversación limpiado.")

            elif tipo == "switch_model":
                if not self.is_ready:
                    self._log("No se puede cambiar modelo: Ollama no esta listo.", level="warning")
                    self.ui_callback("ollama_unavailable")
                    continue
                new_model = payload
                if self._processing or self._speaking:
                    self._log("No se puede cambiar modelo mientras la IA está activa.", level="warning")
                    continue
                self.historial.clear()
                
                self._log(f"Liberando memoria del modelo: {self.current_model}...")
                try:
                    self.ollama.generate(model=self.current_model, prompt='', keep_alive=0)
                except Exception as e:
                    logger.warning(f"No se pudo liberar modelo {self.current_model}: {e}")

                self.current_model = new_model
                self._log(f"🔄 Modelo cambiado a: {new_model}")
                self.ui_callback("model_changed")

            elif tipo == "set_motor_tts":
                self.motor_tts = payload
                nombre = "Ligero (Edge-TTS)" if payload == "ligero" else "Pesado (Qwen3-TTS)"
                self._log(f"Motor de Voz cambiado a: {nombre}")

            elif tipo == "set_profile":
                self.system_prompt = payload.get("prompt", SYSTEM_PROMPT)
                self.use_system_role = payload.get("use_system", False)
                self.historial.clear()
                self._log(f"Perfil actualizado (System Role: {self.use_system_role}). Memoria limpiada.")

            elif tipo == "download_model":
                if not self.is_ready:
                    self._log("No se puede descargar modelo: Ollama no esta listo.", level="warning")
                    self.ui_callback("ollama_unavailable")
                    continue
                model_tag = payload
                if self._downloading:
                    self._log("Ya hay una descarga en curso.", level="warning")
                    continue
                threading.Thread(
                    target=self._download_model_worker,
                    args=(model_tag,),
                    daemon=True
                ).start()

    def _check_ollama_service(self):
        try:
            self.ollama.list()
        except Exception as e:
            self.is_ready = False
            self._log(f"Ollama no esta disponible: {e}", level="warning")
            self.ui_callback("ollama_unavailable")
            return False

        self.is_ready = True
        self.ui_callback("ready")
        self._log("Ollama disponible. Motor IA listo.")
        return True

    def _download_model_worker(self, model_tag):
        self._downloading = True
        self.ui_callback("download_start")
        self._log(f"📥 Descargando modelo '{model_tag}'... Esto puede tardar varios minutos.")

        try:
            last_pct = -1
            for progress in self.ollama.pull(model_tag, stream=True):
                status = getattr(progress, 'status', str(progress))
                total = getattr(progress, 'total', None)
                completed = getattr(progress, 'completed', None)

                if total is not None and completed is not None and total > 0:
                    pct = int((completed / total) * 100)
                    if pct >= last_pct + 10:
                        last_pct = pct
                        size_mb = completed / (1024 * 1024)
                        total_mb = total / (1024 * 1024)
                        self.log_queue.put(
                            f"[Descarga] {model_tag}: {pct}% ({size_mb:.0f}/{total_mb:.0f} MB) - {status}"
                        )
                elif 'success' in str(status).lower():
                    self._log(f"✅ Modelo '{model_tag}' descargado exitosamente.")
                else:
                    status_str = str(status)
                    if status_str and status_str != last_pct:
                        self.log_queue.put(f"[Descarga] {model_tag}: {status_str}")

            self.historial.clear()
            self.current_model = model_tag
            self._log(f"🔄 Modelo activo cambiado a: {model_tag}")
            self.ui_callback("download_done")

        except Exception as e:
            self._log(f"ERROR descargando '{model_tag}': {e}", level="error")
            logger.exception(f"Error descargando modelo {model_tag}")
            self.ui_callback("download_error")
        finally:
            self._downloading = False

    def _ejecutar_inferencia(self, contexto):
        self._log(f"Analizando contexto con {self.current_model}...")
        try:
            messages = []
            
            if self.use_system_role:
                messages.append({'role': 'system', 'content': self.system_prompt})

            for msg in self.historial:
                messages.append(msg)

            if self.use_system_role:
                messages.append({'role': 'user', 'content': contexto})
            else:
                prompt_completo = f"{self.system_prompt}\n\n[Mensaje del usuario]: {contexto}"
                messages.append({'role': 'user', 'content': prompt_completo})

            opciones_llm = {
                'temperature': LLM_TEMPERATURE,
                'top_p': LLM_TOP_P,
                'num_predict': LLM_MAX_TOKENS,
                'num_ctx': 4096,
            }

            if "gemma" in self.current_model.lower():
                opciones_llm.pop('num_ctx', None)
                opciones_llm['temperature'] = 0.7

            if "e2b" in self.current_model.lower() or "qwen3.5:4b" in self.current_model.lower() or "e4b" in self.current_model.lower() or "think" in self.current_model.lower():
                opciones_llm.pop('num_predict', None)
                self._log("Modelo de razonamiento detectado. Límite de tokens removido.", level="debug")

            start_llm = time.time()
            max_intentos = 2
            raw_content = ""
            respuesta = None
            
            for intento in range(max_intentos):
                respuesta = self.ollama.chat(
                    model=self.current_model,
                    messages=messages,
                    keep_alive=-1,
                    options=opciones_llm
                )
                
                msg_obj = respuesta.get('message', {})
                if isinstance(msg_obj, dict):
                    raw_content = msg_obj.get('content', '')
                    thinking = msg_obj.get('thinking', '')
                else:
                    raw_content = getattr(msg_obj, 'content', '')
                    thinking = getattr(msg_obj, 'thinking', '')

                if thinking:
                    logger.debug(f"Pensamiento interno detectado ({len(thinking)} chars)")

                if raw_content.strip():
                    break
                
                self._log(f"⚠️ Intento {intento+1}: {self.current_model} devolvió respuesta vacía. Reintentando...", level="warning")
                time.sleep(0.5)

            dialogo = raw_content.strip().strip('\x00\ufeff')
            elapsed = time.time() - start_llm

            if not dialogo:
                self._log(f"⚠️ {self.current_model} devolvió respuesta vacía ({elapsed:.2f}s).", level="warning")
                logger.warning(f"Empty LLM response. Raw repr: {repr(raw_content)}")
                return

            self.log_queue.put(f"\n🧠 [Kira]: {dialogo} ({elapsed:.2f}s)\n")
            logger.info(f"LLM response ({elapsed:.2f}s): {dialogo[:200]}")

            self.historial.append({'role': 'user', 'content': contexto})
            self.historial.append({'role': 'assistant', 'content': dialogo})
            
            max_mensajes = HISTORY_MAX_TURNS * 2
            if len(self.historial) > max_mensajes:
                self.historial = self.historial[-max_mensajes:]

            self._hablar(dialogo)

        except Exception as e:
            self._log(f"ERROR Ollama: {e}", level="error")
            logger.exception("Error en inferencia LLM")

    def _hablar(self, texto_a_generar):
        with self._lock:
            self._speaking = True
        self.ui_callback("speaking_start")

        ruta_absoluta_ref = os.path.abspath(self.voz_referencia) if self.voz_referencia else ""

        if self.motor_tts == "pesado":
            if not ruta_absoluta_ref or not os.path.exists(ruta_absoluta_ref):
                self._log("ERROR: Archivo de referencia no existe o no ha sido cargado.", level="error")
                with self._lock:
                    self._speaking = False
                self.ui_callback("speaking_end")
                return

        texto_limpio = re.sub(r'\*[^*]+\*', '', texto_a_generar)
        texto_limpio = texto_limpio.replace('"', '').replace('\n', ' ')

        fragmentos_brutos = re.split(r'(?<=[.!?])\s+', texto_limpio)
        oraciones = []
        
        MIN_PALABRAS_POR_CHUNK = 8
        MAX_PALABRAS_POR_CHUNK = 25
        
        for frag in fragmentos_brutos:
            frag = frag.strip()
            if not frag: continue
            
            if len(frag.split()) > MAX_PALABRAS_POR_CHUNK:
                sub_frags = re.split(r'(?<=[,;])\s+', frag)
                temp_chunk = ""
                for sub in sub_frags:
                    temp_chunk += sub + " "
                    if len(temp_chunk.split()) >= MIN_PALABRAS_POR_CHUNK:
                        oraciones.append(temp_chunk.strip())
                        temp_chunk = ""
                if temp_chunk.strip():
                    oraciones.append(temp_chunk.strip())
            else:
                oraciones.append(frag)

        oraciones = [o for o in oraciones if len(o) > 3]

        if not oraciones:
            self._log("⚠️ No se generaron oraciones válidas para sintetizar.", level="warning")
            with self._lock:
                self._speaking = False
            self.ui_callback("speaking_end")
            return

        self._log(f"Sintetizando {len(oraciones)} fragmento(s) con pipeline...")
        start_tts = time.time()

        cola_audios = queue.Queue(maxsize=3)
        error_count = 0

        def productor():
            nonlocal error_count
            for i, oracion in enumerate(oraciones):
                if not self._speaking:
                    break

                ext = ".mp3" if self.motor_tts == "ligero" else ".wav"
                archivo_chunk = os.path.join(TEMP_DIR, f"tts_chunk_{i}_{uuid.uuid4().hex[:4]}{ext}")
                try:
                    if self.motor_tts == "ligero":
                        async def generar_edge():
                            communicate = edge_tts.Communicate(oracion, "es-MX-DaliaNeural")
                            await communicate.save(archivo_chunk)
                        
                        asyncio.run(asyncio.wait_for(generar_edge(), timeout=TTS_LIGHT_TIMEOUT))
                        cola_audios.put((archivo_chunk, i, oracion))
                    else:
                        respuesta = requests.post(
                            TTS_SERVER_URL,
                            json={
                                "texto": oracion, 
                                "referencia": ruta_absoluta_ref,
                                "motor": self.motor_tts
                            },
                            timeout=TTS_HEAVY_TIMEOUT
                        )
                        if respuesta.status_code == 200:
                            with open(archivo_chunk, 'wb') as f:
                                f.write(respuesta.content)
                            cola_audios.put((archivo_chunk, i, oracion))
                        else:
                            error_detail = "desconocido"
                            try:
                                error_detail = respuesta.json().get('error', respuesta.text[:100])
                            except Exception:
                                error_detail = respuesta.text[:100]
                            logger.warning(f"TTS chunk {i} error HTTP {respuesta.status_code}: {error_detail}")
                            cola_audios.put(None)
                            error_count += 1

                except requests.exceptions.ConnectionError:
                    self._log("ERROR: Servidor Qwen3-TTS no disponible.", level="error")
                    cola_audios.put(None)
                    error_count += 1
                    break

                except requests.exceptions.Timeout:
                    logger.warning(f"TTS chunk {i} timeout")
                    cola_audios.put(None)
                    error_count += 1

                except Exception as e:
                    if self.motor_tts == "ligero":
                        self._log("ERROR: Edge-TTS requiere internet. Si estas offline usa Pesado (Qwen3-TTS).", level="error")
                        logger.warning(f"TTS ligero fallo; timeout configurado {TTS_LIGHT_TIMEOUT}s: {e}")
                        cola_audios.put(None)
                        error_count += 1
                        break
                    logger.exception(f"TTS chunk {i} error inesperado")
                    cola_audios.put(None)
                    error_count += 1

            cola_audios.put("FIN")

        hilo_productor = threading.Thread(target=productor, daemon=True)
        hilo_productor.start()

        chunks_played = 0
        try:
            while True:
                item = cola_audios.get(timeout=60)

                if item == "FIN":
                    break
                if item is None:
                    continue

                archivo_chunk, idx, oracion_texto = item

                try:
                    if chunks_played == 0:
                        elapsed_first = time.time() - start_tts
                        self._log(f"🔊 Primer fragmento listo en {elapsed_first:.2f}s. Reproduciendo...")

                    self.pygame.mixer.music.load(archivo_chunk)
                    self.pygame.mixer.music.play()

                    while self.pygame.mixer.music.get_busy():
                        time.sleep(0.05)

                    self.pygame.mixer.music.unload()
                    chunks_played += 1

                except Exception as e:
                    logger.warning(f"Error reproduciendo chunk {idx}: {e}")
                finally:
                    try:
                        if os.path.exists(archivo_chunk):
                            os.remove(archivo_chunk)
                    except OSError:
                        pass

        except queue.Empty:
            self._log("⚠️ Timeout esperando chunks de audio.", level="warning")
        except Exception as e:
            self._log(f"ERROR en reproducción: {e}", level="error")
            logger.exception("Error en consumidor de audio")
        finally:
            total_elapsed = time.time() - start_tts
        self._log(f"✅ Pipeline TTS completado: {chunks_played}/{len(oraciones)} fragmentos en {total_elapsed:.2f}s")
        if error_count > 0:
            self._log(f"⚠️ {error_count} fragmento(s) fallaron.", level="warning")
        with self._lock:
            self._speaking = False
        self.ui_callback("speaking_end")

        hilo_productor.join(timeout=2.0)

    def _log(self, msg, level="info"):
        prefix = "[IA]"
        self.log_queue.put(f"{prefix} {msg}")
        getattr(logger, level)(f"Motor: {msg}")
