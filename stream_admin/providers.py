from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


class ProviderError(RuntimeError):
    pass


class ProviderAuthError(ProviderError):
    pass


class ProviderUnsupportedError(ProviderError):
    pass


@dataclass
class StreamMetadata:
    title: str = ""
    category_id: str = ""
    category_name: str = ""
    description: str = ""
    tags: List[str] = field(default_factory=list)
    video_id: str = ""
    live_chat_id: str = ""
    status: str = "unknown"
    provider: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "category_id": self.category_id,
            "category_name": self.category_name,
            "description": self.description,
            "tags": list(self.tags),
            "video_id": self.video_id,
            "live_chat_id": self.live_chat_id,
            "status": self.status,
            "provider": self.provider,
            "raw": self.raw,
        }


class BaseStreamProvider:
    name = "base"
    supports_write = False
    supports_chat_write = False
    supports_timeout_ban = False
    supports_slow_mode = False

    def __init__(self, config: dict, oauth_store, log_callback=None):
        self.config = config or {}
        self.oauth_store = oauth_store
        self.log_callback = log_callback
        self.connected = False
        self.account = {}

    def authenticate(self, request_write_scopes: bool = False) -> dict:
        raise ProviderUnsupportedError("Provider not implemented")

    def disconnect(self):
        self.oauth_store.delete(self.name)
        self.connected = False
        self.account = {}

    def read_metadata(self) -> StreamMetadata:
        raise ProviderUnsupportedError("Provider does not support metadata")

    def update_metadata(self, metadata: dict) -> StreamMetadata:
        raise ProviderUnsupportedError("Provider does not support metadata writes")

    def post_chat_message(self, message: str, live_chat_id: Optional[str] = None) -> dict:
        raise ProviderUnsupportedError("Provider does not support chat writes")

    def moderate_user(self, action: str, user_channel_id: str, reason: str = "", duration_seconds: int = 300, live_chat_id: Optional[str] = None) -> dict:
        raise ProviderUnsupportedError("Provider does not support moderation")

    def set_slow_mode(self, enabled: bool, delay_seconds: int = 10, duration_minutes: int = 5) -> dict:
        raise ProviderUnsupportedError("Slow Mode is not supported by this provider")

    def status(self) -> dict:
        return {
            "provider": self.name,
            "connected": self.connected or self.oauth_store.has_token(self.name),
            "account": self.account,
            "supports_write": self.supports_write,
            "supports_chat_write": self.supports_chat_write,
            "supports_timeout_ban": self.supports_timeout_ban,
            "supports_slow_mode": self.supports_slow_mode,
        }

    def _log(self, message: str):
        if self.log_callback:
            self.log_callback(message)
