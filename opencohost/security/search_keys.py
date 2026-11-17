"""Shared Tavily key resolution and write-only secure settings.

Environment presence/value are captured by settings at startup. Only an absent
setting consults OS storage; an explicit empty environment disables Tavily.
No operation here validates the provider or spends search quota.
"""
from opencohost.config import settings
from opencohost.security.keyring_store import KeyStoreError, get_keyring_store


def _store():
    return get_keyring_store(service_name="opencohost:search", legacy_token_path=None)


def _environment() -> tuple[bool, str]:
    value = settings.TAVILY_API_KEY or ""
    # Preserve nonempty settings injected by existing callers/tests.
    return settings.TAVILY_API_KEY_ENV_PRESENT or bool(value), value


def _stored_key() -> str:
    value = _store().load_secure("tavily")
    if value is None:
        return ""
    key = value.get("api_key")
    if not isinstance(key, str):
        raise KeyStoreError("tavily_key_store_unavailable")
    return key.strip()


def get_tavily_key() -> str:
    explicit, value = _environment()
    if explicit:
        return value
    try:
        return _stored_key()
    except KeyStoreError:
        return ""


def get_tavily_status() -> dict:
    explicit, value = _environment()
    try:
        stored = bool(_stored_key())
        available = True
    except KeyStoreError:
        stored, available = False, False
    source = ("environment" if value else "environment_disabled") if explicit else ("keyring" if stored else "none")
    return {"api_key_set": bool(value) if explicit else stored,
            "stored_key_set": stored, "source": source, "storage_available": available}


def save_tavily_key(value: str) -> dict:
    value = value.strip()
    try:
        store = _store()
        if value:
            store.save_secure("tavily", {"api_key": value})
        else:
            store.delete_secure("tavily")
        # Do not report success if the selected backend silently ignored a
        # mutation. Readback is local OS storage only, never a provider probe.
        if _stored_key() != value:
            raise KeyStoreError("tavily_key_store_unavailable")
        status = get_tavily_status()
        if not status["storage_available"]:
            raise KeyStoreError("tavily_key_store_unavailable")
        return status
    except KeyStoreError:
        raise KeyStoreError("tavily_key_store_unavailable") from None
