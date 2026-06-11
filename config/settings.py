import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional
from config.storage import STORAGE_PATHS, USER_DATA_DIR

# ──────────────────────────────────────────────
# Configuración global
# ──────────────────────────────────────────────
def get_app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR = str(get_app_dir())
TEMP_DIR = str(STORAGE_PATHS.temp_root)
LOG_DIR = os.path.join(str(USER_DATA_DIR), "logs")
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
LLM_MAX_TOKENS = 768
HISTORY_MAX_TURNS = 10  # Reducido a 10 turnos (20 mensajes) para no desbordar el contexto de 4096
DEFAULT_MODEL = "llama3"
DEFAULT_LLM_TIERS = {
    "quality": "gemma4:e4b",
    "balanced": "llama3",
    "fast": "qwen3:1.7b",
}

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
    "gemma4:12b": {
        "display": "Gemma 4 (12B)",
        "desc": "Google - Multimodal, alto rendimiento. Ideal RTX 3060+.",
        "size_gb": 7.2,
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

PROFILES_FILE = os.path.join(str(USER_DATA_DIR), "perfiles.json")

TTS_SERVER_URL = "http://127.0.0.1:5000/generar"
TTS_HEAVY_TIMEOUT = 180
TTS_LIGHT_TIMEOUT = 45
# Path to a local Piper ONNX voice model file. Resolved relative to the
# storage cache root so packaged builds and storage.yaml overrides work.
# If the file is missing, PiperEngine.load() disables the offline fallback.
TTS_LOCAL_MODEL_PATH: str = os.path.join(
    str(STORAGE_PATHS.cache_root), "piper", "es_AR-daniela-high.onnx"
)
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

PTT_CONFIG_FILE = os.path.join(str(USER_DATA_DIR), "config", "ptt_settings.json")
WINDOW_GEOMETRY_FILE = os.path.join(str(USER_DATA_DIR), "config", "window_geometry.json")
LAST_MODEL_FILE = os.path.join(str(USER_DATA_DIR), "config", "last_model.json")
LLM_TIERS_FILE = os.path.join(str(USER_DATA_DIR), "config", "llm_tiers.json")
ACCIONES_LOG_FILE = os.path.join(str(USER_DATA_DIR), "logs", "acciones.jsonl")

# Writable paths for Cohost, Music, and Avatar modules
COHOST_PROFILES_FILE = os.path.join(str(USER_DATA_DIR), "cohost_profiles.json")
MUSIC_DIR = os.path.join(str(USER_DATA_DIR), "assets", "music")
MUSIC_CONFIG_FILE = os.path.join(str(USER_DATA_DIR), "config", "music_library.json")
AVATAR_CONFIG_FILE = os.path.join(str(USER_DATA_DIR), "config", "avatar.yaml")

# Paths that must live under USER_DATA_DIR so packaged builds under
# Program Files (read-only) do not crash on first write.
EDITORIAL_CARDS_DB = os.path.join(str(USER_DATA_DIR), "data", "editorial_cards", "cards.db")
REFERENCE_WAV_PATH = os.path.join(str(USER_DATA_DIR), "referencia_grabada.wav")


# ──────────────────────────────────────────────
# Experimental feature flags
# ──────────────────────────────────────────────

def _resolve_experimental_heavy_tts() -> bool:
    """Return whether the experimental Qwen3-TTS heavy-TTS UI option is visible.

    Logic:
      - Frozen (packaged) builds default to False — the engine gates and
        auto-fallback remain active; only the UI affordance is hidden.
      - OPENCOHOST_EXPERIMENTAL_TTS=1 overrides to True in any environment.
      - Dev builds (not frozen) default to True.
    """
    env_override = os.environ.get("OPENCOHOST_EXPERIMENTAL_TTS", "")
    if env_override == "1":
        return True
    if getattr(sys, "frozen", False):
        return False
    return True


EXPERIMENTAL_HEAVY_TTS_ENABLED: bool = _resolve_experimental_heavy_tts()


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
OLLAMA_CHAT_TIMEOUT = 180    # max seconds to wait for an Ollama chat generation
HEALTH_POLL_INTERVAL = 5     # seconds between overall health polls


# ──────────────────────────────────────────────
# Model persistence
# ──────────────────────────────────────────────

def _canonical_model_tag(tag: str) -> str:
    """Normalize a model tag for persistence and runtime comparisons."""
    normalized = str(tag or "").strip()
    if normalized.endswith(":latest"):
        return normalized[:-7]
    return normalized


def _normalize_installed_model_tags(
    installed_model_tags: Iterable[str],
) -> set[str]:
    """Return canonical installed-model tags from any discovery source."""
    normalized: set[str] = set()
    for tag in installed_model_tags:
        canonical = _canonical_model_tag(tag)
        if canonical:
            normalized.add(canonical)
    return normalized


def _discover_installed_model_tags() -> set[str]:
    """Best-effort runtime discovery of locally installed Ollama models."""
    try:
        import ollama

        return _normalize_installed_model_tags(
            getattr(model, "model", "")
            for model in getattr(ollama.list(), "models", [])
        )
    except Exception:
        return set()


def is_runtime_model_available(
    tag: str,
    installed_model_tags: Optional[Iterable[str]] = None,
) -> bool:
    """Return whether a tag is safe to use as a runtime model candidate."""
    canonical = _canonical_model_tag(tag)
    if not canonical:
        return False
    if canonical in MODELS_CATALOG:
        return True

    normalized = (
        _normalize_installed_model_tags(installed_model_tags)
        if installed_model_tags is not None
        else _discover_installed_model_tags()
    )
    return canonical in normalized


def resolve_startup_model(
    installed_model_tags: Optional[Iterable[str]] = None,
) -> tuple[str, str]:
    """Return (model_tag, source) for startup.

    Sources:
        'saved' - saved curated model read from last_model.json
        'saved_runtime' - saved non-curated model still available at runtime
        'default' - no saved model found, using DEFAULT_MODEL
        'invalid_saved_fallback' - saved model is not runtime-valid
    """
    try:
        if os.path.exists(LAST_MODEL_FILE):
            with open(LAST_MODEL_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            saved = _canonical_model_tag(data.get("model", ""))
            if saved and is_runtime_model_available(saved, installed_model_tags):
                source = "saved" if saved in MODELS_CATALOG else "saved_runtime"
                return saved, source
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


def resolve_llm_tiers(
    installed_model_tags: Optional[Iterable[str]] = None,
) -> dict[str, str]:
    """Return configurable manual LLM tier slots with safe defaults.

    The file is intentionally simple so operators can adjust the three manual
    buttons without touching Python code:

        config/llm_tiers.json
        {"quality": "gemma4:e4b", "balanced": "llama3", "fast": "qwen3:1.7b"}
    """
    tiers = dict(DEFAULT_LLM_TIERS)
    try:
        if os.path.exists(LLM_TIERS_FILE):
            with open(LLM_TIERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for tier in tiers:
                    model = data.get(tier)
                    if not isinstance(model, str):
                        continue
                    candidate = _canonical_model_tag(model)
                    if candidate and is_runtime_model_available(
                        candidate,
                        installed_model_tags,
                    ):
                        tiers[tier] = candidate
    except Exception:
        pass
    return tiers
