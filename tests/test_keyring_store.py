"""Unit tests for KeyringStore and secure OS credential storage."""

import json
import pytest

from opencohost.security.keyring_store import (
    KeyringStore,
    MemoryKeyring,
    KeyStoreError,
    SERVICE_NAME,
)
from opencohost.stream_admin.oauth_store import OAuthStore


def test_keyring_store_basic_lifecycle():
    store = KeyringStore(service_name="test:unit")
    assert store.has_token("profile_a") is False
    assert store.load("profile_a") is None

    store.save("profile_a", {"api_key": "sk-secret-key-123"})
    assert store.has_token("profile_a") is True
    assert store.load("profile_a") == {"api_key": "sk-secret-key-123"}

    store.delete("profile_a")
    assert store.has_token("profile_a") is False
    assert store.load("profile_a") is None


def test_keyring_store_migration_from_legacy_plaintext_json(tmp_path):
    legacy_file = tmp_path / "legacy_llm_keys.json"
    legacy_file.write_text(
        json.dumps({
            "openai": {"api_key": "sk-legacy-key-999"},
            "anthropic": {"api_key": "sk-ant-legacy-888"},
        }),
        encoding="utf-8",
    )

    store = KeyringStore(service_name="test:migration", legacy_token_path=str(legacy_file))

    # Both keys migrated into keyring
    assert store.load("openai") == {"api_key": "sk-legacy-key-999"}
    assert store.load("anthropic") == {"api_key": "sk-ant-legacy-888"}

    # Legacy file was purged of plaintext secrets
    sanitized_content = legacy_file.read_text(encoding="utf-8").strip()
    assert sanitized_content == "{}" or json.loads(sanitized_content) == {}


def test_keyring_store_raw_string_fallback():
    import keyring

    keyring.set_password("test:raw", "custom_profile", "sk-raw-token-without-json")
    store = KeyringStore(service_name="test:raw")

    loaded = store.load("custom_profile")
    assert loaded == {"api_key": "sk-raw-token-without-json"}


def test_oauth_store_delegation_to_keyring_for_llm_keys(tmp_path, monkeypatch):
    import opencohost.api.deps as api_deps

    keys_file = str(tmp_path / "llm_keys.json")
    monkeypatch.setattr(api_deps, "llm_keys_file", lambda: keys_file)

    oauth_store = OAuthStore(keys_file)
    assert oauth_store.use_keyring is True

    oauth_store.save("nvidia_nim", {"api_key": "nv-secret-key"})
    assert oauth_store.has_token("nvidia_nim") is True
    assert oauth_store.load("nvidia_nim") == {"api_key": "nv-secret-key"}

    oauth_store.delete("nvidia_nim")
    assert oauth_store.has_token("nvidia_nim") is False
    assert oauth_store.load("nvidia_nim") is None


