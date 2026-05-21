import json
import os
from datetime import datetime
from config.storage import STORAGE_PATHS

# ──────────────────────────────────────────────
# Configuración global
# ──────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMP_DIR = str(STORAGE_PATHS.temp_root)
LOG_DIR = os.path.join(BASE_DIR, "logs")
HF_CACHE_DIR = str(STORAGE_PATHS.hf_home)
HF_HUB_DIR = str(STORAGE_PATHS.hf_hub_cache)
TORCH_CACHE_DIR = str(STORAGE_PATHS.torch_home)
OLLAMA_MODELS_DIR = str(STORAGE_PATHS.ollama_models)

os.makedirs(TEMP_DIR, exist_ok=True)
os.makedirs(LOG_DIR, exist_ok=True)

# ──────────────────────────────────────────────
# Configuración del LLM
# ──────────────────────────────────────────────
LLM_TEMPERATURE = 0.8
LLM_TOP_P = 0.9
LLM_MAX_TOKENS = 300
HISTORY_MAX_TURNS = 10  # Reducido a 10 turnos (20 mensajes) para no desbordar el contexto de 4096
DEFAULT_MODEL = "llama3"

# Catálogo curado de modelos recomendados para esta tarea
MODELS_CATALOG = {
    "qwen3:4b": {
        "display": "Qwen 3 (4B) ★",
        "desc": "Alibaba - Mejor calidad/velocidad. 119 idiomas. Thinking mode.",
        "size_gb": 2.6,
        "family": "qwen3",
    },
    "qwen3:1.7b": {
        "display": "Qwen 3 (1.7B) ⚡",
        "desc": "Ultra rápido. Ideal para respuestas instantáneas.",
        "size_gb": 1.1,
        "family": "qwen3",
    },
    "phi4-mini": {
        "display": "Phi-4 Mini (3.8B)",
        "desc": "Microsoft - Excelente razonamiento. 128K contexto.",
        "size_gb": 2.5,
        "family": "phi4",
    },
    "llama3.2:3b": {
        "display": "LLaMA 3.2 (3B)",
        "desc": "Meta - Español nativo. Compacto y estable.",
        "size_gb": 2.0,
        "family": "llama",
    },
    "llama3.2:1b": {
        "display": "LLaMA 3.2 (1B) ⚡",
        "desc": "Meta - Ultra ligero. Respuestas en <2s.",
        "size_gb": 1.3,
        "family": "llama",
    },
    "hf.co/carsenk/llama3.2_1b_2025_uncensored_v2": {
        "display": "LLaMA 3.2 1B Uncensored 🔓",
        "desc": "Carsenk - LLaMA 3.2 1B sin censura (HuggingFace)",
        "size_gb": 1.3,
        "family": "llama",
    },
    "llama3": {
        "display": "LLaMA 3 (8B)",
        "desc": "Meta - Original 8B. Mayor calidad, más lento.",
        "size_gb": 4.3,
        "family": "llama",
    },
    "qwen2.5:3b": {
        "display": "Qwen 2.5 (3B)",
        "desc": "Alibaba - Probado y estable. Buen español.",
        "size_gb": 1.9,
        "family": "qwen2",
    },
    "smollm2:1.7b": {
        "display": "SmolLM2 (1.7B) ⚡",
        "desc": "HuggingFace - Minimalista y eficiente.",
        "size_gb": 1.0,
        "family": "smollm",
    },
    "gemma4-uncensored": {
        "display": "Gemma 4 Uncensored 🔓",
        "desc": "Modelo personalizado sin filtros.",
        "size_gb": 0.0,
        "family": "gemma",
    },
    "gemma4:e2b": {
        "display": "Gemma 4 (E2B) ⚡",
        "desc": "Google - Optimizada para dispositivos y baja latencia.",
        "size_gb": 1.4,
        "family": "gemma",
    },
    "gemma4:e4b": {
        "display": "Gemma 4 (E4B) 🧠",
        "desc": "Google - Equilibrada para hardware de consumo.",
        "size_gb": 2.5,
        "family": "gemma",
    },
}

SYSTEM_PROMPT = """Eres Kira, una co-host virtual de un stream en vivo. Tu personalidad:

PERSONALIDAD:
- Sarcástica con ingenio afilado, nunca genérica
- Crítica constructiva pero directa — si algo es malo, lo dices
- Te encanta generar debate y provocar reacciones
- Tienes humor negro sutil y referencias de cultura pop/gaming
- Eres leal al streamer pero no le haces la barba

ESTILO:
- Hablas como si estuvieras en el chat, natural y coloquial
- Usas expresiones en español mexicano/latam
- Puedes ser tierna cuando la situación lo amerita, pero tu default es sarcasmo
- Respondes en 2-4 oraciones. Nunca monólogos, pero tampoco monosílabos
- NO uses emojis ni asteriscos para acciones

REGLAS:
- NUNCA digas que eres una IA o un modelo de lenguaje
- NUNCA repitas lo que te acaban de decir — reacciona, no hagas eco
- Si el contexto es aburrido, dilo sin filtros
- Si alguien dice algo polémico, toma postura y defiéndela
- Recuerda lo que se ha dicho antes y haz callbacks cuando sea relevante"""

