import atexit
import sys

import customtkinter as ctk
from ui.app import VocalAIApp
from config.logger import get_logger

logger = get_logger()

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
        import ollama
        model = getattr(getattr(_app_instance, "motor_ia", None), "current_model", None)
        if model:
            ollama.generate(model=model, prompt="", keep_alive=0)
    except Exception:
        pass


def main():
    global _app_instance
    logger.info("=== VoiceAI Starting ===")
    atexit.register(_emergency_cleanup)
    _app_instance = VocalAIApp()
    _app_instance.protocol("WM_DELETE_WINDOW", _app_instance.on_closing)
    _app_instance.mainloop()


if __name__ == "__main__":
    main()
