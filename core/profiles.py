import os
import json
from config.settings import PROFILES_FILE, SYSTEM_PROMPT
from config.logger import get_logger

logger = get_logger()

def cargar_perfiles():
    if os.path.exists(PROFILES_FILE):
        try:
            with open(PROFILES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error cargando perfiles: {e}")
    # Default inicial
    return {
        "Kira (Default)": {
            "prompt": SYSTEM_PROMPT,
            "use_system": False
        }
    }

def guardar_perfiles(perfiles):
    try:
        with open(PROFILES_FILE, "w", encoding="utf-8") as f:
            json.dump(perfiles, f, indent=4, ensure_ascii=False)
    except Exception as e:
        logger.error(f"Error guardando perfiles: {e}")
