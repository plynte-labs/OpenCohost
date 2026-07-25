import json
import os
import re
import time
from typing import Optional

from opencohost.config.storage import atomic_write_text


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
        self._write_all(data)

    def delete(self, provider: str):
        data = self._load_all()
        if provider in data:
            del data[provider]
        self._write_all(data)

    def has_token(self, provider: str) -> bool:
        return self.load(provider) is not None

    def _write_all(self, data: dict) -> None:
        """Persist *data* atomically (temp file + os.replace).

        F4: a torn plain write (open('w') + json.dump) can leave the file
        truncated/invalid; ``_load_all`` then silently returns ``{}`` --
        every provider's key reads as absent. Reuse the same tempfile +
        os.replace pattern as ``opencohost.config.storage.atomic_write_text``.
        """
        text = json.dumps(data, indent=2)
        try:
            atomic_write_text(self.token_path, text)
        except PermissionError:
            # Windows self-heal. A prior _restrict_permissions() granted the
            # owner only (R,W) -- which does NOT include DELETE. os.replace()
            # must delete the existing target to swap the new file in, but a
            # config dir that grants only Modify (no FILE_DELETE_CHILD) leaves
            # the owner unable to delete the restricted file, so the replace is
            # denied ([WinError 5]). Before this guard, every save AND delete
            # after the first raised OSError -> the API returned 503. Re-grant
            # the owner DELETE on the existing target, then retry the atomic
            # write once; _restrict_permissions below re-seals it owner-only.
            if not self._make_target_replaceable():
                raise
            atomic_write_text(self.token_path, text)
        self._restrict_permissions()

    def _make_target_replaceable(self) -> bool:
        """Windows only: re-grant the owner DELETE on an already-restricted
        token file so ``os.replace`` can delete/swap it.

        Returns True when a re-grant was attempted (POSIX, a missing file, or
        no USERNAME -> False, and the caller re-raises the original error). The
        grant preserves the stripped inheritance, and ``_restrict_permissions``
        re-applies the owner-only restriction after the retry succeeds.
        """
        if os.name != "nt" or not os.path.exists(self.token_path):
            return False
        username = os.environ.get("USERNAME", "")
        if not username:
            return False
        try:
            import subprocess
            subprocess.run(
                ["icacls", self.token_path, "/grant:r", f"{username}:(R,W,D)"],
                capture_output=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return True
        except Exception:
            return False

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
        """Restrict token file permissions.

        On Windows, os.chmod(0o600) is ineffective on NTFS.
        We use icacls to strip inherited permissions and grant
        only the current user read/write/delete access. DELETE is required
        so the next atomic save's os.replace() can swap this file in a config
        dir that grants only Modify (no FILE_DELETE_CHILD); it does not widen
        exposure -- inheritance is still stripped and only the owner is granted.
        """
        try:
            if os.name == "nt":
                import subprocess
                username = os.environ.get("USERNAME", "")
                if username:
                    subprocess.run(
                        ["icacls", self.token_path, "/inheritance:r",
                         "/grant:r", f"{username}:(R,W,D)"],
                        capture_output=True, timeout=5,
                        creationflags=subprocess.CREATE_NO_WINDOW,
                    )
            else:
                os.chmod(self.token_path, 0o600)
        except Exception:
            pass


def redact_token_text(value: str) -> str:
    if not value:
        return value
    redacted = str(value)
    patterns = [
        r"ya29\.[A-Za-z0-9._\-]+",
        r"1//[A-Za-z0-9._\-]+",
        r"GOCSPX-[A-Za-z0-9_\-]+",
        r"Bearer\s+[A-Za-z0-9._\-]+",
        r"(?i)(access_token|refresh_token|client_secret|id_token)\s*[:=]\s*['\"]?[^'\",}\s]+",
        r"(?i)(liveChatId|live_chat_id|channelId|author_channel_id)\s*[:=]\s*['\"]?[^'\",}\s]+",
    ]
    for pattern in patterns:
        redacted = re.sub(pattern, lambda m: f"{m.group(1)}=<redacted>" if m.lastindex else "<redacted>", redacted)
    return redacted
