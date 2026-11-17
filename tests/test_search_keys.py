"""Secure Tavily settings; all credential I/O uses an isolated fake OS backend."""
from types import SimpleNamespace

import pytest

from opencohost.config import settings
from opencohost.security import keyring_store

CANARY = "tvly-CANARY-not-a-real-credential"


class FakeSecureBackend:
    __module__ = "keyring.backends.Windows"

    def __init__(self):
        self.vault = {}
        self.failure = False

    def get_password(self, service, provider):
        if self.failure:
            raise RuntimeError(CANARY)
        return self.vault.get((service, provider))

    def set_password(self, service, provider, value):
        if self.failure:
            raise RuntimeError(CANARY)
        self.vault[(service, provider)] = value

    def delete_password(self, service, provider):
        if self.failure:
            raise RuntimeError(CANARY)
        self.vault.pop((service, provider), None)


@pytest.fixture
def secure_search(monkeypatch):
    from opencohost.security import search_keys

    backend = FakeSecureBackend()
    monkeypatch.setattr(keyring_store, "_KEYRING_AVAILABLE", True)
    monkeypatch.setattr(keyring_store, "keyring", SimpleNamespace(get_keyring=lambda: backend))
    monkeypatch.setattr(search_keys, "get_keyring_store", lambda **kwargs: keyring_store.KeyringStore(**kwargs))
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "")
    monkeypatch.setattr(settings, "TAVILY_API_KEY_ENV_PRESENT", False, raising=False)
    return search_keys, backend


def test_saved_key_survives_new_service_instance(secure_search):
    service, backend = secure_search
    assert service.save_tavily_key("  " + CANARY + "  ") == {
        "api_key_set": True, "stored_key_set": True, "source": "keyring", "storage_available": True
    }
    assert service.get_tavily_key() == CANARY
    assert backend.vault.keys() == {("opencohost:search", "tavily")}
    assert service.get_tavily_status()["stored_key_set"] is True
    service.save_tavily_key("")
    assert service.get_tavily_key() == ""
    assert backend.vault == {}


@pytest.mark.parametrize("value,source", [("env-canary", "environment"), ("", "environment_disabled")])
def test_explicit_env_key_and_empty_disable_win(secure_search, monkeypatch, value, source):
    service, _ = secure_search
    service.save_tavily_key(CANARY)
    monkeypatch.setattr(settings, "TAVILY_API_KEY_ENV_PRESENT", True)
    monkeypatch.setattr(settings, "TAVILY_API_KEY", value)
    monkeypatch.setenv("OPENCOHOST_TAVILY_API_KEY", "changed-after-import")
    assert service.get_tavily_key() == value
    assert service.get_tavily_status()["source"] == source
    assert service.get_tavily_status()["stored_key_set"] is True


def test_remove_stored_key_keeps_environment_source(secure_search, monkeypatch):
    service, _ = secure_search
    service.save_tavily_key(CANARY)
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "injected-env-key")
    status = service.save_tavily_key("")
    assert status == {"api_key_set": True, "stored_key_set": False, "source": "environment", "storage_available": True}
    from opencohost.core.web.lookup import _get_api_key
    assert _get_api_key("tavily") == service.get_tavily_key() == "injected-env-key"


@pytest.mark.parametrize("operation", ["read", "save", "delete"])
def test_backend_failure_never_logs_or_returns_canary(secure_search, caplog, operation):
    service, backend = secure_search
    backend.failure = True
    if operation == "read":
        assert service.get_tavily_key() == ""
        assert service.get_tavily_status() == {
            "api_key_set": False, "stored_key_set": False, "source": "none", "storage_available": False
        }
    else:
        with pytest.raises(keyring_store.KeyStoreError) as exc:
            service.save_tavily_key(CANARY if operation == "save" else "")
        assert str(exc.value) == "tavily_key_store_unavailable"
    assert CANARY not in caplog.text


@pytest.mark.parametrize("backend", [None, keyring_store.MemoryKeyring()])
def test_ram_or_unavailable_storage_is_not_durable(secure_search, monkeypatch, backend):
    service, _ = secure_search
    monkeypatch.setattr(keyring_store, "keyring", SimpleNamespace(get_keyring=lambda: backend))
    with pytest.raises(keyring_store.KeyStoreError):
        service.save_tavily_key(CANARY)
    assert service.get_tavily_status()["storage_available"] is False


def test_no_legacy_path_or_cloud_namespace(secure_search, monkeypatch):
    service, _ = secure_search
    calls = []
    def factory(**kwargs):
        calls.append(kwargs)
        return keyring_store.KeyringStore(**kwargs)
    monkeypatch.setattr(service, "get_keyring_store", factory)
    service.save_tavily_key(CANARY)
    assert calls and all(c == {"service_name": "opencohost:search", "legacy_token_path": None} for c in calls)


@pytest.mark.parametrize("operation", ["save", "delete"])
def test_unconfirmed_store_mutation_never_reports_success(secure_search, monkeypatch, operation):
    service, backend = secure_search
    if operation == "save":
        monkeypatch.setattr(backend, "set_password", lambda *args: None)
        value = CANARY
    else:
        service.save_tavily_key(CANARY)
        monkeypatch.setattr(backend, "delete_password", lambda *args: None)
        value = ""
    with pytest.raises(keyring_store.KeyStoreError, match="tavily_key_store_unavailable"):
        service.save_tavily_key(value)


def test_environment_still_effective_when_storage_is_unavailable(secure_search, monkeypatch):
    service, backend = secure_search
    backend.failure = True
    monkeypatch.setattr(settings, "TAVILY_API_KEY", "environment-canary")
    assert service.get_tavily_key() == "environment-canary"
    assert service.get_tavily_status() == {
        "api_key_set": True, "stored_key_set": False,
        "source": "environment", "storage_available": False
    }
