import os
import time
import logging
from config.settings import LOG_DIR

# ──────────────────────────────────────────────
# Logging estructurado
# ──────────────────────────────────────────────
log_formatter = logging.Formatter(
    "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S"
)

file_handler = logging.FileHandler(
    os.path.join(LOG_DIR, f"voiceai_{time.strftime('%Y%m%d_%H%M%S')}.log"),
    encoding="utf-8"
)
file_handler.setFormatter(log_formatter)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

logger = logging.getLogger("VoiceAI")
logger.setLevel(logging.DEBUG)
logger.addHandler(file_handler)
logger.addHandler(console_handler)

def get_logger():
    return logger
