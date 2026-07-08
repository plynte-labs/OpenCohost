import os
import uuid
import time
import logging
import threading
import asyncio
import inspect
from pathlib import Path

from opencohost.config.storage import STORAGE_PATHS
from opencohost.core.temp_file_cleanup import register_temp_file_cleanup

from flask import Flask, request, send_file, jsonify
import torch
import soundfile as sf
import edge_tts

from opencohost.i18n import active as i18n_active

BASE_DIR = Path(__file__).resolve().parent
HF_CACHE_DIR = STORAGE_PATHS.hf_home
HF_HUB_DIR = STORAGE_PATHS.hf_hub_cache
QWEN_REPO_ID = "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
QWEN_CACHE_DIR = HF_HUB_DIR / "models--Qwen--Qwen3-TTS-12Hz-0.6B-Base"

# Hugging Face debe buscar primero en el cache configurable de OpenCohost. Si la
# snapshot local existe, activamos offline para evitar llamadas de red.

os.environ.setdefault("HF_HOME", str(HF_CACHE_DIR))
os.environ.setdefault("HF_HUB_CACHE", str(HF_HUB_DIR))
os.environ.setdefault("TRANSFORMERS_CACHE", str(HF_HUB_DIR))

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
if not logger.handlers:
    logger.addHandler(console_handler)

TEMP_DIR = str(STORAGE_PATHS.temp_root)
os.makedirs(TEMP_DIR, exist_ok=True)


def _snapshot_from_local_cache():
    refs_main = QWEN_CACHE_DIR / "refs" / "main"
    if refs_main.exists():
        revision = refs_main.read_text(encoding="utf-8").strip()
        snapshot = QWEN_CACHE_DIR / "snapshots" / revision
        if snapshot.exists():
            return snapshot

    snapshots_dir = QWEN_CACHE_DIR / "snapshots"
    if not snapshots_dir.exists():
        return None

    snapshots = [p for p in snapshots_dir.iterdir() if p.is_dir()]
    if not snapshots:
        return None
    return max(snapshots, key=lambda p: p.stat().st_mtime)


_LOCAL_MODEL_PATH_AT_IMPORT = _snapshot_from_local_cache()
if _LOCAL_MODEL_PATH_AT_IMPORT:
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"


from qwen_tts import Qwen3TTSModel


def _from_pretrained_kwargs(model_path, device):
    kwargs = {
        "device_map": device,
        "dtype": torch.bfloat16 if device == "cuda:0" else torch.float32,
    }
    try:
        signature = inspect.signature(Qwen3TTSModel.from_pretrained)
        accepts_kwargs = any(p.kind == inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values())
        if accepts_kwargs or "local_files_only" in signature.parameters:
            kwargs["local_files_only"] = Path(str(model_path)).exists()
    except (TypeError, ValueError):
        pass
    return kwargs

# ──────────────────────────────────────────────
# Inicialización del Servidor y Modelos
# ──────────────────────────────────────────────
app = Flask(__name__)
_tts_lock = threading.Lock()
APP_ID = "opencohost-qwen-tts"

logger.info("Inicializando Motor Pesado (Qwen3-TTS 0.6B)...")
device = "cuda:0" if torch.cuda.is_available() else "cpu"
LOCAL_MODEL_PATH = _LOCAL_MODEL_PATH_AT_IMPORT
MODEL_SOURCE = str(LOCAL_MODEL_PATH) if LOCAL_MODEL_PATH else QWEN_REPO_ID

if LOCAL_MODEL_PATH:
    logger.info(f"Usando Qwen3-TTS local: {LOCAL_MODEL_PATH}")
else:
    logger.warning("Qwen3-TTS no esta en cache local; se intentara descargar desde Hugging Face.")

try:
    model = Qwen3TTSModel.from_pretrained(MODEL_SOURCE, **_from_pretrained_kwargs(MODEL_SOURCE, device))
    logger.info(f"✅ Qwen3-TTS cargado exitosamente en {device.upper()}")
except Exception as e:
    logger.error(f"Fallo al cargar Qwen3-TTS: {e}")
    model = None

@app.route('/health', methods=['GET'])
def health_check():
    """Health probe for the HealthMonitor daemon."""
    if model is not None:
        return jsonify({"app": APP_ID, "status": "ok", "model_loaded": True}), 200
    return jsonify({"app": APP_ID, "status": "error", "model_loaded": False}), 503

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
            # Privacy guard: read tts_local_only flag directly from the persisted
            # JSON file using stdlib only.  This server runs in a SEPARATE Python
            # environment (xtts_env) that does NOT have opencohost installed, so we
            # cannot import opencohost.config.settings — hence the inline read.
            # The file path mirrors TTS_LOCAL_ONLY_FILE from settings.py:
            #   USER_DATA_DIR (dev=repo root, packaged=%APPDATA%/OpenCohost)
            #   └─ config/tts_local_only.json
            # In both cases the repo root is two levels above __file__
            # (opencohost/server_qwen.py → opencohost/ → repo root).
            _tts_local_only_flag = False
            try:
                import json as _json
                _flag_path = os.path.join(
                    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "config", "tts_local_only.json",
                )
                if os.path.exists(_flag_path):
                    with open(_flag_path, "r", encoding="utf-8") as _f:
                        _tts_local_only_flag = bool(
                            _json.load(_f).get("tts_local_only", False)
                        )
            except Exception:
                _tts_local_only_flag = False

            if _tts_local_only_flag:
                return jsonify({
                    "error": "tts_local_only is enabled; light synthesis must stay local"
                }), 400

            logger.info(f"[{request_id}] Motor LIGERO: '{texto[:60]}...'")
            voz_edge = i18n_active.edge_voice()
            async def generar_edge():
                communicate = edge_tts.Communicate(texto, voz_edge)
                await communicate.save(archivo_salida)
            asyncio.run(generar_edge())

        # ── RUTA 2: MOTOR PESADO (Qwen3-TTS | Clonación Zero-Shot) ──
        else:
            if model is None:
                return jsonify({"error": "Qwen3-TTS no esta cargado; revisa el log del servidor."}), 503
            if not referencia or not os.path.exists(referencia):
                return jsonify({"error": "Falta referencia válida para Qwen3-TTS"}), 400
                
            logger.info(f"[{request_id}] Motor PESADO (Qwen): '{texto[:60]}...'")
            
            with _tts_lock:
                # Síntesis oficial usando la API de qwen_tts
                wavs, sr = model.generate_voice_clone(
                    text=texto,
                    language=i18n_active.qwen_language(),
                    ref_audio=referencia,
                    ref_text=TEXTO_DE_TU_GRABACION,
                )
                
                # Escribir el tensor al disco usando soundfile
                sf.write(archivo_salida, wavs[0], sr)

        elapsed = time.time() - start_time
        logger.info(f"[{request_id}] Audio generado en {elapsed:.2f}s → {archivo_salida}")
        response = send_file(archivo_salida, mimetype="audio/wav")
        return register_temp_file_cleanup(response, archivo_salida, logger)

    except Exception as e:
        logger.exception(f"[{request_id}] Error generando audio")
        return jsonify({"error": str(e)}), 500
if __name__ == '__main__':
    logger.info("Servidor Multi-Motor iniciando en http://127.0.0.1:5000")
    app.run(host='127.0.0.1', port=5000, debug=False)
