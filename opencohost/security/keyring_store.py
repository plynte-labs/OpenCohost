"""OS Keyring-backed credential store for OpenCohost.

Provides secure credential storage for Cloud LLM API keys via the OS Keyring
(Windows Credential Manager / macOS Keychain / Linux SecretService), eliminating
plaintext API keys on disk.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Optional, Set

try:
    import keyring
    from keyring.backend import KeyringBackend
    import keyring.errors

    _KEYRING_AVAILABLE = True
except (ImportError, ModuleNotFoundError):
    keyring = None  # type: ignore
    KeyringBackend = object  # type: ignore
    _KEYRING_AVAILABLE = False

from opencohost.config.storage import atomic_write_text

logger = logging.getLogger(__name__)

SERVICE_NAME = "opencohost:cloud_llm"
PROFILE_INDEX_KEY = "__profiles__"

_FALLBACK_VAULT: dict[tuple[str, str], str] = {}
_FALLBACK_LOCK = threading.Lock()


class KeyStoreError(OSError):
    """Raised when an operation on the secure credential store fails."""


class MemoryKeyring(KeyringBackend):
    """In-memory keyring backend for isolated tests and headless environments."""

    priority = 0

    def __init__(self) -> None:
        self._vault: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()

    def set_password(self, service: str, username: str, password: str) -> None:
        with self._lock:
            self._vault[(service, username)] = password

    def get_password(self, service: str, username: str) -> Optional[str]:
        with self._lock:
            return self._vault.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        with self._lock:
            if (service, username) not in self._vault:
                if _KEYRING_AVAILABLE and hasattr(keyring, "errors"):
                    raise keyring.errors.PasswordDeleteError(
                        f"Password for {username} in {service} not found"
                    )
                return
            del self._vault[(service, username)]

    def clear(self) -> None:
        with self._lock:
            self._vault.clear()


class KeyringStore:
    """Manages profile API keys using the system credential manager."""

    def __init__(
        self,
        service_name: str = SERVICE_NAME,
        legacy_token_path: Optional[str] = None,
    ) -> None:
        self.service_name = service_name
        self.legacy_token_path = legacy_token_path
        if self.legacy_token_path and os.path.exists(self.legacy_token_path):
            self._migrate_legacy_file(self.legacy_token_path)

    def _secure_backend(self):
        """Select a durable OS backend only; never fall back or log backend errors.

        This strict surface is opt-in. Legacy cloud/OAuth methods keep their
        historical fallback and migration behavior unchanged.
        """
        durable_modules = {
            "keyring.backends.Windows", "keyring.backends.macOS",
            "keyring.backends.SecretService", "keyring.backends.kwallet",
            "keyring.backends.libsecret",
        }
        try:
            if not _KEYRING_AVAILABLE or keyring is None:
                raise KeyStoreError("secure_store_unavailable")
            pending = [keyring.get_keyring()]
            seen = set()
            while pending:
                backend = pending.pop()
                identity = id(backend)
                if identity in seen:
                    continue
                seen.add(identity)

                if type(backend).__module__ in durable_modules:
                    return backend

                children = getattr(backend, "backends", ())
                pending.extend(reversed(tuple(children)))
            raise KeyStoreError("secure_store_unavailable")
        except Exception:
            raise KeyStoreError("secure_store_unavailable") from None

    def load_secure(self, provider: str) -> Optional[dict]:
        """Read without plaintext migration, RAM fallback or exception logging."""
        try:
            raw = self._secure_backend().get_password(self.service_name, provider)
            if raw is None:
                return None
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise KeyStoreError("secure_store_unavailable")
            return value
        except Exception:
            raise KeyStoreError("secure_store_unavailable") from None

    def save_secure(self, provider: str, token: dict) -> None:
        """Write only to the selected OS store, without legacy/profile side effects."""
        try:
            self._secure_backend().set_password(self.service_name, provider, json.dumps(token))
        except Exception:
            raise KeyStoreError("secure_store_unavailable") from None

    def delete_secure(self, provider: str) -> None:
        """Idempotently delete from the OS store, without fallback or raw errors."""
        try:
            backend = self._secure_backend()
            if backend.get_password(self.service_name, provider) is not None:
                backend.delete_password(self.service_name, provider)
        except Exception:
            raise KeyStoreError("secure_store_unavailable") from None

    def _get_tracked_profiles(self) -> Set[str]:
        try:
            raw = self._get_secret(PROFILE_INDEX_KEY)
            if not raw:
                return set()
            data = json.loads(raw)
            return set(data) if isinstance(data, list) else set()
        except Exception:
            return set()

    def _track_profile(self, profile_id: str) -> None:
        try:
            profiles = self._get_tracked_profiles()
            if profile_id not in profiles:
                profiles.add(profile_id)
                self._set_secret(PROFILE_INDEX_KEY, json.dumps(sorted(profiles)))
        except Exception:
            pass

    def _untrack_profile(self, profile_id: str) -> None:
        try:
            profiles = self._get_tracked_profiles()
            if profile_id in profiles:
                profiles.remove(profile_id)
                self._set_secret(PROFILE_INDEX_KEY, json.dumps(sorted(profiles)))
        except Exception:
            pass

    def _get_secret(self, key: str) -> Optional[str]:
        if _KEYRING_AVAILABLE and keyring is not None:
            return keyring.get_password(self.service_name, key)
        with _FALLBACK_LOCK:
            return _FALLBACK_VAULT.get((self.service_name, key))

    def _set_secret(self, key: str, value: str) -> None:
        if _KEYRING_AVAILABLE and keyring is not None:
            keyring.set_password(self.service_name, key, value)
        else:
            with _FALLBACK_LOCK:
                _FALLBACK_VAULT[(self.service_name, key)] = value

    def _delete_secret(self, key: str) -> None:
        if _KEYRING_AVAILABLE and keyring is not None:
            try:
                keyring.delete_password(self.service_name, key)
            except keyring.errors.PasswordDeleteError:
                pass
        else:
            with _FALLBACK_LOCK:
                _FALLBACK_VAULT.pop((self.service_name, key), None)

    def load(self, provider: str) -> Optional[dict]:
        """Load the credentials dictionary for the given profile."""
        try:
            raw = self._get_secret(provider)
            if raw is None:
                # Fallback: if not yet migrated from legacy path, check it
                if self.legacy_token_path and os.path.exists(self.legacy_token_path):
                    legacy = self._load_legacy_token(provider)
                    if legacy is not None:
                        self.save(provider, legacy)
                        return legacy
                return None
            try:
                data = json.loads(raw)
                return data if isinstance(data, dict) else {"api_key": raw}
            except json.JSONDecodeError:
                return {"api_key": raw}
        except Exception as e:
            if _KEYRING_AVAILABLE and isinstance(e, keyring.errors.KeyringError):
                logger.warning("Failed to load key for %s from keyring: %s", provider, e)
                return None
            logger.warning("Unexpected error loading key for %s: %s", provider, e)
            return None

    def save(self, provider: str, token: dict) -> None:
        """Store the credentials dictionary securely in the OS keyring."""
        token_dict = dict(token)
        serialized = json.dumps(token_dict)
        try:
            self._set_secret(provider, serialized)
            self._track_profile(provider)
            # Purge from legacy file if present so plaintext is never left behind
            if self.legacy_token_path and os.path.exists(self.legacy_token_path):
                self._delete_legacy_token(provider)
        except OSError:
            raise
        except Exception as e:
            logger.exception("Unexpected error saving key for provider %s: %s", provider, e)
            raise KeyStoreError(f"key_store_save_failed: {e}") from e

    def delete(self, provider: str) -> None:
        """Remove the credentials for the given profile from the OS keyring."""
        try:
            self._delete_secret(provider)
            self._untrack_profile(provider)
            if self.legacy_token_path and os.path.exists(self.legacy_token_path):
                self._delete_legacy_token(provider)
        except OSError:
            raise
        except Exception as e:
            logger.exception("Unexpected error deleting key for provider %s: %s", provider, e)
            raise KeyStoreError(f"key_store_delete_failed: {e}") from e

    def has_token(self, provider: str) -> bool:
        """Check whether credentials exist for the given profile."""
        return self.load(provider) is not None

    def _migrate_legacy_file(self, file_path: str) -> None:
        """One-way migration of plaintext JSON keys to the OS keyring."""
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not data:
                return
            migrated_any = False
            for pid, token in data.items():
                if isinstance(token, dict) and token.get("api_key"):
                    existing = self.load(pid)
                    if not existing or not existing.get("api_key"):
                        self.save(pid, token)
                        migrated_any = True
            if migrated_any:
                logger.info("Migrated plaintext LLM API keys from %s into OS keyring.", file_path)
                try:
                    atomic_write_text(file_path, "{}\n")
                except PermissionError:
                    try:
                        with open(file_path, "w", encoding="utf-8") as f:
                            f.write("{}\n")
                    except Exception:
                        pass
        except Exception as e:
            logger.warning("Could not migrate legacy keys file %s: %s", file_path, e)

    def _load_legacy_token(self, provider: str) -> Optional[dict]:
        if not self.legacy_token_path or not os.path.exists(self.legacy_token_path):
            return None
        try:
            with open(self.legacy_token_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            token = data.get(provider)
            return token if isinstance(token, dict) else None
        except Exception:
            return None

    def _delete_legacy_token(self, provider: str) -> None:
        if not self.legacy_token_path or not os.path.exists(self.legacy_token_path):
            return
        try:
            with open(self.legacy_token_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if provider in data:
                del data[provider]
                try:
                    atomic_write_text(self.legacy_token_path, json.dumps(data, indent=2))
                except PermissionError:
                    with open(self.legacy_token_path, "w", encoding="utf-8") as f:
                        json.dump(data, f, indent=2)
        except Exception:
            pass


_STORE_INSTANCES: dict[tuple[str, Optional[str]], KeyringStore] = {}
_STORE_LOCK = threading.Lock()


def get_keyring_store(
    legacy_token_path: Optional[str] = None, service_name: str = SERVICE_NAME
) -> KeyringStore:
    """Return a singleton KeyringStore instance for the given service and legacy path."""
    key = (service_name, legacy_token_path)
    with _STORE_LOCK:
        if key not in _STORE_INSTANCES:
            _STORE_INSTANCES[key] = KeyringStore(
                service_name=service_name, legacy_token_path=legacy_token_path
            )
        return _STORE_INSTANCES[key]
