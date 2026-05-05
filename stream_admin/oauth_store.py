import json
import os
import time
from typing import Optional


class OAuthStore:
    def __init__(self, token_path: str):
        self.token_path = token_path

    def load(self, provider: str) -> Optional[dict]:
        data = self._load_all()
        token = data.get(provider)
        if isinstance(token, dict):
            return token
        return None

    def save(self, provider: str, token: dict):
        data = self._load_all()
        token = dict(token)
        if "expires_in" in token and "expires_at" not in token:
            try:
                token["expires_at"] = time.time() + float(token["expires_in"]) - 60
            except (TypeError, ValueError):
                pass
        data[provider] = token
        os.makedirs(os.path.dirname(self.token_path), exist_ok=True)
        with open(self.token_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        self._restrict_permissions()

    def delete(self, provider: str):
        data = self._load_all()
        if provider in data:
            del data[provider]
        os.makedirs(os.path.dirname(self.token_path), exist_ok=True)
        with open(self.token_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        self._restrict_permissions()

    def has_token(self, provider: str) -> bool:
        return self.load(provider) is not None

    def _load_all(self) -> dict:
        if not os.path.exists(self.token_path):
            return {}
        try:
            with open(self.token_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _restrict_permissions(self):
        try:
            os.chmod(self.token_path, 0o600)
        except Exception:
            pass


def redact_token_text(value: str) -> str:
    if not value:
        return value
    redacted = str(value)
    for marker in ("access_token", "refresh_token", "client_secret"):
        redacted = redacted.replace(marker, f"{marker}_redacted")
    return redacted
