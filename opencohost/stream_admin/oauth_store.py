import json
import logging
import os
import re
import time
from typing import Optional

from opencohost.config.storage import atomic_write_text

logger = logging.getLogger(__name__)


def _current_user_principal(subprocess_mod, creationflags: int) -> str:
    """Return an icacls principal for the current user.

    Prefers the user's SID (``*S-1-5-...``, immune to name/domain quirks) from
    ``whoami /user``; falls back to the USERNAME environment variable. Returns
    "" when neither is available.
    """
    try:
        result = subprocess_mod.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True, timeout=5, creationflags=creationflags,
        )
        if result.returncode == 0:
            out = result.stdout.decode("utf-8", "replace") if isinstance(result.stdout, bytes) else str(result.stdout or "")
            match = re.search(r"S-1-\d+(?:-\d+)+", out)
            if match:
                return f"*{match.group(0)}"
    except Exception:
        pass
    return os.environ.get("USERNAME", "")


def restrict_file_permissions(path: str | os.PathLike) -> bool:
    """Restrict file permissions to the current owner/user only.

    On Windows, os.chmod(0o600) is ineffective on NTFS.
    We use icacls to strip inherited permissions (/inheritance:r) and grant
    only the current user (by SID when available) read/write/delete access.
    DELETE is required so atomic replace (os.replace) can swap this file even in a
    config directory that grants only Modify (no FILE_DELETE_CHILD).
    On non-Windows (POSIX), os.chmod(path, 0o600) is applied.

    Returns True only when the restriction was actually applied. A non-zero
    icacls exit code, a missing principal or any exception returns False and
    logs a warning (never the command output, which may echo paths/names).
    """
    path_str = str(path)
    try:
        if os.name == "nt":
            import subprocess

            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            principal = _current_user_principal(subprocess, creationflags)
            if not principal:
                logger.warning("Could not restrict file permissions: current user unknown")
                return False
            result = subprocess.run(
                ["icacls", path_str, "/inheritance:r",
                 "/grant:r", f"{principal}:(R,W,D)"],
                capture_output=True, timeout=5,
                creationflags=creationflags,
            )
            if result.returncode != 0:
                logger.warning("icacls failed to restrict file permissions (exit code %s)", result.returncode)
                return False
            return True
        os.chmod(path_str, 0o600)
        return True
    except Exception as exc:
        logger.warning("Could not restrict file permissions (%s)", type(exc).__name__)
        return False


class OAuthStore:
    def __init__(self, token_path: str, use_keyring: Optional[bool] = None):
        self.token_path = token_path
        if use_keyring is None:
            use_keyring = self._should_use_keyring(token_path)
        self.use_keyring = use_keyring
        self._keyring_store = None
        if self.use_keyring:
            from opencohost.security.keyring_store import get_keyring_store

            self._keyring_store = get_keyring_store(token_path)

    @staticmethod
    def _should_use_keyring(token_path: str) -> bool:
        try:
            from opencohost.api import deps

            if os.path.normpath(token_path) == os.path.normpath(deps.llm_keys_file()):
                return True
        except Exception:
            pass
        try:
            from opencohost.config.settings import LLM_KEYS_FILE

            if os.path.normpath(token_path) == os.path.normpath(LLM_KEYS_FILE):
                return True
        except Exception:
            pass
        return False

    def load(self, provider: str) -> Optional[dict]:
        if self._keyring_store is not None:
            return self._keyring_store.load(provider)
        data = self._load_all()
        token = data.get(provider)
        if isinstance(token, dict):
            return token
        return None

    def save(self, provider: str, token: dict):
        if self._keyring_store is not None:
            self._keyring_store.save(provider, token)
            return
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
        if self._keyring_store is not None:
            self._keyring_store.delete(provider)
            return
        data = self._load_all()
        if provider in data:
            del data[provider]
        self._write_all(data)

    def has_token(self, provider: str) -> bool:
        if self._keyring_store is not None:
            return self._keyring_store.has_token(provider)
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
        if not restrict_file_permissions(self.token_path):
            logger.warning("Token file permissions could not be restricted")


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