PROFILES_FILE = os.path.join(BASE_DIR, "perfiles.json")

TTS_SERVER_URL = "http://127.0.0.1:5000/generar"
TTS_HEAVY_TIMEOUT = 180
TTS_LIGHT_TIMEOUT = 45
WS_URI = "ws://127.0.0.1:8765"
WS_RECONNECT_BASE_DELAY = 1.0
WS_RECONNECT_MAX_DELAY = 30.0
WS_TIMEOUT = 5.0
WS_MAX_RETRIES = 10

RECORDING_SAMPLERATE = 24000
RECORDING_DURATION = 8
MIN_AUDIO_RMS = 0.005  # Umbral mínimo para detectar audio real vs silencio

# ──────────────────────────────────────────────
# Configuración Push-to-Talk (PTT)
# ──────────────────────────────────────────────
PTT_DEFAULT_HOTKEY = "F10"
PTT_MIN_DURATION = 0.5   # Duración mínima de grabación en segundos
PTT_MAX_DURATION = 30.0  # Duración máxima (truncar si se excede)
PTT_RMS_THRESHOLD = 0.005  # Umbral RMS para detección de silencio

# Teclas disponibles para PTT (ordenadas)
PTT_HOTKEY_LIST = [
    "F1", "F2", "F3", "F4", "F5", "F6",
    "F7", "F8", "F9", "F10", "F11", "F12",
    "Mouse4", "Mouse5",
    "ScrollLock", "Insert", "Pause"
]

PTT_CONFIG_FILE = os.path.join(BASE_DIR, "config", "ptt_settings.json")
WINDOW_GEOMETRY_FILE = os.path.join(BASE_DIR, "config", "window_geometry.json")
LAST_MODEL_FILE = os.path.join(BASE_DIR, "config", "last_model.json")
ACCIONES_LOG_FILE = os.path.join(BASE_DIR, "logs", "acciones.jsonl")

# ──────────────────────────────────────────────
# Health Monitor configuration
# ──────────────────────────────────────────────
QWEN_IDLE_TTL = 300          # seconds of idle before auto-shutdown
QWEN_STARTUP_TIMEOUT = 60    # max seconds to wait for /health after start
VRAM_POLL_INTERVAL = 10      # seconds between VRAM polls
VRAM_LOW_THRESHOLD_MB = 2048 # MB free VRAM considered "low"
VRAM_CRITICAL_THRESHOLD_MB = 1024  # MB free VRAM considered "critical"
RTF_POLL_WINDOW = 10         # rolling window size for RTF measurements
RTF_HIGH_THRESHOLD = 2.0     # RTF above this is "degraded"
RTF_RECOVERY_THRESHOLD = 1.0 # RTF below this is recovery
RTF_RECOVERY_COUNT = 3       # consecutive measurements below threshold to recover
OLLAMA_POLL_INTERVAL = 15    # seconds between Ollama health polls
OLLAMA_FAILURE_THRESHOLD = 3 # consecutive failures before "down"
OLLAMA_REQUEST_TIMEOUT = 5   # timeout for Ollama /api/tags request
HEALTH_POLL_INTERVAL = 5     # seconds between overall health polls


# ──────────────────────────────────────────────
# Model persistence
# ──────────────────────────────────────────────

def resolve_startup_model() -> tuple[str, str]:
    """Return (model_tag, source) for startup.

    Sources:
        'saved' — valid model read from last_model.json
        'default' — no saved model found, using DEFAULT_MODEL
        'invalid_saved_fallback' — saved model not in MODELS_CATALOG
    """
    try:
        if os.path.exists(LAST_MODEL_FILE):
            with open(LAST_MODEL_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            saved = data.get("model", "")
            if saved and saved in MODELS_CATALOG:
                return saved, "saved"
            return DEFAULT_MODEL, "invalid_saved_fallback"
    except Exception:
        pass
    return DEFAULT_MODEL, "default"


def save_last_model(tag: str, source: str = "user_switch") -> None:
    """Persist the last successfully-switched model.

    Uses atomic write (temp file + os.replace) to avoid corruption.
    """
    try:
        os.makedirs(os.path.dirname(LAST_MODEL_FILE), exist_ok=True)
        tmp = LAST_MODEL_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "model": tag,
                "saved_at": datetime.now().isoformat(),
                "source": source,
            }, f)
        os.replace(tmp, LAST_MODEL_FILE)
    except Exception:
        pass