def test_oauth_store_file_mode_for_stream_tokens(tmp_path):
    tokens_file = tmp_path / "tokens.json"
    oauth_store = OAuthStore(str(tokens_file))
    assert oauth_store.use_keyring is False

    oauth_store.save("twitch", {"access_token": "tw-token", "refresh_token": "tw-refresh"})
    assert tokens_file.exists()

    with open(tokens_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    assert "twitch" in data
    assert data["twitch"]["access_token"] == "tw-token"


class _FakeWindowsKeyring:
    __module__ = "keyring.backends.Windows"

    def __init__(self, *, failure=None):
        self.vault = {}
        self.failure = failure

    def get_password(self, service, username):
        if self.failure:
            raise RuntimeError(self.failure)
        return self.vault.get((service, username))

    def set_password(self, service, username, password):
        if self.failure:
            raise RuntimeError(self.failure)
        self.vault[(service, username)] = password

    def delete_password(self, service, username):
        if self.failure:
            raise RuntimeError(self.failure)
        self.vault.pop((service, username), None)


class _TestChain:
    def __init__(self, backends):
        self.backends = backends
        self.calls = 0

    def get_password(self, service, username):
        self.calls += 1
        for backend in self.backends:
            try:
                value = backend.get_password(service, username)
            except Exception:
                continue
            if value is not None:
                return value

    def set_password(self, service, username, password):
        self.calls += 1
        for backend in self.backends:
            try:
                return backend.set_password(service, username, password)
            except Exception:
                continue

    def delete_password(self, service, username):
        self.calls += 1
        for backend in self.backends:
            try:
                return backend.delete_password(service, username)
            except Exception:
                continue


class _TrackingMemoryKeyring(MemoryKeyring):
    def __init__(self):
        super().__init__()
        self.calls = 0

    def get_password(self, service, username):
        self.calls += 1
        return super().get_password(service, username)

    def set_password(self, service, username, password):
        self.calls += 1
        return super().set_password(service, username, password)

    def delete_password(self, service, username):
        self.calls += 1
        return super().delete_password(service, username)


def _secure_store_for_backend(monkeypatch, backend):
    from types import SimpleNamespace

    from opencohost.security import keyring_store

    monkeypatch.setattr(keyring_store, "_KEYRING_AVAILABLE", True)
    monkeypatch.setattr(
        keyring_store, "keyring", SimpleNamespace(get_keyring=lambda: backend)
    )
    return KeyringStore(service_name="test:secure-chain")


def test_secure_store_unwraps_actual_chainer_to_os_backend(monkeypatch):
    pytest.importorskip("keyring.backends.chainer")
    from keyring.backends.chainer import ChainerBackend

    unsafe = _TrackingMemoryKeyring()
    durable = _FakeWindowsKeyring()
    monkeypatch.setattr(ChainerBackend, "backends", [unsafe, durable])
    chain = ChainerBackend()
    store = _secure_store_for_backend(monkeypatch, chain)

    assert store._secure_backend() is durable


def test_secure_operations_bypass_generic_chain_and_memory_fallback(monkeypatch):
    unsafe = _TrackingMemoryKeyring()
    durable = _FakeWindowsKeyring()
    chain = _TestChain([unsafe, durable])
    store = _secure_store_for_backend(monkeypatch, chain)

    store.save_secure("tavily", {"api_key": "test-only-secret"})
    assert store.load_secure("tavily") == {"api_key": "test-only-secret"}
    store.delete_secure("tavily")

    assert chain.calls == 0
    assert unsafe.calls == 0
    assert durable.vault == {}


def test_secure_store_does_not_fall_through_to_memory_when_os_leaf_fails(monkeypatch):
    unsafe = _TrackingMemoryKeyring()
    unsafe.set_password("test:secure-chain", "tavily", '{"api_key":"memory-only"}')
    durable = _FakeWindowsKeyring(failure="private backend detail")
    chain = _TestChain([durable, unsafe])
    store = _secure_store_for_backend(monkeypatch, chain)

    assert store._secure_backend() is durable
    with pytest.raises(KeyStoreError, match="secure_store_unavailable") as exc:
        store.load_secure("tavily")

    assert "private backend detail" not in str(exc.value)
    assert chain.calls == 0
    assert unsafe.calls == 1


def test_secure_store_resolves_nested_cyclic_generic_chains(monkeypatch):
    unsafe = _TrackingMemoryKeyring()
    durable = _FakeWindowsKeyring()
    outer = _TestChain([])
    inner = _TestChain([unsafe, durable])
    outer.backends = [inner]
    inner.backends.insert(0, outer)
    store = _secure_store_for_backend(monkeypatch, outer)

    assert store._secure_backend() is durable


def test_secure_store_rejects_chain_with_no_os_backend(monkeypatch):
    chain = _TestChain([_TrackingMemoryKeyring()])
    store = _secure_store_for_backend(monkeypatch, chain)

    with pytest.raises(KeyStoreError, match="secure_store_unavailable"):
        store._secure_backend()


def test_secure_store_sanitizes_backend_inspection_errors(monkeypatch):
    class BrokenChain:
        @property
        def backends(self):
            raise RuntimeError("private backend detail")

    store = _secure_store_for_backend(monkeypatch, BrokenChain())

    with pytest.raises(KeyStoreError, match="secure_store_unavailable") as exc:
        store._secure_backend()

    assert "private backend detail" not in str(exc.value)


def test_memory_keyring_is_not_automatically_selected(monkeypatch):
    pytest.importorskip("keyring.backends.chainer")
    import keyring.backend
    from keyring.backends.chainer import ChainerBackend

    monkeypatch.setattr(keyring.backend, "get_all_keyring", lambda: [MemoryKeyring()])

    assert ChainerBackend.backends == []
