import os
import re
import time
import logging
from opencohost.config.settings import LOG_DIR


def _debug_enabled() -> bool:
    """DEBUG logging is enabled when OPENCOHOST_DEBUG=1."""
    return os.getenv("OPENCOHOST_DEBUG") == "1"

# ──────────────────────────────────────────────
# Logging estructurado
# ──────────────────────────────────────────────
log_formatter = logging.Formatter(
    "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S"
)


class SensitiveDataFilter(logging.Filter):
    _patterns = [
        re.compile(r"ya29\.[A-Za-z0-9._\-]+"),
        re.compile(r"1//[A-Za-z0-9._\-]+"),
        re.compile(r"GOCSPX-[A-Za-z0-9_\-]+"),
        re.compile(r"Bearer\s+[A-Za-z0-9._\-]+", re.IGNORECASE),
        # multi_provider_llm_20260723: cloud API key in raw key=value form
        # (outside the Bearer-header form already covered above). Optional
        # closing quote before the separator also matches dict-repr/JSON
        # forms ('api_key': '...', "api_key": "...").
        re.compile(r"(?i)(access_token|refresh_token|client_secret|id_token|api_key)['\"]?\s*[:=]\s*['\"]?[^'\",}\s]+"),
        re.compile(r"(?i)(liveChatId|live_chat_id|channelId|author_channel_id)\s*[:=]\s*['\"]?[^'\",}\s]+"),
    ]

    def filter(self, record):
        message = record.getMessage()
        for pattern in self._patterns:
            message = pattern.sub(lambda m: f"{m.group(1)}=<redacted>" if m.lastindex else "<redacted>", message)
        record.msg = message
        record.args = ()
        return True

file_handler = logging.FileHandler(
    os.path.join(LOG_DIR, f"opencohost_{time.strftime('%Y%m%d_%H%M%S')}.log"),
    encoding="utf-8"
)
file_handler.setFormatter(log_formatter)

console_handler = logging.StreamHandler()
console_handler.setFormatter(log_formatter)

logger = logging.getLogger("OpenCohost")
logger.setLevel(logging.DEBUG if _debug_enabled() else logging.INFO)
logger.addFilter(SensitiveDataFilter())
logger.addHandler(file_handler)
logger.addHandler(console_handler)

def get_logger():
    return logger
