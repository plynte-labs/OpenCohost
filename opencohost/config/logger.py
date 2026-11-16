import os
import re
import time
import logging
import traceback
from opencohost.config.settings import LOG_DIR


def _debug_enabled() -> bool:
    """DEBUG logging is enabled when OPENCOHOST_DEBUG=1."""
    return os.getenv("OPENCOHOST_DEBUG") == "1"


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
        # web-search-api-fallback-20261010 T5: also covers x-subscription-token and bare tvly-* keys.
        re.compile(r"tvly-[A-Za-z0-9_\-]+"),
        re.compile(r"(?i)(access_token|refresh_token|client_secret|id_token|api_key|x-subscription-token)['\"]?\s*[:=]\s*['\"]?[^'\",}\s]+"),
        re.compile(r"(?i)(liveChatId|live_chat_id|channelId|author_channel_id)\s*[:=]\s*['\"]?[^'\",}\s]+"),
    ]

    @classmethod
    def redact(cls, text: str) -> str:
        if not text:
            return "" if text is not None else None
        for pattern in cls._patterns:
            text = pattern.sub(lambda m: f"{m.group(1)}=<redacted>" if m.lastindex else "<redacted>", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        # Redact message and formatted args
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        record.msg = self.redact(message)
        record.args = ()

        # Redact cached traceback representations and exc_info
        if getattr(record, "exc_text", None):
            record.exc_text = self.redact(record.exc_text)
        elif getattr(record, "exc_info", None):
            try:
                exc_info = record.exc_info
                if isinstance(exc_info, tuple) and len(exc_info) == 3:
                    raw_tb = "".join(traceback.format_exception(*exc_info)).rstrip("\n")
                elif isinstance(exc_info, BaseException):
                    raw_tb = "".join(traceback.format_exception(type(exc_info), exc_info, exc_info.__traceback__)).rstrip("\n")
                else:
                    raw_tb = None
                if raw_tb:
                    record.exc_text = self.redact(raw_tb)
            except Exception:
                pass

        # Redact stack_info if present
        if getattr(record, "stack_info", None):
            record.stack_info = self.redact(record.stack_info)

        return True


# ──────────────────────────────────────────────
# Logging estructurado
# ──────────────────────────────────────────────
class SensitiveDataFormatter(logging.Formatter):
    """Formatter that ensures formatted messages and exceptions are sanitized."""

    def format(self, record: logging.LogRecord) -> str:
        s = super().format(record)
        return SensitiveDataFilter.redact(s)

    def formatException(self, ei) -> str:
        s = super().formatException(ei)
        return SensitiveDataFilter.redact(s)

    def formatStack(self, stack_info: str) -> str:
        s = super().formatStack(stack_info)
        return SensitiveDataFilter.redact(s)


log_formatter = SensitiveDataFormatter(
    "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S"
)

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
