import customtkinter as ctk
from ui.app import VocalAIApp
from config.logger import get_logger

logger = get_logger()

# ──────────────────────────────────────────────
# UI Theme
# ──────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

def main():
    logger.info("=== VoiceAI Starting ===")
    app = VocalAIApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()

if __name__ == "__main__":
    main()