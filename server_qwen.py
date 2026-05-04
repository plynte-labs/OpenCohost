import os
import uuid
import time
import logging
import threading
import asyncio
from flask import Flask, request, send_file, jsonify
import torch
import soundfile as sf
import edge_tts
from qwen_tts import Qwen3TTSModel

# ──────────────────────────────────────────────
# CONFIGURACIÓN DEL AUDIO DE REFERENCIA
# ──────────────────────────────────────────────
# Qwen3-TTS NECESITA saber qué dijiste en tu grabación para clonarte bien.
# Escribe aquí la transcripción exacta de tu archivo 'referencia_grabada.wav'
TEXTO_DE_TU_GRABACION = "Hola, estoy grabando esta nota de voz para calibrar el tono, la emocion y la personalidad del asistente."

# ──────────────────────────────────────────────
# Logging estructurado
# ──────────────────────────────────────────────
LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
os.makedirs(LOG_DIR, exist_ok=True)

log_formatter = logging.Formatter("[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s", datefmt="%H:%M:%S")
console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)
logger = logging.getLogger("MultiTTS-Server")
logger.setLevel(logging.DEBUG)
logger.addHandler(console_handler)

TEMP_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "temp")
os.makedirs(TEMP_DIR, exist_ok=True)

# ──────────────────────────────────────────────
# Inicialización del Servidor y Modelos
# ──────────────────────────────────────────────
app = Flask(__name__)
_tts_lock = threading.Lock()

logger.info("Inicializando Motor Pesado (Qwen3-TTS 0.6B)...")
device = "cuda:0" if torch.cuda.is_available() else "cpu"

try:
    # Carga nativa del modelo usando la API oficial
    model = Qwen3TTSModel.from_pretrained(
        "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
        device_map=device,
        dtype=torch.bfloat16 if device == "cuda:0" else torch.float32,
    )
    logger.info(f"✅ Qwen3-TTS cargado exitosamente en {device.upper()}")
except Exception as e:
    logger.error(f"Fallo al cargar Qwen3-TTS: {e}")
    model = None

@app.route('/generar', methods=['POST'])
def generar_audio():
    data = request.json
    texto = data.get('texto', '').strip()
    referencia = data.get('referencia', '').strip()
    motor_solicitado = data.get('motor', 'pesado').lower()

    if not texto:
        return jsonify({"error": "Falta el campo 'texto'"}), 400

    request_id = uuid.uuid4().hex[:8]
    archivo_salida = os.path.join(TEMP_DIR, f"out_{motor_solicitado}_{request_id}.wav")
    start_time = time.time()

    try:
        # ── RUTA 1: MOTOR LIGERO (Edge-TTS | 0% GPU) ──
        if motor_solicitado == 'ligero':
            logger.info(f"[{request_id}] Motor LIGERO: '{texto[:60]}...'")
            voz_edge = "es-MX-DaliaNeural" 
            async def generar_edge():
                communicate = edge_tts.Communicate(texto, voz_edge)
                await communicate.save(archivo_salida)
            asyncio.run(generar_edge())

        # ── RUTA 2: MOTOR PESADO (Qwen3-TTS | Clonación Zero-Shot) ──
        else:
            if not referencia or not os.path.exists(referencia):
                return jsonify({"error": "Falta referencia válida para Qwen3-TTS"}), 400
                
            logger.info(f"[{request_id}] Motor PESADO (Qwen): '{texto[:60]}...'")
            
            with _tts_lock:
                # Síntesis oficial usando la API de qwen_tts
                wavs, sr = model.generate_voice_clone(
                    text=texto,
                    language="Spanish",
                    ref_audio=referencia,
                    ref_text=TEXTO_DE_TU_GRABACION,
                )
                
                # Escribir el tensor al disco usando soundfile
                sf.write(archivo_salida, wavs[0], sr)

        elapsed = time.time() - start_time
        logger.info(f"[{request_id}] Audio generado en {elapsed:.2f}s → {archivo_salida}")
        return send_file(archivo_salida, mimetype="audio/wav")

    except Exception as e:
        logger.exception(f"[{request_id}] Error generando audio")
        return jsonify({"error": str(e)}), 500
    finally:
        def cleanup_file():
            try:
                if os.path.exists(archivo_salida):
                    os.remove(archivo_salida)
            except OSError:
                pass
        threading.Timer(5.0, cleanup_file).start()

if __name__ == '__main__':
    logger.info("Servidor Multi-Motor iniciando en http://127.0.0.1:5000")
    app.run(host='127.0.0.1', port=5000, debug=False)