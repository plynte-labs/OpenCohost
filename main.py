import atexit
import sys

import customtkinter as ctk
from ui.app import VocalAIApp
from config.logger import get_logger

logger = get_logger()

# Cached ollama reference to prevent dynamic import failures inside atexit handler
try:
    import ollama
except ImportError:
    ollama = None

# ──────────────────────────────────────────────
# UI Theme
# ──────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

# Global app reference for atexit cleanup
_app_instance = None


def _emergency_cleanup():
    """Release VRAM and stop subprocesses even on crash."""
    global _app_instance
    if _app_instance is None:
        return
    try:
        # Stop health monitor (kills Qwen subprocess if managed)
        if hasattr(_app_instance, "health_monitor") and _app_instance.health_monitor:
            _app_instance.health_monitor.stop()
    except Exception:
        pass
    try:
        # Unload Ollama model from VRAM
        if ollama is not None:
            model = getattr(getattr(_app_instance, "motor_ia", None), "current_model", None)
            if model:
                ollama.generate(model=model, prompt="", keep_alive=0)
    except Exception:
        pass


def main():
    global _app_instance
    logger.info("=== OpenCohost Starting ===")
    
    # Explicitly initialize environment vars and directories with robust permission error catch
    try:
        from config.storage import apply_storage_environment
        apply_storage_environment()
    except PermissionError as exc:
        logger.critical(f"Falla de permisos al crear directorios de trabajo: {exc}")
        # En una app de producción, aquí se puede alertar visualmente al usuario
        # antes de realizar un apagado ordenado y limpio del hilo principal.
        sys.exit(1)
    except Exception as exc:
        logger.error(f"Falla inesperada al inicializar el almacenamiento: {exc}")

    atexit.register(_emergency_cleanup)
    _app_instance = VocalAIApp()
    _app_instance.protocol("WM_DELETE_WINDOW", _app_instance.on_closing)
    _app_instance.mainloop()


if __name__ == "__main__":
    main()
